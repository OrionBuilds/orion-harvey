"""Resend mail provider: send over the Resend API, read replies over IMAP.

Resend only sends. Replies go to the address mail is sent from, so that
(sub)domain's MX must point at a real mailbox (Microsoft 365 for this
install) and Harvey reads that mailbox over IMAP, exactly like the SMTP
provider. Bounces and spam complaints for mail Resend sent do not come back
to that mailbox: Resend reports them by webhook (see harvey.webhooks).

Replies are read from the reply mailbox in one of two ways: over IMAP when
IMAP_* is set, otherwise through Microsoft Graph when ``harvey microsoft login``
has been run (no password, and Microsoft 365 usually has IMAP basic auth off).
In the Graph case ``channels.email.reply_to`` must be the signed-in mailbox.

.env:
    RESEND_API_KEY=          RESEND_WEBHOOK_SECRET=   (whsec_..., from the webhook page)
    IMAP_HOST= IMAP_PORT=993 IMAP_USERNAME= IMAP_PASSWORD=   (optional; the reply mailbox)

harvey.yaml: channels.email.sender_domain (the verified Resend domain),
reply_to (optional override), persona.email (the From address).
"""

import logging
from email.utils import make_msgid

import httpx

from harvey.integrations.imap_reader import ImapReader
from harvey.integrations.mail_provider import InboundMessage, MailProvider, SendResult

logger = logging.getLogger("harvey.resend")

API_BASE = "https://api.resend.com"
TIMEOUT = 30


class ResendMailProvider(MailProvider):
    name = "resend"
    supports_headers = True

    def __init__(self, config, env):
        self.config = config
        self.api_key = getattr(env, "resend_api_key", "")
        self.reader = ImapReader(
            getattr(env, "imap_host", ""),
            getattr(env, "imap_port", 993),
            getattr(env, "imap_username", ""),
            getattr(env, "imap_password", ""),
        )
        # Graph reads the mailbox when IMAP isn't set up. It reads the Reply-To mailbox,
        # which is not the From address.
        self.graph_reader = None
        if not self.reader.is_configured():
            from harvey.integrations.ms_mail import MsGraphMailProvider
            reply_to = (getattr(self._email_cfg, "reply_to", "") or "").strip()
            candidate = MsGraphMailProvider(config, env, mailbox=reply_to)
            if reply_to and candidate.auth.is_configured() and candidate.auth.is_authorized() \
                    and candidate.auth.has_feature("mail"):
                self.graph_reader = candidate

    @property
    def _email_cfg(self):
        return self.config.channels.email

    @property
    def from_address(self) -> str:
        return getattr(self.config.persona, "email", "") or ""

    @property
    def sender_domain(self) -> str:
        domain = (getattr(self._email_cfg, "sender_domain", "") or "").strip().lower()
        if domain:
            return domain
        return self.from_address.rsplit("@", 1)[-1].lower() if "@" in self.from_address else ""

    def is_configured(self) -> bool:
        # IMAP is required too: without it replies can't be read, and sending
        # without reading replies would break stop-on-reply.
        return bool(self.api_key and self.from_address
                    and (self.reader.is_configured() or self.graph_reader is not None))

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers: dict | None = None,
    ) -> SendResult:
        persona = self.config.persona
        message_id = make_msgid(domain=self.sender_domain or "localhost")
        out_headers = {**(headers or {}), "Message-ID": message_id}
        if in_reply_to:
            out_headers["In-Reply-To"] = in_reply_to
            out_headers["References"] = in_reply_to
        # Reply-To belongs in the dedicated field, not the headers map.
        reply_to = out_headers.pop("Reply-To", "") or getattr(self._email_cfg, "reply_to", "")

        payload = {
            "from": f"{persona.name} <{self.from_address}>",
            "to": [to_email],
            "subject": subject,
            "text": body,
            "headers": out_headers,
        }
        if reply_to:
            payload["reply_to"] = reply_to

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/emails", json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as e:
            logger.error(f"Resend request failed: {e}")
            return SendResult(ok=False, error=f"resend request failed: {e}"[:300])

        if resp.status_code >= 400:
            detail = resp.text[:200]
            logger.error(f"Resend {resp.status_code} sending to {to_email}: {detail}")
            return SendResult(ok=False, error=f"resend {resp.status_code}: {detail}")
        try:
            resend_id = resp.json().get("id", "")
        except ValueError:
            resend_id = ""
        if not resend_id:
            return SendResult(ok=False, error="resend accepted the request but returned no id")
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id,
                          provider_id=resend_id)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        if self.graph_reader is not None:
            return await self.graph_reader.get_replies(limit)
        return await self.reader.get_replies(limit)

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, ("RESEND_API_KEY and persona.email are required, plus a way to read "
                           "replies: IMAP_* in .env, or `harvey microsoft login` with "
                           "channels.email.reply_to set to the signed-in mailbox")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{API_BASE}/domains",
                    headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as e:
            return False, f"Resend unreachable: {str(e)[:200]}"
        restricted = resp.status_code in (401, 403) and "restricted" in resp.text.lower()
        if resp.status_code >= 400 and not restricted:
            return False, f"Resend rejected the API key ({resp.status_code})"
        if not restricted:
            domains = (resp.json() or {}).get("data", [])
            match = next((d for d in domains if d.get("name", "").lower() == self.sender_domain), None)
            if match is None:
                return False, f"domain '{self.sender_domain}' is not added in Resend"
            if match.get("status") != "verified":
                return False, f"domain '{self.sender_domain}' is '{match.get('status')}', not verified (check SPF/DKIM)"
        # A sending-only key (the safer choice) can't list domains: its validity is shown by
        # Resend answering "restricted" instead of "invalid", and domain status by the dashboard.
        if self.graph_reader is not None:
            try:
                await self.graph_reader.get_replies(limit=1)
            except Exception as e:
                return False, f"Resend OK but the Microsoft inbox could not be read: {str(e)[:200]}"
            checked = ("key is sending-only (domain status not checkable here)" if restricted
                       else f"domain {self.sender_domain} verified")
            return True, f"Resend {checked}; replies read from {self.graph_reader.mailbox} via Microsoft Graph"
        try:
            import asyncio
            await asyncio.get_event_loop().run_in_executor(None, self.reader.check_login)
        except Exception as e:
            return False, f"Resend OK but IMAP login failed: {str(e)[:200]}"
        return True, f"Resend domain {self.sender_domain} verified; IMAP connected as {self.reader.user}"
