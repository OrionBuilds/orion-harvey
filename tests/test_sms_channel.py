"""SMS: consent-gated, windowed, STOP-respecting, off by default."""

import json
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

from harvey.channels.drain import drain_channel
from harvey.channels.sms import SmsChannel, keyword
from harvey.config import EnvConfig, HarveyConfig
from harvey.integrations.mail_provider import InboundMessage, SendResult
from harvey.integrations.sms_transport import SmsTransport, TwilioTransport
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.test_codex_e2e import BASE

NUMBER = "+18135550142"
WEEKDAY_NOON = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)      # Tue 12:00 ET
WEEKDAY_NIGHT = datetime(2026, 11, 3, 3, 30, tzinfo=timezone.utc)     # Mon 22:30 ET
SATURDAY_NOON = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)


class FakeSms(SmsTransport):
    def __init__(self):
        self.sent, self.inbox = [], []

    def is_configured(self):
        return True

    async def send(self, to, body):
        self.sent.append((to, body))
        return SendResult(ok=True, message_id="SM1")

    async def poll(self, since):
        return self.inbox


def config(**sms):
    return HarveyConfig(**{**BASE, "channels": {
        **BASE["channels"], "sms": {"enabled": True, "from_number": "+18135550100",
                                    "transport": "twilio", **sms}}})


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(db_path=str(tmp_path / "s.db"))
    await s.init_db()
    return s


async def person(state, consent=True, phone=NUMBER, phone_status="verified"):
    pid = await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="VP", company="Acme", email="jane@acme.com",
        email_status="verified", phone=phone, status="replied"))
    await state.set_prospect_phone(pid, phone, phone_status)
    if consent:
        await state.set_prospect_consent(pid, {"sms": {"basis": "inbound_contact"}})
    return pid


async def queue(state, pid, body="Harvey here, Jordan's assistant. Thanks for getting in touch.", status="approved"):
    return await state.add_outbox_item(
        prospect_id=pid, channel="sms", kind="reply", to_email="", to_ref=NUMBER, subject="", body=body,
        send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), status=status)


def channel(state, now=WEEKDAY_NOON, transport=None, **sms):
    return SmsChannel(config(**sms), EnvConfig(), state, transport or FakeSms(), now=lambda: now)


@pytest.mark.asyncio
async def test_off_by_default():
    cfg = HarveyConfig(**BASE)
    assert cfg.channels.sms.enabled is False
    assert not SmsChannel(cfg, EnvConfig(), None, FakeSms()).is_configured()


@pytest.mark.asyncio
async def test_a_consented_verified_number_in_the_window_is_texted_with_the_stop_footer(state):
    pid = await person(state)
    item = await queue(state, pid)
    transport = FakeSms()
    assert await drain_channel(state, channel(state, transport=transport), pacing_enabled=False) == 1
    to, body = transport.sent[0]
    assert to == NUMBER and body.endswith("Reply STOP to opt out.")
    assert (await state.get_outbox_item(item))["status"] == "sent"


@pytest.mark.asyncio
async def test_the_daily_sms_cap_stops_texts_the_way_the_email_cap_does(state):
    assert HarveyConfig(**BASE).channels.sms.max_daily_sms == 20   # a cap exists by default
    pid = await person(state)
    first, second = await queue(state, pid), await queue(state, pid)
    transport = FakeSms()
    sms = channel(state, transport=transport, max_daily_sms=1)
    assert sms.daily_cap() == 1
    assert await drain_channel(state, sms, pacing_enabled=False) == 1
    assert await drain_channel(state, sms, pacing_enabled=False) == 0   # cap reached: nothing more today
    assert len(transport.sent) == 1 and await state.count_sent_today("sms") == 1
    statuses = {(await state.get_outbox_item(i))["status"] for i in (first, second)}
    assert statuses == {"sent", "approved"}   # the second text waits for tomorrow, it is not dropped


@pytest.mark.asyncio
async def test_no_consent_means_no_text_even_if_approved(state):
    pid = await person(state, consent=False)
    item = await queue(state, pid)
    transport = FakeSms()
    await drain_channel(state, channel(state, transport=transport), pacing_enabled=False)
    row = await state.get_outbox_item(item)
    assert transport.sent == [] and row["status"] == "cancelled" and "consent" in row["error"]


@pytest.mark.asyncio
async def test_an_unverified_number_is_never_texted(state):
    pid = await person(state, phone_status="")
    item = await queue(state, pid)
    transport = FakeSms()
    await drain_channel(state, channel(state, transport=transport), pacing_enabled=False)
    assert transport.sent == [] and "not verified" in (await state.get_outbox_item(item))["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("now", [WEEKDAY_NIGHT, SATURDAY_NOON])
async def test_outside_the_texting_window_the_text_waits_instead_of_being_lost(state, now):
    pid = await person(state)
    item = await queue(state, pid)
    transport = FakeSms()
    await drain_channel(state, channel(state, now=now, transport=transport), pacing_enabled=False)
    assert transport.sent == [] and (await state.get_outbox_item(item))["status"] == "approved"
    # and it goes the next time the window is open
    await drain_channel(state, channel(state, transport=transport), pacing_enabled=False)
    assert len(transport.sent) == 1


@pytest.mark.asyncio
async def test_a_suppressed_number_is_never_texted(state):
    pid = await person(state)
    await state.add_suppression("phone", NUMBER, channel="sms", reason="stop_keyword", source="sms")
    item = await queue(state, pid)
    transport = FakeSms()
    await drain_channel(state, channel(state, transport=transport), pacing_enabled=False)
    assert transport.sent == [] and (await state.get_outbox_item(item))["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("body, reason", [
    ("Harvey: Hi {{first_name}}, thanks for texting.", "merge"),
    ("Harvey: check https://evil.example.com/x for more.", "may appear"),
    ("Harvey " + "x" * 260, "over 240"),
    ("Harvey: Thanks! Talk soon.", "exclamation"),
    ("Thanks for getting in touch.", "must say it is Harvey"),
])
async def test_content_rules_block_a_bad_text(state, body, reason):
    pid = await person(state)
    item = await queue(state, pid, body=body)
    transport = FakeSms()
    await drain_channel(state, channel(state, transport=transport), pacing_enabled=False)
    row = await state.get_outbox_item(item)
    assert transport.sent == [] and row["status"] == "failed" and reason in row["error"]


def test_only_a_bare_stop_word_is_a_keyword():
    assert keyword("STOP") == "stop" and keyword(" stop. ") == "stop" and keyword("Unsubscribe") == "stop"
    assert keyword("start") == "start"
    assert keyword("please stop calling me about the other thing") == ""
    assert keyword("Interested, tell me more") == ""


# ── inbound: STOP is permanent and handled before any model sees the text ──

class NoBrain:
    async def think_json(self, *a, **k):
        raise AssertionError("a STOP must never reach the model")

    async def think(self, *a, **k):
        raise AssertionError("a STOP must never reach the model")


async def handler_with(state, transport):
    from harvey.agents.handler import Handler
    h = Handler(brain=NoBrain(), state=state, config=config(), env=EnvConfig())
    h.provider = None
    h._channels = lambda: [SmsChannel(config(), EnvConfig(), state, transport)]
    return h


@pytest.mark.asyncio
async def test_stop_silences_the_number_cancels_queued_texts_and_never_calls_the_model(state):
    pid = await person(state)
    item = await queue(state, pid, status="pending_review")
    transport = FakeSms()
    transport.inbox = [InboundMessage(provider_id="SMstop", from_email="", body="STOP", channel="sms", from_ref=NUMBER)]
    assert await (await handler_with(state, transport))._run_native() is True

    assert await state.find_suppression("phone", NUMBER, channel="sms")
    assert (await state.get_outbox_item(item))["status"] == "cancelled"
    assert "sms" not in (await state.get_prospect(pid)).consent()


@pytest.mark.asyncio
async def test_start_restores_texting(state):
    pid = await person(state, consent=False)
    await state.add_suppression("phone", NUMBER, channel="sms", reason="stop_keyword", source="sms")
    transport = FakeSms()
    transport.inbox = [InboundMessage(provider_id="SMstart", from_email="", body="START", channel="sms", from_ref=NUMBER)]
    await (await handler_with(state, transport))._run_native()
    assert not await state.find_suppression("phone", NUMBER, channel="sms")
    assert (await state.get_prospect(pid)).consent()["sms"]["basis"] == "inbound_contact"


# ── Twilio wire format ──

@pytest.mark.asyncio
async def test_twilio_send_and_poll():
    seen = {}

    def handler(req: httpx.Request):
        if req.method == "POST":
            seen["form"] = dict(httpx.QueryParams(req.content.decode()))
            return httpx.Response(201, json={"sid": "SMabc"})
        return httpx.Response(200, json={"messages": [
            {"sid": "SM1", "direction": "inbound", "from": NUMBER, "body": "hi", "date_sent": "Tue, 03 Nov 2026 17:00:00 +0000"},
            {"sid": "SM2", "direction": "outbound-api", "from": "+18135550100", "body": "out", "date_sent": "Tue, 03 Nov 2026 17:01:00 +0000"}]})

    t = TwilioTransport("AC1", "tok", "+18135550100", transport=httpx.MockTransport(handler))
    ok = await t.send(NUMBER, "hello")
    assert ok.ok and ok.message_id == "SMabc" and seen["form"]["To"] == NUMBER and seen["form"]["From"] == "+18135550100"
    inbound = await t.poll(None)
    assert [m.provider_id for m in inbound] == ["SM1"] and inbound[0].channel == "sms" and inbound[0].from_ref == NUMBER


@pytest.mark.asyncio
async def test_twilio_refusal_is_a_failure_and_an_outage_is_retried():
    refuse = TwilioTransport("a", "b", "+1", transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"message": "bad number"})))
    outage = TwilioTransport("a", "b", "+1", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    r1, r2 = await refuse.send(NUMBER, "x"), await outage.send(NUMBER, "x")
    assert not r1.ok and not r1.defer and "bad number" in r1.error
    assert not r2.ok and r2.defer


@pytest.mark.asyncio
async def test_texting_us_first_records_consent_and_verifies_the_number(state):
    pid = await person(state, consent=False, phone_status="")
    transport = FakeSms()
    transport.inbox = [InboundMessage(provider_id="SMin", from_email="", body="START", channel="sms", from_ref=NUMBER)]
    await (await handler_with(state, transport))._run_native()
    prospect = await state.get_prospect(pid)
    assert prospect.consent()["sms"]["basis"] == "inbound_contact" and prospect.phone_status == "verified"


# ── sending through the OS from Clara's number ──

class Rec:
    """A stand-in OS client that records calls."""
    def __init__(self, blocked=False, fail=None, status=200):
        self.calls, self.blocked, self.fail, self.status = [], blocked, fail, status
        self.auth = type("A", (), {"is_authorized": staticmethod(lambda: True)})()

    async def whoami(self):
        return {"organization_id": "org-1"}

    async def _send(self, method, path, **kw):
        from harvey.integrations.os_client import OsError
        self.calls.append((method, path, kw.get("json")))
        if self.fail:
            raise OsError(self.fail, self.status)
        return {"ok": True, "sid": "SMos1"}

    async def rpc(self, name, args=None):
        self.calls.append(("rpc", name, args))
        if self.fail == "rpc down":
            raise RuntimeError("rpc down")
        return {"hits": [], "blocked": self.blocked}


def os_config(**sms):
    return config(transport="os", from_number="+15550100199", **sms)


@pytest.mark.asyncio
async def test_through_the_os_it_stays_off_until_the_os_hands_threads_back(state):
    from harvey.integrations.sms_transport import OsSmsTransport
    t = OsSmsTransport(Rec(), "+15550100199")
    off = SmsChannel(os_config(), EnvConfig(), state, t)
    on = SmsChannel(os_config(inbound_ready=True), EnvConfig(), state, t)
    assert not off.is_configured() and on.is_configured()


@pytest.mark.asyncio
async def test_os_send_uses_harveys_identity_and_the_organizations_number(state):
    from harvey.integrations.sms_transport import OsSmsTransport
    rec = Rec()
    result = await OsSmsTransport(rec, "+15550100199").send(NUMBER, "Harvey here.")
    assert result.ok and result.message_id == "SMos1"
    method, path, payload = rec.calls[0]
    assert (method, path) == ("POST", "/functions/v1/twilio-sms-send")
    assert payload["fromNumber"] == "+15550100199" and payload["toNumber"] == NUMBER and payload["orgId"] == "org-1"


@pytest.mark.asyncio
async def test_os_refusal_fails_the_message_and_an_outage_retries_later(state):
    from harvey.integrations.sms_transport import OsSmsTransport
    refused = await OsSmsTransport(Rec(fail="Forbidden", status=403), "+1").send(NUMBER, "x")
    outage = await OsSmsTransport(Rec(fail="boom", status=503), "+1").send(NUMBER, "x")
    assert not refused.ok and not refused.defer and outage.defer


@pytest.mark.asyncio
async def test_a_number_on_the_os_do_not_contact_list_is_never_texted(state):
    from harvey.integrations.sms_transport import OsSmsTransport
    pid = await person(state)
    item = await queue(state, pid)
    rec = Rec(blocked=True)
    ch = SmsChannel(os_config(inbound_ready=True), EnvConfig(), state, OsSmsTransport(rec, "+15550100199"), now=lambda: WEEKDAY_NOON)
    await drain_channel(state, ch, pacing_enabled=False)
    row = await state.get_outbox_item(item)
    assert row["status"] == "cancelled" and "do-not-contact" in row["error"]
    assert not any(c[1] == "/functions/v1/twilio-sms-send" for c in rec.calls)


@pytest.mark.asyncio
async def test_if_the_do_not_contact_list_cannot_be_checked_nothing_is_sent(state):
    from harvey.integrations.sms_transport import OsSmsTransport
    pid = await person(state)
    item = await queue(state, pid)
    rec = Rec(fail="rpc down")
    ch = SmsChannel(os_config(inbound_ready=True), EnvConfig(), state, OsSmsTransport(rec, "+15550100199"), now=lambda: WEEKDAY_NOON)
    await drain_channel(state, ch, pacing_enabled=False)
    assert (await state.get_outbox_item(item))["status"] == "approved"      # held, not lost, not sent
    assert not any(c[1] == "/functions/v1/twilio-sms-send" for c in rec.calls)


@pytest.mark.asyncio
async def test_through_the_os_the_text_says_replies_go_to_clara_and_still_passes_the_gate(state):
    from harvey.integrations.sms_transport import OsSmsTransport
    pid = await person(state)
    ch = SmsChannel(os_config(inbound_ready=True), EnvConfig(), state, OsSmsTransport(Rec(), "+15550100199"))
    prospect = await state.get_prospect(pid)
    item = {"body": "Harvey here, Jordan's assistant. Following up on our emails."}
    subject, body = await ch.prepare(item, prospect)
    assert body.endswith("Replies go to Clara, Jordan's assistant.\nReply STOP to opt out.")
    assert ch.content_gate(item, subject, body, prospect)               # the added lines don't trip the length/link rules
    _, direct = await channel(state).prepare(item, prospect)             # Twilio-direct: no Clara line
    assert "Clara" not in direct and direct.endswith("Reply STOP to opt out.")


# ── the owner's commands: consent is explicit, a text is queued only when it could be sent ──

def _run_cli(monkeypatch, state, **args):
    import argparse
    import harvey.cli as cli
    import harvey.config as cfg
    import harvey.state as st
    monkeypatch.setattr(cfg, "load_config", lambda: config(transport="os", from_number="+15550100199", inbound_ready=True))
    monkeypatch.setattr(cfg, "load_env", lambda: EnvConfig())
    monkeypatch.setattr(st, "StateManager", lambda: state)
    defaults = dict(sms_action="status", email="", text="", phone="", basis="opted_in", note="")
    cli.cmd_sms(argparse.Namespace(**{**defaults, **args}))


@pytest.mark.asyncio
async def test_cli_consent_then_queue_stages_a_text_that_waits_for_approval(state, monkeypatch, capsys):
    import asyncio
    pid = await state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="VP", company="Acme",
                                            email="jane@acme.com", email_status="verified", status="replied"))
    loop = asyncio.get_running_loop()
    run = lambda **a: loop.run_in_executor(None, lambda: _run_cli(monkeypatch, state, **a))

    with pytest.raises(SystemExit):                                          # no consent yet: refused
        await run(sms_action="queue", email="jane@acme.com", text="Harvey here, Jordan's assistant. Hello.")
    with pytest.raises(SystemExit):                                          # consent needs real evidence
        await run(sms_action="consent", email="jane@acme.com", phone=NUMBER, note="ok")
    with pytest.raises(SystemExit):                                          # and a valid number
        await run(sms_action="consent", email="jane@acme.com", phone="8135550142", note="She agreed on our call Nov 3")
    await run(sms_action="consent", email="jane@acme.com", phone=NUMBER, note="She agreed to texts on our call Nov 3")
    p = await state.get_prospect(pid)
    assert p.phone == NUMBER and p.phone_status == "verified"
    assert p.consent()["sms"]["basis"] == "opted_in" and "call Nov 3" in p.consent()["sms"]["note"]

    with pytest.raises(SystemExit):                                          # a text must say it is Harvey
        await run(sms_action="queue", email="jane@acme.com", text="Quick hello about the project.")
    await run(sms_action="queue", email="jane@acme.com", text="Harvey here, Jordan's assistant. Quick hello about the project.")
    queued = await state.get_outbox(status="pending_review", channel="sms")
    assert len(queued) == 1 and queued[0]["to_ref"] == NUMBER


@pytest.mark.asyncio
async def test_a_real_handler_with_sms_switched_on_still_reads_replies(state):
    """Regression: the handler built its channel list from an env it never stored, so switching
    SMS on crashed the reply reader (which also holds email sending). Built the way main builds it."""
    from harvey.agents.handler import Handler
    h = Handler(brain=NoBrain(), state=state, config=config(transport="os", from_number="+15550100199",
                                                              inbound_ready=True), env=EnvConfig())
    assert any(ch.name == "sms" for ch in h._channels())
