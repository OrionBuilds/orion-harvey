"""SMS channel: consent-gated texts, never cold.

Rules, all enforced in code (a text that breaks one is cancelled or held, never sent):
  - consent: the prospect has an explicit consent record for SMS (they texted first,
    booked with a phone number, or opted in). An email reply is NOT consent to text.
  - a verified E.164 number, and the number is not suppressed
  - TCPA window: 9 am to 6 pm in the recipient's time, Monday to Friday; outside it the
    text waits, it is not dropped
  - one short plain message, no links but the booking link, "Reply STOP to opt out" added
  - STOP / UNSUBSCRIBE / CANCEL / END / QUIT silences the number permanently

Through Orion (transport "os") the number is its Clara assistant's and Orion owns the inbound side: a reply
is filed and answered by Clara, and a STOP lands on Orion's do-not-contact list (`dnc_numbers`), which
Harvey checks before every text. So Harvey cannot learn that someone texted first: consent there is
recorded explicitly (`harvey sms consent`), never inferred.
"""

import re
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from harvey import owner, persona_guard
from harvey.channels import Channel
from harvey.gate import BANNED_PHRASES, MERGE_TAG_RE, URL_RE, GateResult
from harvey.integrations.mail_provider import InboundMessage, SendResult
from harvey.integrations.sms_transport import OsSmsTransport, SmsTransport, TwilioTransport

E164 = re.compile(r"^\+[1-9]\d{7,14}$")
CONSENT_BASES = {"inbound_contact", "booked", "opted_in"}
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_WORDS = {"START", "UNSTOP", "YES"}
FOOTER = "Reply STOP to opt out."
# Through Orion the number is Clara's, and Clara (not Harvey) answers replies. Say so, so a reply
# from a different voice is no surprise.
OS_NOTE = f"Replies go to Clara, {owner.FIRST}'s assistant."
MAX_BODY = 240
ALLOWED_LINK = owner.BOOKING_URL


def keyword(body: str) -> str:
    """'stop' | 'start' | '' for an inbound text. Only a bare keyword counts, as carriers define it."""
    word = re.sub(r"[^A-Za-z]", "", (body or "")).upper()
    if word in STOP_WORDS and len((body or "").split()) <= 2:
        return "stop"
    if word in START_WORDS and len((body or "").split()) <= 2:
        return "start"
    return ""


class SmsChannel(Channel):
    name = "sms"

    def __init__(self, config, env, state=None, transport: SmsTransport | None = None, now=None):
        self.config, self.state = config, state
        self.transport = transport or self._build_transport(env)
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def _cfg(self):
        return self.config.channels.sms

    def _build_transport(self, env) -> SmsTransport:
        if self._cfg.transport == "twilio":
            return TwilioTransport(getattr(env, "twilio_account_sid", ""),
                                   getattr(env, "twilio_auth_token", ""), self._cfg.from_number)
        from harvey.integrations.os_auth import OsAuth
        from harvey.integrations.os_client import OsClient
        return OsSmsTransport(OsClient(OsAuth.from_env(env)), self._cfg.from_number)

    def is_configured(self) -> bool:
        if not (self._cfg.enabled and self.transport.is_configured()):
            return False
        # Through the OS, inbound (replies, STOP) is the OS's. No route back to Harvey, no texting.
        return self._cfg.transport != "os" or self._cfg.inbound_ready

    def daily_cap(self) -> int:
        return int(self._cfg.max_daily_sms)

    def max_per_cycle(self) -> int:
        return 5

    def pacing(self):
        return (20.0, 60.0)

    # ── content ──

    async def prepare(self, item: dict, prospect) -> tuple[str, str]:
        body = (item.get("body") or "").strip()
        if self._cfg.transport == "os" and OS_NOTE.lower() not in body.lower():
            body = f"{body}\n{OS_NOTE}"
        if FOOTER.lower() not in body.lower():
            body = f"{body}\n{FOOTER}"
        return "", body

    def content_gate(self, item: dict, subject: str, body: str, prospect) -> GateResult:
        reasons: list[str] = []
        text = body.replace(FOOTER, "").replace(OS_NOTE, "").strip()
        if not text:
            reasons.append("empty message")
        elif "harvey" not in text.lower():
            # The number may be Clara's. The person must know who is writing: always Harvey.
            reasons.append("must say it is Harvey writing")
        if len(text) > MAX_BODY:
            reasons.append(f"over {MAX_BODY} characters ({len(text)})")
        if MERGE_TAG_RE.search(text):
            reasons.append("unrendered merge tags")
        links = URL_RE.findall(text)
        if any(not link.rstrip(".,").startswith(ALLOWED_LINK) for link in links) or len(links) > 1:
            reasons.append(f"only {ALLOWED_LINK} may appear")
        hits = [p for p in BANNED_PHRASES if p in text.lower()]
        if hits:
            reasons.append(f"banned phrases: {hits[:3]}")
        reasons += [f"persona: {p}" for p in persona_guard.violations(text)]
        return GateResult(ok=not reasons, reasons=reasons)

    # ── who and when ──

    def _window_open(self) -> bool:
        tz = ZoneInfo(self._cfg.timezone)
        local = self._now().astimezone(tz)
        if local.weekday() > 4:
            return False
        return dtime.fromisoformat(self._cfg.window_start) <= local.time() < dtime.fromisoformat(self._cfg.window_end)

    async def pre_send(self, state, item: dict, prospect) -> GateResult:
        number = (item.get("to_ref") or prospect.phone or "").strip()
        if not E164.match(number):
            return GateResult(ok=False, reasons=["no valid E.164 phone number"])
        if prospect.phone and number != prospect.phone:
            return GateResult(ok=False, reasons=["number does not match the prospect's number"])
        if (prospect.phone_status or "") != "verified":
            return GateResult(ok=False, reasons=["phone number is not verified"])
        consent = (prospect.consent() or {}).get("sms") or {}
        if consent.get("basis") not in CONSENT_BASES:
            return GateResult(ok=False, reasons=["no SMS consent on record (they must text first, book, or opt in)"])
        if await state.find_suppression("phone", number, channel="sms"):
            return GateResult(ok=False, reasons=["this number opted out"])
        check = getattr(self.transport, "suppressed", None)
        if check is not None:
            try:
                if await check(number):
                    return GateResult(ok=False, reasons=["this number is on the OS do-not-contact list"])
            except Exception as e:   # fail closed: if we can't check, we don't text
                return GateResult(ok=False, defer=True, reasons=[f"could not check the OS do-not-contact list: {e}"])
        if not self._window_open():
            return GateResult(ok=False, defer=True, reasons=[
                f"outside the texting window ({self._cfg.window_start}-{self._cfg.window_end} "
                f"{self._cfg.timezone}, Mon-Fri)"])
        return GateResult(ok=True)

    # ── wire ──

    async def send(self, item: dict, subject: str, body: str) -> SendResult:
        return await self.transport.send((item.get("to_ref") or "").strip(), body)

    async def poll_inbound(self) -> list[InboundMessage]:
        since = None
        if self.state is not None:
            raw = await self.state.get_setting("sms_inbox_checked_at", "")
            try:
                since = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc) if raw else None
            except ValueError:
                since = None
        messages = await self.transport.poll(since)
        if self.state is not None:
            await self.state.set_setting(
                "sms_inbox_checked_at", self._now().astimezone(timezone.utc).replace(tzinfo=None).isoformat())
        return messages
