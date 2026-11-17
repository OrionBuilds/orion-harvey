"""Schema v12 + channel-neutral state: outbox channels, suppressions, send ledger."""

import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from harvey.models.prospect import Prospect
from harvey.state import MIGRATIONS, StateManager, norm_linkedin, norm_phone


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "test.db"))
    await sm.init_db()
    yield sm


def _future(days=1):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _past(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# ── Migration ──

@pytest.mark.asyncio
async def test_v12_upgrades_a_v11_database_and_backfills(tmp_path):
    db_path = str(tmp_path / "old.db")
    async with aiosqlite.connect(db_path) as db:
        for script in MIGRATIONS[:11]:
            await db.executescript(script)
        await db.execute("PRAGMA user_version = 11")
        await db.execute(
            "INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, to_email, "
            "status, sent_at) VALUES ('o1', 'c1', 'p1', 1, 'sequence', 'a@x.com', "
            "'sent', '2026-10-01T12:00:00.123456')"
        )
        await db.execute(
            "INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, to_email, status) "
            "VALUES ('o2', 'c1', 'p2', 1, 'sequence', 'b@x.com', 'approved')"
        )
        await db.commit()

    sm = StateManager(db_path)
    await sm.init_db()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("PRAGMA user_version") as c:
            assert (await c.fetchone())[0] == len(MIGRATIONS)
        async with db.execute("SELECT id, channel, to_ref FROM outbox ORDER BY id") as c:
            rows = [dict(r) for r in await c.fetchall()]
        assert rows == [
            {"id": "o1", "channel": "email", "to_ref": "a@x.com"},
            {"id": "o2", "channel": "email", "to_ref": "b@x.com"},
        ]
        # Only the already-sent row seeds the ledger, so caps survive the upgrade.
        async with db.execute("SELECT channel, prospect_id, ref, outcome FROM send_ledger") as c:
            ledger = [dict(r) for r in await c.fetchall()]
        assert ledger == [{"channel": "email", "prospect_id": "p1", "ref": "o1", "outcome": "sent"}]


@pytest.mark.asyncio
async def test_failed_migration_leaves_database_at_previous_version(tmp_path, monkeypatch):
    db_path = str(tmp_path / "old.db")
    async with aiosqlite.connect(db_path) as db:
        for script in MIGRATIONS[:11]:
            await db.executescript(script)
        await db.execute("PRAGMA user_version = 11")
        await db.commit()

    # Fail inside the transaction, just before its COMMIT.
    head, tail = MIGRATIONS[11].rsplit("COMMIT;", 1)
    broken = MIGRATIONS[:11] + [head + "SELECT * FROM table_that_does_not_exist;\nCOMMIT;" + tail]
    monkeypatch.setattr("harvey.state.MIGRATIONS", broken)
    with pytest.raises(Exception):
        await StateManager(db_path).init_db()

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA user_version") as c:
            assert (await c.fetchone())[0] == 11
        async with db.execute("PRAGMA table_info(outbox)") as c:
            cols = {r[1] for r in await c.fetchall()}
        assert "channel" not in cols  # rolled back, not half-applied


# ── Outbox is channel-aware ──

@pytest.mark.asyncio
async def test_same_step_on_two_channels_is_not_a_duplicate(state):
    kw = dict(prospect_id="p1", campaign_id="c1", step=1, subject="s", body="b",
              send_at="2026-10-20T10:00:00")
    email = await state.add_outbox_item(to_email="a@x.com", **kw)
    linkedin = await state.add_outbox_item(
        to_email="", channel="linkedin", to_ref="https://linkedin.com/in/jane",
        payload={"note": "hi"}, **kw)
    dup = await state.add_outbox_item(to_email="a@x.com", **kw)

    assert email and linkedin and dup is None
    item = await state.get_outbox_item(linkedin)
    assert item["channel"] == "linkedin"
    assert item["to_ref"] == "https://linkedin.com/in/jane"
    assert json.loads(item["payload_json"]) == {"note": "hi"}


@pytest.mark.asyncio
async def test_email_items_default_channel_and_to_ref(state):
    item_id = await state.add_outbox_item(
        prospect_id="p1", to_email="A@X.com", subject="s", body="b", send_at="x")
    item = await state.get_outbox_item(item_id)
    assert item["channel"] == "email"
    assert item["to_ref"] == "a@x.com"


@pytest.mark.asyncio
async def test_get_outbox_filters_by_channel(state):
    await state.add_outbox_item(prospect_id="p1", to_email="a@x.com", subject="s",
                                body="b", send_at="x")
    await state.add_outbox_item(prospect_id="p2", to_email="", channel="sms",
                                to_ref="8135551212", subject="", body="hi", send_at="x")
    assert len(await state.get_outbox()) == 2
    sms = await state.get_outbox(channel="sms")
    assert [i["to_ref"] for i in sms] == ["8135551212"]


# ── Send ledger ──

@pytest.mark.asyncio
async def test_ledger_counts_by_channel_and_contact(state):
    await state.record_send(channel="email", prospect_id="p1", company_id="c1",
                            sender_domain="go.example.com", ref="o1")
    await state.record_send(channel="email", prospect_id="p2", company_id="c1", ref="o2")
    await state.record_send(channel="linkedin", prospect_id="p1", company_id="c1", ref="o3")

    assert await state.count_sent_today("email") == 2
    assert await state.count_sent_today("linkedin") == 1
    assert await state.count_sent_today("sms") == 0
    assert await state.count_sent_for(channel="email", prospect_id="p1") == 1
    assert await state.count_sent_for(channel="email", company_id="c1") == 2
    assert await state.count_sent_for(channel="email") == 0  # needs a subject


@pytest.mark.asyncio
async def test_outcomes_and_recent_health(state):
    for i in range(10):
        await state.record_send(channel="email", prospect_id=f"p{i}", ref=f"o{i}",
                                sender_domain="go.example.com")
    assert await state.set_send_outcome("o3", "hard_bounce") == 1
    assert await state.set_send_outcome("o4", "complaint") == 1
    assert await state.set_send_outcome("", "hard_bounce") == 0

    assert await state.count_outcomes_today("email", "hard_bounce") == 1
    assert await state.count_outcomes_today("email", "hard_bounce", "go.example.com") == 1
    assert await state.count_outcomes_today("email", "hard_bounce", "other.com") == 0
    assert await state.recent_send_health("email", last_n=100) == (10, 2)
    assert (await state.recent_send_health("email", last_n=5))[0] == 5


@pytest.mark.asyncio
async def test_rolling_window_excludes_old_sends(state):
    await state.record_send(channel="email", prospect_id="p1", ref="o1")
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE send_ledger SET created_at = ?", (_past(3),))
        await db.commit()
    assert await state.count_sent_for(channel="email", prospect_id="p1", hours=24) == 0
    assert await state.count_sent_for(channel="email", prospect_id="p1", hours=24 * 7) == 1


# ── Suppressions ──

@pytest.mark.asyncio
async def test_suppression_is_normalized_and_idempotent(state):
    assert await state.add_suppression("email", " Jane@Acme.COM ", reason="unsubscribe")
    assert await state.add_suppression("email", "jane@acme.com", reason="unsubscribe")
    assert await state.is_suppressed("email", "JANE@acme.com")
    assert not await state.is_suppressed("email", "other@acme.com")


@pytest.mark.asyncio
async def test_suppression_channel_scoping(state):
    await state.add_suppression("phone", "(813) 555-1212", channel="sms", reason="stop_keyword")
    assert await state.is_suppressed("phone", "+1 813-555-1212", channel="sms")
    assert not await state.is_suppressed("phone", "8135551212", channel="email")
    await state.add_suppression("email", "x@y.com", channel="all", reason="complaint")
    assert await state.is_suppressed("email", "x@y.com", channel="linkedin")


@pytest.mark.asyncio
async def test_suppression_expiry(state):
    await state.add_suppression("company", "acme.com", reason="not_interested", expires_at=_future(180))
    await state.add_suppression("company", "old.com", reason="not_interested", expires_at=_past(1))
    assert await state.is_suppressed("company", "acme.com")
    assert not await state.is_suppressed("company", "old.com")


@pytest.mark.asyncio
async def test_suppression_rejects_unknown_kind_and_blank_values(state):
    with pytest.raises(ValueError):
        await state.add_suppression("shoe-size", "9")
    assert await state.add_suppression("email", "   ") is False


@pytest.mark.asyncio
async def test_remove_suppression(state):
    await state.add_suppression("email", "a@b.com", reason="manual")
    assert await state.remove_suppression("email", "a@b.com") == 1
    assert not await state.is_suppressed("email", "a@b.com")


# ── Lookups ──

def test_normalizers():
    assert norm_phone("+1 (813) 555-1212") == "8135551212"
    assert norm_phone("813.555.1212") == "8135551212"
    assert norm_phone("") == ""
    assert (norm_linkedin("https://www.LinkedIn.com/in/Jane-Doe/?utm=x")
            == "linkedin.com/in/jane-doe")


@pytest.mark.asyncio
async def test_prospect_lookup_by_linkedin_and_phone(state):
    pid = await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="CEO", email="jane@acme.com",
        linkedin_url="https://www.linkedin.com/in/jane-doe/", phone="(813) 555-1212"))
    by_li = await state.get_prospect_by_linkedin_url("http://linkedin.com/in/JANE-DOE?x=1")
    by_ph = await state.get_prospect_by_phone("+18135551212")
    assert by_li and by_li.id == pid
    assert by_ph and by_ph.id == pid
    assert await state.get_prospect_by_linkedin_url("") is None
    assert await state.get_prospect_by_phone("999") is None


@pytest.mark.asyncio
async def test_prospect_consent_roundtrip(state):
    pid = await state.add_prospect(Prospect(
        first_name="J", last_name="D", title="CEO", email="j@d.com"))
    p = await state.get_prospect(pid)
    assert p.consent() == {}
    assert p.phone_status == ""
