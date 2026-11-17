"""Generic SMTP + IMAP mailbox provider.

Works with any real mailbox: AgentMail (their IMAP/SMTP relay), Fastmail,
a Google Workspace account with an app password, or self-hosted mail.

.env:
    SMTP_HOST=            IMAP_HOST=
    SMTP_PORT=587         IMAP_PORT=993
    SMTP_USERNAME=        IMAP_USERNAME=   (defaults to SMTP_USERNAME)
    SMTP_PASSWORD=        IMAP_PASSWORD=   (defaults to SMTP_PASSWORD)

Sends as the configured persona (name + email). IMAP polling fetches
UNSEEN inbox messages and marks them seen after retrieval.
"""

import asyncio
import logging
from email.message import EmailMessage
from email.utils import make_msgid

import aiosmtplib

from harvey.integrations.imap_reader import ImapReader
from harvey.integrations.mail_provider import InboundMessage, MailProvider, SendResult

logger = logging.getLogger("harvey.smtp")


class SmtpImapProvider(MailProvider):
    name = "smtp"
    supports_headers = True

    def __init__(self, config, env):
        self.config = config
        self.smtp_host = getattr(env, "smtp_host", "")
        self.smtp_port = int(getattr(env, "smtp_port", 587) or 587)
        self.smtp_user = getattr(env, "smtp_username", "")
        self.smtp_pass = getattr(env, "smtp_password", "")
        self.imap_host = getattr(env, "imap_host", "") or self.smtp_host
        self.imap_port = int(getattr(env, "imap_port", 993) or 993)
        self.imap_user = getattr(env, "imap_username", "") or self.smtp_user
        self.imap_pass = getattr(env, "imap_password", "") or self.smtp_pass

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

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
        msg = EmailMessage()
        for key, value in (headers or {}).items():
            msg[key] = value
        msg["To"] = to_email
        msg["From"] = f"{persona.name} <{persona.email or self.smtp_user}>"
        msg["Subject"] = subject
        message_id = make_msgid(domain=(persona.email or self.smtp_user).split("@")[-1])
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_pass,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465),
                timeout=30,
            )
        except Exception as e:
            logger.error(f"SMTP send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300])
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id)

    @property
    def reader(self) -> ImapReader:
        return ImapReader(self.imap_host, self.imap_port, self.imap_user, self.imap_pass)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        return await self.reader.get_replies(limit)

    def _fetch_unseen(self, limit: int) -> list[InboundMessage]:
        return self.reader.fetch_unseen(limit)

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD missing from .env"
        # SMTP handshake
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.smtp_host, port=self.smtp_port,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465), timeout=15,
            )
            await smtp.connect()
            await smtp.login(self.smtp_user, self.smtp_pass)
            await smtp.quit()
        except Exception as e:
            return False, f"SMTP login failed: {str(e)[:200]}"
        # IMAP handshake
        loop = asyncio.get_event_loop()
        _imap_check = self.reader.check_login

        try:
            await loop.run_in_executor(None, _imap_check)
        except Exception as e:
            return False, f"SMTP OK but IMAP login failed: {str(e)[:200]}"
        return True, f"SMTP + IMAP connected as {self.smtp_user}"
