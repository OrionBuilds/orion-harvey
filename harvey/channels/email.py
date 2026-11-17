"""Email channel: wraps a MailProvider (Gmail / SMTP / Resend) as a Channel."""

from datetime import datetime, timedelta, timezone

from harvey import compliance, persona_guard
from harvey.channels import Channel
from harvey.gate import GateResult, pre_send_check
from harvey.integrations.mail_provider import InboundMessage, MailProvider, SendResult
from harvey.persona import is_assistant

SEND_JITTER_SECONDS = (4, 15)
_ZERO = timedelta(0)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_ts(value) -> datetime:
    """Outbox timestamps are naive UTC strings ('2026-10-22T04:44:00' or with a space)."""
    if not value:
        return _now_utc()
    return datetime.fromisoformat(str(value).replace(" ", "T")[:26])


class EmailChannel(Channel):
    name = "email"

    def __init__(self, config, provider: MailProvider, state=None):
        self.config = config
        self.provider = provider
        self.state = state  # needed to mint per-message unsubscribe links

    @property
    def _cfg(self):
        return self.config.channels.email

    def is_configured(self) -> bool:
        return self.provider.is_configured()

    def daily_cap(self) -> int:
        return int(self._cfg.max_daily_sends)

    def pacing(self):
        return SEND_JITTER_SECONDS

    def sender_domain(self) -> str:
        configured = (getattr(self._cfg, "sender_domain", "") or "").strip().lower()
        if configured:
            return configured
        persona_email = getattr(getattr(self.config, "persona", None), "email", "") or ""
        return persona_email.rsplit("@", 1)[-1].lower() if "@" in persona_email else ""

    def quotas(self):
        return compliance.EmailQuotas(global_per_day=self.daily_cap())

    @property
    def _physical_address(self) -> str:
        return (getattr(self._cfg, "physical_address", "") or "").strip()

    @property
    def _enforce(self) -> bool:
        # Real configs default to enforcing; bare test stubs that don't define
        # the field are left alone.
        return bool(getattr(self._cfg, "enforce_compliance", False))

    @property
    def _from_address(self) -> str:
        # The mailbox people can write back to: footer "unsubscribe" email and the
        # List-Unsubscribe mailto must reach an inbox Harvey reads, which with Resend is
        # the Reply-To mailbox, not the From address.
        reply_to = (getattr(self._cfg, "reply_to", "") or "").strip()
        return reply_to or getattr(getattr(self.config, "persona", None), "email", "") or ""

    async def prepare(self, item: dict, prospect) -> tuple[str, str]:
        subject, body = item.get("subject", ""), item.get("body", "")
        addr = self._physical_address
        if item.get("kind") != "sequence" or not addr or addr.lower() in body.lower():
            return subject, body
        base = (getattr(self._cfg, "unsubscribe_base_url", "") or "").strip().rstrip("/")
        if base and self.state is not None:
            token = await self.state.mint_unsub_token(prospect.id, item["id"])
            item["_unsubscribe_url"] = f"{base}/u/{token}"
        body = compliance.append_compliance_footer(
            body, addr, unsubscribe_url=item.get("_unsubscribe_url", ""),
            reply_to=self._from_address)
        return subject, body

    def content_gate(self, item: dict, subject: str, body: str, prospect) -> GateResult:
        result = pre_send_check(
            item["to_email"], subject, body,
            prospect=prospect,
            allow_risky=bool(getattr(self._cfg, "send_to_risky", False)),
            kind=item["kind"],
            enforce_compliance=self._enforce,
            physical_address=self._physical_address,
            allow_role=bool(getattr(self._cfg, "allow_role_addresses", False)),
        )
        if not is_assistant(self.config):
            return result
        # The assistant persona's hard rules, checked on what will actually be
        # sent (the footer, which carries its own links and address, excluded).
        problems = persona_guard.violations(
            body.split("\n\n--\n")[0], allowed_hosts=self._link_hosts())
        problems += persona_guard.violations(subject, allowed_hosts=self._link_hosts())
        if problems:
            return GateResult(ok=False, reasons=result.reasons + [f"persona: {p}" for p in problems])
        return result

    def _link_hosts(self) -> tuple[str, ...]:
        hosts = list(persona_guard.ALLOWED_LINK_HOSTS)
        base = (getattr(self._cfg, "unsubscribe_base_url", "") or "").split("//")[-1].split("/")[0]
        return tuple(hosts + ([base] if base else []))

    async def pre_send(self, state, item: dict, prospect) -> GateResult:
        """A follow-up goes out only after the step before it was really sent, and
        keeps the gap the sequence was written with (day 0 / 3 / 7), measured from
        when that earlier step actually went. Staged send times are fixed at write
        time, so without this a sequence approved late would collapse into
        back-to-back emails, or a follow-up could go to someone who never got the
        first email."""
        step = int(item.get("step") or 1)
        if item.get("kind") != "sequence" or step <= 1 or not item.get("campaign_id"):
            return GateResult(ok=True)
        previous = await state.get_sequence_item(item["campaign_id"], item["prospect_id"], step - 1)
        if previous is None:
            return GateResult(ok=True)
        if previous["status"] != "sent":
            return GateResult(ok=False, defer=True, reasons=[
                f"waiting for step {step - 1} to be sent (it is {previous['status']})"])
        gap = _parse_ts(item.get("send_at")) - _parse_ts(previous.get("send_at"))
        earliest = _parse_ts(previous.get("sent_at")) + max(gap, _ZERO)
        if _now_utc() < earliest:
            return GateResult(ok=False, defer=True, reasons=[
                f"step {step} waits until {earliest:%Y-%m-%d %H:%M} UTC (gap kept from step {step - 1})"])
        return GateResult(ok=True)

    async def breaker_reason(self, state) -> str:
        domain = self.sender_domain()
        if domain:
            reason = compliance.domain_breaker_reason(
                await state.count_outcomes_today("email", "hard_bounce", domain),
                await state.count_outcomes_today("email", "complaint", domain),
            )
            if reason:
                return f"{domain}: {reason}"
        sent, bad = await state.recent_send_health(
            "email", compliance.GLOBAL_BREAKER_WINDOW)
        return compliance.global_breaker_reason(sent, bad)

    async def send(self, item: dict, subject: str, body: str) -> SendResult:
        kwargs = {}
        url = item.get("_unsubscribe_url", "")
        if url and self._from_address and getattr(self.provider, "supports_headers", False):
            kwargs["headers"] = compliance.build_compliance_headers(
                url, self._from_address,
                reply_to=getattr(self._cfg, "reply_to", "") or "")
        return await self.provider.send_email(
            item["to_email"], subject, body,
            thread_ref=item.get("thread_ref", ""),
            in_reply_to=item.get("in_reply_to", ""),
            **kwargs,
        )

    async def poll_inbound(self) -> list[InboundMessage]:
        return await self.provider.get_replies()
