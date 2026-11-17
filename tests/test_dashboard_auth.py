"""The dashboard can rewrite .env and start/stop Harvey, so it must not be open
to any web page or local process that can reach 127.0.0.1."""
import os

import stat

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash

TOKEN = "t0ken-for-tests-0123456789abcdef"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("HARVEY_DASHBOARD_AUTH", "on")
    monkeypatch.setenv("HARVEY_API_TOKEN", TOKEN)
    monkeypatch.delenv("HARVEY_PUBLIC_HOSTS", raising=False)
    monkeypatch.setattr(dash, "TOKEN_FILE", tmp_path / "dashboard_token")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    # follow_redirects off so /auth's 303 and cookie can be inspected.
    return TestClient(dash.app, base_url="http://127.0.0.1:5555", follow_redirects=False)


def bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# Routes that read data, write .env, or control processes.
SENSITIVE = [
    ("GET", "/api/settings"),
    ("POST", "/api/settings/env"),
    ("GET", "/api/prospects"),
    ("GET", "/api/outbox"),
    ("POST", "/api/outbox/approve-all"),
    ("POST", "/api/harvey/start"),
    ("POST", "/api/harvey/stop"),
    ("POST", "/api/sending/pause"),
    ("GET", "/api/export/prospects.csv"),
]


@pytest.mark.parametrize("method, path", SENSITIVE)
def test_sensitive_routes_reject_anonymous_callers(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method, path", SENSITIVE)
def test_sensitive_routes_reject_a_wrong_token(client, method, path):
    assert client.request(method, path, headers=bearer("wrong")).status_code == 401


def test_a_correct_bearer_token_gets_through(client):
    assert client.get("/api/stats", headers=bearer()).status_code == 200


def test_a_token_in_the_query_string_is_not_accepted_on_api_routes(client):
    assert client.get(f"/api/stats?token={TOKEN}").status_code == 401


def test_the_static_shell_is_public_so_the_sign_in_flow_can_load(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_sign_in_sets_a_strict_httponly_cookie_that_then_authorizes(client):
    bad = client.get("/auth?token=nope")
    assert bad.status_code == 401 and "set-cookie" not in bad.headers

    ok = client.get(f"/auth?token={TOKEN}")
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie

    # The TestClient keeps the cookie, as a browser would.
    assert client.get("/api/stats").status_code == 200


@pytest.mark.parametrize("host", ["evil.example.com", "127.0.0.1.evil.com", "10.0.0.5:5555"])
def test_foreign_host_headers_are_refused_even_with_a_valid_token(client, host):
    """DNS rebinding: a page on evil.com that resolves to 127.0.0.1 sends its
    own Host header. It must not be able to reach the API, token or not."""
    r = client.get("/api/stats", headers={**bearer(), "Host": host})
    assert r.status_code == 403
    assert client.get("/", headers={"Host": host}).status_code == 403


def test_local_names_and_explicitly_allowed_hosts_work(client, monkeypatch):
    for host in ("localhost:5555", "127.0.0.1:5555", "[::1]:5555"):
        assert client.get("/api/stats", headers={**bearer(), "Host": host}).status_code == 200
    # A public (tunnel) hostname reaches ONLY the internet routes, never the
    # control API, even with a valid token.
    monkeypatch.setenv("HARVEY_PUBLIC_HOSTS", "go.example.com")
    pub = {"Host": "go.example.com"}
    assert client.get("/api/stats", headers={**bearer(), **pub}).status_code == 403
    assert client.get("/", headers=pub).status_code == 403
    assert client.get("/u/unknown-token", headers=pub).status_code == 404  # reachable, token unknown


def test_the_env_writer_does_not_run_for_anonymous_requests(client, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(dash, "ENV_FILE", env_file)
    r = client.post("/api/settings/env", json={"SMTP_PASSWORD": "pwned"})
    assert r.status_code == 401 and not env_file.exists()


def test_a_token_is_generated_once_with_owner_only_permissions(monkeypatch, tmp_path):
    monkeypatch.delenv("HARVEY_API_TOKEN", raising=False)
    monkeypatch.setattr(dash, "TOKEN_FILE", tmp_path / "data" / "dashboard_token")
    first = dash.get_api_token()
    assert len(first) >= 32
    assert dash.get_api_token() == first                      # stable across calls
    mode = stat.S_IMODE((tmp_path / "data" / "dashboard_token").stat().st_mode)
    assert os.name == "nt" or mode == 0o600  # Windows has no POSIX permission bits


def test_auth_can_only_be_disabled_explicitly(monkeypatch):
    monkeypatch.delenv("HARVEY_DASHBOARD_AUTH", raising=False)
    assert dash._auth_enabled() is True
    monkeypatch.setenv("HARVEY_DASHBOARD_AUTH", "off")
    assert dash._auth_enabled() is False
