"""The OS bridge against an in-memory fake of the OS (PostgREST + RPC)."""

import json
import re
import time

import httpx
import pytest
import pytest_asyncio

from harvey.config import EnvConfig, HarveyConfig
from harvey.integrations.os_auth import OsAuth
from harvey.integrations.os_client import OsClient
from harvey.models.prospect import Prospect
from harvey.os_bridge import MANUAL_PAUSE_PREFIX, OsBridge, interpret
from harvey.state import StateManager
from tests.test_codex_e2e import BASE
from tests.test_outbox_native import _now_iso, seed_prospect

URL = "https://os.example.supabase.co"
ORG = "00000000-0000-0000-0000-000000000001"


class FakeOs:
    """Just enough of the OS for the bridge: filters are eq / in / is.null."""

    def __init__(self):
        self.queue: list[dict] = []          # workflow_approval_queue
        self.commands: list[dict] = []
        self.requests: list[dict] = []
        self.reports: list[dict] = []
        self.request_reports: list[dict] = []
        self.command_reports: list[dict] = []
        self.notifications: list[dict] = []
        self.role = "HarveyAgent"
        self.contacts: list[dict] = []
        self.opportunities: list[dict] = []

    @staticmethod
    def _match(row, key, cond):
        value = row.get(key)
        if cond.startswith("eq."):
            return str(value) == cond[3:]
        if cond.startswith("in."):
            return str(value) in cond[4:-1].split(",")
        if cond == "is.null":
            return value is None
        return True

    def _filter(self, rows, params):
        reserved = {"select", "order", "limit"}
        return [r for r in rows if all(self._match(r, k, v) for k, v in params.items() if k not in reserved)]

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        params = dict(req.url.params)
        body = json.loads(req.content) if req.content else None
        if path == "/auth/v1/token":
            return httpx.Response(200, json={
                "access_token": "a", "refresh_token": "r2", "expires_in": 3600,
                "user": {"id": "u-harvey", "email": "harvey@example.com"}})
        if path.startswith("/rest/v1/rpc/"):
            name = path.rsplit("/", 1)[1]
            return httpx.Response(200, json=self.rpc(name, body or {}))
        if path == "/rest/v1/pipelines":
            return httpx.Response(200, json=[{"id": "pipe-out"}] if params.get("name") == "eq.Outbound" else [])
        if path == "/rest/v1/pipeline_stages":
            return httpx.Response(200, json=[{"id": "stage-engaged"}] if params.get("name") == "eq.Engaged" else [])
        if path in ("/rest/v1/contacts", "/rest/v1/opportunities") and req.method == "POST":
            table = self.contacts if path.endswith("contacts") else self.opportunities
            if any(r["id"] == body["id"] for r in table):
                return httpx.Response(409, json={"code": "23505", "message": "duplicate key"})
            table.append(body)
            return httpx.Response(201)
        if path == "/rest/v1/workflow_approval_queue":
            if req.method == "GET":
                return httpx.Response(200, json=self._filter(self.queue, params))
            if req.method == "POST":
                if any(r["external_ref"] == body["external_ref"] for r in self.queue):
                    return httpx.Response(409, json={"code": "23505", "message": "duplicate key"})
                row = {**body, "id": f"appr-{len(self.queue) + 1}", "acked_at": None}
                self.queue.append(row)
                return httpx.Response(201, json=[row])
            if req.method == "PATCH":
                hit = self._filter(self.queue, params)
                for r in hit:
                    r.update(body)
                return httpx.Response(200, json=hit)
        return httpx.Response(404, json={"message": f"unhandled {req.method} {path}"})

    def rpc(self, name, args):
        if name == "agent_whoami":
            return {"user_id": "u-harvey", "organization_id": ORG, "email": "harvey@example.com",
                    "role": self.role, "agent_slug": "harvey" if self.role == "HarveyAgent" else None}
        if name == "harvey_commands_claim":
            out = [c for c in self.commands if c["status"] == "queued"]
            for c in out:
                c["status"] = "claimed"
            return out
        if name == "harvey_command_report":
            self.command_reports.append(args)
            return None
        if name == "agent_requests_claim":
            out = [r for r in self.requests if r["status"] == "queued"]
            for r in out:
                r["status"] = "claimed"
            return out
        if name == "agent_request_report":
            self.request_reports.append(args)
            return None
        if name == "harvey_report":
            self.reports.append(args)
            return {"prospects": len(args["p_prospects"]), "events": len(args["p_events"])}
        if name == "agent_notify_owner":
            self.notifications.append(args)
            return {"ok": True}
        raise AssertionError(f"unexpected rpc {name}")


@pytest_asyncio.fixture
async def rig(tmp_path):
    state = StateManager(db_path=str(tmp_path / "h.db"))
    await state.init_db()
    fake = FakeOs()
    auth = OsAuth(URL, "anon", "harvey@example.com", session_file=tmp_path / "os" / "os-session.json")
    auth._write({"refresh_token": "r1", "access_token": "a", "expires_at": time.time() + 3000,
                 "email": "harvey@example.com"})
    client = OsClient(auth, transport=httpx.MockTransport(fake.handler))
    bridge = OsBridge(state, HarveyConfig(**BASE), EnvConfig(), client=client)
    return state, fake, bridge


async def _draft(state, pid, step=1, status="pending_review"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject=f"hello {step}", body="body text",
        send_at=_now_iso(), status=status, campaign_id="c1", step=step)


@pytest.mark.asyncio
async def test_pending_draft_is_raised_once_in_the_shared_approval_queue(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    await _draft(state, pid)
    assert (await bridge.cycle())["ok"]
    assert (await bridge.cycle())["ok"]
    assert len(fake.queue) == 1
    row = fake.queue[0]
    assert row["source"] == "agent_approval" and row["agent_slug"] == "harvey"
    assert row["draft_content"]["draft"]["to"] == ["jane@acme.com"]
    assert row["external_ref"].startswith("outbox:") and row["external_ref"].endswith(":1")


@pytest.mark.asyncio
async def test_approved_in_os_releases_the_draft_with_the_owners_edits(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item_id = await _draft(state, pid)
    await bridge.cycle()
    row = fake.queue[0]
    row["status"] = "approved"
    row["draft_content"]["draft"]["body"] = "Edited by Jordan"
    await bridge.cycle()
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "approved" and item["body"] == "Edited by Jordan"
    assert row["acked_at"]                      # acknowledged so it is not applied twice


@pytest.mark.asyncio
async def test_rejected_in_os_stops_the_draft(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item_id = await _draft(state, pid)
    await bridge.cycle()
    fake.queue[0]["status"] = "rejected"
    await bridge.cycle()
    assert (await state.get_outbox_item(item_id))["status"] == "rejected"


@pytest.mark.asyncio
async def test_expired_is_not_a_no_and_is_raised_again(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item_id = await _draft(state, pid)
    await bridge.cycle()
    fake.queue[0]["status"] = "expired"
    await bridge.cycle()
    assert (await state.get_outbox_item(item_id))["status"] == "pending_review"
    await bridge.cycle()
    refs = sorted(r["external_ref"] for r in fake.queue)
    assert refs[0].endswith(":1") and refs[1].endswith(":2")


@pytest.mark.asyncio
async def test_a_draft_decided_in_the_local_dashboard_closes_its_os_card(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item_id = await _draft(state, pid)
    await bridge.cycle()
    await state.approve_outbox(item_id)           # approved locally first
    await bridge.cycle()
    assert fake.queue[0]["status"] == "superseded"


@pytest.mark.asyncio
async def test_pause_and_resume_commands(rig):
    state, fake, bridge = rig
    fake.commands = [{"id": "c1", "command": "pause", "status": "queued"}]
    await bridge.cycle()
    assert (await state.get_setting("sending_paused")).startswith(MANUAL_PAUSE_PREFIX)
    assert fake.command_reports[-1]["p_status"] == "done"
    fake.commands = [{"id": "c2", "command": "resume", "status": "queued"}]
    await bridge.cycle()
    assert await state.get_setting("sending_paused") == ""


@pytest.mark.asyncio
async def test_thread_status_pause_and_cautious_resume(rig):
    state, fake, bridge = rig
    fake.requests = [{"id": "q1", "status": "queued", "body": "how are you doing, status?"}]
    await bridge.cycle()
    assert "Harvey is running" in fake.request_reports[-1]["p_summary"]
    assert fake.request_reports[-1]["p_status"] == "completed"

    # An automatic pause is not lifted by a casual "resume".
    await state.set_setting("sending_paused", "2 policy blocks (5.7.x) in 24h; receivers refuse our mail")
    fake.requests = [{"id": "q2", "status": "queued", "body": "resume"}]
    await bridge.cycle()
    assert "resume anyway" in fake.request_reports[-1]["p_summary"]
    assert await state.get_setting("sending_paused")
    fake.requests = [{"id": "q3", "status": "queued", "body": "resume anyway"}]
    await bridge.cycle()
    assert await state.get_setting("sending_paused") == ""


@pytest.mark.asyncio
async def test_report_sends_snapshot_prospects_and_events_only_when_changed(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    await _draft(state, pid)
    await bridge.cycle()
    first = fake.reports[-1]
    assert first["p_snapshot"]["pending_review"] == 1
    assert [p["prospect_id"] for p in first["p_prospects"]] == [pid]
    assert any(e["kind"] == "draft" for e in first["p_events"])
    await bridge.cycle()
    assert fake.reports[-1]["p_prospects"] == []          # nothing changed
    await bridge.step_commands()
    bridge._force_resync = True
    await bridge.cycle()
    assert len(fake.reports[-1]["p_prospects"]) == 1       # sync_now resends everything


@pytest.mark.asyncio
async def test_an_automatic_pause_alerts_the_owner_once(rig):
    state, fake, bridge = rig
    await state.set_setting("sending_paused", "bounce rate 3/20 exceeded 5%")
    await bridge.cycle()
    await bridge.cycle()
    paused = [n for n in fake.notifications if n["p_kind"] == "harvey.paused"]
    assert len(paused) == 1 and paused[0]["p_category"] == "production"


@pytest.mark.asyncio
async def test_not_the_harvey_agent_is_refused(rig):
    state, fake, bridge = rig
    fake.role = "Member"
    outcome = await bridge.cycle()
    assert not outcome["ok"] and not fake.reports


def test_interpret_the_owners_words():
    assert interpret("Pause sending please") == "pause"
    assert interpret("ok resume") == "resume"
    assert interpret("resume anyway") == "resume_force"
    assert interpret("any replies?") == "replies"
    assert interpret("what's waiting for approval") == "pending"
    assert interpret("give me the digest") == "digest"
    assert interpret("status") == "status"
    assert interpret("write me a poem") == "help"


@pytest.mark.asyncio
async def test_a_dead_mailbox_sign_in_alerts_the_owner_once_a_day(rig, monkeypatch):
    state, fake, bridge = rig
    bridge.env = EnvConfig(ms_client_id="cid")

    from harvey.integrations import ms_auth

    monkeypatch.setattr(ms_auth.MicrosoftAuth, "is_authorized", lambda self: True)

    async def revoked(self, **kw):
        raise ms_auth.MicrosoftAuthError("Microsoft rejected the saved sign-in (invalid_grant)")

    monkeypatch.setattr(ms_auth.MicrosoftAuth, "aaccess_token", revoked)
    await bridge.cycle()
    bridge._mail_health = (0.0, None)          # let the 5-minute cache lapse
    await bridge.cycle()
    alerts = [n for n in fake.notifications if n["p_kind"] == "harvey.mail_unreadable"]
    assert len(alerts) == 1 and alerts[0]["p_category"] == "production"
    assert fake.reports[-1]["p_snapshot"]["mail_read"]["ok"] is False


@pytest.mark.asyncio
async def test_a_rewritten_draft_gets_a_fresh_card_and_the_old_one_is_closed(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item_id = await _draft(state, pid)
    await bridge.cycle()
    assert len(fake.queue) == 1
    await state.update_outbox_item(item_id, subject="a better subject", body="a better body")
    await bridge.cycle()
    assert len(fake.queue) == 2
    old, new = fake.queue
    assert old["status"] == "superseded" and "rewrote" in old["resolution_note"]
    assert new["status"] == "pending_approval" and new["draft_content"]["draft"]["subject"] == "a better subject"
    assert new["external_ref"].endswith(":2")
    await bridge.cycle()
    assert len(fake.queue) == 2                      # and it is not raised a third time


@pytest.mark.asyncio
async def test_a_draft_rewritten_after_an_old_approval_gets_a_fresh_card_even_without_a_stored_hash(rig):
    """Cards raised before content hashing existed carry no hash. When such a draft is approved,
    acknowledged, then rewritten back into review, the old approval must not hide the new words."""
    import json
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item_id = await _draft(state, pid)
    await bridge.cycle()
    card = fake.queue[0]
    await state.set_setting(f"os_appr:{item_id}", json.dumps({"round": 1, "approval_id": card["id"]}))   # legacy: no hash
    card.update(status="approved", acked_at="2026-11-01T00:00:00Z")
    await state.update_outbox_item(item_id, subject="rewritten subject", body="rewritten body",
                                   status="pending_review")
    await bridge.cycle()
    assert len(fake.queue) == 2
    assert fake.queue[1]["status"] == "pending_approval" and fake.queue[1]["external_ref"].endswith(":2")
    assert fake.queue[1]["draft_content"]["draft"]["subject"] == "rewritten subject"
    await bridge.cycle()
    assert len(fake.queue) == 2                      # raised once, not every cycle


@pytest.mark.asyncio
async def test_a_legacy_card_that_is_still_open_and_unchanged_is_left_alone(rig):
    import json
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item_id = await _draft(state, pid)
    await bridge.cycle()
    await state.set_setting(f"os_appr:{item_id}", json.dumps({"round": 1, "approval_id": fake.queue[0]["id"]}))
    await bridge.cycle()
    assert len(fake.queue) == 1


@pytest.mark.asyncio
async def test_a_linkedin_draft_is_raised_as_a_readable_linkedin_card(rig):
    state, fake, bridge = rig
    bridge.config.channels.linkedin.mode = "auto"   # only auto mode sends it for the owner
    pid = await seed_prospect(state, status="queued")
    await state.add_outbox_item(
        prospect_id=pid, to_email="", channel="linkedin", kind="message",
        to_ref="https://www.linkedin.com/in/jane-doe", subject="", body="Thanks for connecting, Jane.",
        send_at=_now_iso(), status="pending_review", provider="linkedin")
    await bridge.cycle()
    card = fake.queue[0]
    assert card["action_type"] == "harvey_linkedin" and "LinkedIn message" in card["title"]
    assert card["draft_content"]["draft"] == {
        "kind": "linkedin", "type": "message", "to": ["https://www.linkedin.com/in/jane-doe"],
        "body": "Thanks for connecting, Jane."}
    assert "Thanks for connecting, Jane." in card["description"] and "Harvey sends it" in card["description"]


# ── the whole reply path: inbound reply -> Handler -> OS event + hot-lead alert + approval card ──

@pytest.mark.asyncio
async def test_a_real_reply_reaches_the_os_alerts_the_owner_and_queues_an_answer_for_approval(rig):
    from harvey.integrations.mail_provider import InboundMessage
    from tests.test_outbox_native import FakeProvider, make_handler, _sent_item

    state, fake, bridge = rig
    pid = await seed_prospect(state, status="contacted")
    sent_id = await _sent_item(state, pid, "<m1@x>")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="r1", from_email="jane@acme.com", subject="Re: hello",
        body="Interested. Can you send more detail on how this would work for us?",
        in_reply_to="<m1@x>")]
    handler = make_handler(state, provider, intent="interested")
    await handler._run_native()

    await bridge.cycle()
    reports = [e for r in fake.reports for e in r["p_events"]]
    reply = [e for e in reports if e["kind"] == "reply"]
    assert len(reply) == 1 and "Interested" in reply[0]["data"]["excerpt"]
    assert reply[0]["data"]["intent"] == "interested"
    assert any(p["stage"] for r in fake.reports for p in r["p_prospects"])

    hot = [n for n in fake.notifications if n["p_kind"] == "harvey.hot_reply"]
    assert len(hot) == 1 and hot[0]["p_priority"] == "critical" and hot[0]["p_category"] == "revenue"
    assert "Jane" in hot[0]["p_text"]

    # Harvey's drafted answer waits for the owner in the shared approval queue; nothing is sent.
    assert provider.sent == []
    answer_cards = [r for r in fake.queue if r["draft_content"]["facts"].get("prospect") == "Jane Doe"]
    assert answer_cards, "the drafted reply should be raised for approval"

    # A second cycle must not alert or report the same reply again.
    before = len(fake.notifications)
    await bridge.cycle()
    assert len(fake.notifications) == before


@pytest.mark.asyncio
async def test_an_interested_reply_opens_one_crm_contact_and_opportunity_owned_by_nobody_new(rig):
    from harvey.integrations.mail_provider import InboundMessage
    from tests.test_outbox_native import FakeProvider, make_handler, _sent_item

    state, fake, bridge = rig
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "<m1@x>")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="r1", from_email="jane@acme.com", subject="Re: hi",
                                       body="Yes, interested. Tell me more.", in_reply_to="<m1@x>")]
    await make_handler(state, provider, intent="interested")._run_native()
    await bridge.cycle()
    await bridge.cycle()                                  # idempotent

    assert len(fake.contacts) == 1 and fake.contacts[0]["email"] == "jane@acme.com"
    assert fake.contacts[0]["source"] == "harvey" and "owner_id" not in fake.contacts[0]
    assert len(fake.opportunities) == 1
    opp = fake.opportunities[0]
    assert (opp["pipeline_id"], opp["stage_id"], opp["contact_id"]) == ("pipe-out", "stage-engaged", fake.contacts[0]["id"])
    assert "Yes, interested" in opp["description"]
    assert any(e["kind"] == "hot_lead" for r in fake.reports for e in r["p_events"])


@pytest.mark.asyncio
async def test_a_question_or_objection_is_not_a_crm_opportunity(rig):
    from harvey.integrations.mail_provider import InboundMessage
    from tests.test_outbox_native import FakeProvider, make_handler, _sent_item

    state, fake, bridge = rig
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "<m1@x>")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="r1", from_email="jane@acme.com", subject="Re: hi",
                                       body="What does this cost?", in_reply_to="<m1@x>")]
    await make_handler(state, provider, intent="question")._run_native()
    await bridge.cycle()
    assert fake.contacts == [] and fake.opportunities == []


@pytest.mark.asyncio
async def test_a_contact_already_in_the_crm_is_not_duplicated(rig):
    from harvey.integrations.mail_provider import InboundMessage
    from tests.test_outbox_native import FakeProvider, make_handler, _sent_item

    state, fake, bridge = rig
    pid = await seed_prospect(state, status="contacted")
    await _sent_item(state, pid, "<m1@x>")
    fake.contacts.append({"id": OsBridge.crm_id("contact", "jane@acme.com"), "email": "jane@acme.com"})
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="r1", from_email="jane@acme.com", subject="Re: hi",
                                       body="Interested.", in_reply_to="<m1@x>")]
    await make_handler(state, provider, intent="interested")._run_native()
    await bridge.cycle()
    assert len(fake.contacts) == 1 and len(fake.opportunities) == 1


@pytest.mark.asyncio
async def test_the_digest_lands_once_a_day_after_the_morning_cutoff(rig, monkeypatch):
    import harvey.os_bridge as ob
    from datetime import datetime
    from zoneinfo import ZoneInfo
    state, fake, bridge = rig

    class At(datetime):
        hour_min = (6, 0)

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 4, *cls.hour_min, tzinfo=tz)   # a Wednesday

    monkeypatch.setattr(ob, "datetime", At)
    assert await bridge.step_digest() is False                         # 6:00: too early
    At.hour_min = (7, 45)
    assert await bridge.step_digest() is True
    assert await bridge.step_digest() is False                         # once a day
    digest = [n for n in fake.notifications if n["p_kind"] == "harvey.digest"]
    assert len(digest) == 1 and digest[0]["p_priority"] == "normal" and digest[0]["p_category"] is None
    assert bridge._queued_events[-1]["kind"] == "digest"


@pytest.mark.asyncio
async def test_a_text_waiting_for_approval_is_an_sms_card_not_an_email(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="replied")
    await state.add_outbox_item(
        prospect_id=pid, channel="sms", kind="reply", to_email="", to_ref="+18135550142", subject="",
        body="Harvey here, Jordan's assistant. Following up.", send_at=_now_iso(), status="pending_review")
    await bridge.cycle()
    card = fake.queue[0]
    assert card["action_type"] == "harvey_sms"
    assert card["draft_content"]["draft"] == {"kind": "sms", "to": ["+18135550142"],
                                               "body": "Harvey here, Jordan's assistant. Following up."}


@pytest.mark.asyncio
async def test_a_factory_review_step_reports_numbers_evidence_and_a_gate_not_free_text(rig):
    state, fake, bridge = rig
    fake.requests = [{"id": "f1", "status": "queued", "title": "Sequence and reply review", "body": "handoff",
                      "source": {"channel": "factory", "factory_step_run_id": "sr1", "requested_by": "owner"}}]
    await bridge.cycle()
    rep = fake.request_reports[-1]
    assert rep["p_status"] == "completed"
    result = rep["p_result"]
    assert result["output"].startswith("## Outbound review")
    assert "One change to test" in result["output"]
    assert {e["source"] for e in result["evidence"]} >= {"Harvey send_ledger (last 7 days)"}
    assert result["gate"]["passed"] is True and len(result["gate"]["checks"]) == 2
    # nothing sent yet -> the suggestion is about the outbox, not about copy
    assert "Nothing was sent" in rep["p_summary"]


@pytest.mark.asyncio
async def test_stalled_work_loop_alerts_the_owner_once(rig):
    from datetime import datetime, timedelta, timezone
    state, fake, bridge = rig
    fresh = datetime.now(timezone.utc).isoformat()
    await state.set_setting("heartbeat_alive_at", fresh)
    await bridge.step_alerts()
    assert not [n for n in fake.notifications if n["p_kind"] == "harvey.stalled"]

    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    await state.set_setting("heartbeat_alive_at", old)
    await bridge.step_alerts()
    await bridge.step_alerts()
    stalled = [n for n in fake.notifications if n["p_kind"] == "harvey.stalled"]
    assert len(stalled) == 1 and stalled[0]["p_priority"] == "critical"


# ── LinkedIn assist: the owner sends by hand and marks it sent from the OS ──

async def _li_item(state, pid, status="approved", kind="connect", channel="linkedin", body="Hi Jane, let's connect."):
    return await state.add_outbox_item(
        prospect_id=pid, to_email="", channel=channel, kind=kind,
        to_ref="https://www.linkedin.com/in/jane-doe", subject="", body=body,
        send_at=_now_iso(), status=status, provider="linkedin")


async def _command(fake, bridge, command, args, cid="k1"):
    fake.commands = [{"id": cid, "command": command, "args": args, "status": "queued"}]
    await bridge.step_commands()
    return fake.command_reports[-1]


@pytest.mark.asyncio
async def test_linkedin_sent_records_the_send_updates_the_stage_and_reports_a_sent_event(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item = await _li_item(state, pid)
    report = await _command(fake, bridge, "linkedin_sent", {"outbox_id": item})
    assert (report["p_status"], report["p_result"]) == ("done", "marked sent")
    assert (await state.get_outbox_item(item))["status"] == "sent"
    assert (await state.get_prospect(pid)).status == "contacted"
    assert (await state.get_linkedin_thread(pid))["state"] == "requested"
    assert await state.count_kind_since("linkedin", ("connect",), today_only=True) == 1
    await bridge.step_report()
    events = fake.reports[-1]["p_events"]
    sent = [e for e in events if e["kind"] == "sent"]
    assert sent and sent[0]["data"] == {"channel": "linkedin", "kind": "connect"}
    assert any(e["kind"] == "command" for e in events)


@pytest.mark.asyncio
async def test_linkedin_sent_is_refused_outside_assist_mode(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item = await _li_item(state, pid)
    bridge.config.channels.linkedin.mode = "auto"
    report = await _command(fake, bridge, "linkedin_sent", {"outbox_id": item})
    assert report["p_status"] == "failed" and report["p_result"] == "LinkedIn is not in assist mode"
    assert (await state.get_outbox_item(item))["status"] == "approved"


@pytest.mark.asyncio
async def test_linkedin_sent_is_refused_for_an_item_that_is_not_approved_or_not_linkedin(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    pending = await _li_item(state, pid, status="pending_review")
    report = await _command(fake, bridge, "linkedin_sent", {"outbox_id": pending}, cid="k1")
    assert report["p_status"] == "failed" and "approve it first" in report["p_result"]
    email = await _draft(state, pid, status="approved")
    report = await _command(fake, bridge, "linkedin_sent", {"outbox_id": email}, cid="k2")
    assert report["p_status"] == "failed" and report["p_result"] == "not a LinkedIn item"
    report = await _command(fake, bridge, "linkedin_sent", {"outbox_id": "nope"}, cid="k3")
    assert report["p_status"] == "failed"
    report = await _command(fake, bridge, "linkedin_sent", {}, cid="k4")
    assert report["p_status"] == "failed" and "outbox_id" in report["p_result"]
    assert await state.count_kind_since("linkedin", ("connect", "message"), today_only=True) == 0


# ── import_prospects ──

def _row(**kw):
    return {"first_name": "Ann", "last_name": "Lee", "title": "CTO", "company": "Initech",
            "email": "ann@initech.com", "linkedin_url": "", "industry": "software",
            "company_size": "11-50", "website": "https://www.initech.com", **kw}


async def _all_prospects(state):
    async with state._connect() as db:
        async with db.execute("SELECT id FROM prospects") as cur:
            return [r[0] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_import_creates_new_unverified_prospects_with_a_company(rig):
    state, fake, bridge = rig
    rows = [_row(), _row(first_name="Bo", last_name="Ray", email="", linkedin_url="https://www.linkedin.com/in/bo-ray",
                         company="Globex", website="")]
    report = await _command(fake, bridge, "import_prospects", {"rows": rows, "source": "  trade show "})
    assert report["p_status"] == "done"
    assert report["p_result"] == "imported 2, skipped 0 duplicates, rejected 0 (none)"
    ann = await state.get_prospect_by_email("ANN@initech.com")
    assert (ann.status, ann.email_status, ann.email_verified) == ("new", "", False)
    assert ann.source == "os-import:trade show" and ann.company == "Initech" and ann.title == "CTO"
    company = await state.get_company(ann.company_id)
    assert company.domain == "initech.com" and company.name == "Initech"
    bo = await state.get_prospect_by_linkedin_url("linkedin.com/in/bo-ray")
    assert bo.company_id and bo.email == ""
    await bridge.step_report()
    imports = [e for e in fake.reports[-1]["p_events"] if e["kind"] == "import"]
    assert len(imports) == 1 and imports[0]["summary"] == report["p_result"]


@pytest.mark.asyncio
async def test_import_skips_duplicates_by_email_linkedin_and_name_at_company(rig):
    state, fake, bridge = rig
    await seed_prospect(state)   # Jane Doe, Acme, jane@acme.com
    await state.add_prospect(Prospect(first_name="Cy", last_name="Dee", title="VP", company="Hooli",
                                      linkedin_url="https://www.linkedin.com/in/cy-dee/"))
    rows = [
        _row(email="JANE@acme.com"),
        _row(first_name="Cy", last_name="Dee", email="", linkedin_url="http://linkedin.com/in/cy-dee?x=1"),
        _row(first_name="jane", last_name="DOE", company="acme", email="other@acme.com"),
        _row(first_name="Jane", last_name="Doe", company="Another Co", email="jane@another.com"),   # new
        _row(first_name="Jane", last_name="Doe", company="Another Co", email="jane2@another.com"),  # in-batch dup
    ]
    report = await _command(fake, bridge, "import_prospects", {"rows": rows, "source": "x"})
    assert report["p_result"] == "imported 1, skipped 4 duplicates, rejected 0 (none)"


@pytest.mark.asyncio
async def test_import_never_brings_in_someone_on_a_do_not_contact_list(rig):
    state, fake, bridge = rig
    await state.add_suppression("email", "ann@initech.com", reason="opt-out", source="test")
    await state.add_suppression("domain", "globex.com", reason="complaint", source="test")
    await state.add_suppression("linkedin", "https://www.linkedin.com/in/zed", reason="opt-out", source="test",
                                channel="linkedin")
    rows = [_row(), _row(first_name="Bo", last_name="Ray", email="bo@globex.com", company="Globex", website=""),
            _row(first_name="Zed", last_name="Q", email="", linkedin_url="https://linkedin.com/in/zed/"),
            _row(first_name="Ok", last_name="Person", email="ok@fine.com", company="Fine", website="")]
    report = await _command(fake, bridge, "import_prospects", {"rows": rows, "source": "x"})
    assert report["p_result"] == "imported 1, skipped 0 duplicates, rejected 3 (on the do-not-contact list: 3)"
    assert await state.get_prospect_by_email("ann@initech.com") is None
    assert await state.get_prospect_by_email("ok@fine.com") is not None


@pytest.mark.asyncio
async def test_import_rejects_bad_rows_without_failing_the_command(rig):
    state, fake, bridge = rig
    rows = [_row(first_name="", last_name=""), _row(email="", linkedin_url=""),
            _row(email="not-an-email"), _row(email="", linkedin_url="https://example.com/in/x"),
            "garbage", None, _row(first_name="", last_name="Solo", email="solo@x.com", company="X", website="")]
    report = await _command(fake, bridge, "import_prospects", {"rows": rows, "source": ""})
    assert report["p_status"] == "done"
    assert report["p_result"] == ("imported 1, skipped 0 duplicates, rejected 6 "
                                  "(no name: 1, no email or LinkedIn profile: 2, invalid email: 1, not an object: 2)")
    assert (await state.get_prospect_by_email("solo@x.com")).source == "os-import:unlabelled"


@pytest.mark.asyncio
async def test_import_is_capped_at_500_rows(rig):
    state, fake, bridge = rig
    rows = [_row(first_name=f"P{i}", last_name="Test", email=f"p{i}@co{i}.com", company=f"Co{i}", website="")
            for i in range(503)]
    report = await _command(fake, bridge, "import_prospects", {"rows": rows, "source": "big"})
    assert report["p_result"] == "imported 500, skipped 0 duplicates, rejected 3 (over the 500-row cap: 3)"
    assert len(await _all_prospects(state)) == 500


@pytest.mark.asyncio
async def test_imported_prospects_are_not_deliverable_and_the_import_needs_a_row_list(rig):
    state, fake, bridge = rig
    await _command(fake, bridge, "import_prospects", {"rows": [_row()], "source": "x"})
    from harvey.gate import deliverable_statuses
    assert (await state.get_prospect_by_email("ann@initech.com")).email_status not in deliverable_statuses(bridge.config)
    report = await _command(fake, bridge, "import_prospects", {"source": "x"}, cid="k2")
    assert report["p_status"] == "failed" and "rows" in report["p_result"]


# ── snapshot: linkedin / sms / safety ──

@pytest.mark.asyncio
async def test_snapshot_linkedin_queue_lists_approved_items_newest_first_in_assist_mode(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    first = await _li_item(state, pid, kind="connect", body="")
    second = await _li_item(state, pid, kind="message", body="Thanks for connecting.")
    await _li_item(state, pid, status="pending_review")          # not approved: not listed
    await _li_item(state, pid, status="sent", kind="reply")      # already sent: not listed
    await state.update_outbox_item(second, body="Thanks for connecting.")   # bumps updated_at: newest
    snap = await bridge.build_snapshot()
    li = snap["linkedin"]
    assert (li["enabled"], li["mode"], li["tripped_until"], li["disabled"]) == (True, "assist", None, False)
    assert li["sent_today"] == {"connect": 0, "message": 0} and li["weekly_connections_sent"] == 0
    assert li["limits"] == {"connects_per_day": 7, "messages_per_day": 5, "connects_per_week": 30}
    assert [q["outbox_id"] for q in li["queue"]] == [second, first]
    q = li["queue"][0]
    assert set(q) == {"outbox_id", "prospect_id", "name", "company", "title", "profile_url", "kind", "body", "approved_at"}
    assert (q["name"], q["company"], q["title"], q["kind"]) == ("Jane Doe", "Acme", "VP", "message")
    assert q["profile_url"] == "https://www.linkedin.com/in/jane-doe" and q["approved_at"].endswith("Z")
    json.dumps(snap)   # everything the OS receives is plain JSON


@pytest.mark.asyncio
async def test_snapshot_linkedin_queue_is_empty_when_not_assist_and_limits_are_the_clamped_ones(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    await _li_item(state, pid)
    li_cfg = bridge.config.channels.linkedin
    li_cfg.mode = "auto"
    li_cfg.max_daily_connections = 99   # above the hard maximum
    li = (await bridge.build_snapshot())["linkedin"]
    assert li["mode"] == "auto" and li["queue"] == [] and li["limits"]["connects_per_day"] == 10
    li_cfg.mode = "off"
    assert (await bridge.build_snapshot())["linkedin"]["queue"] == []


@pytest.mark.asyncio
async def test_assist_mode_linkedin_channel_never_sends_by_itself(rig):
    from harvey.channels.drain import drain_channel
    from harvey.channels.linkedin import LinkedInChannel
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    item = await _li_item(state, pid)
    channel = LinkedInChannel(bridge.config, state, automation_factory=lambda: (_ for _ in ()).throw(AssertionError("browser")))
    assert channel.is_configured() is False
    await drain_channel(state, channel)
    assert (await state.get_outbox_item(item))["status"] == "approved"


@pytest.mark.asyncio
async def test_snapshot_sms_and_safety_meters(rig):
    state, fake, bridge = rig
    cfg = bridge.config.channels
    cfg.email.max_daily_sends = 10
    cfg.sms.enabled = True
    cfg.sms.max_daily_sms = 4
    for _ in range(8):
        await state.record_send(channel="email", prospect_id="p")
    for _ in range(4):
        await state.record_send(channel="sms", prospect_id="p")
    for _ in range(6):
        await state.record_send(channel="linkedin", prospect_id="p", kind="connect")
    await state.record_send(channel="linkedin", prospect_id="p", kind="message")
    snap = await bridge.build_snapshot()
    assert snap["sms"] == {"enabled": True, "daily_cap": 4, "sent_today": 4}
    by = {m["channel"]: m for m in snap["safety"]}
    assert [m["channel"] for m in snap["safety"]] == [
        "email", "linkedin_connect", "linkedin_message", "linkedin_week", "sms"]
    assert by["email"] == {"channel": "email", "label": "Email per day", "cap": 10, "used": 8,
                           "period": "day", "state": "near"}
    assert (by["linkedin_connect"]["cap"], by["linkedin_connect"]["used"], by["linkedin_connect"]["state"]) == (7, 6, "near")
    assert (by["linkedin_message"]["used"], by["linkedin_message"]["state"]) == (1, "ok")
    assert (by["linkedin_week"]["period"], by["linkedin_week"]["cap"], by["linkedin_week"]["state"]) == ("week", 30, "ok")
    assert by["sms"]["state"] == "full"
    json.dumps(snap)


@pytest.mark.asyncio
async def test_snapshot_safety_marks_linkedin_tripped_and_skips_disabled_channels(rig):
    from datetime import datetime, timedelta, timezone
    from harvey.channels.linkedin import DISABLED, TRIPPED_UNTIL
    state, fake, bridge = rig
    snap = await bridge.build_snapshot()
    assert snap["sms"]["enabled"] is False
    assert [m["channel"] for m in snap["safety"]] == ["email", "linkedin_connect", "linkedin_message", "linkedin_week"]
    until = (datetime.now(timezone.utc) + timedelta(hours=5)).replace(tzinfo=None).isoformat()
    await state.set_setting(TRIPPED_UNTIL, until)
    snap = await bridge.build_snapshot()
    assert snap["linkedin"]["tripped_until"] == until[:19] + "Z"
    states = {m["channel"]: m["state"] for m in snap["safety"]}
    assert states["email"] == "ok" and states["linkedin_connect"] == states["linkedin_week"] == "tripped"
    await state.set_setting(TRIPPED_UNTIL, "")
    await state.set_setting(DISABLED, "2 trips")
    snap = await bridge.build_snapshot()
    assert snap["linkedin"]["disabled"] is True and snap["linkedin"]["tripped_until"] is None
    assert {m["state"] for m in snap["safety"] if m["channel"].startswith("linkedin")} == {"tripped"}
    bridge.config.channels.linkedin.mode = "off"
    assert [m["channel"] for m in (await bridge.build_snapshot())["safety"]] == ["email"]


@pytest.mark.asyncio
async def test_a_linkedin_send_is_reported_as_a_sent_event_with_channel_and_kind_and_email_too(rig):
    state, fake, bridge = rig
    await state.record_send(channel="email", prospect_id="p1", ref="o1")
    await state.record_send(channel="linkedin", prospect_id="p1", ref="o2", kind="message")
    events = {e["data"]["channel"]: e for e in await bridge._new_events() if e["kind"] == "sent"}
    assert events["email"]["data"] == {"channel": "email", "kind": ""}
    assert events["linkedin"]["data"] == {"channel": "linkedin", "kind": "message"}


# ── approval card wording ──

@pytest.mark.asyncio
async def test_assist_mode_card_tells_the_owner_to_send_it_personally_and_never_says_harvey_sends(rig):
    state, fake, bridge = rig
    pid = await seed_prospect(state, status="queued")
    await _li_item(state, pid, status="pending_review", body="Hi Jane, let's connect.")
    await bridge.step_mirror()
    desc = fake.queue[0]["description"]
    assert desc == (
        "Harvey drafted a LinkedIn connection request to Jane Doe, VP at Acme "
        "(https://www.linkedin.com/in/jane-doe). Text: Hi Jane, let's connect. "
        "Approve it, then send it yourself from LinkedIn and press 'Mark sent' on the LinkedIn tab of the "
        "Harvey page in Orion OS. In assist mode Harvey never sends anything on LinkedIn.")
    assert "Harvey sends it" not in desc
    # the wording never touches the content hash: nothing is re-raised on the next pass
    await bridge.step_mirror()
    assert len(fake.queue) == 1


# ── imported addresses get verified, a few per cycle ──

async def _import(fake, bridge, rows, cid="imp"):
    return await _command(fake, bridge, "import_prospects", {"rows": rows, "source": "t"}, cid=cid)


def _person(first, email):
    return {"first_name": first, "last_name": "Test", "email": email, "company": "Acme"}


@pytest.mark.asyncio
async def test_imported_addresses_are_verified_and_only_verified_ones_become_deliverable(rig):
    state, fake, bridge = rig
    await _import(fake, bridge, [_person("Ok", "ok@acme.com"), _person("Bad", "bad@acme.com"), _person("Risky", "risky@acme.com")])
    verdicts = {"ok@acme.com": "verified", "bad@acme.com": "invalid", "risky@acme.com": "risky"}

    async def fake_verify(email, env):
        return verdicts[email]
    bridge._verify_email = fake_verify
    assert await bridge.step_verify() == 3
    by_email = {p.email: p for p in await state.get_prospects_by_status("new")}
    assert {e: p.email_status for e, p in by_email.items()} == verdicts
    assert by_email["ok@acme.com"].email_verified is True
    assert by_email["bad@acme.com"].email_verified is False
    assert await bridge.step_verify() == 0     # nothing left to do


@pytest.mark.asyncio
async def test_an_inconclusive_check_is_retried_then_held_as_a_guess(rig):
    state, fake, bridge = rig
    await _import(fake, bridge, [_person("Maybe", "maybe@acme.com")])
    calls = []

    async def unsure(email, env):
        calls.append(email)
        return "guess"
    bridge._verify_email = unsure
    assert await bridge.step_verify() == 0 and await bridge.step_verify() == 0
    assert (await state.get_prospect_by_email("maybe@acme.com")).email_status == ""   # still unresolved, will retry
    assert await bridge.step_verify() == 1
    assert (await state.get_prospect_by_email("maybe@acme.com")).email_status == "guess"
    assert len(calls) == 3
    assert await bridge.step_verify() == 0


@pytest.mark.asyncio
async def test_a_verifier_that_raises_or_hangs_never_breaks_the_cycle_and_other_prospects_are_untouched(rig):
    state, fake, bridge = rig
    other = await seed_prospect(state, status="new")          # not imported: must never be touched
    await _import(fake, bridge, [_person("Boom", "boom@acme.com")])
    bridge.VERIFY_TIMEOUT = 0.05

    async def hang(email, env):
        import asyncio
        await asyncio.sleep(5)
    bridge._verify_email = hang
    assert await bridge.step_verify() == 0
    assert (await state.get_prospect(other)).email_status != "guess"


@pytest.mark.asyncio
async def test_each_cycle_verifies_at_most_a_small_batch(rig):
    state, fake, bridge = rig
    await _import(fake, bridge, [_person(f"P{i}", f"p{i}@acme.com") for i in range(12)])

    async def ok(email, env):
        return "verified"
    bridge._verify_email = ok
    assert await bridge.step_verify() == bridge.VERIFY_BATCH
    assert await bridge.step_verify() == 12 - bridge.VERIFY_BATCH
