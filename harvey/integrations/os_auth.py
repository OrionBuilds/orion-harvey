"""Harvey's own sign-in to Orion, the business OS (a Supabase project).

Harvey is a user of the OS like CLARA and FRIDAY, with an identity of its own:
it never borrows the owner's session. The session file holds one long-lived refresh
token that Supabase ROTATES on every use, so each refresh must be persisted
before the new access token is handed out, and refreshes are serialized with a
file lock across the run loop, the dashboard and the CLI.

Session file: ~/.agentic-os/harvey/os-session.json (dir 0700, file 0600), the
same place and shape the other agents use. ``expected_email`` pins the identity:
a session for any other OS user is refused rather than silently adopted.
"""

import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

try:  # POSIX only; Harvey's supported platforms are macOS and Linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

logger = logging.getLogger("harvey.osauth")

SESSION_FILE = Path.home() / ".agentic-os" / "harvey" / "os-session.json"
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
EXPIRY_MARGIN = 60   # refresh this many seconds before the access token expires


class OsAuthError(RuntimeError):
    """Not configured, not signed in, wrong identity, or the OS rejected the sign-in."""


class OsAuth:
    def __init__(self, url: str, anon_key: str, expected_email: str = "", *,
                 session_file: Path | None = None, transport: httpx.BaseTransport | None = None):
        self.url = (url or "").strip().rstrip("/")
        self.anon_key = (anon_key or "").strip()
        self.expected_email = (expected_email or "").strip().lower()
        self.session_file = Path(session_file) if session_file else SESSION_FILE
        self._transport = transport
        self._mem_lock = threading.Lock()

    @classmethod
    def from_env(cls, env, **kwargs) -> "OsAuth":
        return cls(getattr(env, "os_supabase_url", ""), getattr(env, "os_supabase_anon_key", ""),
                   getattr(env, "os_expected_email", ""), **kwargs)

    # ── state ──

    def is_configured(self) -> bool:
        return bool(self.url and self.anon_key)

    def _read(self) -> dict | None:
        try:
            data = json.loads(self.session_file.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read the OS session file: {e}")
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def _write(self, data: dict) -> None:
        """Atomic write, owner-only from the first byte (the file holds a refresh token)."""
        self.session_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.session_file.parent, prefix=".os-session.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.session_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def is_authorized(self) -> bool:
        return self.is_configured() and self._read() is not None

    def email(self) -> str:
        return (self._read() or {}).get("email", "")

    def logout(self) -> bool:
        try:
            self.session_file.unlink()
            return True
        except FileNotFoundError:
            return False

    @contextmanager
    def _file_lock(self):
        if fcntl is None:  # pragma: no cover
            yield
            return
        self.session_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.session_file.with_suffix(".lock"), "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ── Supabase auth endpoints ──

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"apikey": self.anon_key, **kwargs.pop("headers", {})}
        try:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport) as client:
                return client.request(method, f"{self.url}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise OsAuthError(f"Could not reach the OS: {e}") from e

    @staticmethod
    def _reason(resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}"
        if isinstance(body, dict):
            msg = body.get("msg") or body.get("error_description") or body.get("message") or body.get("error")
            if msg:
                return str(msg)[:240]
        return f"HTTP {resp.status_code}"

    def _check_identity(self, email: str) -> None:
        if self.expected_email and (email or "").lower() != self.expected_email:
            raise OsAuthError(
                f"Refusing to sign in as {email or 'an unknown user'}: Harvey's OS identity is "
                f"{self.expected_email}.")

    def _store(self, body: dict, previous: dict | None = None) -> dict:
        previous = previous or {}
        email = ((body.get("user") or {}).get("email") or previous.get("email") or "").lower()
        self._check_identity(email)
        record = {
            "refresh_token": body.get("refresh_token") or previous.get("refresh_token", ""),
            "access_token": body.get("access_token", ""),
            "expires_at": float(body.get("expires_at") or time.time() + float(body.get("expires_in", 3600))),
            "email": email,
            "user_id": (body.get("user") or {}).get("id") or previous.get("user_id", ""),
        }
        if not record["refresh_token"]:
            raise OsAuthError("The OS returned no refresh token.")
        self._write(record)
        return record

    # ── access token (used by every OS call) ──

    def access_token(self, *, force_refresh: bool = False) -> str:
        """A valid access token, refreshing under a cross-process lock when needed."""
        if not self.is_configured():
            raise OsAuthError("ORION_SUPABASE_URL / ORION_SUPABASE_ANON_KEY are not set in .env.")
        with self._mem_lock, self._file_lock():
            stored = self._read()   # re-read inside the lock: another process may have refreshed
            if not stored:
                raise OsAuthError("Harvey is not signed in to the OS. Run 'harvey os login'.")
            if (not force_refresh and stored.get("access_token")
                    and time.time() < float(stored.get("expires_at", 0)) - EXPIRY_MARGIN):
                return stored["access_token"]
            resp = self._request("POST", "/auth/v1/token", params={"grant_type": "refresh_token"},
                                 json={"refresh_token": stored["refresh_token"]})
            if resp.status_code != 200:
                reason = self._reason(resp)
                if resp.status_code in (400, 401):
                    raise OsAuthError(f"The OS rejected Harvey's saved sign-in ({reason}). "
                                      "Run 'harvey os login' again.")
                raise OsAuthError(f"OS token refresh failed ({resp.status_code}): {reason}")
            return self._store(resp.json(), stored)["access_token"]

    async def aaccess_token(self, *, force_refresh: bool = False) -> str:
        import asyncio
        return await asyncio.to_thread(self.access_token, force_refresh=force_refresh)

    # ── one-time sign-in ──

    def login_password(self, email: str, password: str) -> dict:
        """Sign in with the agent account's email and password (one time; only the rotating
        refresh token is kept, never the password)."""
        if not self.is_configured():
            raise OsAuthError("ORION_SUPABASE_URL / ORION_SUPABASE_ANON_KEY are not set in .env.")
        resp = self._request("POST", "/auth/v1/token", params={"grant_type": "password"},
                             json={"email": email, "password": password})
        if resp.status_code != 200:
            raise OsAuthError(f"The OS refused the sign-in: {self._reason(resp)}")
        with self._mem_lock, self._file_lock():
            return self._store(resp.json())

    def login_refresh_token(self, refresh_token: str) -> dict:
        """Adopt a refresh token minted elsewhere (e.g. captured from a browser sign-in)."""
        if not self.is_configured():
            raise OsAuthError("ORION_SUPABASE_URL / ORION_SUPABASE_ANON_KEY are not set in .env.")
        resp = self._request("POST", "/auth/v1/token", params={"grant_type": "refresh_token"},
                             json={"refresh_token": refresh_token.strip()})
        if resp.status_code != 200:
            raise OsAuthError(f"The OS refused that token: {self._reason(resp)}")
        with self._mem_lock, self._file_lock():
            return self._store(resp.json())
