"""Mail provider abstraction — Harvey owns its sending, Instantly optional.

A MailProvider gives Harvey full inbox control: send, read replies,
thread. Two native providers ship:

- ``gmail``  — a Google Workspace / Gmail mailbox via the Gmail REST API
  (recommended: a real mailbox on a dedicated secondary domain is the
  most deliverable way to send <50 cold emails/day).
- ``m365``   — a Microsoft 365 mailbox over Microsoft Graph, signed in with
  OAuth (``harvey microsoft login``): no SMTP/IMAP passwords.
- ``smtp``   — any mailbox that speaks SMTP + IMAP (AgentMail, Fastmail,
  a Workspace app-mailbox, self-hosted). Configuration via .env.

``instantly`` remains supported through the legacy Sender/Handler path —
it isn't a MailProvider because Instantly owns sequencing/sending itself.
"""

import logging
from abc import ABC, abstractmethod
import re
from dataclasses import dataclass, field

logger = logging.getLogger("harvey.mail")

NATIVE_PROVIDERS = ("gmail", "smtp", "resend", "m365")


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""     # RFC Message-ID (or provider id) of the sent mail
    thread_ref: str = ""     # provider thread handle (Gmail threadId, etc.)
    error: str = ""
    provider_id: str = ""    # the provider's own id (Resend email id), for webhooks
    # True when the send was not attempted for a temporary reason (a tripped
    # breaker, an expired session): keep the item queued, don't mark it failed.
    defer: bool = False


@dataclass
class InboundMessage:
    provider_id: str         # provider-unique id, used for reply dedup
    from_email: str
    subject: str = ""
    body: str = ""
    message_id: str = ""     # RFC Message-ID header
    in_reply_to: str = ""    # RFC In-Reply-To header (bounce/thread matching)
    thread_ref: str = ""
    date: str = ""
    is_bounce: bool = False
    headers: dict = field(default_factory=dict)
    # Channel-neutral addressing: email providers leave these defaulted
    # (from_email is the address); LinkedIn/SMS set channel and from_ref
    # (profile URL / E.164 phone).
    channel: str = "email"
    from_ref: str = ""


BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem")
BOUNCE_SUBJECTS = (
    "delivery status notification", "undeliverable", "returned mail",
    "delivery failure", "failure notice", "could not be delivered",
    "delivery incomplete", "address not found",
)


def looks_like_bounce(from_email: str, subject: str) -> bool:
    f = (from_email or "").lower()
    s = (subject or "").lower()
    return any(m in f for m in BOUNCE_SENDERS) or any(m in s for m in BOUNCE_SUBJECTS)


_STATUS_CODE_RE = re.compile(r"(?<![\d.])([245])\.(\d{1,3})\.(\d{1,3})(?!\.?\d)")
_HARD_WORDS = ("user unknown", "unknown user", "no such user", "does not exist", "doesn't exist",
               "mailbox not found", "mailbox unavailable", "invalid recipient", "address rejected",
               "recipient not found", "couldn't be found", "could not be found", "no mailbox")
_POLICY_WORDS = ("access denied", "traffic not accepted", "blocked", "blacklist", "blocklist",
                 "reputation", "spam", "denied by policy", "not authorized", "not authorised",
                 "sender rejected", "dmarc", "spf", "dkim", "rate limit", "too many")
_SOFT_WORDS = ("mailbox full", "over quota", "quota exceeded", "try again later", "temporarily",
               "message too large", "out of office")


def classify_bounce(subject: str, body: str) -> str:
    """What a bounce means: "hard" (address is bad), "policy" (the receiving side refused
    OUR mail: IP/domain reputation or authentication, so the address may be fine),
    "soft" (temporary: full mailbox, deferral) or "unknown".

    A policy block must never mark a good address invalid; it is a sender-health
    signal instead. Hard evidence wins over policy wording: Exchange reports a
    nonexistent recipient as 5.4.1 "Recipient address rejected: Access denied"."""
    text = f"{subject}\n{body}".lower()
    codes = [(int(a), int(b), int(c)) for a, b, c in _STATUS_CODE_RE.findall(text)]
    failing = [c for c in codes if c[0] in (4, 5)]
    if any(c[0] == 5 and (c[1] == 1 or (c[1], c[2]) == (4, 1)) for c in failing) \
            or any(w in text for w in _HARD_WORDS):
        return "hard"
    if any(c[0] == 5 and c[1] == 7 for c in failing) or (
            failing and any(w in text for w in _POLICY_WORDS) and not any(c[0] == 4 for c in failing)):
        return "policy"
    if any(c[0] == 4 or (c[0] == 5 and c[1] == 2) for c in failing) or any(w in text for w in _SOFT_WORDS):
        return "soft"
    if any(w in text for w in _POLICY_WORDS):
        return "policy"
    return "unknown"


class MailProvider(ABC):
    """A mailbox Harvey can send from and read replies out of."""

    name: str = "base"
    supports_headers: bool = False

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether required credentials are present (no network)."""

    @abstractmethod
    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers: dict | None = None,
    ) -> SendResult:
        """Send one plain-text email. Reply threading when refs are given.

        ``headers`` (List-Unsubscribe etc.) is only passed to providers that
        set ``supports_headers``; the others never see the argument."""

    @abstractmethod
    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """Fetch recent inbound messages (dedup is the caller's job)."""

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Cheap end-to-end credential check. Returns (ok, detail)."""


def get_mail_provider(config, env) -> MailProvider | None:
    """Build the configured native provider, or None for instantly/unknown."""
    provider = (config.channels.email.provider or "").strip().lower()
    if provider == "gmail":
        from harvey.integrations.gmail import GmailProvider
        return GmailProvider(config, env)
    if provider == "smtp":
        from harvey.integrations.smtp_mail import SmtpImapProvider
        return SmtpImapProvider(config, env)
    if provider == "resend":
        from harvey.integrations.resend_mail import ResendMailProvider
        return ResendMailProvider(config, env)
    if provider == "m365":
        from harvey.integrations.ms_mail import MsGraphMailProvider
        return MsGraphMailProvider(config, env)
    return None
