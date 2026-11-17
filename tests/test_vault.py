import json
from pathlib import Path

import pytest
import pytest_asyncio

from harvey.config import HarveyConfig
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager
from harvey.vault import Vault, register_with_obsidian, safe
from tests.test_codex_e2e import BASE


@pytest_asyncio.fixture
async def world(tmp_path):
    state = StateManager(db_path=str(tmp_path / "h.db"))
    await state.init_db()
    await seed_signal_catalog(state)
    cid = await state.add_company(Company(name="Northwind", domain="northwind.example", description="Business funding."))
    pid = await state.add_prospect(Prospect(first_name="Ben", last_name="Sample", title="COO", company="Northwind",
                                            email="ben@northwind.example", email_status="verified", status="queued",
                                            source="hunter_domain_search"))
    async with state._connect() as db:
        await db.execute("UPDATE prospects SET company_id = ? WHERE id = ?", (cid, pid))
        await db.commit()
    await state.add_observations([
        {"company_id": cid, "signal_code": "HIRING_ROLE", "collector": "discover",
         "value_text": "SVP, DATA & AI STRATEGY", "evidence_url": "https://www.indeed.com/viewjob?jk=1"},
        {"company_id": cid, "signal_code": "HIRING_ROLE", "collector": "profile", "value_text": "Apply Here",
         "evidence_url": "https://northwind.example/careers"},
        {"company_id": cid, "signal_code": "CONTACT_FOUND", "collector": "profile",
         "value_text": "Pat Placeholder — Chief Data and AI Officer", "evidence_url": "https://northwind.example/about"}])
    await state.add_outbox_item(prospect_id=pid, to_email="ben@northwind.example", subject="data and ai ownership",
                                body="Hey Ben,\n\nNorthwind is hiring.\n\nHarvey", send_at="2026-11-04T12:00:00",
                                status="approved", campaign_id="c", step=1)
    persona = tmp_path / "persona"
    persona.mkdir()
    (persona / "identity.md").write_text("# Identity\nJordan.")
    vault = Vault(state, HarveyConfig(**BASE), tmp_path / "HARVEY", persona_dir=persona)
    return state, vault, tmp_path / "HARVEY", pid


@pytest.mark.asyncio
async def test_creates_linked_notes_for_people_and_companies(world):
    state, vault, root, pid = world
    result = await vault.sync()
    assert result["prospects"] == 1 and result["companies"] == 1
    person = (root / "Prospects" / "Ben Sample - Northwind.md").read_text()
    assert 'stage: "queued"' in person and "[[Companies/Northwind]]" in person
    assert "Public job posting: **SVP, Data & AI Strategy**" in person        # tidied, with the link
    assert "https://www.indeed.com/viewjob?jk=1" in person and "Apply Here" not in person
    assert "> [!note]- Step 1: data and ai ownership (approved)" in person
    company = (root / "Companies" / "Northwind.md").read_text()
    assert "[[Prospects/Ben Sample - Northwind|Ben Sample]]" in company
    assert "Pat Placeholder" in company and "Apply Here" not in company
    assert "[[Prospects/Ben Sample - Northwind|Ben Sample]]" in (root / "Pipeline.md").read_text()
    assert "Sending: **on**" in (root / "Home.md").read_text()


@pytest.mark.asyncio
async def test_a_reply_shows_up_in_the_conversation(world):
    state, vault, root, pid = world
    from harvey.models.conversation import Conversation, Message
    convo = Conversation(id="", prospect_id=pid, intent="interested",
                         thread=[Message(sender="harvey", content="Who owns it?"),
                                 Message(sender="prospect", content="We do. Tell me more.")])
    await state.add_conversation(convo)
    await vault.sync()
    note = (root / "Prospects" / "Ben Sample - Northwind.md").read_text()
    assert "**Ben Sample**" in note and "> We do. Tell me more." in note and "`interested`" in note


@pytest.mark.asyncio
async def test_second_sync_changes_nothing_and_your_notes_are_never_touched(world):
    state, vault, root, pid = world
    await vault.sync()
    mine = root / "Notes" / "my thoughts.md"
    mine.write_text("mine")
    again = await Vault(state, vault.config, root, persona_dir=vault.persona_dir).sync()
    assert again["written"] == 0 and mine.read_text() == "mine"


@pytest.mark.asyncio
async def test_persona_is_a_read_only_mirror_and_nothing_is_deleted(world):
    state, vault, root, pid = world
    await vault.sync()
    mirror = (root / "Persona" / "identity.md").read_text()
    assert "Read-only copy" in mirror and "Jordan." in mirror
    stray = root / "Prospects" / "Old Person - Gone.md"
    stray.write_text("stale")
    await Vault(state, vault.config, root, persona_dir=vault.persona_dir).sync()
    assert stray.exists()                      # Harvey never deletes from the vault


@pytest.mark.asyncio
async def test_the_vault_holds_no_secrets(world, monkeypatch):
    state, vault, root, pid = world
    await vault.sync()
    blob = "".join(p.read_text() for p in root.rglob("*.md"))
    for needle in ("refresh_token", "RESEND_API_KEY", "whsec_", "Bearer "):
        assert needle not in blob


def test_digest_note_and_filename_safety(tmp_path):
    vault = Vault(None, None, tmp_path / "V", persona_dir=tmp_path)
    vault.write_digest("2026-11-04", "Last 24h: 3 sent.", {"days": 1})
    assert "Last 24h: 3 sent." in (tmp_path / "V" / "Digests" / "2026-11-04.md").read_text()
    assert safe('A/B: "C"? <D>|E #F') == "AB C DE F"


def test_registering_with_obsidian_adds_once_and_backs_up(tmp_path):
    cfg = tmp_path / "obsidian.json"
    cfg.write_text(json.dumps({"vaults": {"abc": {"path": "/other", "ts": 1}}}))
    vault = tmp_path / "HARVEY"
    vault.mkdir()
    assert register_with_obsidian(vault, cfg) == "added"
    assert register_with_obsidian(vault, cfg) == "present"
    data = json.loads(cfg.read_text())
    assert len(data["vaults"]) == 2 and any(v["path"] == str(vault.resolve()) for v in data["vaults"].values())
    assert (tmp_path / "obsidian.json.harvey-bak").exists()
    assert register_with_obsidian(vault, tmp_path / "missing.json") == "no obsidian config"
