"""P2: Resend provider, one-click unsubscribe, signed webhooks, shared IMAP reader."""

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import webhooks
from harvey.channels.drain import drain_channel
from harvey.channels.email import EmailChannel
from harvey.integrations.imap_reader import ImapReader
from harvey.integrations.resend_mail import ResendMailProvider
from harvey.models.prospect import Prospect
from harvey.state import StateManager

_REAL_ASYNC_CLIENT = httpx.AsyncClient  # before any test patches it
ADDRESS = "1 Test Street, Tampa, FL 33601"
SECRET_RAW = b"super-secret-signing-key-bytes!!"
SECRET = "whsec_" + base64.b64encode(SECRET_RAW).decode()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


@pytest.fixture(autouse=True)
def no_stop_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HARVEY_STOP_FILE", str(tmp_path / "STOP-not-present"))


def cfg(**email):
    e = SimpleNamespace(
        enabled=True, provider="resend", max_daily_sends=50, send_to_risky=False,
        require_approval=False, max_bounce_rate=0.05, enforce_compliance=True,
        physical_address=ADDRESS, allow_role_addresses=False,
        sender_domain="go.example.com", reply_to="", unsubscribe_base_url="https://go.example.com",
    )
    for k, v in email.items():
        setattr(e, k, v)
    return SimpleNamespace(
        persona=SimpleNamespace(name="Jordan Example", email="jordan@go.example.com",
                                company="X", role="CTO", tone="direct"),
        channels=SimpleNamespace(email=e),
    )


ENV = SimpleNamespace(resend_api_key="re_test", imap_host="imap.example.com", imap_port=993,
                      imap_username="jordan@go.example.com", imap_password="pw")


class FakeResend:
    """Stands in for api.resend.com via httpx's MockTransport."""

    def __init__(self, monkeypatch, status=200, body=None, error=None):
        self.requests = []
        self.status, self.body, self.error = status, body, error
        outer = self
        real_client = _REAL_ASYNC_CLIENT

        def handler(request: httpx.Request) -> httpx.Response:
            outer.requests.append(request)
            if outer.error:
                raise outer.error
            payload = outer.body if outer.body is not None else {"id": "re_msg_1"}
            return httpx.Response(outer.status, json=payload)

        monkeypatch.setattr(
            "harvey.integrations.resend_mail.httpx.AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    @property
    def last_json(self):
        return json.loads(self.requests[-1].content)


# ── Resend provider ──

@pytest.mark.asyncio
async def test_send_builds_the_expected_resend_request(monkeypatch):
    api = FakeResend(monkeypatch)
    provider = ResendMailProvider(cfg(reply_to="inbox@go.example.com"), ENV)
    result = await provider.send_email(
        "jane@acme.com", "hello there", "Body", in_reply_to="<prev@x>",
        headers={"List-Unsubscribe": "<mailto:jordan@go.example.com>, <https://go.example.com/u/t>",
                 "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})

    assert result.ok and result.provider_id == "re_msg_1"
    req = api.requests[0]
    assert req.url == "https://api.resend.com/emails"
    assert req.headers["authorization"] == "Bearer re_test"
    body = api.last_json
    assert body["from"] == "Jordan Example <jordan@go.example.com>"
    assert body["to"] == ["jane@acme.com"] and body["text"] == "Body"
    assert body["reply_to"] == "inbox@go.example.com"
    h = body["headers"]
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert h["In-Reply-To"] == "<prev@x>" and h["References"] == "<prev@x>"
    # We mint the Message-ID so IMAP replies can be matched to the send.
    assert h["Message-ID"] == result.message_id and result.message_id.endswith("@go.example.com>")


@pytest.mark.asyncio
async def test_reply_to_header_is_moved_to_the_reply_to_field(monkeypatch):
    api = FakeResend(monkeypatch)
    await ResendMailProvider(cfg(), ENV).send_email(
        "jane@acme.com", "s", "b", headers={"Reply-To": "elsewhere@go.example.com"})
    assert api.last_json["reply_to"] == "elsewhere@go.example.com"
    assert "Reply-To" not in api.last_json["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, fragment", [
    ({"status": 422, "body": {"message": "domain not verified"}}, "422"),
    ({"status": 429, "body": {"message": "rate limited"}}, "429"),
    ({"status": 200, "body": {}}, "no id"),
    ({"error": httpx.ConnectError("boom")}, "request failed"),
])
async def test_send_failures_are_reported_not_raised(monkeypatch, kwargs, fragment):
    FakeResend(monkeypatch, **kwargs)
    result = await ResendMailProvider(cfg(), ENV).send_email("jane@acme.com", "s", "b")
    assert not result.ok and fragment in result.error


def test_configured_only_when_it_can_also_read_replies():
    assert ResendMailProvider(cfg(), ENV).is_configured()
    no_imap = SimpleNamespace(resend_api_key="re_test", imap_host="", imap_username="", imap_password="")
    assert not ResendMailProvider(cfg(), no_imap).is_configured()
    no_key = SimpleNamespace(resend_api_key="", imap_host="h", imap_username="u", imap_password="p")
    assert not ResendMailProvider(cfg(), no_key).is_configured()


@pytest.mark.asyncio
async def test_connection_check_requires_a_verified_sending_domain(monkeypatch):
    FakeResend(monkeypatch, body={"data": [{"name": "go.example.com", "status": "pending"}]})
    ok, detail = await ResendMailProvider(cfg(), ENV).test_connection()
    assert not ok and "not verified" in detail

    FakeResend(monkeypatch, body={"data": [{"name": "other.com", "status": "verified"}]})
    ok, detail = await ResendMailProvider(cfg(), ENV).test_connection()
    assert not ok and "not added" in detail


@pytest.mark.asyncio
async def test_unreadable_mailbox_raises_through_the_provider(monkeypatch):
    provider = ResendMailProvider(cfg(), ENV)

    def broken(limit):
        raise OSError("imap down")

    monkeypatch.setattr(provider.reader, "fetch_unseen", broken)
    with pytest.raises(OSError):
        await provider.get_replies()


# ── Full send path: footer link, headers, provider id, ledger ──

async def seed(state, email="jane@acme.com"):
    return await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="VP", company="Acme",
        email=email, email_status="verified", email_verified=True))


async def queue(state, pid, email="jane@acme.com"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="hello there",
        body="One short observation. Worth a reply?", send_at="2000-01-01T00:00:00",
        status="approved", kind="sequence", campaign_id="c1", step=1)


@pytest.mark.asyncio
async def test_a_sequence_email_carries_a_unique_unsubscribe_link_and_headers(state, monkeypatch):
    api = FakeResend(monkeypatch)
    config = cfg()
    pid = await seed(state)
    oid = await queue(state, pid)
    channel = EmailChannel(config, ResendMailProvider(config, ENV), state)

    assert await drain_channel(state, channel, pacing_enabled=False) == 1

    sent = api.last_json
    token = (await state.mint_unsub_token(pid, oid))          # idempotent: same token
    link = f"https://go.example.com/u/{token}"
    assert link in sent["text"] and ADDRESS in sent["text"]
    assert f"<{link}>" in sent["headers"]["List-Unsubscribe"]
    assert "mailto:jordan@go.example.com" in sent["headers"]["List-Unsubscribe"]
    assert sent["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    item = await state.get_outbox_item(oid)
    assert item["status"] == "sent" and item["provider_id"] == "re_msg_1"
    assert (await state.find_outbox_by_provider_id("re_msg_1"))["id"] == oid
    assert await state.count_sent_today("email") == 1


@pytest.mark.asyncio
async def test_without_a_public_url_mail_falls_back_to_reply_stop(state, monkeypatch):
    api = FakeResend(monkeypatch)
    config = cfg(unsubscribe_base_url="")
    pid = await seed(state)
    await queue(state, pid)
    channel = EmailChannel(config, ResendMailProvider(config, ENV), state)
    assert await drain_channel(state, channel, pacing_enabled=False) == 1
    sent = api.last_json
    assert "reply STOP" in sent["text"] and "/u/" not in sent["text"]
    assert "List-Unsubscribe" not in sent["headers"]


@pytest.mark.asyncio
async def test_unsubscribe_token_is_stable_per_message_and_unique_across_messages(state):
    a = await state.mint_unsub_token("p1", "o1")
    assert await state.mint_unsub_token("p1", "o1") == a
    assert await state.mint_unsub_token("p1", "o2") != a
    assert (await state.get_unsub_token(a))["prospect_id"] == "p1"
    assert await state.get_unsub_token("nope") is None


@pytest.mark.asyncio
async def test_smtp_and_gmail_providers_apply_extra_headers(monkeypatch):
    from harvey.integrations.smtp_mail import SmtpImapProvider
    captured = {}

    async def fake_send(msg, **kw):
        captured["msg"] = msg

    monkeypatch.setattr("harvey.integrations.smtp_mail.aiosmtplib.send", fake_send)
    env = SimpleNamespace(smtp_host="h", smtp_port=587, smtp_username="u@x.com", smtp_password="p")
    provider = SmtpImapProvider(cfg(provider="smtp"), env)
    assert provider.supports_headers
    result = await provider.send_email(
        "jane@acme.com", "s", "b", headers={"List-Unsubscribe": "<mailto:u@x.com>"})
    assert result.ok and captured["msg"]["List-Unsubscribe"] == "<mailto:u@x.com>"


# ── Svix signature verification ──

def sign(body: bytes, msg_id="msg_1", ts=None, secret_raw=SECRET_RAW):
    ts = str(int(ts if ts is not None else time.time()))
    mac = hmac.new(secret_raw, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {"svix-id": msg_id, "svix-timestamp": ts,
            "svix-signature": "v1," + base64.b64encode(mac).decode()}


def test_valid_signature_verifies():
    body = b'{"type":"email.bounced"}'
    assert webhooks.verify_svix(SECRET, sign(body), body)


def test_signature_rejects_tampering_wrong_key_stale_and_missing_pieces():
    body = b'{"type":"email.bounced"}'
    good = sign(body)
    assert not webhooks.verify_svix(SECRET, good, body + b" ")                  # body altered
    assert not webhooks.verify_svix(SECRET, sign(body, secret_raw=b"x" * 32), body)  # wrong key
    assert not webhooks.verify_svix(SECRET, sign(body, ts=time.time() - 3600), body)  # replay
    assert not webhooks.verify_svix(SECRET, {k: v for k, v in good.items() if k != "svix-id"}, body)
    assert not webhooks.verify_svix("", good, body)                             # no secret: fail closed
    assert not webhooks.verify_svix("whsec_%%%not-base64", good, body)


def test_any_listed_signature_may_match_for_key_rotation():
    body = b"{}"
    good = sign(body)
    rotated = {**good, "svix-signature": "v1,AAAA " + good["svix-signature"]}
    assert webhooks.verify_svix(SECRET, rotated, body)


# ── Event handling ──

async def sent_item(state, email="jane@acme.com", provider_id="re_msg_1"):
    pid = await seed(state, email)
    oid = await queue(state, pid, email)
    await state.update_outbox_item(oid, status="sent", provider_id=provider_id)
    await state.record_send(channel="email", prospect_id=pid, ref=oid,
                            sender_domain="go.example.com")
    follow_up = await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="s", body="b", send_at="x",
        status="approved", kind="sequence", campaign_id="c1", step=2)
    return pid, oid, follow_up


@pytest.mark.asyncio
async def test_hard_bounce_event_invalidates_suppresses_and_cancels(state):
    pid, oid, follow_up = await sent_item(state)
    event = {"type": "email.bounced", "data": {
        "email_id": "re_msg_1", "to": ["jane@acme.com"], "bounce": {"type": "Permanent"}}}
    assert await webhooks.handle_resend_event(state, event) == "hard_bounce"

    assert (await state.get_prospect(pid)).email_status == "invalid"
    assert await state.is_suppressed("email", "jane@acme.com")
    assert (await state.get_outbox_item(follow_up))["status"] == "cancelled"
    assert await state.count_outcomes_today("email", "hard_bounce", "go.example.com") == 1


@pytest.mark.asyncio
async def test_transient_bounce_changes_nothing(state):
    pid, oid, follow_up = await sent_item(state)
    event = {"type": "email.bounced", "data": {
        "email_id": "re_msg_1", "to": ["jane@acme.com"], "bounce": {"type": "Transient"}}}
    assert await webhooks.handle_resend_event(state, event) == "soft_bounce"
    assert (await state.get_prospect(pid)).email_status == "verified"
    assert (await state.get_outbox_item(follow_up))["status"] == "approved"
    assert not await state.is_suppressed("email", "jane@acme.com")


@pytest.mark.asyncio
async def test_complaint_event_opts_the_person_out_everywhere(state):
    pid, oid, follow_up = await sent_item(state)
    async with __import__("aiosqlite").connect(state.db_path) as db:
        await db.execute("UPDATE prospects SET phone='(813) 555-1212', "
                         "linkedin_url='https://linkedin.com/in/jane' WHERE id=?", (pid,))
        await db.commit()
    event = {"type": "email.complained", "data": {"email_id": "re_msg_1", "to": ["jane@acme.com"]}}
    assert await webhooks.handle_resend_event(state, event) == "complaint"

    assert (await state.get_prospect(pid)).status == "opted_out"
    assert await state.is_suppressed("phone", "8135551212", channel="sms")
    assert await state.is_suppressed("linkedin", "linkedin.com/in/jane", channel="linkedin")
    assert (await state.get_outbox_item(follow_up))["status"] == "cancelled"
    assert await state.count_outcomes_today("email", "complaint", "go.example.com") == 1


@pytest.mark.asyncio
async def test_event_for_an_unknown_email_id_falls_back_to_a_recipient_we_emailed(state):
    pid, _oid, _follow = await sent_item(state)         # we really did email jane@acme.com
    event = {"type": "email.complained", "data": {"email_id": "re_other", "to": ["jane@acme.com"]}}
    assert await webhooks.handle_resend_event(state, event) == "complaint"
    assert (await state.get_prospect(pid)).status == "opted_out"


@pytest.mark.asyncio
async def test_uninteresting_events_and_unknown_recipients_are_harmless(state):
    assert await webhooks.handle_resend_event(state, {"type": "email.delivered", "data": {}}) == "ignored"
    event = {"type": "email.bounced", "data": {"email_id": "nope", "to": ["ghost@nowhere.com"],
                                               "bounce": {"type": "Permanent"}}}
    assert await webhooks.handle_resend_event(state, event) == "not_ours"


@pytest.mark.asyncio
async def test_breaker_trips_after_a_complaint_from_the_webhook(state, monkeypatch):
    api = FakeResend(monkeypatch)
    config = cfg()
    pid, oid, _ = await sent_item(state)
    await webhooks.handle_resend_event(state, {"type": "email.complained", "data": {
        "email_id": "re_msg_1", "to": ["jane@acme.com"]}})
    other = await seed(state, "bob@other.com")
    await queue(state, other, "bob@other.com")
    channel = EmailChannel(config, ResendMailProvider(config, ENV), state)
    assert await drain_channel(state, channel, pacing_enabled=False) == 0
    assert "breaker" in await state.get_setting("sending_paused")
    assert api.requests == []


# ── HTTP routes ──

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("HARVEY_DASHBOARD_AUTH", "on")
    monkeypatch.setenv("HARVEY_API_TOKEN", "tok-for-tests-0123456789abcdef0123")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("HARVEY_PUBLIC_HOSTS", "go.example.com")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    return TestClient(dash.app, base_url="http://go.example.com", follow_redirects=False)


async def _populate(db_path):
    sm = StateManager(str(db_path))
    await sm.init_db()
    pid = await seed(sm)
    oid = await queue(sm, pid)
    await sm.update_outbox_item(oid, status="sent", provider_id="re_msg_1")
    token = await sm.mint_unsub_token(pid, oid)
    follow_up = await sm.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s", body="b", send_at="x",
        status="approved", kind="sequence", campaign_id="c1", step=2)
    return sm, pid, token, follow_up


@pytest.mark.asyncio
async def test_get_does_not_unsubscribe_but_post_does(client, tmp_path):
    sm, pid, token, follow_up = await _populate(tmp_path / "harvey.db")

    page = client.get(f"/u/{token}")
    assert page.status_code == 200 and "Yes, unsubscribe me" in page.text
    assert (await sm.get_prospect(pid)).status != "opted_out"      # a link scanner changed nothing

    done = client.post(f"/u/{token}", content="List-Unsubscribe=One-Click",
                       headers={"content-type": "application/x-www-form-urlencoded"})
    assert done.status_code == 200 and "unsubscribed" in done.text.lower()
    assert (await sm.get_prospect(pid)).status == "opted_out"
    assert await sm.is_suppressed("email", "jane@acme.com", channel="linkedin")
    assert (await sm.get_outbox_item(follow_up))["status"] == "cancelled"
    assert (await sm.get_unsub_token(token))["used_at"]
    assert client.post(f"/u/{token}").status_code == 200           # idempotent


def test_unknown_unsubscribe_token_is_a_plain_404(client):
    assert client.get("/u/not-a-token").status_code == 404
    assert client.post("/u/not-a-token").status_code == 404


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signatures_and_applies_good_ones_once(client, tmp_path):
    sm, pid, token, follow_up = await _populate(tmp_path / "harvey.db")
    body = json.dumps({"type": "email.complained", "data": {
        "email_id": "re_msg_1", "to": ["jane@acme.com"]}}).encode()

    assert client.post("/api/webhooks/resend", content=body).status_code == 401
    forged = sign(body, secret_raw=b"y" * 32)
    assert client.post("/api/webhooks/resend", content=body, headers=forged).status_code == 401
    assert (await sm.get_prospect(pid)).status != "opted_out"

    good = sign(body, msg_id="evt_9")
    first = client.post("/api/webhooks/resend", content=body, headers=good)
    assert first.status_code == 200 and first.json()["result"] == "complaint"
    assert (await sm.get_prospect(pid)).status == "opted_out"
    again = client.post("/api/webhooks/resend", content=body, headers=good)
    assert again.json()["result"] == "duplicate"


def test_webhook_fails_closed_without_a_configured_secret(client, monkeypatch):
    monkeypatch.delenv("RESEND_WEBHOOK_SECRET")
    body = b"{}"
    r = client.post("/api/webhooks/resend", content=body, headers=sign(body))
    assert r.status_code == 503


def test_the_public_hostname_cannot_reach_the_control_api(client):
    assert client.get("/api/stats").status_code == 403
    assert client.post("/api/settings/env", json={"SMTP_PASSWORD": "x"}).status_code == 403
    assert client.get("/").status_code == 403
    assert client.get("/u/whatever").status_code == 404            # reachable, just unknown


# ── Shared IMAP reader ──

RAW = (b"From: Jane Doe <Jane@Acme.com>\r\nSubject: Re: hello\r\nMessage-ID: <abc@acme.com>\r\n"
       b"In-Reply-To: <prev@go.example.com>\r\nContent-Type: text/plain\r\n\r\nSure, call me.\r\n")


def test_imap_reader_parses_a_reply():
    msg = ImapReader.parse_rfc822(RAW)
    assert msg.from_email == "jane@acme.com" and msg.body.strip() == "Sure, call me."
    assert msg.in_reply_to == "<prev@go.example.com>" and not msg.is_bounce
    assert ImapReader.parse_rfc822(b"Subject: no sender\r\n\r\nx") is None


def test_imap_reader_flags_bounces():
    raw = (b"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\n"
           b"Subject: Delivery Status Notification (Failure)\r\n\r\n550 no such user\r\n")
    assert ImapReader.parse_rfc822(raw).is_bounce


def test_imap_reader_needs_all_credentials():
    assert ImapReader("h", 993, "u", "p").is_configured()
    assert not ImapReader("h", 993, "", "p").is_configured()


@pytest.mark.asyncio
async def test_policy_bounce_event_keeps_address_valid_and_pauses_on_repeat(state):
    pid, oid, follow_up = await sent_item(state)
    event = {"type": "email.bounced", "data": {
        "email_id": "re_msg_1", "to": ["jane@acme.com"],
        "bounce": {"type": "Permanent", "subType": "General",
                   "message": "550 5.7.1 Message blocked due to poor sender reputation"}}}
    assert await webhooks.handle_resend_event(state, event) == "policy_block"
    assert (await state.get_prospect(pid)).email_status == "verified"
    assert not await state.is_suppressed("email", "jane@acme.com")
    assert (await state.get_outbox_item(follow_up))["status"] == "approved"
    assert await state.get_setting("sending_paused") == ""      # one block: no pause

    assert await webhooks.handle_resend_event(state, event) == "policy_block"
    assert "policy blocks" in await state.get_setting("sending_paused")


@pytest.mark.asyncio
async def test_other_software_sharing_the_resend_account_cannot_pause_or_poison_harvey(state):
    """The OS sends its own mail (bookings, notices, tests) through the same Resend account, and
    every bounce hits this webhook. Harvey acted on them as his own and paused himself."""
    await sent_item(state)                      # Harvey has sent exactly one email, to jane@acme.com
    for n in range(4):
        foreign = {"type": "email.bounced", "data": {
            "email_id": f"re_os_{n}", "to": [f"owner+t{n}@example.net"],
            "bounce": {"type": "Permanent", "subType": "General",
                       "message": "550 5.7.708 Service unavailable. Access denied, traffic not accepted"}}}
        assert await webhooks.handle_resend_event(state, foreign) == "not_ours"
    complaint = {"type": "email.complained", "data": {"email_id": "re_os_9", "to": ["admin@example.net"]}}
    assert await webhooks.handle_resend_event(state, complaint) == "not_ours"

    assert await state.get_setting("sending_paused") == ""
    assert await state.get_setting("policy_block_times", "[]") in ("", "[]")
    assert not await state.is_suppressed("email", "admin@example.net")
