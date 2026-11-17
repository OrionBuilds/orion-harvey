"""Microsoft 365 mailbox provider over Microsoft Graph (no SMTP AUTH, no IMAP).

Sends and reads as the signed-in mailbox, so there is no password to store and
no dependence on basic-auth IMAP, which Microsoft is retiring. Sign in once with
``harvey microsoft login``; see ms_auth.py for the Entra app registration.

Sending goes draft-first: the message is created from MIME (so List-Unsubscribe
and threading headers survive, which Graph's JSON sendMail would drop), the
draft's ``internetMessageId`` is read back (that is the id replies and bounces
will reference), and only then is the draft sent. The sender is always the
signed-in mailbox, so ``persona.email`` must be that mailbox's primary address.
"""

import base64
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from harvey.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)
from harvey.integrations.ms_auth import MicrosoftAuth, MicrosoftAuthError
from harvey.integrations.ms_graph import GraphClient, GraphError

logger = logging.getLogger("harvey.m365")

REPLY_WINDOW_DAYS = 7
INBOX_SELECT = ("id,subject,from,receivedDateTime,internetMessageId,conversationId,"
                "body,bodyPreview,internetMessageHeaders")


class MsGraphMailProvider(MailProvider):
    name = "m365"
    supports_headers = True

    def __init__(self, config, env, *, graph: GraphClient | None = None, mailbox: str = ""):
        self.config = config
        self.auth = graph.auth if graph else MicrosoftAuth.from_env(env)
        self.graph = graph or GraphClient(self.auth)
        # Only set when this provider just READS a mailbox that differs from the From
        # address (Resend sends as harvey@..., replies land in the signed-in mailbox).
        self.mailbox = (mailbox or "").strip()

    @property
    def from_address(self) -> str:
        return (getattr(self.config.persona, "email", "") or "").strip()

    def is_configured(self) -> bool:
        return bool(self.auth.is_configured() and self.auth.is_authorized()
                    and self.auth.has_feature("mail") and self.from_address)

    def _mime(self, to_email, subject, body, in_reply_to, headers) -> bytes:
        msg = EmailMessage()
        for key, value in (headers or {}).items():
            msg[key] = value
        msg["From"] = f"{self.config.persona.name} <{self.from_address}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=self.from_address.rsplit("@", 1)[-1] or "localhost")
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        return msg.as_bytes()

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers: dict | None = None,
    ) -> SendResult:
        try:
            mime = self._mime(to_email, subject, body, in_reply_to, headers)
        except (ValueError, TypeError) as e:
            return SendResult(ok=False, error=f"could not build the message: {e}"[:300])

        try:
            draft = await self.graph.request(
                "POST", "/me/messages", content=base64.b64encode(mime),
                headers={"Content-Type": "text/plain"})
        except (GraphError, MicrosoftAuthError) as e:
            return self._failed(e, "create")
        draft_id = draft.get("id", "")
        if not draft_id:
            return SendResult(ok=False, error="graph created no draft")
        message_id = draft.get("internetMessageId", "")

        try:
            await self.graph.request("POST", f"/me/messages/{draft_id}/send")
        except (GraphError, MicrosoftAuthError) as e:
            await self._discard(draft_id)
            return self._failed(e, "send")
        return SendResult(ok=True, message_id=message_id or draft_id,
                          thread_ref=draft.get("conversationId", "") or message_id,
                          provider_id=draft_id)

    def _failed(self, err: Exception, stage: str) -> SendResult:
        logger.error(f"Microsoft Graph {stage} failed: {err}")
        # Keep the item queued (don't mark it failed) only when the mail certainly did not
        # leave: no sign-in, throttled, or nothing but a draft created. A timeout or 5xx on
        # the send call itself is ambiguous, and retrying could deliver it twice.
        status = getattr(err, "status", 0)
        if isinstance(err, MicrosoftAuthError) or status == 429:
            temporary = True
        else:
            temporary = stage == "create" and status in (0, 503, 504)
        return SendResult(ok=False, error=f"m365 {stage}: {err}"[:300], defer=temporary)

    async def _discard(self, draft_id: str) -> None:
        """Don't leave an unsent draft behind in the mailbox."""
        try:
            await self.graph.request("DELETE", f"/me/messages/{draft_id}")
        except (GraphError, MicrosoftAuthError) as e:
            logger.warning(f"Could not delete unsent draft {draft_id}: {e}")

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        # Raise, never return []: an unreadable mailbox is not an empty one, and the
        # heartbeat must not send while replies may be unread.
        since = (datetime.now(timezone.utc) - timedelta(days=REPLY_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            rows = await self.graph.get_all(
                "/me/mailFolders/inbox/messages",
                params={"$filter": f"receivedDateTime ge {since}",
                        "$orderby": "receivedDateTime desc",
                        "$top": str(min(max(limit, 1), 100)),
                        "$select": INBOX_SELECT},
                headers={"Prefer": 'outlook.body-content-type="text"'},
                limit=limit)
        except (GraphError, MicrosoftAuthError) as e:
            raise RuntimeError(f"Microsoft 365 inbox could not be read: {e}") from e
        me = (self.mailbox or self.from_address).lower()
        out = []
        for row in rows:
            msg = self._parse(row)
            if msg and msg.from_email != me:
                out.append(msg)
        return out

    @staticmethod
    def _parse(row: dict) -> InboundMessage | None:
        sender = ((row.get("from") or {}).get("emailAddress") or {})
        from_email = (sender.get("address") or "").lower()
        if not from_email:
            return None
        headers = {h.get("name", "").lower(): h.get("value", "")
                   for h in row.get("internetMessageHeaders") or []}
        subject = row.get("subject") or ""
        body = (row.get("body") or {}).get("content") or row.get("bodyPreview") or ""
        message_id = row.get("internetMessageId", "")
        return InboundMessage(
            provider_id=row.get("id", "") or message_id,
            from_email=from_email,
            subject=subject,
            body=body[:5000],
            message_id=message_id,
            in_reply_to=headers.get("in-reply-to", ""),
            thread_ref=row.get("conversationId", ""),
            date=row.get("receivedDateTime", ""),
            is_bounce=looks_like_bounce(from_email, subject),
            headers=headers,
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.auth.is_configured():
            return False, "MS_CLIENT_ID is missing from .env"
        if not self.auth.is_authorized():
            return False, "Not signed in yet. Run 'harvey microsoft login'"
        if not self.auth.has_feature("mail"):
            return False, "Mail permission was not granted. Re-run 'harvey microsoft login' and accept Mail access"
        if not self.from_address:
            return False, "persona.email is not set in harvey.local.yaml"
        try:
            me = await self.graph.request("GET", "/me", params={"$select": "mail,userPrincipalName"})
            await self.graph.request("GET", "/me/mailFolders/inbox", params={"$select": "id"})
        except (GraphError, MicrosoftAuthError) as e:
            return False, f"Microsoft 365 check failed: {e}"
        signed_in = (me.get("mail") or me.get("userPrincipalName") or "").lower()
        if signed_in and signed_in != self.from_address.lower():
            return False, (f"Signed in as {signed_in}, but persona.email is {self.from_address}. "
                           "Graph sends as the signed-in mailbox: sign in as that address or change persona.email.")
        return True, f"Connected as {signed_in or self.from_address} (inbox readable)"
