"""LinkedIn channel: connection requests and messages from the owner's real account.

Every limit and the circuit breaker come from ``linkedin_policy``; the
browser work comes from ``LinkedInAutomation``. This class is the glue and
the safety rails: what may go out *now*, and what happens when LinkedIn
pushes back.
"""

import json
import logging
import re
import time
import asyncio
from datetime import date, datetime, timedelta, timezone

from harvey import linkedin_ops, persona_guard
from harvey.channels import Channel
from harvey.gate import BANNED_PHRASES, MERGE_TAG_RE, URL_RE, GateResult
from harvey.integrations import linkedin_policy as policy
from harvey.integrations.linkedin import (
    COOKIES_PATH,
    LinkedInAutomation,
    LinkedInSessionExpired,
    LinkedInTripped,
    LinkedInUnavailable,
)
from harvey.integrations.mail_provider import InboundMessage, SendResult
from harvey.persona import is_assistant

logger = logging.getLogger("harvey.linkedin.channel")

TRIPPED_UNTIL = "linkedin_tripped_until"
TRIPS = "linkedin_trips"
DISABLED = "linkedin_disabled"
INBOX_CHECKED_AT = "linkedin_inbox_checked_at"
INBOX_RETRY_AT = "linkedin_inbox_retry_at"
INBOX_RETRY_MINUTES = 30
INBOX_READ_TIMEOUT_SECONDS = 240
WARMUP_START = "linkedin_warmup_start"
UNAVAILABLE_STREAK = "linkedin_unavailable_streak"

# This many profiles in a row that can't be actioned looks less like three odd
# profiles and more like LinkedIn's page having changed. Stop and look.
UI_DRIFT_STREAK = 3

PROFILE_URL_RE = re.compile(r"linkedin\.com/in/", re.I)
HTML_RE = re.compile(r"<\s*(html|body|div|table|img|a)\b", re.I)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc) if value else None
    except ValueError:
        return None


_LOCAL_BROWSER_LOSS = re.compile(
    r"(target (page, context or browser|closed)|browser has been closed|page has been closed|context has been closed)", re.I)


class LinkedInChannel(Channel):
    name = "linkedin"
    pauses_on_breaker = False  # a trip lock expires by itself; a human re-enables only after a disable

    def __init__(self, config, state, *, automation_factory=None, now_fn=None):
        self.config = config
        self.state = state
        self._factory = automation_factory
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._auto = None
        self._session_started: float | None = None

    # ── configuration ──

    @property
    def _cfg(self):
        return self.config.channels.linkedin

    def limits(self) -> policy.Limits:
        c = self._cfg
        return policy.Limits(
            c.max_daily_connections, c.max_daily_messages, c.max_weekly_connections
        ).clamped()

    def is_configured(self) -> bool:
        return bool(self._cfg.enabled and self._cfg.mode == "auto" and self._has_session())

    def _has_session(self) -> bool:
        return self._factory is not None or COOKIES_PATH.exists()

    def daily_cap(self) -> int:
        lim = self.limits()
        return lim.connects_per_day + lim.messages_per_day

    def max_per_cycle(self) -> int:
        return 3

    def pacing(self):
        return policy.ACTION_GAP_SECONDS

    # ── content ──

    def content_gate(self, item: dict, subject: str, body: str, prospect) -> GateResult:
        reasons: list[str] = []
        text = (body or "").strip()
        kind = item["kind"]
        if not PROFILE_URL_RE.search(item.get("to_ref", "") or "") and "/messaging/thread/" not in (item.get("thread_ref") or ""):
            reasons.append("no LinkedIn profile URL to send to")
        if kind == policy.CONNECT:
            if len(text) > policy.MAX_NOTE_CHARS:
                reasons.append(f"connection note too long ({len(text)} > {policy.MAX_NOTE_CHARS})")
            if URL_RE.search(text):
                reasons.append("no links in a connection note")
        else:
            if not text:
                reasons.append("empty message")
            if len(URL_RE.findall(text)) > 1:
                reasons.append("too many links")
        if MERGE_TAG_RE.search(text):
            reasons.append("unrendered merge tags")
        if HTML_RE.search(text):
            reasons.append("HTML markup in message")
        hits = [p for p in BANNED_PHRASES if p in text.lower()]
        if hits:
            reasons.append(f"banned phrases: {hits[:3]}")
        if is_assistant(self.config):
            reasons += [f"persona: {p}" for p in persona_guard.violations(text)]
        return GateResult(ok=not reasons, reasons=reasons)

    # ── breaker ──

    async def breaker_reason(self, state) -> str:
        disabled = await state.get_setting(DISABLED)
        if disabled:
            return f"LinkedIn automation disabled: {disabled}"
        until = _parse(await state.get_setting(TRIPPED_UNTIL))
        if until and until > self._now():
            return f"LinkedIn cooling off after a trip until {until.isoformat(timespec='minutes')}Z"
        return ""

    async def _trip(self, reason: str) -> None:
        if _LOCAL_BROWSER_LOSS.search(reason or ""):
            # The browser or page was closed under us (Harvey restarting, a crash): nothing LinkedIn did.
            # Locking for 72 hours here would punish a restart. Skip this round and try again next cycle.
            logger.warning(f"LinkedIn browser closed mid-action (not a trip): {reason}")
            await self.state.log_action(
                action_type="linkedin_browser_closed", agent="linkedin", details={"reason": reason[:300]})
            return
        now = self._now()
        recent = []
        try:
            recent = json.loads(await self.state.get_setting(TRIPS) or "[]")
        except ValueError:
            pass
        cutoff = now - timedelta(days=policy.TRIP_WINDOW_DAYS)
        recent = [t for t in recent if (_parse(t) or now) > cutoff] + [_iso(now)]
        await self.state.set_setting(TRIPS, json.dumps(recent))
        if policy.trip_outcome(len(recent)) == "disable":
            await self.state.set_setting(DISABLED, f"{len(recent)} trips in {policy.TRIP_WINDOW_DAYS} days; last: {reason}")
            logger.error(f"LinkedIn automation DISABLED until a human re-enables it: {reason}")
        else:
            await self.state.set_setting(TRIPPED_UNTIL, _iso(policy.lock_until(now)))
            logger.error(f"LinkedIn trip, locking for {policy.TRIP_LOCK_HOURS}h: {reason}")
        await self.state.log_action(
            action_type="linkedin_trip", agent="linkedin", details={"reason": reason, "trips": len(recent)})

    async def _pause_for_login(self, reason: str) -> None:
        await self.state.set_setting(self.pause_key, f"{reason} - run `harvey linkedin login`, then resume")
        logger.error(f"LinkedIn paused: {reason}")

    # ── pre-send rules ──

    async def _warmup_start(self) -> date:
        configured = self._cfg.warmup_start_date
        if configured:
            return date.fromisoformat(configured)
        stored = await self.state.get_setting(WARMUP_START)
        if stored:
            return date.fromisoformat(stored)
        today = policy.to_local(self._now(), self._cfg.timezone).date()
        await self.state.set_setting(WARMUP_START, today.isoformat())
        return today

    async def pre_send(self, state, item: dict, prospect) -> GateResult:
        kind = item["kind"]
        thread = await state.get_linkedin_thread(prospect.id) or {}
        tstate = thread.get("state", "")

        if kind == policy.CONNECT and tstate not in ("", "staged"):
            return GateResult(False, [f"already {tstate} on LinkedIn"])
        if kind == policy.MESSAGE:
            if tstate not in ("connected", "message_staged", "messaged"):
                return GateResult(False, [f"cannot message while LinkedIn state is '{tstate or 'none'}'"])
            if int(thread.get("messages_sent") or 0) >= policy.MAX_UNANSWERED_MESSAGES:
                return GateResult(False, [f"{policy.MAX_UNANSWERED_MESSAGES} unanswered messages already; stopping"])

        now_local = policy.to_local(self._now(), self._cfg.timezone)
        verdict = policy.decide(
            kind,
            now_local=now_local,
            limits=self.limits(),
            connects_today=await state.count_kind_since("linkedin", (policy.CONNECT,), today_only=True),
            connects_week=await state.count_kind_since("linkedin", (policy.CONNECT,), days=7),
            messages_today=await state.count_kind_since("linkedin", policy.MESSAGE_KINDS, today_only=True),
            warmup_start=await self._warmup_start(),
        )
        if not verdict.ok:
            return GateResult(False, [verdict.reason], defer=True)

        if kind in policy.MESSAGE_KINDS:
            checked = _parse(await state.get_setting(INBOX_CHECKED_AT))
            if not checked or self._now() - checked > timedelta(minutes=policy.INBOX_FRESH_MINUTES):
                return GateResult(False, ["LinkedIn inbox not read recently; reading before sending"], defer=True)

        if self._session_started and time.monotonic() - self._session_started > policy.SESSION_MAX_SECONDS:
            return GateResult(False, ["session time limit reached; resuming next cycle"], defer=True)
        return GateResult(ok=True)

    # ── sending ──

    async def _session(self) -> LinkedInAutomation:
        if self._auto is None:
            self._auto = self._factory() if self._factory else LinkedInAutomation("", "")
            await self._auto.ensure_session()
            self._session_started = time.monotonic()
        return self._auto

    async def send(self, item: dict, subject: str, body: str) -> SendResult:
        kind = item["kind"]
        try:
            auto = await self._session()
            if kind == policy.CONNECT:
                status = await auto.send_connection_request(
                    item["to_ref"], policy.clamp_note(body))
            else:
                target = item.get("thread_ref") or item["to_ref"]
                status = await auto.send_message(target, body)
        except LinkedInSessionExpired as e:
            await self._pause_for_login(str(e))
            return SendResult(ok=False, defer=True, error=f"linkedin session: {e}")
        except LinkedInTripped as e:
            await self._trip(str(e))
            return SendResult(ok=False, defer=True, error=f"linkedin tripped: {e}")
        except LinkedInUnavailable as e:
            streak = await self.state.increment_setting(UNAVAILABLE_STREAK)
            if streak >= UI_DRIFT_STREAK:
                await self.state.set_setting(UNAVAILABLE_STREAK, "0")
                await self._trip(f"{streak} profiles in a row could not be actioned; LinkedIn's page may have changed")
            return SendResult(ok=False, error=f"unavailable: {e}")

        await self.state.set_setting(UNAVAILABLE_STREAK, "0")
        item["_li_status"] = status
        return SendResult(ok=True, message_id=f"linkedin:{status}")

    async def after_send(self, state, item: dict, prospect) -> None:
        await linkedin_ops.apply_sent(state, item, prospect)
        if item.get("_li_status") == "already_connected":
            await linkedin_ops.mark_accepted(state, prospect.id)

    # ── inbox ──

    async def poll_inbound(self) -> list[InboundMessage]:
        if not self.is_configured() or await self.breaker_reason(self.state):
            return []
        now = self._now()
        if not policy.in_window(policy.to_local(now, self._cfg.timezone)):
            return []
        last = _parse(await self.state.get_setting(INBOX_CHECKED_AT))
        if last and now - last < timedelta(minutes=policy.INBOX_POLL_MINUTES):
            return []  # read recently enough; polling more often is itself a pattern

        # A failed or hung read must not retry every heartbeat (each retry opens another browser)
        # and must not hold email hostage: LinkedIn sends already refuse to go out while the
        # inbox is stale (gate), so a miss here only pauses LinkedIn, not email.
        if retry_at := _parse(await self.state.get_setting(INBOX_RETRY_AT)):
            if now < retry_at:
                return []
        try:
            async def read():
                auto = await self._session()
                return await auto.read_inbox()
            found = await asyncio.wait_for(read(), INBOX_READ_TIMEOUT_SECONDS)
        except LinkedInSessionExpired as e:
            # LinkedIn stops; email must not. See module docs of Handler.
            await self._pause_for_login(str(e))
            return []
        except LinkedInTripped as e:
            await self._trip(str(e))
            return []
        except Exception as e:   # includes the read timeout; the browser may be wedged, drop it
            logger.warning(f"LinkedIn inbox read failed ({type(e).__name__}: {str(e)[:160]}); "
                           f"retrying in {INBOX_RETRY_MINUTES} min. Email is unaffected.")
            await self.state.set_setting(
                INBOX_RETRY_AT, _iso(self._now() + timedelta(minutes=INBOX_RETRY_MINUTES)))
            await self._drop_browser()
            return []
        await self.state.set_setting(INBOX_CHECKED_AT, _iso(self._now()))
        return [
            InboundMessage(
                provider_id=m["provider_id"], from_email="", channel="linkedin",
                from_ref=m["profile_url"], body=m["text"], thread_ref=m["thread_url"])
            for m in found
        ]

    async def guarded(self, action):
        """Run a browser action for the agent with the channel's own trip and
        login handling. Returns (completed, result); (False, None) means
        LinkedIn is now paused or cooling off and the caller should stop.
        LinkedInUnavailable (one prospect) is left for the caller."""
        try:
            return True, await action(await self._session())
        except LinkedInSessionExpired as e:
            await self._pause_for_login(str(e))
        except LinkedInTripped as e:
            await self._trip(str(e))
        return False, None

    async def close(self) -> None:
        """Close the browser. Bounded and never raises: a wedged browser must not stall a cycle."""
        auto, self._auto, self._session_started = self._auto, None, None
        if auto is not None:
            try:
                await asyncio.wait_for(auto.stop(), 20)
            except Exception as e:
                logger.warning(f"LinkedIn browser did not close cleanly ({type(e).__name__}); "
                               "the stale-driver reaper will remove it.")

    async def _drop_browser(self) -> None:
        await self.close()
        try:
            from harvey.integrations.linkedin import reap_stale_playwright
            reap_stale_playwright(max_age_seconds=60)   # this process's drivers only
        except Exception:
            pass
