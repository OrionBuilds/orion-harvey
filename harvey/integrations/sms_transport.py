"""How a text actually leaves and arrives. Twilio's REST API, nothing else.

The channel (harvey/channels/sms.py) owns every rule; a transport only moves bytes,
so tests and a future OS-routed sender can swap in without touching the rules.
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import httpx

from harvey.integrations.mail_provider import InboundMessage, SendResult

logger = logging.getLogger("harvey.sms")

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
API = "https://api.twilio.com/2010-04-01"


class SmsTransport(ABC):
    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    async def send(self, to: str, body: str) -> SendResult: ...

    @abstractmethod
    async def poll(self, since: datetime | None) -> list[InboundMessage]:
        """Texts received since ``since``. MUST raise on failure (a failed read is not "no replies")."""


class TwilioTransport(SmsTransport):
    def __init__(self, account_sid: str, auth_token: str, from_number: str, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.sid, self.token, self.from_number = account_sid, auth_token, from_number
        self._transport = transport

    def is_configured(self) -> bool:
        return bool(self.sid and self.token and self.from_number)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, auth=(self.sid, self.token), transport=self._transport)

    async def send(self, to: str, body: str) -> SendResult:
        try:
            async with self._client() as client:
                resp = await client.post(f"{API}/Accounts/{self.sid}/Messages.json",
                                         data={"To": to, "From": self.from_number, "Body": body})
        except httpx.HTTPError as e:
            return SendResult(ok=False, error=f"Twilio unreachable: {e}", defer=True)
        if resp.status_code in (200, 201):
            return SendResult(ok=True, message_id=resp.json().get("sid", ""))
        try:
            detail = resp.json().get("message", "")
        except ValueError:
            detail = ""
        # 5xx and rate limits are worth retrying later; a 4xx is a refusal of this message.
        return SendResult(ok=False, error=f"Twilio {resp.status_code}: {detail}"[:300],
                          defer=resp.status_code >= 500 or resp.status_code == 429)

    async def poll(self, since: datetime | None) -> list[InboundMessage]:
        params = {"To": self.from_number, "PageSize": "50"}
        async with self._client() as client:
            resp = await client.get(f"{API}/Accounts/{self.sid}/Messages.json", params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"Twilio inbox read failed ({resp.status_code})")
        out = []
        for m in resp.json().get("messages", []):
            if m.get("direction") != "inbound":
                continue
            sent = _parse_rfc2822(m.get("date_sent") or m.get("date_created"))
            if since and sent and sent < since:
                continue
            out.append(InboundMessage(
                provider_id=m["sid"], from_email="", body=m.get("body", ""), channel="sms",
                from_ref=m.get("from", ""), date=sent.isoformat() if sent else ""))
        return out


def _parse_rfc2822(value: str | None) -> datetime | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


class OsSmsTransport(SmsTransport):
    """Texts go out through the OS (the `twilio-sms-send` function) under Harvey's own sign-in,
    from a number the organization already owns (Clara's). The OS logs and bills it; Harvey
    adds nothing but the message.

    Receiving is NOT possible from here: inbound texts to that number are filed and answered by
    the OS, and the OS hides those messages from Harvey. So `poll` returns nothing, and the
    channel refuses to run until `channels.sms.inbound_ready` says the OS hands Harvey's
    threads (including STOP) to him. Never flip that flag without that route existing."""

    def __init__(self, client, from_number: str):
        self.client, self.from_number = client, from_number

    def is_configured(self) -> bool:
        return bool(self.from_number) and self.client.auth.is_authorized()

    async def send(self, to: str, body: str) -> SendResult:
        from harvey.integrations.os_client import OsError
        try:
            who = await self.client.whoami()
            reply = await self.client._send("POST", "/functions/v1/twilio-sms-send", json={
                "orgId": who["organization_id"], "toNumber": to, "body": body,
                "fromNumber": self.from_number, "metadata": {"source": "harvey"}})
        except OsError as e:
            # 5xx / unreachable: try again later. A 4xx is the OS refusing this message.
            return SendResult(ok=False, error=f"OS SMS: {e}"[:300], defer=e.status == 0 or e.status >= 500)
        if isinstance(reply, dict) and reply.get("ok"):
            return SendResult(ok=True, message_id=str(reply.get("sid") or ""))
        return SendResult(ok=False, error=f"OS SMS refused: {(reply or {}).get('error', 'no detail')}"[:300])

    async def poll(self, since: datetime | None) -> list[InboundMessage]:
        return []

    async def suppressed(self, phone: str) -> bool:
        """The OS's shared do-not-contact list (STOPs, imported DNC, manual). Raises if unreachable."""
        res = await self.client.rpc("suppression_check", {"p_channel": "sms", "p_phone": phone})
        return bool(isinstance(res, dict) and res.get("blocked"))
