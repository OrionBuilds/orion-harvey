"""A small PostgREST / RPC client for Orion, under Harvey's own JWT.

Every call is made as the HarveyAgent user, so the OS's row-level security is the
authorization gate: Harvey can only see and write what a Harvey is allowed to.
One refresh-and-retry on a 401; transient failures (connection, 5xx) get two
retries; anything else raises OsError with the OS's own message.
"""

import asyncio
import logging

import httpx

from harvey.integrations.os_auth import OsAuth, OsAuthError

logger = logging.getLogger("harvey.os")

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
RETRIES = 2


class OsError(RuntimeError):
    """The OS refused a call or could not be reached."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class OsClient:
    def __init__(self, auth: OsAuth, *, transport: httpx.AsyncBaseTransport | None = None):
        self.auth = auth
        self._transport = transport
        self._whoami: dict | None = None

    async def _send(self, method: str, path: str, *, params=None, json=None, prefer: str = "",
                    _refreshed: bool = False):
        for attempt in range(RETRIES + 1):
            try:
                token = await self.auth.aaccess_token(force_refresh=_refreshed)
            except OsAuthError as e:
                raise OsError(str(e)) from e
            headers = {"apikey": self.auth.anon_key, "Authorization": f"Bearer {token}",
                       "Content-Type": "application/json"}
            if prefer:
                headers["Prefer"] = prefer
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                    resp = await client.request(method, f"{self.auth.url}{path}", params=params,
                                                json=json, headers=headers)
            except httpx.HTTPError as e:
                if attempt < RETRIES:
                    await asyncio.sleep(1 + attempt * 2)
                    continue
                raise OsError(f"Could not reach the OS: {e}") from e
            if resp.status_code == 401 and not _refreshed:
                return await self._send(method, path, params=params, json=json, prefer=prefer,
                                        _refreshed=True)
            if resp.status_code >= 500 and attempt < RETRIES:
                await asyncio.sleep(1 + attempt * 2)
                continue
            if resp.status_code >= 400:
                code, message = "", f"HTTP {resp.status_code}"
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        code = str(body.get("code", ""))
                        message = str(body.get("message") or body.get("error") or message)[:300]
                except ValueError:
                    pass
                raise OsError(message, resp.status_code, code)
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return None
        raise OsError("unreachable")  # pragma: no cover

    async def rpc(self, name: str, args: dict | None = None):
        return await self._send("POST", f"/rest/v1/rpc/{name}", json=args or {})

    async def select(self, table: str, params: dict | None = None) -> list[dict]:
        rows = await self._send("GET", f"/rest/v1/{table}", params=params or {})
        return rows if isinstance(rows, list) else []

    async def insert(self, table: str, row: dict, *, minimal: bool = False) -> dict | None:
        """minimal=True asks for nothing back: needed when the row will belong to someone else
        (e.g. a CRM contact owned by the owner), because reading it back would be refused."""
        rows = await self._send("POST", f"/rest/v1/{table}", json=row,
                                prefer="return=minimal" if minimal else "return=representation")
        return rows[0] if isinstance(rows, list) and rows else None

    async def update(self, table: str, filters: dict, patch: dict) -> list[dict]:
        rows = await self._send("PATCH", f"/rest/v1/{table}", params=filters, json=patch,
                                prefer="return=representation")
        return rows if isinstance(rows, list) else []

    async def whoami(self) -> dict:
        """Harvey's own identity (user_id, organization_id, role, agent_slug), cached."""
        if self._whoami is None:
            who = await self.rpc("agent_whoami")
            if not isinstance(who, dict) or who.get("agent_slug") != "harvey":
                raise OsError(
                    f"This OS user is not Harvey (role {who.get('role') if isinstance(who, dict) else '?'}). "
                    "Run harvey_provision() in the OS and sign in again.")
            self._whoami = who
        return self._whoami
