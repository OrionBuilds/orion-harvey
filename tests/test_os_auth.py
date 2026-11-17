import json
import time

import httpx
import pytest

from harvey.integrations.os_auth import OsAuth, OsAuthError

URL = "https://os.example.supabase.co"


def _auth(tmp_path, handler, expected="harvey@example.com"):
    return OsAuth(URL, "anon", expected, session_file=tmp_path / "h" / "os-session.json",
                  transport=httpx.MockTransport(handler))


def _tokens(n, email="harvey@example.com"):
    return {"access_token": f"a{n}", "refresh_token": f"r{n}", "expires_in": 3600,
            "user": {"id": "u1", "email": email}}


def test_password_login_stores_only_the_refresh_token(tmp_path):
    seen = {}

    def handler(req):
        seen["grant"] = req.url.params["grant_type"]
        seen["apikey"] = req.headers["apikey"]
        return httpx.Response(200, json=_tokens(1))

    auth = _auth(tmp_path, handler)
    auth.login_password("harvey@example.com", "pw")
    assert seen == {"grant": "password", "apikey": "anon"}
    saved = json.loads(auth.session_file.read_text())
    assert saved["refresh_token"] == "r1" and "pw" not in auth.session_file.read_text()
    assert oct(auth.session_file.stat().st_mode & 0o777) == "0o600"
    assert oct(auth.session_file.parent.stat().st_mode & 0o777) == "0o700"


def test_wrong_identity_is_refused_and_nothing_stored(tmp_path):
    auth = _auth(tmp_path, lambda r: httpx.Response(200, json=_tokens(1, email="jordan@example.com")))
    with pytest.raises(OsAuthError, match="Harvey's OS identity"):
        auth.login_password("jordan@example.com", "pw")
    assert not auth.session_file.exists()


def test_refresh_rotates_and_persists_the_new_refresh_token(tmp_path):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body["refresh_token"])
        return httpx.Response(200, json=_tokens(len(calls) + 1))

    auth = _auth(tmp_path, handler)
    auth._write({"refresh_token": "r1", "access_token": "old", "expires_at": time.time() - 5,
                 "email": "harvey@example.com"})
    assert auth.access_token() == "a2"
    assert json.loads(auth.session_file.read_text())["refresh_token"] == "r2"
    assert auth.access_token() == "a2"          # cached, no second call
    assert calls == ["r1"]
    assert auth.access_token(force_refresh=True) == "a3"
    assert calls == ["r1", "r2"]                 # the rotated token was used


def test_rejected_refresh_asks_for_a_new_login(tmp_path):
    auth = _auth(tmp_path, lambda r: httpx.Response(400, json={"msg": "Invalid Refresh Token"}))
    auth._write({"refresh_token": "dead", "access_token": "", "expires_at": 0,
                 "email": "harvey@example.com"})
    with pytest.raises(OsAuthError, match="harvey os login"):
        auth.access_token()


def test_not_signed_in_and_not_configured(tmp_path):
    assert not OsAuth("", "", session_file=tmp_path / "s.json").is_configured()
    with pytest.raises(OsAuthError, match="not set"):
        OsAuth("", "", session_file=tmp_path / "s.json").access_token()
    with pytest.raises(OsAuthError, match="not signed in"):
        _auth(tmp_path, lambda r: httpx.Response(500)).access_token()
