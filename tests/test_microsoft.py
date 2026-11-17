"""Microsoft 365 OAuth, Graph client, mail provider, calendar and To Do."""

import base64
import hashlib
import json
import os
import stat
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from types import SimpleNamespace

import httpx
import pytest

from harvey.integrations.calendar import CalendarClient, compute_free_slots
from harvey.integrations.ms_auth import SCOPES, MicrosoftAuth, MicrosoftAuthError
from harvey.integrations.ms_graph import GraphClient, GraphError
from harvey.integrations.ms_mail import MsGraphMailProvider
from harvey.integrations.ms_todo import TodoClient

# Reuse the Handler harness from the conversation tests (fixtures resolve by name).
from tests.test_conversationalist import FakeBrain, add_prospect, make_handler, persona_dir, state  # noqa: F401

ALL_SCOPES = "Mail.ReadWrite Mail.Send Calendars.ReadWrite Tasks.ReadWrite User.Read"


def token_body(**over):
    body = {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 3600, "scope": ALL_SCOPES}
    body.update(over)
    return body


def make_auth(tmp_path, handler=None, **kw):
    transport = httpx.MockTransport(handler) if handler else None
    return MicrosoftAuth("client-123", "tenant-456", token_file=tmp_path / "ms_token.json",
                         transport=transport, **kw)


def seed(auth, *, access="AT-old", refresh="RT-old", expires_in=3600, scope=ALL_SCOPES, account="jordan@x.com"):
    auth._write({"refresh_token": refresh, "access_token": access, "expires_at": time.time() + expires_in,
                 "scope": scope, "account": account, "tenant": "t", "obtained_at": 0})


# ── OAuth ──

def test_unconfigured_and_signed_out_say_what_to_do(tmp_path):
    assert not MicrosoftAuth("", token_file=tmp_path / "t.json").is_configured()
    with pytest.raises(MicrosoftAuthError, match="MS_CLIENT_ID"):
        MicrosoftAuth("", token_file=tmp_path / "t.json").access_token()
    with pytest.raises(MicrosoftAuthError, match="harvey microsoft login"):
        make_auth(tmp_path).access_token()


def test_a_valid_access_token_is_reused_without_calling_microsoft(tmp_path):
    auth = make_auth(tmp_path, lambda r: pytest.fail("no request expected"))
    seed(auth)
    assert auth.access_token() == "AT-old"


def test_an_expiring_token_is_refreshed_and_the_rotated_refresh_token_saved(tmp_path):
    seen = {}

    def handler(request):
        seen.update(urllib.parse.parse_qs(request.content.decode()))
        assert str(request.url).endswith("/tenant-456/oauth2/v2.0/token")
        return httpx.Response(200, json=token_body(access_token="AT-new", refresh_token="RT-rotated"))

    auth = make_auth(tmp_path, handler)
    seed(auth, expires_in=30)                       # inside the 120s safety margin
    assert auth.access_token() == "AT-new"
    assert seen["grant_type"] == ["refresh_token"] and seen["refresh_token"] == ["RT-old"]
    assert "client_secret" not in seen              # public client: PKCE, no secret
    stored = json.loads(auth.token_file.read_text())
    assert stored["refresh_token"] == "RT-rotated" and stored["account"] == "jordan@x.com"


def test_force_refresh_ignores_a_token_that_still_looks_valid(tmp_path):
    auth = make_auth(tmp_path, lambda r: httpx.Response(200, json=token_body(access_token="AT-forced")))
    seed(auth)
    assert auth.access_token(force_refresh=True) == "AT-forced"


def test_a_rejected_refresh_token_asks_for_a_new_login(tmp_path):
    auth = make_auth(tmp_path, lambda r: httpx.Response(400, json={
        "error": "invalid_grant", "error_description": "AADSTS70008: expired\r\nTrace ID: x"}))
    seed(auth, expires_in=-10)
    with pytest.raises(MicrosoftAuthError, match="invalid_grant.*harvey microsoft login"):
        auth.access_token()


def test_the_token_file_is_owner_only_and_features_follow_granted_scopes(tmp_path):
    auth = make_auth(tmp_path)
    seed(auth, scope="Mail.ReadWrite Mail.Send User.Read")
    if os.name != "nt":  # Windows has no POSIX permission bits
        assert stat.S_IMODE(os.stat(auth.token_file).st_mode) == 0o600
    assert auth.has_feature("mail") and not auth.has_feature("calendar") and not auth.has_feature("todo")
    assert auth.logout() and not auth.is_authorized() and not auth.logout()


def test_scopes_cover_all_three_services_and_offline_access():
    assert {"offline_access", "Mail.Send", "Calendars.ReadWrite", "Tasks.ReadWrite"} <= set(SCOPES)


def test_interactive_login_uses_pkce_and_verifies_state(tmp_path, monkeypatch):
    exchange = {}

    def handler(request):
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(200, json={"mail": "J.Example@example.com"})
        exchange.update(urllib.parse.parse_qs(request.content.decode()))
        return httpx.Response(200, json=token_body())

    auth = make_auth(tmp_path, handler)
    auth_url = {}

    def fake_browser(url):
        auth_url["url"] = url
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

        def visit():
            base = q["redirect_uri"][0]
            # A callback with the wrong state is refused and does not complete the login...
            try:
                urllib.request.urlopen(f"{base}/?code=evil&state=wrong")
            except urllib.error.HTTPError as e:
                assert e.code == 400
            urllib.request.urlopen(f"{base}/?code=the-code&state={q['state'][0]}").read()
        threading.Thread(target=visit, daemon=True).start()

    monkeypatch.setattr("harvey.integrations.ms_auth.webbrowser.open", fake_browser)
    record = auth.login_interactive(login_hint="j.example@example.com", say=lambda *_: None)

    q = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url["url"]).query)
    assert "/tenant-456/oauth2/v2.0/authorize" in auth_url["url"]
    assert q["code_challenge_method"] == ["S256"] and q["login_hint"] == ["j.example@example.com"]
    assert q["redirect_uri"][0].startswith("http://localhost:")
    # The verifier sent at exchange time must hash to the challenge sent at authorize time.
    digest = hashlib.sha256(exchange["code_verifier"][0].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"][0]
    assert exchange["code"] == ["the-code"] and exchange["redirect_uri"] == q["redirect_uri"]
    assert record["account"] == "j.example@example.com"
    assert json.loads(auth.token_file.read_text())["account"] == "j.example@example.com"


def test_interactive_login_reports_a_declined_consent(tmp_path, monkeypatch):
    auth = make_auth(tmp_path, lambda r: pytest.fail("no token exchange after a denial"))

    def fake_browser(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        threading.Thread(target=lambda: urllib.request.urlopen(
            f"{q['redirect_uri'][0]}/?error=access_denied&error_description=declined&state={q['state'][0]}").read(),
            daemon=True).start()

    monkeypatch.setattr("harvey.integrations.ms_auth.webbrowser.open", fake_browser)
    with pytest.raises(MicrosoftAuthError, match="access_denied"):
        auth.login_interactive(say=lambda *_: None)
    assert not auth.is_authorized()


def test_device_code_login_waits_through_pending_then_saves(tmp_path):
    polls = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABC", "interval": 1,
                                             "expires_in": 600, "verification_uri": "https://microsoft.com/devicelogin"})
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(200, json={"userPrincipalName": "jordan@x.com"})
        polls["n"] += 1
        if polls["n"] < 3:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(200, json=token_body())

    auth = make_auth(tmp_path, handler)
    record = auth.login_device_code(say=lambda *_: None, sleep=lambda s: None)
    assert polls["n"] == 3 and record["account"] == "jordan@x.com"


def test_device_code_login_stops_on_a_hard_error(tmp_path):
    def handler(request):
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "dc", "user_code": "A", "interval": 1,
                                             "expires_in": 600, "verification_uri": "u"})
        return httpx.Response(400, json={"error": "authorization_declined"})

    with pytest.raises(MicrosoftAuthError, match="authorization_declined"):
        make_auth(tmp_path, handler).login_device_code(say=lambda *_: None, sleep=lambda s: None)


# ── Graph client ──

def graph_for(tmp_path, graph_handler, token_handler=None):
    auth = make_auth(tmp_path, token_handler)
    seed(auth)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    return GraphClient(auth, transport=httpx.MockTransport(graph_handler), sleep=fake_sleep), sleeps


@pytest.mark.asyncio
async def test_graph_retries_throttling_using_retry_after(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "TooManyRequests"}})
        return httpx.Response(200, json={"ok": True})

    graph, sleeps = graph_for(tmp_path, handler)
    assert await graph.request("GET", "/me") == {"ok": True}
    assert sleeps == [7.0] and calls["n"] == 2


@pytest.mark.asyncio
async def test_graph_refreshes_once_on_401_then_succeeds(tmp_path):
    tokens = []

    def handler(request):
        tokens.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer AT-old":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return httpx.Response(200, json={"ok": 1})

    graph, _ = graph_for(tmp_path, handler,
                         lambda r: httpx.Response(200, json=token_body(access_token="AT-fresh")))
    assert await graph.request("GET", "/me") == {"ok": 1}
    assert tokens == ["Bearer AT-old", "Bearer AT-fresh"]


@pytest.mark.asyncio
async def test_graph_errors_carry_graph_s_code_and_do_not_loop(tmp_path):
    graph, _ = graph_for(tmp_path, lambda r: httpx.Response(
        403, json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}}))
    with pytest.raises(GraphError) as e:
        await graph.request("GET", "/me/messages")
    assert e.value.status == 403 and e.value.code == "ErrorAccessDenied"


@pytest.mark.asyncio
async def test_graph_get_all_follows_next_links_up_to_the_limit(tmp_path):
    def handler(request):
        if "page2" in str(request.url):
            return httpx.Response(200, json={"value": [{"n": 3}, {"n": 4}]})
        return httpx.Response(200, json={"value": [{"n": 1}, {"n": 2}],
                                         "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?page2"})

    graph, _ = graph_for(tmp_path, handler)
    assert [r["n"] for r in await graph.get_all("/x", limit=10)] == [1, 2, 3, 4]
    assert [r["n"] for r in await graph.get_all("/x", limit=3)] == [1, 2, 3]


# ── Mail provider ──

def mail_config(email="j.example@example.com"):
    return SimpleNamespace(persona=SimpleNamespace(name="Harvey", email=email))


def mail_provider(tmp_path, graph_handler, email="j.example@example.com"):
    graph, sleeps = graph_for(tmp_path, graph_handler)
    return MsGraphMailProvider(mail_config(email), SimpleNamespace(), graph=graph)


@pytest.mark.asyncio
async def test_send_creates_a_mime_draft_with_headers_then_sends_it(tmp_path):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1.0/me/messages":
            assert request.headers["Content-Type"] == "text/plain"
            mime = message_from_bytes(base64.b64decode(request.content))
            assert mime["To"] == "jane@acme.com" and mime["Subject"] == "ai access"
            assert mime["From"] == "Harvey <j.example@example.com>"
            assert mime["List-Unsubscribe"] == "<mailto:j.example@example.com?subject=unsubscribe>"
            assert mime["In-Reply-To"] == "<prev@x>" and mime["References"] == "<prev@x>"
            assert "Hey Jane" in mime.get_payload(decode=True).decode()
            return httpx.Response(201, json={"id": "D1", "internetMessageId": "<real@outlook>",
                                             "conversationId": "C1"})
        assert request.url.path == "/v1.0/me/messages/D1/send"
        return httpx.Response(202)

    p = mail_provider(tmp_path, handler)
    result = await p.send_email(
        "jane@acme.com", "ai access", "Hey Jane,\n\nBody.", in_reply_to="<prev@x>",
        headers={"List-Unsubscribe": "<mailto:j.example@example.com?subject=unsubscribe>"})
    assert result.ok and result.message_id == "<real@outlook>" and result.thread_ref == "C1"
    assert seen == [("POST", "/v1.0/me/messages"), ("POST", "/v1.0/me/messages/D1/send")]


@pytest.mark.asyncio
async def test_a_failed_send_removes_the_draft_and_is_marked_failed(tmp_path):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/v1.0/me/messages" and request.method == "POST":
            return httpx.Response(201, json={"id": "D2", "internetMessageId": "<m@x>"})
        if request.url.path.endswith("/send"):
            return httpx.Response(500, json={"error": {"code": "ErrorInternalServerError", "message": "boom"}})
        return httpx.Response(204)

    result = await mail_provider(tmp_path, handler).send_email("jane@acme.com", "s", "b")
    assert not result.ok and not result.defer           # ambiguous 5xx: never auto-retried
    assert ("DELETE", "/v1.0/me/messages/D2") in seen


@pytest.mark.asyncio
async def test_throttled_and_signed_out_sends_are_deferred_not_failed(tmp_path):
    def throttled(request):
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"code": "TooManyRequests"}})

    result = await mail_provider(tmp_path, throttled).send_email("jane@acme.com", "s", "b")
    assert not result.ok and result.defer

    signed_out = MsGraphMailProvider(mail_config(), SimpleNamespace(),
                                     graph=GraphClient(make_auth(tmp_path / "none")))
    result = await signed_out.send_email("jane@acme.com", "s", "b")
    assert not result.ok and result.defer and "harvey microsoft login" in result.error


@pytest.mark.asyncio
async def test_a_timeout_on_the_send_call_is_not_retried_blindly(tmp_path):
    def handler(request):
        if request.url.path.endswith("/send"):
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(201, json={"id": "D3", "internetMessageId": "<m@x>"})

    result = await mail_provider(tmp_path, handler).send_email("jane@acme.com", "s", "b")
    assert not result.ok and not result.defer


def inbox_row(**over):
    row = {"id": "M1", "subject": "Re: ai access", "from": {"emailAddress": {"address": "Jane@Acme.com"}},
           "receivedDateTime": "2026-10-21T14:00:00Z", "internetMessageId": "<reply@acme>",
           "conversationId": "C1", "body": {"contentType": "text", "content": "Interesting, say more."},
           "internetMessageHeaders": [{"name": "In-Reply-To", "value": "<real@outlook>"}]}
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_replies_are_parsed_and_the_mailbox_s_own_mail_is_skipped(tmp_path):
    def handler(request):
        assert "receivedDateTime ge" in request.url.params["$filter"]
        assert request.headers["Prefer"] == 'outlook.body-content-type="text"'
        return httpx.Response(200, json={"value": [
            inbox_row(),
            inbox_row(id="M2", **{"from": {"emailAddress": {"address": "J.Example@example.com"}}}),
            inbox_row(id="M3", subject="Undeliverable: ai access",
                      **{"from": {"emailAddress": {"address": "postmaster@acme.com"}}}),
        ]})

    msgs = await mail_provider(tmp_path, handler).get_replies()
    assert [m.provider_id for m in msgs] == ["M1", "M3"]
    first = msgs[0]
    assert first.from_email == "jane@acme.com" and first.in_reply_to == "<real@outlook>"
    assert first.message_id == "<reply@acme>" and first.thread_ref == "C1" and not first.is_bounce
    assert msgs[1].is_bounce


@pytest.mark.asyncio
async def test_an_unreadable_inbox_raises_instead_of_looking_empty(tmp_path):
    p = mail_provider(tmp_path, lambda r: httpx.Response(
        503, json={"error": {"code": "ServiceNotAvailable", "message": "later"}}))
    with pytest.raises(RuntimeError, match="could not be read"):
        await p.get_replies()


@pytest.mark.asyncio
async def test_connection_check_flags_a_signed_in_mailbox_that_is_not_the_sender(tmp_path):
    def handler(request):
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"mail": "someone.else@example.com"})
        return httpx.Response(200, json={"id": "inbox"})

    ok, detail = await mail_provider(tmp_path, handler).test_connection()
    assert not ok and "someone.else@example.com" in detail and "persona.email" in detail

    def good(request):
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"mail": "J.Example@example.com"})
        return httpx.Response(200, json={"id": "inbox"})

    ok, detail = await mail_provider(tmp_path, good).test_connection()
    assert ok and "inbox readable" in detail


def test_provider_is_registered_and_needs_sign_in_and_mail_permission(tmp_path, monkeypatch):
    from harvey.integrations import ms_auth
    from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
    monkeypatch.setattr(ms_auth, "TOKEN_FILE", tmp_path / "no-token.json")  # not the real sign-in
    assert "m365" in NATIVE_PROVIDERS
    cfg = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(provider="m365")),
                          persona=SimpleNamespace(name="Harvey", email="j.example@example.com"))
    provider = get_mail_provider(cfg, SimpleNamespace(ms_client_id="abc", ms_tenant_id=""))
    assert isinstance(provider, MsGraphMailProvider) and not provider.is_configured()


# ── Calendar ──

UTC = timezone.utc
NY_MON = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)        # Monday 09:00 New York (EDT)


def test_free_slots_respect_working_hours_busy_time_and_weekends():
    start, end = NY_MON, NY_MON + timedelta(days=7)
    busy = [(NY_MON + timedelta(hours=1), NY_MON + timedelta(hours=2, minutes=15))]   # 10:00-11:15 NY
    slots = compute_free_slots(busy, start, end, duration_min=60)
    starts = [datetime.fromisoformat(s["start"]) for s in slots]
    assert NY_MON in starts                                              # 09:00 is free
    assert NY_MON + timedelta(hours=1) not in starts                     # 10:00 overlaps the meeting
    assert NY_MON + timedelta(hours=2) not in starts                     # 11:00 overlaps its tail
    assert NY_MON + timedelta(hours=2, minutes=30) in starts             # 11:30 is clear
    assert all(datetime.fromisoformat(s["end"]).astimezone().weekday() < 5 for s in slots[:3])
    saturday = NY_MON + timedelta(days=5)
    assert not [s for s in starts if s.date() == saturday.date()]
    assert max(starts) + timedelta(hours=1) <= NY_MON + timedelta(days=4, hours=8)   # nothing after 17:00 Friday


def graph_cal(tmp_path, handler, scope=ALL_SCOPES):
    auth = make_auth(tmp_path)
    seed(auth, scope=scope)
    return GraphClient(auth, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_calendar_busy_comes_from_graph_and_cancelled_or_free_events_do_not_block(tmp_path):
    def handler(request):
        assert request.headers["Prefer"] == 'outlook.timezone="UTC"'
        return httpx.Response(200, json={"value": [
            {"start": {"dateTime": "2026-10-19T14:00:00.0000000", "timeZone": "UTC"},
             "end": {"dateTime": "2026-10-19T15:00:00.0000000", "timeZone": "UTC"}, "showAs": "busy"},
            {"start": {"dateTime": "2026-10-19T16:00:00.0000000", "timeZone": "UTC"},
             "end": {"dateTime": "2026-10-19T17:00:00.0000000", "timeZone": "UTC"}, "showAs": "free"},
            {"start": {"dateTime": "2026-10-19T18:00:00.0000000", "timeZone": "UTC"},
             "end": {"dateTime": "2026-10-19T19:00:00.0000000", "timeZone": "UTC"}, "showAs": "busy",
             "isCancelled": True},
        ]})

    cal = CalendarClient(graph_cal(tmp_path, handler))
    busy = await cal.busy_blocks(NY_MON, NY_MON + timedelta(days=1))
    assert busy == [(datetime(2026, 10, 19, 14, tzinfo=UTC), datetime(2026, 10, 19, 15, tzinfo=UTC))]


@pytest.mark.asyncio
async def test_an_unreadable_calendar_yields_no_slots_never_everything_free(tmp_path):
    cal = CalendarClient(graph_cal(tmp_path, lambda r: httpx.Response(500, json={"error": {"code": "x"}})))
    assert await cal.busy_blocks(NY_MON, NY_MON + timedelta(days=1)) is None
    assert await cal.find_free_slots(NY_MON, NY_MON + timedelta(days=1)) == []


@pytest.mark.asyncio
async def test_calendar_degrades_without_permission_or_sign_in(tmp_path):
    cal = CalendarClient(graph_cal(tmp_path, lambda r: pytest.fail("no call expected"), scope="Mail.ReadWrite"))
    assert await cal.list_events(NY_MON, NY_MON + timedelta(days=1)) == []
    assert await cal.create_event("x", NY_MON, NY_MON + timedelta(hours=1)) is None
    assert await CalendarClient(None).get_available_slots("2026-10-19") == []
    assert await CalendarClient(None).book_meeting("jane@acme.com", {"start": "a", "end": "b"}) is False
    assert await CalendarClient(None).book_meeting("", {}) is False


@pytest.mark.asyncio
async def test_create_event_posts_utc_times_and_attendees(tmp_path):
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "E1"})

    cal = CalendarClient(graph_cal(tmp_path, handler))
    event = await cal.create_event("Conversation", NY_MON, NY_MON + timedelta(minutes=30),
                                   attendees=["jane@acme.com"])
    assert event == {"id": "E1"}
    assert sent["start"] == {"dateTime": "2026-10-19T13:00:00", "timeZone": "UTC"}
    assert sent["attendees"][0]["emailAddress"]["address"] == "jane@acme.com"


# ── To Do ──

class TodoServer:
    def __init__(self, lists=(), tasks=()):
        self.lists, self.tasks, self.created = list(lists), list(tasks), []

    def __call__(self, request):
        path, method = request.url.path, request.method
        if path == "/v1.0/me/todo/lists" and method == "GET":
            return httpx.Response(200, json={"value": self.lists})
        if path == "/v1.0/me/todo/lists" and method == "POST":
            row = {"id": "L-new", "displayName": json.loads(request.content)["displayName"]}
            self.lists.append(row)
            return httpx.Response(201, json=row)
        if path.endswith("/tasks") and method == "GET":
            return httpx.Response(200, json={"value": self.tasks})
        if path.endswith("/tasks") and method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            return httpx.Response(201, json={"id": "T1", **body})
        if method == "PATCH":
            self.created.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": {"code": "nope"}})


def todo_for(tmp_path, server, scope=ALL_SCOPES):
    return TodoClient(graph_cal(tmp_path, server, scope=scope), "Harvey")


@pytest.mark.asyncio
async def test_todo_creates_the_list_once_and_adds_a_task(tmp_path):
    server = TodoServer()
    todo = todo_for(tmp_path, server)
    task = await todo.add_task("Reply to Jane Doe (Acme)", "She asked for a proposal.", importance="high")
    assert task["id"] == "T1" and server.lists == [{"id": "L-new", "displayName": "Harvey"}]
    assert server.created[0]["importance"] == "high" and "proposal" in server.created[0]["body"]["content"]


@pytest.mark.asyncio
async def test_todo_uses_an_existing_list_and_never_duplicates_an_open_task(tmp_path):
    server = TodoServer(lists=[{"id": "L1", "displayName": "harvey"}],
                        tasks=[{"id": "T9", "title": "Reply to Jane Doe (Acme)", "status": "notStarted"}])
    task = await todo_for(tmp_path, server).add_task("reply to jane doe (acme)", "again")
    assert task["id"] == "T9" and server.created == []


@pytest.mark.asyncio
async def test_todo_degrades_without_the_tasks_permission_and_on_errors(tmp_path):
    todo = todo_for(tmp_path, lambda r: pytest.fail("no call expected"), scope="Mail.ReadWrite")
    assert await todo.add_task("x") is None and await todo.open_tasks() == []
    broken = todo_for(tmp_path, lambda r: httpx.Response(500, json={"error": {"code": "x"}}))
    assert await broken.add_task("x") is None
    assert await TodoClient(None).add_task("x") is None
    assert await todo_for(tmp_path, TodoServer()).complete_task("T1") is True


# ── Handler hand-off ──

class SpyTodo:
    def __init__(self, available=True, boom=False):
        self.available, self.boom, self.tasks = available, boom, []

    def is_available(self):
        return self.available

    async def add_task(self, title, body="", *, importance="normal"):
        if self.boom:
            raise RuntimeError("graph down")
        self.tasks.append((title, body, importance))


@pytest.mark.asyncio
async def test_an_escalated_reply_lands_on_owners_todo_list(state, persona_dir):      # noqa: F811
    p = await add_prospect(state)
    h = make_handler(state, FakeBrain(intent="question"))
    h.todo = SpyTodo()
    await h._process_reply(p.email, "Can you send over a proposal and an SOW?", "", reply_meta={"subject": "x"})
    assert len(h.todo.tasks) == 1
    title, body, importance = h.todo.tasks[0]
    assert title.startswith("Reply to Jane") and importance == "high"
    assert "proposal" in body and "pricing negotiation" in body


@pytest.mark.asyncio
async def test_a_todo_failure_or_missing_sign_in_never_breaks_reply_handling(state, persona_dir):   # noqa: F811
    p = await add_prospect(state)
    h = make_handler(state, FakeBrain(intent="question"))
    h.todo = SpyTodo(boom=True)
    await h._process_reply(p.email, "Can you send a proposal?", "", reply_meta={"subject": "x"})
    assert len(await state.get_conversations_by_status("needs_human")) == 1

    h2 = make_handler(state, FakeBrain(intent="question"))
    h2.todo = SpyTodo(available=False)
    from harvey.models.prospect import Prospect
    pid = await state.add_prospect(Prospect(
        first_name="Bob", last_name="Lee", title="CTO", company="Other Co",
        email="bob@other.com", email_status="verified", status="contacted"))
    await h2._process_reply("bob@other.com", "Can you send a proposal?", "", reply_meta={"subject": "x"})
    assert h2.todo.tasks == []


@pytest.mark.asyncio
async def test_an_ordinary_reply_does_not_create_a_task(state, persona_dir):          # noqa: F811
    p = await add_prospect(state)
    good = {"reply": "Jordan's general view is to start simple. What does your team use today?", "off_topic": False}
    h = make_handler(state, FakeBrain([good], intent="question"))
    h.todo = SpyTodo()
    await h._process_reply(p.email, "What would you do first?", "", reply_meta={"subject": "x"})
    assert h.todo.tasks == []


def test_handler_builds_a_todo_client_only_when_microsoft_is_configured():
    from harvey.agents.handler import Handler
    from harvey.config import IntegrationsConfig, MicrosoftConfig
    on = SimpleNamespace(integrations=IntegrationsConfig())
    assert Handler._build_todo(on, SimpleNamespace(ms_client_id="")) is None
    assert isinstance(Handler._build_todo(on, SimpleNamespace(ms_client_id="abc", ms_tenant_id="")), TodoClient)
    off = SimpleNamespace(integrations=IntegrationsConfig(microsoft=MicrosoftConfig(hand_off_tasks=False)))
    assert Handler._build_todo(off, SimpleNamespace(ms_client_id="abc")) is None
    assert Handler._build_todo(SimpleNamespace(), SimpleNamespace(ms_client_id="abc")) is None


# ── Resend sends, Graph reads the reply mailbox ──

def _resend_cfg(reply_to):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="resend", sender_domain="mail.example.com",
                                                       reply_to=reply_to)),
        persona=SimpleNamespace(name="Harvey", email="harvey@mail.example.com"))


def test_resend_reads_replies_through_graph_when_imap_is_not_set(monkeypatch):
    from harvey.integrations.resend_mail import ResendMailProvider
    monkeypatch.setattr(MicrosoftAuth, "is_configured", lambda self: True)
    monkeypatch.setattr(MicrosoftAuth, "is_authorized", lambda self: True)
    monkeypatch.setattr(MicrosoftAuth, "has_feature", lambda self, f: True)
    env = SimpleNamespace(resend_api_key="re_x", ms_client_id="abc", ms_tenant_id="")
    p = ResendMailProvider(_resend_cfg("j.example@example.com"), env)
    assert p.is_configured() and p.graph_reader.mailbox == "j.example@example.com"


def test_resend_without_imap_or_reply_mailbox_stays_unconfigured(monkeypatch):
    from harvey.integrations.resend_mail import ResendMailProvider
    monkeypatch.setattr(MicrosoftAuth, "is_configured", lambda self: True)
    monkeypatch.setattr(MicrosoftAuth, "is_authorized", lambda self: True)
    monkeypatch.setattr(MicrosoftAuth, "has_feature", lambda self, f: True)
    env = SimpleNamespace(resend_api_key="re_x", ms_client_id="abc", ms_tenant_id="")
    # no reply_to: replies would be unreadable, so sending must not start
    assert not ResendMailProvider(_resend_cfg(""), env).is_configured()
