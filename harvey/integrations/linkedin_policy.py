"""LinkedIn safety policy. Pure functions: no browser, no database.

Every limit that protects the owner's real LinkedIn account lives here, so it can be
read in one place and tested exhaustively. These are ceilings, not targets, and
they are enforced in code: neither a prompt nor a config typo can raise them
past the hard maximums below.

Automating LinkedIn is outside its Terms of Service at any volume. Low caps,
a business-hours window, human-like gaps and a warm-up ramp reduce the chance
of being flagged; they do not make it compliant.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Hard maximums: config may lower these, never raise them.
HARD_CONNECTS_PER_DAY = 10
HARD_MESSAGES_PER_DAY = 8
HARD_CONNECTS_PER_WEEK = 40

# Defaults: 7 connects and 5 messages a day.
DEFAULT_CONNECTS_PER_DAY = 7
DEFAULT_MESSAGES_PER_DAY = 5
DEFAULT_CONNECTS_PER_WEEK = 30
PROFILE_VIEWS_PER_DAY = 25

WINDOW_START = time(9, 0)
WINDOW_END = time(17, 30)
BUSINESS_DAYS = (0, 1, 2, 3, 4)  # Monday-Friday

ACTION_GAP_SECONDS = (45, 180)
SESSION_MAX_SECONDS = 25 * 60
WARMUP_DAYS = 14

# A reply is overdue-checked often enough that a send never goes out blind.
INBOX_FRESH_MINUTES = 240
INBOX_POLL_MINUTES = 90

# Circuit breaker.
TRIP_LOCK_HOURS = 72
TRIP_WINDOW_DAYS = 30
DISABLE_AFTER_TRIPS = 2

# Stop after this many messages with no answer.
MAX_UNANSWERED_MESSAGES = 2

# Connection-request note limit LinkedIn enforces on free accounts.
MAX_NOTE_CHARS = 300

CONNECT = "connect"
MESSAGE = "message"
REPLY = "reply"
MESSAGE_KINDS = (MESSAGE, REPLY)


@dataclass(frozen=True)
class Limits:
    connects_per_day: int = DEFAULT_CONNECTS_PER_DAY
    messages_per_day: int = DEFAULT_MESSAGES_PER_DAY
    connects_per_week: int = DEFAULT_CONNECTS_PER_WEEK

    def clamped(self) -> "Limits":
        return Limits(
            connects_per_day=max(0, min(self.connects_per_day, HARD_CONNECTS_PER_DAY)),
            messages_per_day=max(0, min(self.messages_per_day, HARD_MESSAGES_PER_DAY)),
            connects_per_week=max(0, min(self.connects_per_week, HARD_CONNECTS_PER_WEEK)),
        )


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""


def warmup_day(start: date | None, today: date) -> int:
    """1-based day of the warm-up. With no start date the account is treated as
    brand new (day 1), the cautious reading."""
    if start is None:
        return 1
    return max(1, (today - start).days + 1)


def ramped_connects(cap: int, day: int) -> int:
    """Day 1: 2, then +1 every 3 days, reaching the cap by the end of warm-up."""
    return cap if day > WARMUP_DAYS else min(cap, 2 + day // 3)


def ramped_messages(cap: int, day: int) -> int:
    return cap if day > WARMUP_DAYS else min(cap, 1 + day // 4)


def in_window(now_local: datetime) -> bool:
    return (now_local.weekday() in BUSINESS_DAYS
            and WINDOW_START <= now_local.time() <= WINDOW_END)


def to_local(now_utc: datetime, tz_name: str) -> datetime:
    """`now_utc` may be naive (assumed UTC) or aware."""
    from datetime import timezone
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(ZoneInfo(tz_name))


def decide(
    kind: str,
    *,
    now_local: datetime,
    limits: Limits,
    connects_today: int,
    connects_week: int,
    messages_today: int,
    warmup_start: date | None,
) -> Decision:
    """May one more action of this kind go out right now?

    Every refusal is temporary (the caller defers the item): the window opens
    again, the day rolls over, the ramp grows. Hard-stop conditions such as a
    tripped breaker or a suppressed person are handled elsewhere.
    """
    limits = limits.clamped()
    if not in_window(now_local):
        return Decision(False, "outside the Mon-Fri 09:00-17:30 sending window")

    day = warmup_day(warmup_start, now_local.date())

    if kind == CONNECT:
        daily = ramped_connects(limits.connects_per_day, day)
        if connects_today >= daily:
            return Decision(False, f"connect cap reached today ({connects_today}/{daily}, warm-up day {day})")
        if connects_week >= limits.connects_per_week:
            return Decision(False, f"weekly connect cap reached ({connects_week}/{limits.connects_per_week})")
        return Decision(True)

    if kind in MESSAGE_KINDS:
        # Replies get the full cap (someone wrote to us first); first messages
        # follow the warm-up ramp. Both count toward the same daily total.
        daily = limits.messages_per_day if kind == REPLY else ramped_messages(limits.messages_per_day, day)
        if messages_today >= daily:
            return Decision(False, f"message cap reached today ({messages_today}/{daily}, warm-up day {day})")
        return Decision(True)

    return Decision(False, f"unknown action kind '{kind}'")


def next_gap_seconds(rng) -> float:
    """A randomized pause between actions. `rng` is `random` or a seeded Random."""
    return rng.uniform(*ACTION_GAP_SECONDS)


def trip_outcome(trips_in_window: int) -> str:
    """After a trip: 'lock' (pause for TRIP_LOCK_HOURS) or 'disable' (until a human re-enables)."""
    return "disable" if trips_in_window >= DISABLE_AFTER_TRIPS else "lock"


def lock_until(now: datetime) -> datetime:
    return now + timedelta(hours=TRIP_LOCK_HOURS)


def clamp_note(note: str) -> str:
    """Trim a connection note to LinkedIn's limit on a word boundary."""
    note = (note or "").strip()
    if len(note) <= MAX_NOTE_CHARS:
        return note
    cut = note[:MAX_NOTE_CHARS].rsplit(" ", 1)[0].rstrip(",;:- ")
    return cut
