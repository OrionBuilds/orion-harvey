"""Outbound channels (email, LinkedIn, SMS) behind one interface.

A Channel supplies the channel-specific parts of sending — its caps, its
content rules, how a message actually goes out, how replies come back. The
shared skeleton (pause check, cap, gates, ledger, pacing, stop-on-reply) is
``harvey.channels.drain.drain_channel``, so every channel gets the same
safety behavior and a new channel only has to implement these hooks.
"""

from abc import ABC, abstractmethod

from harvey import compliance
from harvey.gate import GateResult
from harvey.integrations.mail_provider import InboundMessage, SendResult

__all__ = ["Channel", "GateResult", "InboundMessage", "SendResult", "pause_key_for"]

# The email key predates channels and is what the dashboard and CLI toggle.
_PAUSE_KEYS = {"email": "sending_paused"}


def pause_key_for(channel_name: str) -> str:
    return _PAUSE_KEYS.get(channel_name, f"{channel_name}_paused")


class Channel(ABC):
    name: str = "base"
    # True: a tripped breaker pauses the channel until a human resumes it (email
    # bounces need a look at list quality). False: the breaker is time-boxed and
    # lifts itself, so it must not leave a manual pause behind.
    pauses_on_breaker: bool = True

    @property
    def pause_key(self) -> str:
        return pause_key_for(self.name)

    @abstractmethod
    def is_configured(self) -> bool:
        """Credentials/prereqs present (no network)."""

    @abstractmethod
    def daily_cap(self) -> int:
        """Maximum sends per local business day on this channel."""

    def max_per_cycle(self) -> int:
        """Maximum sends drained per heartbeat, to spread volume."""
        return 8

    def pacing(self) -> tuple[float, float] | None:
        """(min, max) seconds to wait between sends, or None for no pause."""
        return None

    def sender_domain(self) -> str:
        """The sending identity's domain, for per-domain breakers. '' if n/a."""
        return ""

    def quotas(self) -> "compliance.EmailQuotas | None":
        """Per-contact/company frequency limits, or None to skip them."""
        return None

    async def prepare(self, item: dict, prospect) -> tuple[str, str]:
        """Final (subject, body) to gate and send: footers etc. are added here."""
        return item.get("subject", ""), item.get("body", "")

    def content_gate(self, item: dict, subject: str, body: str, prospect) -> GateResult:
        """Deterministic, channel-specific content checks."""
        return GateResult(ok=True)

    async def breaker_reason(self, state) -> str:
        """Non-empty when delivery health says this channel must pause."""
        return ""

    async def pre_send(self, state, item: dict, prospect) -> GateResult:
        """Channel rules that need state or the clock (rate windows, relationship
        state). A result with ``defer=True`` leaves the item queued for a later
        cycle; ``ok=False`` without it cancels the item."""
        return GateResult(ok=True)

    async def after_send(self, state, item: dict, prospect) -> None:
        """Bookkeeping once a message has actually gone out."""

    async def close(self) -> None:
        """Release anything held open across a drain (a browser session)."""

    @abstractmethod
    async def send(self, item: dict, subject: str, body: str) -> SendResult:
        """Deliver one prepared message."""

    @abstractmethod
    async def poll_inbound(self) -> list[InboundMessage]:
        """Fetch new inbound messages. MUST raise on failure: a failed read
        returned as an empty list would let sends proceed without having
        checked for replies."""
