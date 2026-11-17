"""P3 (part 1): LinkedIn safety policy, page classification, state, and the
browser flows driven through a scripted UI (no real browser, no LinkedIn)."""

import os
import random
import tempfile
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

import harvey.integrations.linkedin as li
from harvey.integrations import linkedin_policy as pol
from harvey.integrations import linkedin_selectors as sel
from harvey.models.prospect import Prospect
from harvey.state import StateManager

WED_11AM_ET = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)   # Wednesday, 11:00 EDT
SAT_11AM_ET = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
LIMITS = pol.Limits()


def local(dt):
    return pol.to_local(dt, "America/New_York")


def decide(kind, **kw):
    args = dict(now_local=local(WED_11AM_ET), limits=LIMITS, connects_today=0,
                connects_week=0, messages_today=0, warmup_start=date(2026, 9, 1))
    args.update(kw)
    return pol.decide(kind, **args)


# ── policy ──

def test_limits_are_clamped_to_hard_maximums():
    big = pol.Limits(connects_per_day=500, messages_per_day=500, connects_per_week=500).clamped()
    assert (big.connects_per_day, big.messages_per_day, big.connects_per_week) == (10, 8, 40)
    assert pol.Limits(-3, -3, -3).clamped() == pol.Limits(0, 0, 0)


@pytest.mark.parametrize("day, connects, messages", [
    (1, 2, 1), (3, 3, 1), (4, 3, 2), (6, 4, 2), (12, 6, 4), (14, 6, 4), (15, 7, 5), (60, 7, 5)])
def test_warmup_ramp(day, connects, messages):
    assert pol.ramped_connects(7, day) == connects
    assert pol.ramped_messages(5, day) == messages


def test_ramp_never_exceeds_the_cap():
    assert pol.ramped_connects(1, 14) == 1 and pol.ramped_messages(0, 3) == 0


def test_warmup_day_counts_from_the_start_and_defaults_to_day_one():
    assert pol.warmup_day(date(2026, 10, 21), date(2026, 10, 21)) == 1
    assert pol.warmup_day(date(2026, 10, 1), date(2026, 10, 21)) == 21
    assert pol.warmup_day(None, date(2026, 10, 21)) == 1
    assert pol.warmup_day(date(2026, 12, 1), date(2026, 10, 21)) == 1   # start in the future


@pytest.mark.parametrize("dt, expected", [
    (datetime(2026, 10, 21, 9, 0), True), (datetime(2026, 10, 21, 17, 30), True),
    (datetime(2026, 10, 21, 8, 59), False), (datetime(2026, 10, 21, 17, 31), False),
    (datetime(2026, 10, 24, 11, 0), False), (datetime(2026, 10, 25, 11, 0), False)])
def test_sending_window(dt, expected):
    assert pol.in_window(dt) is expected


def test_connect_caps_daily_weekly_and_window():
    assert decide("connect").ok
    assert not decide("connect", connects_today=7).ok
    assert not decide("connect", connects_week=30).ok
    refused = decide("connect", now_local=local(SAT_11AM_ET))
    assert not refused.ok and "window" in refused.reason


def test_connect_cap_follows_the_warmup_ramp():
    day1 = dict(warmup_start=date(2026, 10, 21))
    assert decide("connect", connects_today=1, **day1).ok
    assert not decide("connect", connects_today=2, **day1).ok
    assert "warm-up day 1" in decide("connect", connects_today=2, **day1).reason


def test_replies_get_the_full_message_cap_but_first_messages_ramp():
    day1 = dict(warmup_start=date(2026, 10, 21))
    assert not decide("message", messages_today=1, **day1).ok     # ramped to 1 on day 1
    assert decide("reply", messages_today=1, **day1).ok           # someone wrote to us first
    assert not decide("reply", messages_today=5, **day1).ok       # but the hard daily total holds


def test_unknown_kinds_are_refused():
    assert not decide("poke").ok


def test_clamp_note_trims_on_a_word_boundary():
    assert pol.clamp_note("  short  ") == "short"
    long = ("word " * 100).strip()
    out = pol.clamp_note(long)
    assert len(out) <= 300 and not out.endswith(" ") and out.split()[-1] == "word"


def test_trip_outcomes_and_lock_duration():
    assert pol.trip_outcome(1) == "lock" and pol.trip_outcome(2) == "disable"
    assert (pol.lock_until(datetime(2026, 10, 1)) - datetime(2026, 10, 1)).total_seconds() == 72 * 3600


def test_action_gaps_are_human_sized():
    rng = random.Random(1)
    gaps = [pol.next_gap_seconds(rng) for _ in range(200)]
    assert min(gaps) >= 45 and max(gaps) <= 180 and len({round(g) for g in gaps}) > 20


# ── page classification ──

@pytest.mark.parametrize("url, text, status, kind", [
    ("https://www.linkedin.com/in/jane/", "", 200, ""),
    ("https://www.linkedin.com/authwall?trk=x", "", 200, "expired"),
    ("https://www.linkedin.com/login?session_redirect=x", "", 200, "expired"),
    ("https://www.linkedin.com/checkpoint/challenge/abc", "", 200, "trip"),
    ("https://www.linkedin.com/in/jane/", "", 429, "trip"),
    ("https://www.linkedin.com/in/jane/", "", 999, "trip"),
    ("https://www.linkedin.com/in/jane/", "You've reached the weekly invitation limit", 200, "trip"),
    ("https://www.linkedin.com/in/jane/", "We noticed unusual activity on your account", 200, "trip"),
    ("https://www.linkedin.com/in/jane/", "Connect with Jane to grow your network", 200, ""),
])
def test_detect_trip(url, text, status, kind):
    assert sel.detect_trip(url, text, status).kind == kind


def test_login_redirect_wins_over_challenge_wording():
    assert sel.detect_trip("https://x/uas/login", "unusual activity").kind == "expired"


@pytest.mark.parametrize("text, degree", [
    ("Jane Doe · 1st\nCEO at Acme", "1st"), ("Jane Doe • 2nd", "2nd"), ("Jane Doe · 3rd+", "3rd"),
    ("Jane Doe\nCEO", ""), ("", "")])
def test_parse_degree(text, degree):
    assert sel.parse_degree(text) == degree


@pytest.mark.parametrize("labels, degree, expected", [
    (["Connect", "Message", "More"], "2nd", "connectable"),
    (["Invite Jane Doe to connect", "Follow"], "3rd", "connectable"),
    (["Pending", "Message"], "2nd", "pending"),
    (["Pending, click to withdraw invitation sent to Jane Doe"], "", "pending"),
    (["Message", "More"], "1st", "connected"),
    (["Message", "Follow", "More"], "2nd", "more"),                  # Premium/open profile: not connected
    (["Follow", "More actions"], "3rd", "more"),
    (["Message"], "", "unknown"),                                    # can't prove a connection
    ([], "", "unknown"),
])
def test_classify_connection(labels, degree, expected):
    assert sel.classify_connection(labels, degree) == expected


def test_pending_beats_everything_and_connectable_beats_connected():
    assert sel.classify_connection(["Connect", "Pending"], "1st") == "pending"
    assert sel.classify_connection(["Connect", "Message"], "1st") == "connectable"


# ── state ──

@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as d:
        sm = StateManager(os.path.join(d, "t.db"))
        await sm.init_db()
        yield sm


async def prospect(state, n=1, url=True, score=50, status="new", email=None):
    return await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="CEO", company=f"Co{n}",
        email=email or f"p{n}@co{n}.com", email_status="verified", status=status, score=score,
        linkedin_url=f"https://www.linkedin.com/in/p{n}" if url else ""))


@pytest.mark.asyncio
async def test_thread_roundtrip_and_unknown_fields_are_ignored(state):
    pid = await prospect(state)
    assert await state.get_linkedin_thread(pid) is None
    await state.set_linkedin_thread(pid, state="staged", bogus="x")
    await state.set_linkedin_thread(pid, state="requested", requested_at="2026-10-21T10:00:00")
    t = await state.get_linkedin_thread(pid)
    assert t["state"] == "requested" and t["requested_at"] == "2026-10-21T10:00:00"
    assert [x["prospect_id"] for x in await state.list_linkedin_threads("requested")] == [pid]
    assert await state.list_linkedin_threads("connected") == []


@pytest.mark.asyncio
async def test_candidates_need_a_url_an_open_status_and_no_prior_touch(state):
    best = await prospect(state, 1, score=90)
    mid = await prospect(state, 2, score=60)
    await prospect(state, 3, url=False)
    await prospect(state, 4, status="replied")
    await prospect(state, 5, status="opted_out")
    touched = await prospect(state, 6, score=99)
    await state.set_linkedin_thread(touched, state="staged")
    got = await state.get_linkedin_candidates(10)
    assert [p.id for p in got] == [best, mid]
    assert len(await state.get_linkedin_candidates(1)) == 1


@pytest.mark.asyncio
async def test_ledger_kinds_and_open_outbox_counts(state):
    pid = await prospect(state)
    for kind in ("connect", "connect", "message", "reply"):
        await state.record_send(channel="linkedin", prospect_id=pid, kind=kind)
    await state.record_send(channel="linkedin_view", prospect_id=pid, kind="view")
    await state.record_send(channel="email", prospect_id=pid, kind="sequence")
    assert await state.count_kind_since("linkedin", ("connect",), today_only=True) == 2
    assert await state.count_kind_since("linkedin", ("message", "reply"), today_only=True) == 2
    assert await state.count_kind_since("linkedin", ("connect",), days=7) == 2
    assert await state.count_kind_since("linkedin_view", ("view",), today_only=True) == 1
    assert await state.count_sent_today("linkedin") == 4          # views live on their own channel

    await state.add_outbox_item(prospect_id=pid, to_email="", channel="linkedin", kind="connect",
                                to_ref="u", subject="", body="", send_at="x", status="approved")
    await state.add_outbox_item(prospect_id=pid, to_email="", channel="linkedin", kind="connect",
                                to_ref="u", subject="", body="", send_at="x", status="sent")
    assert await state.count_outbox_open("linkedin", ("connect",)) == 1


# ── browser flows through a scripted UI ──

class ScriptedUI(li.LinkedInAutomation):
    """The real flow logic with every touch of the page replaced by a script."""

    def __init__(self, *, labels=("Connect",), degree="2nd", profile_loads=True,
                 dialog="Add a note to your invitation?", dialog_appears=True,
                 dialog_closes=True, more_has_connect=True, compose_clears=True,
                 alert="", status=None):
        super().__init__("", "")
        self.page = SimpleNamespace(url="https://www.linkedin.com/feed/")
        self.labels, self.degree = list(labels), degree
        self.profile_loads, self.dialog_text = profile_loads, dialog
        self.dialog_appears, self.dialog_closes = dialog_appears, dialog_closes
        self.more_has_connect, self.compose_clears = more_has_connect, compose_clears
        self.alert, self.status = alert, status
        self.actions = []

    async def _goto(self, url):
        self.page.url = url
        await self._check_page(status=self.status)

    async def _alert_text(self):
        return self.alert

    async def _random_delay(self, *a, **k): pass
    async def _humanlike_scroll(self): pass
    async def _wait_settled(self, *a, **k): pass
    def _record(self, kind): self.actions.append(("record", kind))

    async def _open_profile(self, url):
        await self._goto(url)
        if not self.profile_loads:
            raise li.LinkedInUnavailable("profile did not load (removed or restricted)")

    async def _scope_text(self): return f"Jane Doe · {self.degree}\nCEO"
    async def _card_labels(self): return self.labels
    async def _click_card_button(self, pattern): self.actions.append(("click", pattern.pattern))

    async def _open_more_menu_connect(self):
        self.actions.append(("more",))
        return self.more_has_connect

    async def _dialog_text(self):
        if not self.dialog_appears:
            raise TimeoutError("no dialog")
        return self.dialog_text

    async def _click_in_dialog(self, pattern): self.actions.append(("dialog-click", pattern.pattern))
    async def _type_in_dialog(self, text): self.actions.append(("type", text))
    async def _dialog_closed(self): return self.dialog_closes

    async def _compose_and_send(self, text):
        self.actions.append(("compose", text))
        return self.compose_clears


@pytest.mark.asyncio
async def test_connect_without_a_note():
    ui = ScriptedUI()
    assert await ui.send_connection_request("https://www.linkedin.com/in/jane") == "sent"
    clicks = [a for a in ui.actions if a[0] == "dialog-click"]
    assert len(clicks) == 1 and "send without a note" in clicks[0][1]
    assert not any(a[0] == "type" for a in ui.actions)


@pytest.mark.asyncio
async def test_connect_with_a_note_types_it_then_sends():
    ui = ScriptedUI()
    assert await ui.send_connection_request("u", "Saw your post on hiring.") == "sent"
    kinds = [a[0] for a in ui.actions if a[0] in ("dialog-click", "type")]
    assert kinds == ["dialog-click", "type", "dialog-click"]
    assert ("type", "Saw your post on hiring.") in ui.actions


@pytest.mark.asyncio
async def test_connect_is_idempotent_for_pending_and_connected_profiles():
    assert await ScriptedUI(labels=["Pending", "Message"]).send_connection_request("u") == "already_pending"
    ui = ScriptedUI(labels=["Message", "More"], degree="1st")
    assert await ui.send_connection_request("u") == "already_connected"
    assert not any(a[0] == "click" for a in ui.actions)


@pytest.mark.asyncio
async def test_connect_through_the_more_menu():
    ui = ScriptedUI(labels=["Follow", "More actions"], degree="3rd")
    assert await ui.send_connection_request("u") == "sent"
    assert ("more",) in ui.actions
    with pytest.raises(li.LinkedInUnavailable):
        await ScriptedUI(labels=["Follow", "More"], more_has_connect=False).send_connection_request("u")


@pytest.mark.asyncio
async def test_unactionable_profiles_are_unavailable_not_trips():
    for ui, text in [
        (ScriptedUI(labels=["Message"], degree=""), "connection state"),
        (ScriptedUI(profile_loads=False), "did not load"),
        (ScriptedUI(dialog="Enter their email address to connect"), "email"),
    ]:
        with pytest.raises(li.LinkedInUnavailable, match=text):
            await ui.send_connection_request("u")


@pytest.mark.asyncio
async def test_ui_that_does_not_behave_as_expected_trips_the_breaker():
    with pytest.raises(li.LinkedInSelectorMiss):                       # no dialog after Connect
        await ScriptedUI(dialog_appears=False).send_connection_request("u")
    with pytest.raises(li.LinkedInSelectorMiss):                       # dialog never closes
        await ScriptedUI(dialog_closes=False).send_connection_request("u")
    assert issubclass(li.LinkedInSelectorMiss, li.LinkedInTripped)


@pytest.mark.asyncio
async def test_challenges_and_rate_limit_notices_trip_and_logouts_expire():
    with pytest.raises(li.LinkedInTripped):
        await ScriptedUI(status=429).send_connection_request("u")
    with pytest.raises(li.LinkedInTripped, match="weekly invitation limit"):
        await ScriptedUI(alert="You've reached the weekly invitation limit").send_connection_request("u")
    ui = ScriptedUI()
    ui.page.url = "x"

    async def logged_out(url):
        ui.page.url = "https://www.linkedin.com/authwall"
        await ui._check_page()
    ui._goto = logged_out
    with pytest.raises(li.LinkedInSessionExpired):
        await ui.send_connection_request("u")


@pytest.mark.asyncio
async def test_message_requires_a_connection_and_a_cleared_compose_box():
    ui = ScriptedUI(labels=["Message", "More"], degree="1st")
    assert await ui.send_message("https://www.linkedin.com/in/jane", "Thanks for connecting.") == "sent"
    assert ("compose", "Thanks for connecting.") in ui.actions

    with pytest.raises(li.LinkedInUnavailable, match="not connected"):
        await ScriptedUI(labels=["Connect"], degree="2nd").send_message("https://www.linkedin.com/in/j", "hi")
    with pytest.raises(li.LinkedInSelectorMiss):
        await ScriptedUI(labels=["Message"], degree="1st", compose_clears=False).send_message("u", "hi")


@pytest.mark.asyncio
async def test_replying_in_a_known_thread_skips_the_profile():
    ui = ScriptedUI()
    assert await ui.send_message("https://www.linkedin.com/messaging/thread/2-abc/", "Sure.") == "sent"
    assert not any(a[0] == "click" for a in ui.actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("labels, degree, expected", [
    (["Message", "More"], "1st", "connected"),
    (["Pending", "Message"], "2nd", "pending"),
    (["Connect", "Message"], "2nd", "none"),
    (["Follow", "More"], "3rd", "none"),
    (["Message"], "", "unknown"),
])
async def test_check_connection(labels, degree, expected):
    assert await ScriptedUI(labels=labels, degree=degree).check_connection("u") == expected


@pytest.mark.asyncio
async def test_ensure_session_never_falls_back_to_a_password_login(monkeypatch, tmp_path):
    monkeypatch.setattr(li, "COOKIES_PATH", tmp_path / "missing.json")
    with pytest.raises(li.LinkedInSessionExpired, match="harvey linkedin login"):
        await li.LinkedInAutomation("me@x.com", "hunter2").ensure_session()


def test_user_agent_matches_the_machine(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert "X11; Linux x86_64" in li._user_agent("130.0.0.0") and "Chrome/130.0.0.0" in li._user_agent("130.0.0.0")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert "Macintosh" in li._user_agent()
