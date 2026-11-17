"""IMAP inbox reading shared by every provider that sends without owning a
mailbox of its own (SMTP, Resend). Raises on any failure, never returns an
empty list for an unreadable inbox: see MailProvider.get_replies.
"""

import asyncio
import email
import email.policy
import imaplib
import logging
import re
from email.utils import parseaddr

from harvey.integrations.mail_provider import InboundMessage, looks_like_bounce

logger = logging.getLogger("harvey.imap")


class ImapReader:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.host, self.port, self.user, self.password = host, int(port or 993), user, password

    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self.fetch_unseen, limit)
        except Exception as e:
            logger.error(f"IMAP fetch failed: {e}")
            raise

    def fetch_unseen(self, limit: int) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.user, self.password)
            conn.select("INBOX")
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                raise RuntimeError(f"IMAP search failed: {status}")
            for msg_id in data[0].split()[-limit:]:
                status, parts = conn.fetch(msg_id, "(RFC822)")
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                parsed = self.parse_rfc822(parts[0][1])
                if parsed:
                    out.append(parsed)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    def check_login(self) -> None:
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.user, self.password)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    @staticmethod
    def parse_rfc822(raw: bytes) -> InboundMessage | None:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
        from_email = parseaddr(str(msg.get("From", "")))[1].lower()
        if not from_email:
            return None
        subject = str(msg.get("Subject", ""))

        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    body = re.sub(r"<[^>]+>", " ", body)
        except Exception:
            pass

        message_id = str(msg.get("Message-ID", "")).strip()
        return InboundMessage(
            provider_id=message_id or f"{from_email}:{subject}",
            from_email=from_email,
            subject=subject,
            body=(body or "")[:5000],
            message_id=message_id,
            in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
            thread_ref=str(msg.get("In-Reply-To", "")).strip(),
            date=str(msg.get("Date", "")),
            is_bounce=looks_like_bounce(from_email, subject),
        )
