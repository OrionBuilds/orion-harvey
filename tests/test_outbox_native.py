"""Tests for the native sending path: gate, outbox lifecycle, stage/drain,
stop-on-reply, bounces, and the kill switch."""

import dataclasses
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from harvey.gate import pre_send_check
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from harvey.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class FakeProvider(MailProvider):
    name = "fake"

    def __init__(self):
        self.sent = []
        self.inbound = []
        self.fail_next = False

    def is_configured(self):
        return True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to=""):
        if self.fail_next:
            self.fail_next = False
            return SendResult(ok=False, error="smtp boom")
        self.sent.append({"to": to_email, "subject": subject, "body": body,
                          "thread_ref": thread_ref, "in_reply_to": in_reply_to})
        return SendResult(ok=True, message_id=f"<m{len(self.sent)}@x>", thread_ref="t1")

    async def get_replies(self, limit=50):
        return self.inbound

    async def test_connection(self):
        return True, "fake"


class Cfg:
    class persona:
        name = "Harvey"; email = "harvey@x.co"; company = "X"; role = "BD"; tone = "direct"

    class product:
        name = "P"; description = "d"; pricing = "$"; key_benefits = ["b"]
        objection_responses = {}

    class channels:
        class email:
            enabled = True
            provider = "smtp"
            max_daily_sends = 50
            send_to_risky = False
            require_approval = True
            max_bounce_rate = 0.05

        class linkedin:
            enabled = False


class Env:
    instantly_api_key = ""


def make_sender(state, provider, require_approval=True):
    from harvey.agents.sender import Sender
    cfg = Cfg()
    cfg.channels.email.require_approval = require_approval
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sender.provider = provider  # override registry lookup
    sender.send_pacing = False  # no inter-send sleeps in tests
    return sender


class StubBrain:
    """Minimal brain: canned intent + reply text, no subprocess."""
    def __init__(self, intent="question", reply="Sure, happy to help."):
        self._intent = intent
        self._reply = reply

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        return self._intent if task == "classify_intent" or "category" in prompt.lower() else self._reply


def make_handler(state, provider, intent="question"):
    from harvey.agents.handler import Handler
    handler = Handler(brain=StubBrain(intent=intent), state=state, config=Cfg(), env=Env())
    handler.provider = provider
    return handler


async def seed_prospect(state, email="jane@acme.com", status="new", estatus="verified"):
    return await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="VP", company="Acme",
        email=email, email_status=estatus, email_verified=(estatus == "verified"),
        status=status,
    ))


async def seed_campaign(state, prospect_ids):
    campaign = Campaign(
        id="", name="test-campaign", channel="email",
        sequence=[
            EmailStep(step=1, subject="hi {{first_name}}", body="Body for {{company}}. Question?", delay_days=0),
            EmailStep(step=2, subject="follow", body="Another angle {{first_name}}.", delay_days=3),
        ],
        prospect_ids=prospect_ids, status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


# ── gate ──


def test_gate_blocks_bad_content():
    p = Prospect(first_name="J", last_name="D", title="VP",
                 email="jane@acme.com", email_status="verified")
    ok = pre_send_check("jane@acme.com", "hello", "Short and fine. Question?", p)
    assert ok.ok

    bad = pre_send_check("jane@acme.com", "hello", "Hi {{first_name}}, act now!", p)
    assert not bad.ok
    assert any("merge" in r for r in bad.reasons)
    assert any("banned" in r for r in bad.reasons)

    guess = Prospect(first_name="J", last_name="D", title="VP",
                     email="jane@acme.com", email_status="guess")
    blocked = pre_send_check("jane@acme.com", "hello", "Fine body.", guess)
    assert not blocked.ok

    # Replies to someone who emailed us are exempt from the status gate
    reply_ok = pre_send_check("jane@acme.com", "Re: hi", "Thanks!", guess, kind="reply")
    assert reply_ok.ok


def test_gate_link_and_html_limits():
    r = pre_send_check("a@b.co", "s", "See https://a.com and https://b.com")
    assert any("links" in x for x in r.reasons)
    r2 = pre_send_check("a@b.co", "s", "<div>hello</div>")
    assert any("HTML" in x for x in r2.reasons)


def test_bounce_detection():
    assert looks_like_bounce("mailer-daemon@googlemail.com", "anything")
    assert looks_like_bounce("mx@x.com", "Delivery Status Notification (Failure)")
    assert not looks_like_bounce("jane@acme.com", "Re: your note")


# ── staging ──


@pytest.mark.asyncio
async def test_stage_campaign_renders_and_respects_approval(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider(), require_approval=True)

    await sender._run_native()

    pending = await state.get_outbox(status="pending_review")
    assert len(pending) == 2  # both steps staged
    step1 = next(i for i in pending if i["step"] == 1)
    assert step1["subject"] == "hi Jane"
    assert "Acme" in step1["body"]
    # Nothing sent while awaiting approval
    assert sender.provider.sent == []

    # Idempotent: re-running stages nothing new
    campaign2 = await state.get_campaigns_by_status("active")
    assert campaign2[0].id == campaign.id
    await sender._run_native()
    assert len(await state.get_outbox(status="pending_review")) == 2


@pytest.mark.asyncio
async def test_stage_skips_unverified(state):
    pid = await seed_prospect(state, email="guess@acme.com", estatus="guess")
    await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider())
    await sender._run_native()
    assert await state.get_outbox(status="pending_review") == []


# ── drain ──


@pytest.mark.asyncio
async def test_drain_sends_approved_due_items(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)

    await sender._run_native()  # stage as approved + drain in one pass

    sent = await state.get_outbox(status="sent")
    assert len(sent) == 1                     # step 1 due now; step 2 in 3 days
    assert provider.sent[0]["to"] == "jane@acme.com"
    assert provider.sent[0]["subject"] == "hi Jane"
    prospect = await state.get_prospect(pid)
    assert prospect.status == "contacted"
    approved_left = await state.get_outbox(status="approved")
    assert len(approved_left) == 1 and approved_left[0]["step"] == 2


@pytest.mark.asyncio
async def test_drain_respects_kill_switch(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await state.set_setting("sending_paused", "test pause")

    await sender._run_native()
    assert provider.sent == []
    assert await state.get_outbox(status="sent") == []


@pytest.mark.asyncio
async def test_drain_gate_failure_marks_failed(state):
    pid = await seed_prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello", body="Unrendered {{first_name}} tag",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._drain_due()
    failed = await state.get_outbox(status="failed")
    assert len(failed) == 1
    assert "gate" in failed[0]["error"]
    assert provider.sent == []


@pytest.mark.asyncio
async def test_send_failure_recorded(state):
    pid = await seed_prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello", body="Fine body. Question?",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    provider.fail_next = True
    sender = make_sender(state, provider)
    await sender._drain_due()
    failed = await state.get_outbox(status="failed")
    assert failed and "smtp boom" in failed[0]["error"]


# ── handler: stop-on-reply, bounces, kill switch ──


@pytest.mark.asyncio
async def test_reply_cancels_pending_outbox(state):
    pid = await seed_prospect(state, status="contacted")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2",
        body="b", send_at=_now_iso(), status="approved", campaign_id="c1", step=2,
    )
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com",
        subject="Re: hi", body="unsubscribe please",
    )]
    handler = make_handler(state, provider)
    await handler._run_native()

    cancelled = await state.get_outbox(status="cancelled")
    assert len(cancelled) == 1
    prospect = await state.get_prospect(pid)
    assert prospect.status == "opted_out"   # opt-out keywords honored


@pytest.mark.asyncio
async def test_bounce_marks_invalid_and_kill_switch(state):
    pid = await seed_prospect(state, status="contacted")
    # A sent item whose message-id the bounce references
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s",
        body="b", send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    await state.update_outbox_item(
        item_id, status="sent", message_id="<m1@x>", sent_at=_now_iso()
    )
    # Ten more sent items so the kill-switch minimum-sample rule is met
    for i in range(10):
        oid = await state.add_outbox_item(
            prospect_id=pid, to_email="jane@acme.com", subject="s", body="b",
            send_at=_now_iso(), status="approved", campaign_id=f"c{i+2}", step=1,
        )
        await state.update_outbox_item(oid, status="sent", sent_at=_now_iso())

    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="b1", from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body="couldn't be delivered", in_reply_to="<m1@x>", is_bounce=True,
    )]
    handler = make_handler(state, provider)
    await handler._run_native()

    prospect = await state.get_prospect(pid)
    assert prospect.email_status == "invalid"
    # 1 bounce / 11 sent = 9% > 5% threshold with >= 10 sends → kill switch
    assert await state.get_setting("sending_paused") != ""


@pytest.mark.asyncio
async def test_inbound_dedup(state):
    await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com",
        subject="Re: hi", body="we're all set thanks",
    )]
    handler = make_handler(state, provider, intent="not_interested")
    await handler._run_native()
    first = await state.get_conversations_by_status("closed")
    await handler._run_native()   # same message again → ignored via dedup
    second = await state.get_conversations_by_status("closed")
    assert len(first) == len(second) == 1


# ── bounce classification: a policy block is not a bad address ──

from harvey.integrations.mail_provider import classify_bounce  # noqa: E402

MS_POLICY_NDR = ("550 5.7.708 Service unavailable. Access denied, traffic not accepted "
                 "from this IP 40.107.1.2")


@pytest.mark.parametrize("subject, body, expected", [
    ("Undeliverable: hi", MS_POLICY_NDR, "policy"),
    ("Undeliverable: hi", "550 5.1.1 User unknown", "hard"),
    ("Undeliverable: hi", "550 5.4.1 Recipient address rejected: Access denied", "hard"),
    ("Delivery Status Notification (Failure)", "The address does not exist", "hard"),
    ("Delivery Status Notification (Delay)", "452 4.2.2 mailbox full, try again later", "soft"),
    ("Undeliverable: hi", "552 5.2.2 Mailbox full", "soft"),
    ("Delivery Status Notification (Failure)", "couldn't be delivered", "unknown"),
    ("Undeliverable: hi", "client IP 10.1.5.7.3 refused", "unknown"),
])
def test_classify_bounce(subject, body, expected):
    assert classify_bounce(subject, body) == expected


async def _sent_item(state, pid, message_id):
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s", body="b",
        send_at=_now_iso(), status="approved", campaign_id=message_id, step=1)
    await state.update_outbox_item(item_id, status="sent", message_id=message_id, sent_at=_now_iso())
    return item_id


def _ndr(pid_tag, body):
    return InboundMessage(
        provider_id=f"b-{pid_tag}", from_email="postmaster@outlook.com",
        subject="Undeliverable: hi", body=body, in_reply_to=pid_tag, is_bounce=True)


@pytest.mark.asyncio
async def test_policy_block_keeps_address_valid_and_does_not_count_as_bad(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "<m1@x>")
    provider = FakeProvider()
    provider.inbound = [_ndr("<m1@x>", MS_POLICY_NDR)]
    await make_handler(state, provider)._run_native()

    prospect = await state.get_prospect(pid)
    assert prospect.email_status != "invalid"
    assert not await state.is_suppressed("email", prospect.email)
    assert await state.get_setting("bounce_count", "0") in ("", "0")
    assert await state.get_setting("sending_paused") == ""   # one block does not pause


@pytest.mark.asyncio
async def test_second_policy_block_in_a_day_pauses_sending(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "<m1@x>")
    await _sent_item(state, pid, "<m2@x>")
    provider = FakeProvider()
    provider.inbound = [_ndr("<m1@x>", MS_POLICY_NDR), _ndr("<m2@x>", MS_POLICY_NDR)]
    await make_handler(state, provider)._run_native()

    assert "policy blocks" in await state.get_setting("sending_paused")
    assert (await state.get_prospect(pid)).email_status != "invalid"


@pytest.mark.asyncio
async def test_soft_bounce_leaves_everything_alone(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "<m1@x>")
    provider = FakeProvider()
    provider.inbound = [_ndr("<m1@x>", "552 5.2.2 Mailbox full")]
    await make_handler(state, provider)._run_native()

    assert (await state.get_prospect(pid)).email_status != "invalid"
    assert await state.get_setting("sending_paused") == ""
    assert await state.get_setting("bounce_count", "0") in ("", "0")


@pytest.mark.asyncio
async def test_hard_bounce_still_marks_invalid(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "<m1@x>")
    provider = FakeProvider()
    provider.inbound = [_ndr("<m1@x>", "550 5.1.1 User unknown")]
    await make_handler(state, provider)._run_native()

    prospect = await state.get_prospect(pid)
    assert prospect.email_status == "invalid"
    assert await state.is_suppressed("email", prospect.email)


@pytest.mark.asyncio
async def test_undeliverable_notice_for_someone_elses_mail_is_ignored(state):
    """The mailbox is shared (smoke tests, other agents): a notice that names nobody
    Harvey emailed must not count against Harvey's sender health."""
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "<m1@x>")
    provider = FakeProvider()
    foreign = InboundMessage(
        provider_id="b-foreign", from_email="postmaster@outlook.com",
        subject="Undeliverable: orion smoke test", body=f"to smoke@elsewhere.com: {MS_POLICY_NDR}",
        in_reply_to="<not-ours@y>", is_bounce=True)
    provider.inbound = [foreign, dataclasses.replace(foreign, provider_id="b-foreign2")]
    await make_handler(state, provider)._run_native()

    assert await state.get_setting("sending_paused") == ""
    assert await state.get_setting("policy_block_times", "") in ("", "[]")
    assert await state.get_setting("bounce_count", "0") in ("", "0")


@pytest.mark.asyncio
async def test_notice_without_message_id_is_matched_by_the_recipient_we_emailed(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "<m1@x>")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="b-1", from_email="postmaster@outlook.com", subject="Undeliverable: hi",
        body=f"Delivery to jane@acme.com failed. {MS_POLICY_NDR}", in_reply_to="", is_bounce=True)]
    await make_handler(state, provider)._run_native()

    assert await state.get_setting("policy_block_times", "") not in ("", "[]")


# ── a follow-up waits for the step before it, and keeps the sequence's gap ──

from datetime import datetime, timedelta, timezone  # noqa: E402


def _naive(dt):
    return dt.replace(tzinfo=None).isoformat()


async def _three_steps(state, pid, first_sent=None, statuses=("approved", "approved", "approved")):
    base = datetime.now(timezone.utc) - timedelta(days=9)     # staged long ago, like our real drafts
    ids = []
    for n, (gap, status) in enumerate(zip((0, 3, 7), statuses), start=1):
        item_id = await state.add_outbox_item(
            prospect_id=pid, to_email="jane@acme.com", subject=f"s{n}", body="b",
            send_at=_naive(base + timedelta(days=gap)), status=status, campaign_id="camp", step=n)
        ids.append(item_id)
    if first_sent is not None:
        await state.update_outbox_item(ids[0], status="sent", sent_at=_naive(first_sent))
    return ids


def _ch(state):
    from harvey.channels.email import EmailChannel
    return EmailChannel(make_handler(state, FakeProvider()).config, FakeProvider(), state)


@pytest.mark.asyncio
async def test_step_two_waits_until_step_one_is_actually_sent(state):
    pid = await seed_prospect(state, status="queued")
    ids = await _three_steps(state, pid)
    prospect = await state.get_prospect(pid)
    r1 = await _ch(state).pre_send(state, await state.get_outbox_item(ids[0]), prospect)
    r2 = await _ch(state).pre_send(state, await state.get_outbox_item(ids[1]), prospect)
    assert r1.ok                                        # step 1 is free to go
    assert not r2.ok and r2.defer and "waiting for step 1" in r2.reasons[0]


@pytest.mark.asyncio
async def test_step_two_keeps_the_three_day_gap_from_the_real_send(state):
    pid = await seed_prospect(state, status="queued")
    ids = await _three_steps(state, pid, first_sent=datetime.now(timezone.utc) - timedelta(hours=5))
    prospect = await state.get_prospect(pid)
    early = await _ch(state).pre_send(state, await state.get_outbox_item(ids[1]), prospect)
    assert not early.ok and early.defer and "waits until" in early.reasons[0]

    await state.update_outbox_item(ids[0], sent_at=_naive(datetime.now(timezone.utc) - timedelta(days=3, minutes=1)))
    due = await _ch(state).pre_send(state, await state.get_outbox_item(ids[1]), prospect)
    assert due.ok


@pytest.mark.asyncio
async def test_a_rejected_first_email_holds_the_follow_ups_back(state):
    pid = await seed_prospect(state, status="queued")
    ids = await _three_steps(state, pid, statuses=("rejected", "approved", "approved"))
    r = await _ch(state).pre_send(state, await state.get_outbox_item(ids[1]), await state.get_prospect(pid))
    assert not r.ok and r.defer and "rejected" in r.reasons[0]
