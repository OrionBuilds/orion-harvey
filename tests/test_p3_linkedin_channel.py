import asyncio
"""P3 (part 2): LinkedInChannel rails, LinkedInAgent, Handler, assist endpoints."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import ValidationError

import harvey.dashboard as dash
import harvey.integrations.linkedin as li
from harvey import linkedin_ops
from harvey.agents.linkedin_agent import LinkedInAgent
from harvey.channels.drain import drain_channel
from harvey.channels.linkedin import (
    DISABLED, INBOX_CHECKED_AT, TRIPPED_UNTIL, UNAVAILABLE_STREAK, LinkedInChannel)
from harvey.config import LinkedInChannelConfig
from harvey.models.prospect import Prospect
from harvey.state import StateManager

WED_11AM_ET = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
SAT_11AM_ET = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
NOW = {"t": WED_11AM_ET}


def clock():
    return NOW["t"]


@pytest.fixture(autouse=True)
def reset(monkeypatch, tmp_path):
    NOW["t"] = WED_11AM_ET
    monkeypatch.setenv("HARVEY_STOP_FILE", str(tmp_path / "STOP-not-present"))


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as d:
        sm = StateManager(os.path.join(d, "t.db"))
        await sm.init_db()
        yield sm


def cfg(**li_overrides):
    linkedin = LinkedInChannelConfig(
        enabled=True, mode="auto", require_approval=False, warmup_start_date="2026-09-01")
    for k, v in li_overrides.items():
        setattr(linkedin, k, v)
    return SimpleNamespace(
        persona=SimpleNamespace(name="Jordan Example", email="jordan@go.example.com",
                                role="Fractional CTO", company="SR", tone="direct"),
        channels=SimpleNamespace(
            linkedin=linkedin,
            email=SimpleNamespace(enabled=True, provider="smtp", max_daily_sends=50,
                                  send_to_risky=False, require_approval=False,
                                  max_bounce_rate=0.05)),
        product=SimpleNamespace(name="P", description="d", pricing="$", key_benefits=[],
                                objection_responses={}),
    )


class FakeAuto:
    """Stands in for LinkedInAutomation. Set `fail` to an exception to raise it."""

    def __init__(self):
        self.calls, self.stopped, self.fail = [], False, None
        self.connect_result, self.message_result = "sent", "sent"
        self.check_results, self.inbox = {}, []

    async def ensure_session(self):
        self.calls.append("ensure")
        if isinstance(self.fail, li.LinkedInSessionExpired):
            raise self.fail

    async def send_connection_request(self, url, note=""):
        self.calls.append(("connect", url, note))
        if self.fail:
            raise self.fail
        return self.connect_result

    async def send_message(self, target, text):
        self.calls.append(("message", target, text))
        if self.fail:
            raise self.fail
        return self.message_result

    async def check_connection(self, url):
        self.calls.append(("check", url))
        if isinstance(self.fail, Exception):
            raise self.fail
        return self.check_results.get(url, "pending")

    async def read_inbox(self):
        self.calls.append("inbox")
        if self.fail:
            raise self.fail
        return self.inbox

    async def stop(self):
        self.stopped = True


def channel(state, auto=None, config=None):
    auto = auto or FakeAuto()
    return LinkedInChannel(config or cfg(), state, automation_factory=lambda: auto, now_fn=clock), auto


async def make_prospect(state, n=1, score=50, status="new"):
    return await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="CEO", company=f"Co{n}",
        email=f"p{n}@co{n}.com", email_status="verified", status=status, score=score,
        linkedin_url=f"https://www.linkedin.com/in/p{n}"))


async def queue(state, pid, kind="connect", body="", status="approved", thread_ref=""):
    prospect = await state.get_prospect(pid)
    return await state.add_outbox_item(
        prospect_id=pid, to_email="", channel="linkedin", kind=kind, to_ref=prospect.linkedin_url,
        subject="", body=body, send_at="2000-01-01T00:00:00", status=status,
        provider="linkedin", thread_ref=thread_ref)


async def mark_inbox_read(state):
    await state.set_setting(INBOX_CHECKED_AT, WED_11AM_ET.replace(tzinfo=None).isoformat())


# ── configuration ──

def test_config_defaults_and_validation():
    c = LinkedInChannelConfig()
    assert (c.mode, c.max_daily_connections, c.max_daily_messages, c.max_weekly_connections) == ("assist", 7, 5, 30)
    for bad in ({"mode": "turbo"}, {"warmup_start_date": "tomorrow"},
                {"timezone": "Mars/Base"}, {"max_daily_connections": -1}):
        with pytest.raises(ValidationError):
            LinkedInChannelConfig(**bad)
    assert LinkedInChannelConfig(mode=" AUTO ").mode == "auto"


def test_only_auto_mode_with_a_session_is_configured(monkeypatch, tmp_path):
    monkeypatch.setattr("harvey.channels.linkedin.COOKIES_PATH", tmp_path / "cookies.json")
    for mode in ("off", "assist"):
        assert not LinkedInChannel(cfg(mode=mode), None, automation_factory=object).is_configured()
    assert not LinkedInChannel(cfg(), None).is_configured()                  # no saved session
    (tmp_path / "cookies.json").write_text("[]")
    assert LinkedInChannel(cfg(), None).is_configured()
    assert not LinkedInChannel(cfg(enabled=False), None, automation_factory=object).is_configured()


def test_config_cannot_raise_limits_past_the_hard_maximums():
    c = LinkedInChannel(cfg(max_daily_connections=500, max_daily_messages=500), None,
                        automation_factory=object)
    assert c.limits().connects_per_day == 10 and c.limits().messages_per_day == 8


# ── content ──

@pytest.mark.asyncio
async def test_content_gate(state):
    ch, _ = channel(state)
    prospect = SimpleNamespace(id="p")
    ok = {"kind": "connect", "to_ref": "https://www.linkedin.com/in/jane", "thread_ref": ""}
    assert ch.content_gate(ok, "", "", prospect).ok                                 # no note is fine
    assert not ch.content_gate(ok, "", "x" * 301, prospect).ok
    assert not ch.content_gate(ok, "", "see https://x.com", prospect).ok            # no links in a note
    msg = {**ok, "kind": "message"}
    assert ch.content_gate(msg, "", "Thanks for connecting. How do you hire?", prospect).ok
    assert not ch.content_gate(msg, "", "", prospect).ok
    assert not ch.content_gate(msg, "", "Hi {{first_name}}", prospect).ok
    assert not ch.content_gate(msg, "", "This is a game-changer for you", prospect).ok
    assert not ch.content_gate({**ok, "to_ref": "jane@acme.com"}, "", "hi", prospect).ok
    thread = {**msg, "to_ref": "", "thread_ref": "https://www.linkedin.com/messaging/thread/2-abc/"}
    assert ch.content_gate(thread, "", "Sure thing.", prospect).ok                 # replies target the thread


# ── pre-send rules ──

@pytest.mark.asyncio
async def test_connect_is_deferred_outside_the_window_and_over_the_cap(state):
    pid = await make_prospect(state)
    prospect = await state.get_prospect(pid)
    item = {"kind": "connect"}
    ch, _ = channel(state)
    assert (await ch.pre_send(state, item, prospect)).ok

    NOW["t"] = SAT_11AM_ET
    held = await ch.pre_send(state, item, prospect)
    assert not held.ok and held.defer and "window" in held.reasons[0]

    NOW["t"] = WED_11AM_ET
    for _ in range(7):
        await state.record_send(channel="linkedin", prospect_id="x", kind="connect")
    capped = await ch.pre_send(state, item, prospect)
    assert not capped.ok and capped.defer and "cap" in capped.reasons[0]


@pytest.mark.asyncio
async def test_first_ever_send_starts_the_warmup_clock(state):
    pid = await make_prospect(state)
    prospect = await state.get_prospect(pid)
    ch, _ = channel(state, config=cfg(warmup_start_date=""))
    await state.record_send(channel="linkedin", prospect_id="x", kind="connect")
    await state.record_send(channel="linkedin", prospect_id="x", kind="connect")
    held = await ch.pre_send(state, {"kind": "connect"}, prospect)         # day 1 allows 2
    assert not held.ok and "warm-up day 1" in held.reasons[0]
    assert await state.get_setting("linkedin_warmup_start") == "2026-10-21"


@pytest.mark.asyncio
async def test_messages_need_a_connection_a_fresh_inbox_and_few_prior_messages(state):
    pid = await make_prospect(state)
    prospect = await state.get_prospect(pid)
    ch, _ = channel(state)
    msg = {"kind": "message"}

    blocked = await ch.pre_send(state, msg, prospect)                      # never connected
    assert not blocked.ok and not blocked.defer and "cannot message" in blocked.reasons[0]

    await state.set_linkedin_thread(pid, state="connected")
    stale = await ch.pre_send(state, msg, prospect)                        # inbox never read
    assert not stale.ok and stale.defer and "inbox" in stale.reasons[0]

    await mark_inbox_read(state)
    assert (await ch.pre_send(state, msg, prospect)).ok

    await state.set_linkedin_thread(pid, state="messaged", messages_sent=2)
    done = await ch.pre_send(state, msg, prospect)
    assert not done.ok and not done.defer and "unanswered" in done.reasons[0]

    await state.set_linkedin_thread(pid, state="connected", messages_sent=0)
    NOW["t"] = WED_11AM_ET + timedelta(hours=5)                            # 16:00 ET: in window, inbox >4h old
    old = await ch.pre_send(state, msg, prospect)
    assert not old.ok and old.defer and "inbox" in old.reasons[0]


@pytest.mark.asyncio
async def test_a_second_connect_to_the_same_person_is_refused(state):
    pid = await make_prospect(state)
    await state.set_linkedin_thread(pid, state="requested")
    ch, _ = channel(state)
    res = await ch.pre_send(state, {"kind": "connect"}, await state.get_prospect(pid))
    assert not res.ok and not res.defer


# ── sending, bookkeeping ──

@pytest.mark.asyncio
async def test_connect_then_message_updates_relationship_and_ledger(state):
    pid = await make_prospect(state)
    ch, auto = channel(state)
    await queue(state, pid, "connect", body="")
    assert await drain_channel(state, ch, pacing_enabled=False) == 1
    assert auto.calls[1] == ("connect", "https://www.linkedin.com/in/p1", "")
    assert (await state.get_linkedin_thread(pid))["state"] == "requested"
    assert await state.count_kind_since("linkedin", ("connect",), today_only=True) == 1
    assert auto.stopped, "drain must release the browser"

    await linkedin_ops.mark_accepted(state, pid)
    await mark_inbox_read(state)
    ch2, auto2 = channel(state)
    await queue(state, pid, "message", body="Thanks for connecting. What are you hiring for?")
    assert await drain_channel(state, ch2, pacing_enabled=False) == 1
    t = await state.get_linkedin_thread(pid)
    assert t["state"] == "messaged" and t["messages_sent"] == 1


@pytest.mark.asyncio
async def test_already_connected_invite_is_recorded_as_connected(state):
    pid = await make_prospect(state)
    ch, auto = channel(state)
    auto.connect_result = "already_connected"
    await queue(state, pid, "connect")
    await drain_channel(state, ch, pacing_enabled=False)
    assert (await state.get_linkedin_thread(pid))["state"] == "connected"


@pytest.mark.asyncio
async def test_drain_respects_the_per_kind_caps_and_leaves_extras_queued(state):
    ch, auto = channel(state, config=cfg(max_daily_connections=2, warmup_start_date="2026-09-01"))
    ids = []
    for n in range(1, 5):
        pid = await make_prospect(state, n)
        ids.append(await queue(state, pid, "connect"))
    assert await drain_channel(state, ch, pacing_enabled=False) == 2
    ch2, _ = channel(state, config=cfg(max_daily_connections=2))
    assert await drain_channel(state, ch2, pacing_enabled=False) == 0
    statuses = sorted([(await state.get_outbox_item(i))["status"] for i in ids])
    assert statuses == ["approved", "approved", "sent", "sent"]               # held, never failed


@pytest.mark.asyncio
async def test_a_reply_to_someone_who_wrote_first_goes_to_their_thread(state):
    pid = await make_prospect(state, status="replied")
    await state.set_linkedin_thread(pid, state="replied")
    await mark_inbox_read(state)
    thread = "https://www.linkedin.com/messaging/thread/2-abc/"
    await queue(state, pid, "reply", body="Happy to explain. Which part matters most?", thread_ref=thread)
    ch, auto = channel(state)
    assert await drain_channel(state, ch, pacing_enabled=False) == 1
    assert auto.calls[1][:2] == ("message", thread)


@pytest.mark.asyncio
async def test_stop_on_reply_cancels_queued_linkedin_touches(state):
    pid = await make_prospect(state, status="replied")           # replied by email
    oid = await queue(state, pid, "connect")
    ch, auto = channel(state)
    assert await drain_channel(state, ch, pacing_enabled=False) == 0
    assert (await state.get_outbox_item(oid))["status"] == "cancelled"
    assert "connect" not in [c[0] for c in auto.calls if isinstance(c, tuple)]


@pytest.mark.asyncio
async def test_suppressed_people_are_never_contacted(state):
    pid = await make_prospect(state)
    oid = await queue(state, pid, "connect")
    await state.add_suppression("email", "p1@co1.com", reason="unsubscribe")
    ch, auto = channel(state)
    assert await drain_channel(state, ch, pacing_enabled=False) == 0
    assert (await state.get_outbox_item(oid))["status"] == "cancelled"


# ── breaker ──

@pytest.mark.asyncio
async def test_a_trip_locks_linkedin_for_72_hours_without_failing_the_item(state):
    pid = await make_prospect(state)
    oid = await queue(state, pid, "connect")
    ch, auto = channel(state)
    auto.fail = li.LinkedInTripped("LinkedIn challenge/checkpoint page")
    assert await drain_channel(state, ch, pacing_enabled=False) == 0

    assert (await state.get_outbox_item(oid))["status"] == "approved"          # kept, not failed
    until = datetime.fromisoformat(await state.get_setting(TRIPPED_UNTIL))
    assert 71 <= (until - WED_11AM_ET.replace(tzinfo=None)).total_seconds() / 3600 <= 73
    assert "cooling off" in await ch.breaker_reason(state)
    assert await state.get_setting("linkedin_paused") == ""                   # lifts by itself

    again, auto2 = channel(state)                                              # next heartbeat
    assert await drain_channel(state, again, pacing_enabled=False) == 0
    assert auto2.calls == []                                                   # no browser at all

    NOW["t"] = WED_11AM_ET + timedelta(hours=73)
    assert await again.breaker_reason(state) == ""


@pytest.mark.asyncio
async def test_a_second_trip_within_30_days_disables_until_a_human_returns(state):
    ch, _ = channel(state)
    await ch._trip("first")
    assert not await state.get_setting(DISABLED)
    NOW["t"] = WED_11AM_ET + timedelta(days=10)
    await ch._trip("second")
    assert "2 trips" in await state.get_setting(DISABLED)
    NOW["t"] = WED_11AM_ET + timedelta(days=60)                                # lock expiry doesn't clear it
    assert "disabled" in (await ch.breaker_reason(state)).lower()


@pytest.mark.asyncio
async def test_trips_older_than_30_days_do_not_count(state):
    ch, _ = channel(state)
    await ch._trip("old")
    NOW["t"] = WED_11AM_ET + timedelta(days=31)
    await ch._trip("new")
    assert not await state.get_setting(DISABLED)


@pytest.mark.asyncio
async def test_expired_session_pauses_with_a_login_hint_and_keeps_the_item(state):
    pid = await make_prospect(state)
    oid = await queue(state, pid, "connect")
    ch, auto = channel(state)
    auto.fail = li.LinkedInSessionExpired("LinkedIn session expired (redirected to login)")
    assert await drain_channel(state, ch, pacing_enabled=False) == 0
    assert "harvey linkedin login" in await state.get_setting("linkedin_paused")
    assert (await state.get_outbox_item(oid))["status"] == "approved"
    assert not await state.get_setting(TRIPPED_UNTIL)                          # not a trip


@pytest.mark.asyncio
async def test_three_unactionable_profiles_in_a_row_look_like_ui_drift(state):
    ch, auto = channel(state)
    auto.fail = li.LinkedInUnavailable("could not tell this profile's connection state")
    for n in range(1, 4):
        pid = await make_prospect(state, n)
        await queue(state, pid, "connect")
    # Each is failed individually (they are genuinely unactionable), then the streak trips.
    assert await drain_channel(state, ch, pacing_enabled=False) == 0
    assert await state.get_setting(TRIPPED_UNTIL)
    assert await state.get_setting(UNAVAILABLE_STREAK) == "0"


@pytest.mark.asyncio
async def test_a_success_resets_the_unavailable_streak(state):
    ch, auto = channel(state)
    await state.set_setting(UNAVAILABLE_STREAK, "2")
    pid = await make_prospect(state)
    await queue(state, pid, "connect")
    await drain_channel(state, ch, pacing_enabled=False)
    assert await state.get_setting(UNAVAILABLE_STREAK) == "0"


# ── inbox ──

@pytest.mark.asyncio
async def test_inbox_poll_is_rate_limited_windowed_and_marks_freshness(state):
    ch, auto = channel(state)
    auto.inbox = [{"provider_id": "li:t1:abc", "profile_url": "https://www.linkedin.com/in/p1",
                   "text": "What do you do?", "thread_url": "https://www.linkedin.com/messaging/thread/2-t1/"}]
    msgs = await ch.poll_inbound()
    assert len(msgs) == 1 and msgs[0].channel == "linkedin"
    assert msgs[0].from_ref.endswith("/in/p1") and msgs[0].thread_ref.endswith("2-t1/")
    assert await state.get_setting(INBOX_CHECKED_AT)

    assert await ch.poll_inbound() == []                                       # polled <90 min ago
    assert auto.calls.count("inbox") == 1

    NOW["t"] = SAT_11AM_ET
    other, auto2 = channel(state)
    assert await other.poll_inbound() == [] and auto2.calls == []              # outside the window


@pytest.mark.asyncio
async def test_inbox_trouble_pauses_linkedin_but_never_raises_into_email(state):
    ch, auto = channel(state)
    auto.fail = li.LinkedInSessionExpired("expired")
    assert await ch.poll_inbound() == []
    assert await state.get_setting("linkedin_paused")

    await state.set_setting("linkedin_paused", "")
    other, auto2 = channel(state)
    auto2.fail = li.LinkedInTripped("challenge")
    assert await other.poll_inbound() == []
    assert await state.get_setting(TRIPPED_UNTIL)


@pytest.mark.asyncio
async def test_unexpected_inbox_errors_pause_linkedin_not_email(state):
    """A crashed or hung inbox read backs off 30 min and returns nothing. It must not raise (that
    would hold email hostage); LinkedIn sends still wait because the inbox is stale."""
    ch, auto = channel(state)
    auto.fail = RuntimeError("browser crashed")
    assert await ch.poll_inbound() == []
    assert await state.get_setting("linkedin_inbox_retry_at")
    auto.fail = None
    assert await ch.poll_inbound() == []          # inside the back-off: no second browser
    assert not await state.get_setting("linkedin_inbox_checked_at")


@pytest.mark.asyncio
async def test_hung_inbox_read_times_out_and_closes_browser(state, monkeypatch):
    import harvey.channels.linkedin as li
    monkeypatch.setattr(li, "INBOX_READ_TIMEOUT_SECONDS", 0.05)
    ch, auto = channel(state)

    async def hang():
        await asyncio.sleep(30)
    auto.read_inbox = hang
    assert await ch.poll_inbound() == []
    assert ch._auto is None                       # dropped, not left open


# ── agent ──

class StubBrain:
    def __init__(self, reply="Thanks for connecting. How are you handling hiring right now?"):
        self.reply, self.prompts = reply, []

    def load_skills_for_agent(self, *_):
        return ""

    async def think(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def agent(state, config=None, brain=None, auto=None):
    a = LinkedInAgent(brain or StubBrain(), state, config or cfg(), None,
                      automation_factory=lambda: auto or FakeAuto(), now_fn=clock)
    a.send_pacing = False
    return a


@pytest.mark.asyncio
async def test_agent_stages_connects_best_first_up_to_the_backlog(state):
    ids = [await make_prospect(state, n, score=n) for n in range(1, 21)]
    a = agent(state, config=cfg(mode="assist", require_approval=True))
    assert await a._stage_connects() == 14                                    # 2 days of the 7/day cap
    assert await a._stage_connects() == 0                                     # backlog full
    rows = await state.get_outbox(status="pending_review", channel="linkedin", limit=100)
    assert len(rows) == 14 and all(r["kind"] == "connect" and r["body"] == "" for r in rows)
    staged_ids = {r["prospect_id"] for r in rows}
    assert staged_ids == set(ids[-14:])                                       # highest scores
    assert (await state.get_linkedin_thread(ids[-1]))["state"] == "staged"


@pytest.mark.asyncio
async def test_agent_skips_suppressed_prospects_and_respects_approval_mode(state):
    a_id = await make_prospect(state, 1, score=90)
    b_id = await make_prospect(state, 2, score=80)
    await state.add_suppression("linkedin", "https://www.linkedin.com/in/p1", reason="manual")
    a = agent(state, config=cfg(require_approval=False))
    await a._stage_connects()
    rows = await state.get_outbox(channel="linkedin")
    assert [r["prospect_id"] for r in rows] == [b_id] and rows[0]["status"] == "approved"
    assert a_id


@pytest.mark.asyncio
async def test_assist_mode_stages_and_drafts_but_never_opens_a_browser(state):
    pid = await make_prospect(state)
    auto = FakeAuto()
    a = agent(state, config=cfg(mode="assist", require_approval=False), auto=auto)
    await a.run()
    assert auto.calls == []
    assert len(await state.get_outbox(channel="linkedin")) == 1


@pytest.mark.asyncio
async def test_off_mode_does_nothing(state):
    await make_prospect(state)
    a = agent(state, config=cfg(mode="off"))
    assert not a.active
    await a.run()
    assert await state.get_outbox(channel="linkedin") == []


@pytest.mark.asyncio
async def test_acceptance_checks_move_threads_along(state, monkeypatch):
    now = WED_11AM_ET.replace(tzinfo=None)
    old = (now - timedelta(days=2)).isoformat()
    people = {}
    for n, outcome in enumerate(("connected", "pending", "none"), start=1):
        pid = await make_prospect(state, n)
        await state.set_linkedin_thread(pid, state="requested", requested_at=old)
        people[outcome] = pid
    fresh = await make_prospect(state, 9)
    await state.set_linkedin_thread(fresh, state="requested", requested_at=now.isoformat())

    auto = FakeAuto()
    auto.check_results = {
        "https://www.linkedin.com/in/p1": "connected",
        "https://www.linkedin.com/in/p2": "pending",
        "https://www.linkedin.com/in/p3": "none",
    }
    a = agent(state, auto=auto)
    ch = a._channel()
    # Make the agent's own clock match the test window (reverted after the test).
    import harvey.agents.linkedin_agent as mod
    monkeypatch.setattr(mod, "_now", lambda: now)
    await a._check_acceptances(ch)

    states = {k: (await state.get_linkedin_thread(v))["state"] for k, v in people.items()}
    assert states == {"connected": "connected", "pending": "requested", "none": "declined"}
    assert (await state.get_linkedin_thread(fresh))["state"] == "requested"     # too soon to check
    assert ("check", "https://www.linkedin.com/in/p9") not in auto.calls
    assert await state.count_kind_since("linkedin_view", ("view",), today_only=True) == 3
    assert await state.count_sent_today("linkedin") == 0                        # views aren't sends


@pytest.mark.asyncio
async def test_first_message_is_drafted_once_after_acceptance(state):
    pid = await make_prospect(state)
    await state.set_linkedin_thread(pid, state="connected", accepted_at=WED_11AM_ET.replace(tzinfo=None).isoformat())
    brain = StubBrain()
    a = agent(state, config=cfg(require_approval=True), brain=brain)
    assert await a._stage_first_messages() == 1
    assert await a._stage_first_messages() == 0                                # state moved on
    row = (await state.get_outbox(channel="linkedin"))[0]
    assert row["kind"] == "message" and row["status"] == "pending_review"
    assert row["body"].startswith("Thanks for connecting")
    assert (await state.get_linkedin_thread(pid))["state"] == "message_staged"
    assert "never invent" in brain.prompts[0].lower() or "Never invent" in brain.prompts[0]
    sent_at = datetime.fromisoformat(row["send_at"])
    assert sent_at >= WED_11AM_ET.replace(tzinfo=None) + timedelta(hours=23)    # not instantly after accepting


@pytest.mark.asyncio
async def test_bad_drafts_are_rejected_and_retries_are_capped(state):
    pid = await make_prospect(state)
    await state.set_linkedin_thread(pid, state="connected")
    brain = StubBrain(reply="This game-changer will unlock your growth: https://x.com")
    a = agent(state, brain=brain)
    for _ in range(5):
        assert await a._stage_first_messages() == 0
    assert await state.get_outbox(channel="linkedin") == []
    assert len(brain.prompts) == 3                                              # stopped spending after 3


@pytest.mark.asyncio
async def test_no_first_message_for_people_who_already_replied(state):
    pid = await make_prospect(state, status="replied")
    await state.set_linkedin_thread(pid, state="connected")
    brain = StubBrain()
    assert await agent(state, brain=brain)._stage_first_messages() == 0
    assert brain.prompts == []


@pytest.mark.asyncio
async def test_full_cycle_in_auto_mode(state, monkeypatch):
    import harvey.agents.linkedin_agent as mod
    monkeypatch.setattr(mod, "_now", lambda: datetime(2000, 1, 1))     # staged rows are already due
    pid = await make_prospect(state)
    auto = FakeAuto()
    a = agent(state, auto=auto)
    await a.run()
    assert [c[0] for c in auto.calls if isinstance(c, tuple)] == ["connect"]
    assert (await state.get_linkedin_thread(pid))["state"] == "requested"
    assert auto.stopped


@pytest.mark.asyncio
async def test_agent_does_nothing_with_the_browser_when_stopped_or_paused(state, monkeypatch, tmp_path):
    await make_prospect(state)
    auto = FakeAuto()
    await state.set_setting("linkedin_paused", "session expired")
    await agent(state, auto=auto).run()
    assert auto.calls == []
    await state.set_setting("linkedin_paused", "")
    stop = tmp_path / "STOP"
    stop.write_text("")
    monkeypatch.setenv("HARVEY_STOP_FILE", str(stop))
    await agent(state, auto=auto).run()
    assert auto.calls == []


# ── Handler ──

@pytest.mark.asyncio
async def test_handler_reads_linkedin_and_answers_on_linkedin(state):
    from harvey.agents.handler import Handler
    from harvey.integrations.mail_provider import MailProvider, SendResult

    class Mail(MailProvider):
        name = "fake"
        def is_configured(self): return True
        async def send_email(self, *a, **k): return SendResult(ok=True)
        async def get_replies(self, limit=50): return []
        async def test_connection(self): return True, ""

    pid = await make_prospect(state)
    await state.set_linkedin_thread(pid, state="messaged", messages_sent=1)
    queued_email = await state.add_outbox_item(
        prospect_id=pid, to_email="p1@co1.com", subject="s", body="b", send_at="x",
        status="approved", kind="sequence", campaign_id="c1", step=2)

    class Brain:
        def load_skills_for_agent(self, *_): return ""
        def load_prompt(self, *_a, **_k): return ""
        async def think(self, prompt, session_id=None, agent="", task=""):
            return "question" if task == "classify_intent" else "Happy to explain. What are you working on?"

    config = cfg()
    config.channels.email.provider = "smtp"
    h = Handler(brain=Brain(), state=state, config=config, env=SimpleNamespace(instantly_api_key=""))
    h.provider = Mail()

    auto = FakeAuto()
    auto.inbox = [{"provider_id": "li:t:1", "profile_url": "https://www.linkedin.com/in/p1/",
                   "text": "Interesting. What do you actually do?",
                   "thread_url": "https://www.linkedin.com/messaging/thread/2-t/"}]
    from harvey.channels.email import EmailChannel
    h._channels = lambda: [EmailChannel(config, h.provider),
                           LinkedInChannel(config, state, automation_factory=lambda: auto, now_fn=clock)]
    assert await h._run_native() is True

    assert (await state.get_prospect(pid)).status == "replied"
    assert (await state.get_linkedin_thread(pid))["state"] == "replied"
    assert (await state.get_outbox_item(queued_email))["status"] == "cancelled"       # cross-channel stop
    reply = [r for r in await state.get_outbox(channel="linkedin", status="approved")][0]
    assert reply["kind"] == "reply" and reply["to_email"] == ""
    assert reply["thread_ref"].endswith("2-t/") and reply["to_ref"].endswith("/in/p1")
    assert [c.channel for c in await state.get_conversations_by_status("open")] == ["linkedin"]
    assert await state.get_outbox(channel="email", status="approved") == []


@pytest.mark.asyncio
async def test_linkedin_opt_out_phrases_suppress_the_person_everywhere(state):
    from harvey.agents.handler import Handler
    pid = await make_prospect(state)

    class Brain:
        def load_skills_for_agent(self, *_): return ""
        def load_prompt(self, *_a, **_k): return ""
        async def think(self, *a, **k): return "question"

    h = Handler(brain=Brain(), state=state, config=cfg(), env=SimpleNamespace(instantly_api_key=""))
    await h._process_reply("https://www.linkedin.com/in/p1", "Please stop messaging me.", reply_uuid="",
                           channel="linkedin")
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert await state.is_suppressed("email", "p1@co1.com", channel="email")
    assert await state.is_suppressed("linkedin", "https://www.linkedin.com/in/p1", channel="linkedin")


# ── assist mode: dashboard ──

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    return TestClient(dash.app, base_url="http://127.0.0.1:5555")


@pytest.mark.asyncio
async def test_marking_an_item_sent_books_it_like_an_automated_send(client, tmp_path):
    sm = StateManager(str(tmp_path / "harvey.db"))
    await sm.init_db()
    pid = await make_prospect(sm)
    oid = await queue(sm, pid, "connect")
    pending = await queue(sm, await make_prospect(sm, 2), "connect", status="pending_review")

    assert client.post(f"/api/outbox/{pending}/mark-sent").status_code == 400      # approve it first
    assert client.post("/api/outbox/nope/mark-sent").status_code == 400
    assert client.post(f"/api/outbox/{oid}/mark-sent").json() == {"success": True}

    assert (await sm.get_outbox_item(oid))["status"] == "sent"
    assert (await sm.get_linkedin_thread(pid))["state"] == "requested"
    assert await sm.count_kind_since("linkedin", ("connect",), today_only=True) == 1
    assert (await sm.get_prospect(pid)).status == "contacted"
    assert client.post(f"/api/outbox/{oid}/mark-sent").status_code == 400          # not twice


@pytest.mark.asyncio
async def test_marking_accepted_unlocks_the_first_message(client, tmp_path):
    sm = StateManager(str(tmp_path / "harvey.db"))
    await sm.init_db()
    pid = await make_prospect(sm)
    await sm.set_linkedin_thread(pid, state="requested")
    assert client.post(f"/api/linkedin/{pid}/accepted").json() == {"success": True}
    assert (await sm.get_linkedin_thread(pid))["state"] == "connected"
    assert client.post("/api/linkedin/ghost/accepted").status_code == 404


@pytest.mark.asyncio
async def test_our_own_browser_closing_is_not_a_trip(state):
    ch, _ = channel(state)
    await ch._trip("inbox read failed: Locator.evaluate_all: Target page, context or browser has been closed")
    assert not await state.get_setting(TRIPPED_UNTIL)
    assert not await state.get_setting(DISABLED)
    assert await ch.breaker_reason(state) == ""
    await ch._trip("checkpoint page shown")                    # a real LinkedIn challenge still trips
    assert await state.get_setting(TRIPPED_UNTIL)
