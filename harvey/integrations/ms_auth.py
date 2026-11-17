"""Microsoft 365 sign-in (OAuth 2.0 authorization code + PKCE) for mail, calendar and To Do.

One delegated sign-in as the mailbox owner covers all three through Microsoft
Graph. There is no client secret: Harvey is registered as a public client
(Entra "Mobile and desktop applications" platform), so PKCE protects the code.

Setup (one-time, in the Entra admin center, entra.microsoft.com):
1. App registrations > New registration. Name "Harvey". Supported account
   types: this organizational directory only.
2. Authentication > Add a platform > Mobile and desktop applications. Add the
   redirect URI ``http://localhost``. (Any port is accepted at runtime.)
   For ``--device`` sign-in also turn on "Allow public client flows".
3. API permissions > Add > Microsoft Graph > Delegated: Mail.ReadWrite,
   Mail.Send, Calendars.ReadWrite, Tasks.ReadWrite, User.Read, offline_access.
   Grant admin consent if your tenant requires it.
4. Put the Application (client) ID and Directory (tenant) ID in .env as
   MS_CLIENT_ID and MS_TENANT_ID, then run ``harvey microsoft login``.

The refresh token is stored in data/ms_token.json (mode 600, gitignored).
Microsoft rotates it on every refresh, so refreshes are serialized with a file
lock across the run loop, the dashboard and the CLI.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from contextlib import contextmanager
from pathlib import Path

import httpx

from harvey.paths import PROJECT_ROOT

try:  # POSIX only; Harvey's supported platforms are macOS and Linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

logger = logging.getLogger("harvey.msauth")

TOKEN_FILE = PROJECT_ROOT / "data" / "ms_token.json"
AUTHORITY = "https://login.microsoftonline.com"
DEFAULT_TENANT = "organizations"

# What each Harvey feature needs. Granted scopes are read back from the token
# response, so a feature whose scope was not consented degrades on its own.
SCOPES = (
    "offline_access",
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Tasks.ReadWrite",
)
FEATURE_SCOPES = {
    "mail": ("Mail.ReadWrite", "Mail.Send"),
    "calendar": ("Calendars.ReadWrite",),
    "todo": ("Tasks.ReadWrite",),
}

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
EXPIRY_MARGIN = 120          # refresh this many seconds before the access token expires
LOGIN_TIMEOUT = 300


class MicrosoftAuthError(RuntimeError):
    """Not configured, not signed in, or Microsoft rejected the sign-in."""


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _scope_set(scope: str) -> set[str]:
    return {s.lower() for s in (scope or "").split() if s}


class MicrosoftAuth:
    def __init__(self, client_id: str, tenant_id: str = "", *, token_file: Path | None = None,
                 scopes: tuple[str, ...] = SCOPES, transport: httpx.BaseTransport | None = None):
        self.client_id = (client_id or "").strip()
        self.tenant_id = (tenant_id or "").strip() or DEFAULT_TENANT
        self.token_file = Path(token_file) if token_file else TOKEN_FILE
        self.scopes = scopes
        self._transport = transport
        self._mem_lock = threading.Lock()

    @classmethod
    def from_env(cls, env, **kwargs) -> "MicrosoftAuth":
        return cls(getattr(env, "ms_client_id", ""), getattr(env, "ms_tenant_id", ""), **kwargs)

    # ── state ──

    def is_configured(self) -> bool:
        return bool(self.client_id)

    def _read(self) -> dict | None:
        try:
            data = json.loads(self.token_file.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read the Microsoft token file: {e}")
            return None
        if isinstance(data, dict) and data.get("refresh_token"):
            return data
        return None

    def _write(self, data: dict) -> None:
        """Atomic write, owner-only from the first byte (the file holds a refresh token)."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.token_file.parent, prefix=".ms_token.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.token_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def is_authorized(self) -> bool:
        return self._read() is not None

    def account(self) -> str:
        return (self._read() or {}).get("account", "")

    def granted_scopes(self) -> set[str]:
        return _scope_set((self._read() or {}).get("scope", ""))

    def has_feature(self, feature: str) -> bool:
        """Whether the signed-in grant covers this feature ('mail' | 'calendar' | 'todo')."""
        granted = self.granted_scopes()
        return all(s.lower() in granted for s in FEATURE_SCOPES[feature])

    def logout(self) -> bool:
        try:
            self.token_file.unlink()
            return True
        except FileNotFoundError:
            return False

    # ── token endpoint ──

    @property
    def _token_url(self) -> str:
        return f"{AUTHORITY}/{self.tenant_id}/oauth2/v2.0/token"

    def _post(self, url: str, data: dict) -> httpx.Response:
        try:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport) as client:
                return client.post(url, data=data)
        except httpx.HTTPError as e:
            raise MicrosoftAuthError(f"Could not reach Microsoft sign-in: {e}") from e

    @staticmethod
    def _error(resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}"
        if not isinstance(body, dict):
            return f"HTTP {resp.status_code}"
        code = body.get("error", "")
        lines = (body.get("error_description") or "").splitlines()
        desc = lines[0][:240] if lines else ""
        return f"{code}: {desc}" if desc else code or f"HTTP {resp.status_code}"

    def _store(self, body: dict, previous: dict | None = None) -> dict:
        previous = previous or {}
        record = {
            "refresh_token": body.get("refresh_token") or previous.get("refresh_token", ""),
            "access_token": body.get("access_token", ""),
            "expires_at": time.time() + float(body.get("expires_in", 3600)),
            "scope": body.get("scope") or previous.get("scope", ""),
            "account": previous.get("account", ""),
            "tenant": self.tenant_id,
            "obtained_at": int(time.time()),
        }
        if not record["refresh_token"]:
            raise MicrosoftAuthError("Microsoft returned no refresh token (offline_access not granted).")
        self._write(record)
        return record

    # ── access token (used by every Graph call) ──

    def access_token(self, *, force_refresh: bool = False) -> str:
        """A valid access token, refreshing under a cross-process lock when needed."""
        if not self.is_configured():
            raise MicrosoftAuthError("MS_CLIENT_ID is not set in .env.")
        with self._mem_lock, self._file_lock():
            stored = self._read()   # re-read inside the lock: another process may have refreshed
            if not stored:
                raise MicrosoftAuthError("Not signed in to Microsoft. Run 'harvey microsoft login'.")
            if (not force_refresh and stored.get("access_token")
                    and time.time() < float(stored.get("expires_at", 0)) - EXPIRY_MARGIN):
                return stored["access_token"]
            resp = self._post(self._token_url, {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": stored["refresh_token"],
                "scope": " ".join(self.scopes),
            })
            if resp.status_code != 200:
                reason = self._error(resp)
                if resp.status_code in (400, 401):
                    raise MicrosoftAuthError(
                        f"Microsoft rejected the saved sign-in ({reason}). Run 'harvey microsoft login' again.")
                raise MicrosoftAuthError(f"Microsoft token refresh failed ({resp.status_code}): {reason}")
            return self._store(resp.json(), stored)["access_token"]

    async def aaccess_token(self, *, force_refresh: bool = False) -> str:
        import asyncio
        return await asyncio.to_thread(self.access_token, force_refresh=force_refresh)

    @contextmanager
    def _file_lock(self):
        if fcntl is None:  # pragma: no cover
            yield
            return
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.token_file.with_suffix(".lock")
        with open(lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ── interactive sign-in ──

    def _finish_login(self, body: dict) -> dict:
        """Save a fresh grant and record which account signed in."""
        record = self._store(body)
        try:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport) as client:
                me = client.get("https://graph.microsoft.com/v1.0/me",
                                params={"$select": "mail,userPrincipalName"},
                                headers={"Authorization": f"Bearer {record['access_token']}"})
            if me.status_code == 200:
                info = me.json()
                record["account"] = (info.get("mail") or info.get("userPrincipalName") or "").lower()
                self._write(record)
        except (httpx.HTTPError, ValueError):
            pass
        return record

    def login_interactive(self, *, login_hint: str = "", open_browser: bool = True, say=print) -> dict:
        """Browser sign-in with a loopback redirect. Blocks until it completes."""
        import http.server

        if not self.is_configured():
            raise MicrosoftAuthError("MS_CLIENT_ID is not set in .env (the Entra app's Application ID).")

        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)
        result: dict = {}
        done = threading.Event()

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "state" not in query and "code" not in query and "error" not in query:
                    self.send_response(404); self.end_headers()   # e.g. /favicon.ico
                    return
                if query.get("state", [""])[0] != state:
                    self.send_response(400); self.end_headers()
                    return
                result["code"] = query.get("code", [""])[0]
                result["error"] = query.get("error", [""])[0]
                result["error_description"] = query.get("error_description", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                message = (b"<h2>Harvey is connected to Microsoft 365.</h2>You can close this tab."
                           if result["code"] else b"<h2>Sign-in did not complete.</h2>Return to the terminal.")
                self.wfile.write(message)
                done.set()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Callback)
        port = server.server_address[1]
        redirect_uri = f"http://localhost:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        if login_hint:
            params["login_hint"] = login_hint
        auth_url = f"{AUTHORITY}/{self.tenant_id}/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params)

        say(f"\n  Open this address to sign in as the mailbox owner:\n  {auth_url}\n")
        if open_browser:
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass
        try:
            if not done.wait(timeout=LOGIN_TIMEOUT):
                raise MicrosoftAuthError("Timed out waiting for the browser sign-in.")
        finally:
            server.shutdown()
            server.server_close()

        if result.get("error") or not result.get("code"):
            raise MicrosoftAuthError(
                f"Sign-in failed: {result.get('error') or 'no code returned'} "
                f"{result.get('error_description', '')}".strip()[:300])

        resp = self._post(self._token_url, {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "scope": " ".join(self.scopes),
        })
        if resp.status_code != 200:
            raise MicrosoftAuthError(f"Token exchange failed: {self._error(resp)}")
        with self._file_lock():
            return self._finish_login(resp.json())

    def login_device_code(self, *, say=print, sleep=time.sleep) -> dict:
        """Sign in from another device (for a headless box). Needs 'Allow public client flows'."""
        if not self.is_configured():
            raise MicrosoftAuthError("MS_CLIENT_ID is not set in .env (the Entra app's Application ID).")
        start = self._post(f"{AUTHORITY}/{self.tenant_id}/oauth2/v2.0/devicecode", {
            "client_id": self.client_id, "scope": " ".join(self.scopes)})
        if start.status_code != 200:
            raise MicrosoftAuthError(f"Could not start device sign-in: {self._error(start)}")
        grant = start.json()
        say(f"\n  {grant.get('message') or 'Go to ' + grant['verification_uri'] + ' and enter ' + grant['user_code']}\n")

        interval = int(grant.get("interval", 5))
        deadline = time.time() + int(grant.get("expires_in", 900))
        while time.time() < deadline:
            sleep(interval)
            resp = self._post(self._token_url, {
                "client_id": self.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": grant["device_code"],
            })
            if resp.status_code == 200:
                with self._file_lock():
                    return self._finish_login(resp.json())
            code = (resp.json() or {}).get("error", "") if resp.content else ""
            if code == "authorization_pending":
                continue
            if code == "slow_down":
                interval += 5
                continue
            raise MicrosoftAuthError(f"Device sign-in failed: {self._error(resp)}")
        raise MicrosoftAuthError("Timed out waiting for the device sign-in.")
