"""Thin async Microsoft Graph client shared by mail, calendar and To Do.

Raw REST like the Gmail provider (no SDK). It adds the bearer token, refreshes
once on a 401, honours Retry-After on 429/503/504, and raises GraphError with
Graph's own error code so callers can tell "not found" from "forbidden".
"""

import asyncio
import logging

import httpx

from harvey.integrations.ms_auth import MicrosoftAuth, MicrosoftAuthError

logger = logging.getLogger("harvey.graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=45.0, pool=10.0)
RETRY_STATUSES = (429, 503, 504)
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 30.0


class GraphError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, path: str = ""):
        super().__init__(f"Graph {status} {code} on {path}: {message}".strip())
        self.status, self.code, self.message, self.path = status, code, message, path


class GraphClient:
    def __init__(self, auth: MicrosoftAuth, *, transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep):
        self.auth = auth
        self._transport = transport
        self._sleep = sleep

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: dict | None = None, content: bytes | str | None = None,
                      headers: dict | None = None) -> dict:
        """One Graph call. Returns the decoded JSON body ({} for 202/204)."""
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        refreshed = False
        force = False
        attempt = 0
        while True:
            attempt += 1
            token = await self.auth.aaccess_token(force_refresh=force)
            force = False
            req_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                    resp = await client.request(method, url, params=params, json=json,
                                                content=content, headers=req_headers)
            except httpx.HTTPError as e:
                raise GraphError(0, "network", str(e)[:200], path) from e

            if resp.status_code == 401 and not refreshed:
                refreshed = force = True   # the cached access token was revoked or stale
                continue
            if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                wait = min(self._retry_after(resp), MAX_RETRY_WAIT)
                logger.warning(f"Graph {resp.status_code} on {path}; retrying in {wait:.0f}s")
                await self._sleep(wait)
                continue
            if resp.status_code >= 400:
                raise self._error(resp, path)
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {}

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        try:
            return max(1.0, float(resp.headers.get("Retry-After", "2")))
        except ValueError:
            return 2.0

    @staticmethod
    def _error(resp: httpx.Response, path: str) -> GraphError:
        try:
            err = (resp.json() or {}).get("error") or {}
        except ValueError:
            err = {}
        return GraphError(resp.status_code, err.get("code", "") or "error",
                          (err.get("message") or resp.text or "")[:240], path)

    async def get_all(self, path: str, *, params: dict | None = None, limit: int = 100,
                      headers: dict | None = None) -> list[dict]:
        """Follow @odata.nextLink until `limit` items are collected."""
        items: list[dict] = []
        page = await self.request("GET", path, params=params, headers=headers)
        while True:
            items.extend(page.get("value", []))
            nxt = page.get("@odata.nextLink")
            if not nxt or len(items) >= limit:
                return items[:limit]
            page = await self.request("GET", nxt, headers=headers)


def build_graph(env, **kwargs) -> GraphClient | None:
    """A Graph client when MS_CLIENT_ID is set, else None. Does not check sign-in."""
    auth = MicrosoftAuth.from_env(env)
    return GraphClient(auth, **kwargs) if auth.is_configured() else None


__all__ = ["GraphClient", "GraphError", "MicrosoftAuthError", "build_graph"]
