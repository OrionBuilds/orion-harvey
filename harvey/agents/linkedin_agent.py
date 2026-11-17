"""LinkedInAgent: runs Harvey's LinkedIn relationship loop each heartbeat.

  stage    connection requests for the best-scored prospects with a profile URL
  check    whether pending invitations were accepted (sparingly: each check
           is a profile view)
  draft    a first message, once someone accepts: thanks, one real question,
           no pitch, no links
  send     in ``auto`` mode, through LinkedInChannel and the shared drain loop

In ``assist`` mode it only stages and drafts; the owner sends from the dashboard and
marks the item sent (see linkedin_ops). Browser work happens only when there is
something to do, and never outside the channel's rails.
"""

import logging
import random
from datetime import datetime, timedelta, timezone

from harvey import linkedin_ops
from harvey.channels.drain import STOP_STATUSES, drain_channel
from harvey.channels.linkedin import LinkedInChannel
from harvey.gate import prospect_suppression_keys
from harvey.integrations import linkedin_policy as policy
from harvey.integrations.linkedin import LinkedInUnavailable
from harvey.paths import stop_requested

logger = logging.getLogger("harvey.linkedin_agent")

# Staged-but-unsent requests held in reserve, as a multiple of the daily cap.
BACKLOG_DAYS = 2
ACCEPT_CHECKS_PER_CYCLE = 4
ACCEPT_MIN_WAIT_HOURS = 18
ACCEPT_RECHECK_HOURS = 12
MAX_DRAFT_ATTEMPTS = 3
FIRST_MESSAGE_DELAY_HOURS = (24, 48)
MAX_MESSAGE_CHARS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


class LinkedInAgent:
    def __init__(self, brain, state, config, env, *, automation_factory=None, now_fn=None):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env
        self._factory = automation_factory
        self._now_fn = now_fn
        self.send_pacing = True  # disabled in tests
        self.skills = ""

    @property
    def _cfg(self):
        return self.config.channels.linkedin

    def _channel(self) -> LinkedInChannel:
        return LinkedInChannel(self.config, self.state,
                               automation_factory=self._factory, now_fn=self._now_fn)

    @property
    def active(self) -> bool:
        return bool(self._cfg.enabled and self._cfg.mode != "off")

    async def run(self):
        if not self.active:
            return
        self.skills = self.brain.load_skills_for_agent("linkedin") if self.brain else ""
        channel = self._channel()
        try:
            await self._stage_connects()
            if self._cfg.mode == "auto" and await self._ready(channel):
                await self._check_acceptances(channel)
            await self._stage_first_messages()
            if self._cfg.mode == "auto":
                await drain_channel(self.state, channel, pacing_enabled=self.send_pacing)
        finally:
            await channel.close()

    async def _ready(self, channel: LinkedInChannel) -> bool:
        """Is it OK to open a browser on LinkedIn at all right now?"""
        if stop_requested() or not channel.is_configured():
            return False
        if await self.state.get_setting(channel.pause_key) or await channel.breaker_reason(self.state):
            return False
        now_local = policy.to_local(channel._now(), self._cfg.timezone)
        return policy.in_window(now_local)

    # ── staging ──

    def _initial_status(self) -> str:
        return "pending_review" if self._cfg.require_approval else "approved"

    async def _suppressed(self, prospect) -> bool:
        company = (await self.state.get_company(prospect.company_id)
                   if prospect.company_id else None)
        for kind, value in prospect_suppression_keys(prospect, company):
            if await self.state.find_suppression(kind, value, channel="linkedin"):
                return True
        return False

    async def _stage_connects(self) -> int:
        target = self._channel().limits().connects_per_day * BACKLOG_DAYS
        need = target - await self.state.count_outbox_open("linkedin", (policy.CONNECT,))
        if need <= 0:
            return 0
        staged = 0
        for prospect in await self.state.get_linkedin_candidates(need * 3):
            if staged >= need:
                break
            if await self._suppressed(prospect):
                continue
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id, to_email="", channel="linkedin",
                kind=policy.CONNECT, to_ref=prospect.linkedin_url, subject="",
                body="", send_at=_now().isoformat(), status=self._initial_status(),
                provider="linkedin")
            if item_id:
                await self.state.set_linkedin_thread(prospect.id, state="staged")
                staged += 1
        if staged:
            logger.info(f"LinkedIn: staged {staged} connection request(s).")
        return staged

    # ── acceptance checks ──

    async def _check_acceptances(self, channel: LinkedInChannel) -> None:
        now = _now()
        used = await self.state.count_kind_since("linkedin_view", ("view",), today_only=True)
        budget = min(ACCEPT_CHECKS_PER_CYCLE, policy.PROFILE_VIEWS_PER_DAY - used)
        if budget <= 0:
            return
        checked = 0
        for thread in await self.state.list_linkedin_threads("requested", limit=25):
            if checked >= budget:
                break
            requested = _parse(thread.get("requested_at"))
            last = _parse(thread.get("last_checked_at"))
            if requested and now - requested < timedelta(hours=ACCEPT_MIN_WAIT_HOURS):
                continue
            if last and now - last < timedelta(hours=ACCEPT_RECHECK_HOURS):
                continue
            prospect = await self.state.get_prospect(thread["prospect_id"])
            if prospect is None or not prospect.linkedin_url:
                continue
            try:
                done, result = await channel.guarded(
                    lambda auto, url=prospect.linkedin_url: auto.check_connection(url))
            except LinkedInUnavailable:
                await self.state.set_linkedin_thread(prospect.id, last_checked_at=now.isoformat())
                continue
            if not done:
                return  # LinkedIn is paused or cooling off
            checked += 1
            await self.state.record_send(channel="linkedin_view", prospect_id=prospect.id, kind="view")
            await self._apply_check(prospect, result, now)
            if self.send_pacing and checked < budget:
                import asyncio
                await asyncio.sleep(policy.next_gap_seconds(random))

    async def _apply_check(self, prospect, result: str, now: datetime) -> None:
        if result == "connected":
            await linkedin_ops.mark_accepted(self.state, prospect.id)
            logger.info(f"LinkedIn: {prospect.full_name()} accepted.")
        elif result == "none":
            # The invitation is gone and they aren't connected: declined or expired.
            await self.state.set_linkedin_thread(prospect.id, state="declined",
                                                 last_checked_at=now.isoformat())
        else:
            await self.state.set_linkedin_thread(prospect.id, last_checked_at=now.isoformat())

    # ── first messages ──

    async def _stage_first_messages(self) -> int:
        staged = 0
        for thread in await self.state.list_linkedin_threads("connected", limit=10):
            if int(thread.get("messages_sent") or 0) > 0:
                continue
            prospect = await self.state.get_prospect(thread["prospect_id"])
            if prospect is None or prospect.status in STOP_STATUSES:
                continue
            if await self._suppressed(prospect):
                continue
            fail_key = f"linkedin_draft_fail:{prospect.id}"
            if int(await self.state.get_setting(fail_key) or 0) >= MAX_DRAFT_ATTEMPTS:
                continue
            text = await self._draft_first_message(prospect)
            probe = {"kind": policy.MESSAGE, "to_ref": prospect.linkedin_url}
            problems = (self._channel().content_gate(probe, "", text, prospect).reasons
                        if text else ["no draft produced"])
            if problems:
                await self.state.increment_setting(fail_key)
                logger.warning(f"LinkedIn: draft for {prospect.full_name()} rejected: {problems}")
                continue
            accepted = _parse(thread.get("accepted_at")) or _now()
            delay = timedelta(hours=random.uniform(*FIRST_MESSAGE_DELAY_HOURS))
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id, to_email="", channel="linkedin",
                kind=policy.MESSAGE, to_ref=prospect.linkedin_url, subject="",
                body=text, send_at=max(_now(), accepted + delay).isoformat(),
                status=self._initial_status(), provider="linkedin")
            if item_id:
                await self.state.set_linkedin_thread(prospect.id, state="message_staged")
                staged += 1
        if staged:
            logger.info(f"LinkedIn: drafted {staged} first message(s).")
        return staged

    async def _draft_first_message(self, prospect) -> str:
        persona = self.config.persona
        notes = (prospect.personalization_notes or "").strip() or "(none: ask about their role generally)"
        prompt = f"""{self.skills}

You are writing the FIRST LinkedIn message from {persona.name} ({persona.role}) to
{prospect.first_name} {prospect.last_name}, {prospect.title} at {prospect.company}.
They have just accepted your connection request.

Rules, all of them:
- 50 words or fewer. Plain text. No links, no emojis, no exclamation marks, no em dashes.
- Thank them for connecting in one short clause, then ask ONE genuine question about
  their work or company. Do not pitch, do not name any product or service, do not ask for a call.
- Use only the facts below. Never invent a detail, a mutual connection, or a post you saw.
- Sound like a person typing on their phone, not a marketer.

Facts you may use:
{notes}

Reply with ONLY the message text."""
        reply = await self.brain.think(
            prompt, session_id="harvey-linkedin", agent="linkedin", task="first_message")
        return (reply or "").strip().strip('"').strip()[:MAX_MESSAGE_CHARS]
