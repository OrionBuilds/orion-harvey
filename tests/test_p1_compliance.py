"""P1: compliance rules, the stateful gate, the shared drain loop, the Handler's
suppression writes, fail-closed polling, and the decision engine."""

import os
import tempfile
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from harvey import compliance
from harvey.channels import Channel
from harvey.channels.drain import drain_channel
from harvey.channels.email import EmailChannel
from harvey.gate import GateResult, pre_send_check, stateful_gate
from harvey.integrations.mail_provider import InboundMessage, MailProvider, SendResult
from harvey.main import decide_next_action
from harvey.models.prospect import Prospect
from harvey.state import StateManager

ADDRESS = "1 Test Street, Tampa, FL 33601"


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


@pytest.fixture(autouse=True)
def no_stop_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HARVEY_STOP_FILE", str(tmp_path / "STOP-not-present"))


class FakeProvider(MailProvider):
    name = "fake"

    def __init__(self):
        self.sent, self.inbound, self.poll_error = [], [], None

    def is_configured(self):
        return True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to=""):
        self.sent.append({"to": to_email, "subject": subject, "body": body})
        return SendResult(ok=True, message_id=f"<m{len(self.sent)}@x>", thread_ref="t")

    async def get_replies(self, limit=50):
        if self.poll_error:
            raise self.poll_error
        return self.inbound

    async def test_connection(self):
        return True, "fake"


def Cfg(**email_overrides):
    """A fresh config per call, so a test can't leak settings into the next."""
    email = SimpleNamespace(
        enabled=True, provider="smtp", max_daily_sends=50, send_to_risky=False,
        require_approval=False, max_bounce_rate=0.05, enforce_compliance=True,
        physical_address=ADDRESS, allow_role_addresses=False,
        sender_domain="go.example.com",
    )
    for key, value in email_overrides.items():
        setattr(email, key, value)
    return SimpleNamespace(
        persona=SimpleNamespace(name="Jordan", email="jordan@go.example.com",
                                company="X", role="CTO", tone="direct"),
        product=SimpleNamespace(name="P", description="d", pricing="$",
                                key_benefits=["b"], objection_responses={}),
        channels=SimpleNamespace(email=email, linkedin=SimpleNamespace(enabled=False)),
    )


class Env:
    instantly_api_key = ""


def channel(provider=None, cfg=None):
    return EmailChannel(cfg or Cfg(), provider or FakeProvider())


async def seed(state, email="jane@acme.com", company_id="", status="new", **kw):
    return await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="VP", company="Acme",
        email=email, email_status="verified", email_verified=True,
        status=status, company_id=company_id, **kw))


async def queue(state, pid, email, step=1, kind="sequence", status="approved",
                subject="hello there", body="One short observation. Worth a reply?"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject=subject, body=body,
        send_at="2000-01-01T00:00:00", status=status, kind=kind,
        campaign_id="c1" if kind == "sequence" else "", step=step)


# ── compliance.py (pure) ──

def test_mega_domains_include_subdomains_and_www():
    assert compliance.is_mega_domain("jane@hubspot.com")
    assert compliance.is_mega_domain("jane@blog.hubspot.com")
    assert compliance.is_mega_domain("www.google.com")
    assert not compliance.is_mega_domain("jane@acme.com")
    assert not compliance.is_mega_domain("")


def test_check_address_rules():
    assert compliance.check_address("jane@acme.com").ok
    assert compliance.check_address("info@acme.com").reason == "role_address"
    assert compliance.check_address("info@acme.com", allow_role=True).ok
    assert compliance.check_address("jane@gmail.com").reason == "free_mail"
    assert compliance.check_address("jane@gmail.com", allow_free_mail=True).ok
    assert compliance.check_address("jane@stripe.com").reason == "mega_domain"


def test_quota_reports_first_cap_hit_in_order():
    q = compliance.EmailQuotas(global_per_day=50)
    assert compliance.check_email_quota(compliance.QuotaCounts(), q) == ""
    both = compliance.QuotaCounts(global_today=50, contact_today=1, company_today=1)
    assert compliance.check_email_quota(both, q) == "global_daily_cap"
    assert compliance.check_email_quota(
        compliance.QuotaCounts(contact_today=1, company_today=1), q) == "contact_daily_cap"
    assert compliance.check_email_quota(
        compliance.QuotaCounts(company_today=1), q) == "company_daily_cap"
    assert compliance.check_email_quota(
        compliance.QuotaCounts(company_this_week=3), q) == "company_weekly_cap"


def test_breakers():
    assert compliance.domain_breaker_reason(4, 0) == ""
    assert "hard bounces" in compliance.domain_breaker_reason(5, 0)
    assert "complaint" in compliance.domain_breaker_reason(0, 1)
    # No verdict on a small sample, however bad.
    assert compliance.global_breaker_reason(99, 99) == ""
    assert compliance.global_breaker_reason(100, 3) == ""       # exactly 3% is allowed
    assert "4.0%" in compliance.global_breaker_reason(100, 4)


def test_footer_and_headers():
    footer = compliance.append_compliance_footer("Hi.\n\n", ADDRESS)
    assert footer.startswith("Hi.") and ADDRESS in footer and "reply STOP" in footer
    with_url = compliance.build_footer_text(ADDRESS, "https://u.example/u/abc")
    assert "https://u.example/u/abc" in with_url and "STOP" not in with_url
    h = compliance.build_compliance_headers("https://u.example/u/abc", "jordan@go.example.com")
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "mailto:jordan@go.example.com" in h["List-Unsubscribe"]
    assert "<https://u.example/u/abc>" in h["List-Unsubscribe"]


def test_sms_stop_is_exact_keyword():
    assert compliance.is_sms_stop("STOP") and compliance.is_sms_stop(" stop. ")
    assert not compliance.is_sms_stop("please stop texting me")


# ── pre_send_check with enforcement ──

def _check(email="jane@acme.com", body=None, **kw):
    body = body if body is not None else f"Short note.\n\n--\n{ADDRESS}"
    return pre_send_check(email, "hello there", body, enforce_compliance=True,
                          physical_address=ADDRESS, **kw)


def test_enforced_gate_passes_a_clean_cold_email():
    assert _check().ok


@pytest.mark.parametrize("email, fragment", [
    ("info@acme.com", "role_address"),
    ("jane@gmail.com", "free_mail"),
    ("jane@hubspot.com", "mega_domain"),
])
def test_enforced_gate_blocks_bad_addresses(email, fragment):
    result = _check(email)
    assert not result.ok and any(fragment in r for r in result.reasons)


def test_enforced_gate_requires_address_and_footer():
    assert not _check(body="No footer here.").ok
    no_addr = pre_send_check("jane@acme.com", "hello there", "Body.",
                             enforce_compliance=True, physical_address="")
    assert any("physical address" in r for r in no_addr.reasons)


def test_replies_and_unenforced_mail_are_exempt():
    assert pre_send_check("info@acme.com", "Re: hi", "Thanks.", kind="reply",
                          enforce_compliance=True, physical_address=ADDRESS).ok
    assert pre_send_check("info@acme.com", "hello there", "Short note.").ok


# ── stateful_gate ──

@pytest.mark.asyncio
async def test_suppressed_email_domain_and_company_are_hard_blocks(state):
    pid = await seed(state)
    p = await state.get_prospect(pid)
    await state.add_suppression("email", "jane@acme.com", reason="unsubscribe")
    r = await stateful_gate(state, channel="email", prospect=p)
    assert not r.ok and not r.defer and "unsubscribe" in r.reasons[0]

    await state.remove_suppression("email", "jane@acme.com")
    await state.add_suppression("domain", "acme.com", reason="complaint")
    assert not (await stateful_gate(state, channel="email", prospect=p)).ok


@pytest.mark.asyncio
async def test_frequency_limits_defer_rather_than_fail(state):
    pid = await seed(state)
    p = await state.get_prospect(pid)
    await state.record_send(channel="email", prospect_id=pid, ref="o1")
    r = await stateful_gate(state, channel="email", prospect=p)
    assert not r.ok and r.defer and "contact_daily_cap" in r.reasons[0]
    # A reply to someone who wrote to us is never frequency-limited.
    assert (await stateful_gate(state, channel="email", prospect=p, kind="reply")).ok


@pytest.mark.asyncio
async def test_lookup_failure_fails_closed_but_defers(state, monkeypatch):
    pid = await seed(state)
    p = await state.get_prospect(pid)

    async def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(state, "find_suppression", boom)
    r = await stateful_gate(state, channel="email", prospect=p)
    assert not r.ok and r.defer and "lookup failed" in r.reasons[0]


# ── drain_channel ──

@pytest.mark.asyncio
async def test_drain_appends_footer_records_ledger_and_marks_sent(state):
    pid = await seed(state)
    oid = await queue(state, pid, "jane@acme.com")
    provider = FakeProvider()
    sent = await drain_channel(state, channel_obj := channel(provider), pacing_enabled=False)

    assert sent == 1 and channel_obj.name == "email"
    assert ADDRESS in provider.sent[0]["body"]
    assert (await state.get_outbox_item(oid))["status"] == "sent"
    assert await state.count_sent_today("email") == 1
    assert await state.count_sent_for(channel="email", prospect_id=pid) == 1


@pytest.mark.asyncio
async def test_drain_cancels_a_suppressed_prospect_without_sending(state):
    pid = await seed(state)
    oid = await queue(state, pid, "jane@acme.com")
    await state.add_suppression("email", "jane@acme.com", reason="unsubscribe")
    provider = FakeProvider()
    assert await drain_channel(state, channel(provider), pacing_enabled=False) == 0
    item = await state.get_outbox_item(oid)
    assert item["status"] == "cancelled" and "unsubscribe" in item["error"]
    assert provider.sent == []


@pytest.mark.asyncio
async def test_second_contact_at_same_company_waits_a_day(state):
    cid = await state.add_company(__import__("harvey.models.company", fromlist=["Company"]).Company(
        id="", name="Acme", domain="acme.com"))
    a = await seed(state, "a@acme.com", company_id=cid)
    b = await seed(state, "b@acme.com", company_id=cid)
    oa = await queue(state, a, "a@acme.com")
    ob = await queue(state, b, "b@acme.com")
    provider = FakeProvider()

    assert await drain_channel(state, channel(provider), pacing_enabled=False) == 1
    statuses = {i["id"]: i["status"] for i in await state.get_outbox()}
    assert sorted(statuses.values()) == ["approved", "sent"]  # held, not failed

    # A day later the company limit has rolled over and the held email goes.
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE send_ledger SET created_at = datetime('now', '-2 days')")
        await db.commit()
    assert await drain_channel(state, channel(provider), pacing_enabled=False) == 1
    assert {i["status"] for i in await state.get_outbox()} == {"sent"}
    assert oa and ob


@pytest.mark.asyncio
async def test_drain_enforcement_holds_cold_mail_without_an_address(state):
    cfg = Cfg(physical_address="")
    pid = await seed(state)
    oid = await queue(state, pid, "jane@acme.com")
    provider = FakeProvider()
    assert await drain_channel(state, channel(provider, cfg), pacing_enabled=False) == 0
    item = await state.get_outbox_item(oid)
    assert item["status"] == "failed" and "physical address" in item["error"]
    assert provider.sent == []


@pytest.mark.asyncio
async def test_daily_cap_comes_from_the_ledger(state):
    cfg = Cfg(max_daily_sends=1)
    p1 = await seed(state, "a@acme.com")
    p2 = await seed(state, "b@other.com")
    await queue(state, p1, "a@acme.com")
    await queue(state, p2, "b@other.com")
    provider = FakeProvider()
    assert await drain_channel(state, channel(provider, cfg), pacing_enabled=False) == 1
    assert await drain_channel(state, channel(provider, cfg), pacing_enabled=False) == 0


@pytest.mark.asyncio
async def test_pause_flag_and_stop_file_both_halt_sending(state, monkeypatch, tmp_path):
    pid = await seed(state)
    await queue(state, pid, "jane@acme.com")
    provider = FakeProvider()

    await state.set_setting("sending_paused", "manual")
    assert await drain_channel(state, channel(provider), pacing_enabled=False) == 0
    await state.set_setting("sending_paused", "")

    stop = tmp_path / "STOP"
    stop.write_text("")
    monkeypatch.setenv("HARVEY_STOP_FILE", str(stop))
    assert await drain_channel(state, channel(provider), pacing_enabled=False) == 0
    assert provider.sent == []

    stop.unlink()
    assert await drain_channel(state, channel(provider), pacing_enabled=False) == 1


@pytest.mark.asyncio
async def test_complaint_trips_the_domain_breaker_and_pauses(state):
    pid = await seed(state)
    await queue(state, pid, "jane@acme.com")
    await state.record_send(channel="email", prospect_id="old", ref="o0",
                            sender_domain="go.example.com", outcome="complaint")
    provider = FakeProvider()
    assert await drain_channel(state, channel(provider), pacing_enabled=False) == 0
    assert "breaker" in await state.get_setting("sending_paused")
    assert provider.sent == []


@pytest.mark.asyncio
async def test_stop_on_reply_still_cancels_follow_ups(state):
    pid = await seed(state, status="replied")
    oid = await queue(state, pid, "jane@acme.com")
    provider = FakeProvider()
    assert await drain_channel(state, channel(provider), pacing_enabled=False) == 0
    assert (await state.get_outbox_item(oid))["status"] == "cancelled"


# ── Handler ──

class StubBrain:
    def __init__(self, intent="question", reply="Happy to help."):
        self._intent, self._reply = intent, reply

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        return self._intent if task == "classify_intent" or "category" in prompt.lower() else self._reply


def make_handler(state, provider, intent="question"):
    from harvey.agents.handler import Handler
    h = Handler(brain=StubBrain(intent=intent), state=state, config=Cfg(), env=Env())
    h.provider = provider
    return h


def inbound(email, body, pid="m1", **kw):
    return InboundMessage(provider_id=pid, from_email=email, subject="Re: hi", body=body, **kw)


@pytest.mark.asyncio
async def test_opt_out_suppresses_every_way_to_reach_the_person(state):
    pid = await seed(state, phone="(813) 555-1212",
                     linkedin_url="https://linkedin.com/in/jane-doe")
    oid = await queue(state, pid, "jane@acme.com")
    provider = FakeProvider()
    provider.inbound = [inbound("jane@acme.com", "Please unsubscribe me")]
    assert await make_handler(state, provider)._run_native() is True

    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await state.get_outbox_item(oid))["status"] == "cancelled"
    assert await state.is_suppressed("email", "jane@acme.com", channel="linkedin")
    assert await state.is_suppressed("phone", "8135551212", channel="sms")
    assert await state.is_suppressed("linkedin", "linkedin.com/in/jane-doe")


@pytest.mark.asyncio
async def test_not_interested_cools_the_whole_company_for_180_days(state):
    from harvey.models.company import Company
    cid = await state.add_company(Company(id="", name="Acme", domain="acme.com"))
    pid = await seed(state, company_id=cid)
    provider = FakeProvider()
    provider.inbound = [inbound("jane@acme.com", "No thanks, we're all set.")]
    assert await make_handler(state, provider, intent="not_interested")._run_native() is True

    hit = await state.find_suppression("company", "acme.com")
    assert hit and hit["reason"] == "not_interested"
    expires = datetime.strptime(hit["expires_at"], "%Y-%m-%d %H:%M:%S")
    assert 175 <= (expires - datetime.now(timezone.utc).replace(tzinfo=None)).days <= 180
    # A colleague at the same company is now blocked too.
    colleague = await seed(state, "bob@acme.com", company_id=cid)
    r = await stateful_gate(state, channel="email",
                            prospect=await state.get_prospect(colleague))
    assert not r.ok and "not_interested" in r.reasons[0]
    assert pid


@pytest.mark.asyncio
async def test_bounce_suppresses_address_and_marks_the_send_in_the_ledger(state):
    pid = await seed(state)
    oid = await queue(state, pid, "jane@acme.com")
    provider = FakeProvider()
    assert await drain_channel(state, channel(provider), pacing_enabled=False) == 1
    sent = await state.get_outbox_item(oid)
    provider.inbound = [InboundMessage(
        provider_id="b1", from_email="mailer-daemon@x.com", subject="Undeliverable",
        body="550 no such user", in_reply_to=sent["message_id"], is_bounce=True)]
    assert await make_handler(state, provider)._run_native() is True

    assert await state.is_suppressed("email", "jane@acme.com")
    assert await state.count_outcomes_today("email", "hard_bounce") == 1


@pytest.mark.asyncio
async def test_unreadable_mailbox_returns_incomplete(state):
    provider = FakeProvider()
    provider.poll_error = OSError("imap down")
    assert await make_handler(state, provider)._run_native() is False


class FakeLinkedInChannel(Channel):
    name = "linkedin"

    def __init__(self, messages=None, error=None):
        self.messages, self.error = messages or [], error

    def is_configured(self):
        return True

    def daily_cap(self):
        return 7

    async def send(self, item, subject, body):
        return SendResult(ok=True)

    async def poll_inbound(self):
        if self.error:
            raise self.error
        return self.messages


@pytest.mark.asyncio
async def test_a_failing_second_channel_holds_the_whole_cycle(state):
    provider = FakeProvider()
    h = make_handler(state, provider)
    h._channels = lambda: [channel(provider), FakeLinkedInChannel(error=RuntimeError("li down"))]
    assert await h._run_native() is False


@pytest.mark.asyncio
async def test_linkedin_reply_cancels_email_follow_ups_and_opens_a_linkedin_thread(state):
    pid = await seed(state, linkedin_url="https://www.linkedin.com/in/jane-doe/")
    oid = await queue(state, pid, "jane@acme.com")
    provider = FakeProvider()
    msg = InboundMessage(provider_id="li1", from_email="", channel="linkedin",
                         from_ref="https://linkedin.com/in/jane-doe",
                         body="Thanks for connecting, what do you do?")
    h = make_handler(state, provider)
    h._channels = lambda: [channel(provider), FakeLinkedInChannel([msg])]
    assert await h._run_native() is True

    assert (await state.get_prospect(pid)).status == "replied"
    assert (await state.get_outbox_item(oid))["status"] == "cancelled"
    convos = await state.get_conversations_by_status("open")
    assert [c.channel for c in convos] == ["linkedin"]


# ── Decision engine ──

@pytest.mark.asyncio
@pytest.mark.parametrize("summary, expected", [
    ({"open_conversations": 1}, "handle_replies"),
    ({"draft_campaigns": 1, "writable": 5}, "send_campaign"),
    ({"prospects": {"new": 5}, "writable": 3}, "write_campaign"),
    # New prospects exist but none are deliverable: go find more, don't stall.
    ({"prospects": {"new": 5, "queued": 0}, "writable": 0}, "prospect"),
    ({"prospects": {"new": 0, "queued": 25}, "writable": 0}, "idle"),
])
async def test_decide_next_action(state, summary, expected):
    assert await decide_next_action(None, state, Cfg(), summary=summary) == expected


@pytest.mark.asyncio
async def test_writable_counts_only_deliverable_new_prospects(state):
    await seed(state, "a@acme.com")
    await state.add_prospect(Prospect(first_name="G", last_name="X", title="VP",
                                      email="g@guess.com", email_status="guess"))
    await state.add_prospect(Prospect(first_name="R", last_name="X", title="VP",
                                      email="r@risky.com", email_status="risky"))
    assert await state.count_writable_prospects(("verified",)) == 1
    assert await state.count_writable_prospects(("verified", "risky")) == 2
    summary = await state.get_state_summary()
    assert summary["writable"] == 1 and summary["prospects"]["new"] == 3


# ── Real providers raise instead of returning "no replies" ──

@pytest.mark.asyncio
async def test_smtp_provider_raises_when_the_mailbox_cannot_be_read(monkeypatch):
    from harvey.integrations.smtp_mail import SmtpImapProvider

    class E:
        smtp_host = smtp_username = smtp_password = imap_host = imap_username = imap_password = "x"
        smtp_port = 587
        imap_port = 993

    provider = SmtpImapProvider(Cfg(), E())

    def broken(limit):
        raise OSError("connection refused")

    monkeypatch.setattr(provider, "_fetch_unseen", broken)
    with pytest.raises(OSError):
        await provider.get_replies()


@pytest.mark.asyncio
async def test_gmail_provider_raises_when_the_inbox_cannot_be_read(monkeypatch):
    from harvey.integrations.gmail import GmailProvider

    provider = GmailProvider.__new__(GmailProvider)

    async def failing(method, path, **kw):
        return None

    monkeypatch.setattr(provider, "_request", failing)
    with pytest.raises(RuntimeError):
        await provider.get_replies()


def test_the_gate_lets_harvey_say_it_is_an_ai_but_blocks_model_boilerplate():
    ok = pre_send_check("jane@acme.com", "quick note", "Yes, I'm an AI assistant. Harvey, Jordan's assistant.")
    assert ok.ok
    for leak in ("As an AI language model, I cannot", "As a language model I can't help"):
        assert not pre_send_check("jane@acme.com", "quick note", leak).ok
