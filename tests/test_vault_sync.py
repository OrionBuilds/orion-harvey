import hashlib
import json
import time

import httpx
import pytest

from harvey.integrations.os_auth import OsAuth
from harvey.integrations.os_client import OsClient, OsError
from harvey.vault_sync import collect, sync_to_os

URL = "https://os.example.supabase.co"


class FakeMemory:
    """The OS vault-sync function, in memory: manifest -> need/remove, upsert, finish."""

    def __init__(self, fail_upsert=False):
        self.notes, self.links, self.status, self.calls = {}, {}, {}, []
        self.fail_upsert = fail_upsert

    def handler(self, req: httpx.Request):
        if req.url.path != "/functions/v1/vault-sync":
            return httpx.Response(404)
        body = json.loads(req.content)
        self.calls.append(body["action"])
        assert body["vault"] == "harvey"
        if body["action"] == "manifest":
            want = {e["path"]: e["hash"] for e in body["entries"]}
            need = [p for p, h in want.items() if self.notes.get(p, {}).get("hash") != h]
            remove = [p for p in self.notes if p not in want]
            for p in remove:
                del self.notes[p]
            return httpx.Response(200, json={"need": need, "removed": len(remove), "total": len(want)})
        if body["action"] == "upsert":
            if self.fail_upsert:
                return httpx.Response(500, json={"error": "boom"})
            assert len(body["notes"]) <= 100
            for n in body["notes"]:
                self.notes[n["path"]] = n
            return httpx.Response(200, json={"upserted": len(body["notes"])})
        self.status = {"error": body.get("error")}
        return httpx.Response(200, json={"notes": len(self.notes), "links": sum(len(n["links"]) for n in self.notes.values())})


def client(tmp_path, memory):
    auth = OsAuth(URL, "anon", session_file=tmp_path / "s.json")
    auth._write({"refresh_token": "r", "access_token": "a", "expires_at": time.time() + 3000, "email": "h@x"})
    return OsClient(auth, transport=httpx.MockTransport(memory.handler))


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "HARVEY"
    for rel, text in {
        "Home.md": "# Harvey\n[[Pipeline]] and [[Prospects/Ben Sample - Northwind|Ben]]",
        "Pipeline.md": "# Pipeline\n- [[Prospects/Ben Sample - Northwind]]",
        "Prospects/Ben Sample - Northwind.md": "# Ben Sample\nat [[Companies/Northwind]]",
        "Companies/Northwind.md": "# Northwind\n[[Nobody]] [[Northwind]]",
        "Notes/private.md": "# My private thought",
        ".obsidian/workspace.md": "# config",
    }.items():
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_text(text)
    return v


def test_only_generated_notes_are_collected_and_links_resolve_inside_the_vault(vault):
    notes = collect(vault)
    assert set(notes) == {"Home.md", "Pipeline.md", "Prospects/Ben Sample - Northwind.md", "Companies/Northwind.md"}
    assert not any(p.startswith("Notes/") or ".obsidian" in p for p in notes)
    assert sorted(notes["Home.md"]["links"]) == ["Pipeline.md", "Prospects/Ben Sample - Northwind.md"]
    assert notes["Companies/Northwind.md"]["links"] == []                 # unresolved and self links dropped
    assert notes["Prospects/Ben Sample - Northwind.md"]["links"] == ["Companies/Northwind.md"]
    assert notes["Home.md"]["title"] == "Harvey" and notes["Companies/Northwind.md"]["folder"] == "Companies"
    assert notes["Home.md"]["hash"] == hashlib.sha256((vault / "Home.md").read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_first_sync_sends_everything_and_the_second_sends_nothing(vault, tmp_path):
    memory = FakeMemory()
    c = client(tmp_path, memory)
    first = await sync_to_os(c, vault)
    assert first["sent"] == 4 and len(memory.notes) == 4 and "Notes/private.md" not in memory.notes
    memory.calls.clear()
    second = await sync_to_os(c, vault)
    assert second["sent"] == 0 and "upsert" not in memory.calls
    (vault / "Pipeline.md").write_text("# Pipeline\nchanged")
    third = await sync_to_os(c, vault)
    assert third["sent"] == 1 and "changed" in memory.notes["Pipeline.md"]["content"]


@pytest.mark.asyncio
async def test_a_note_removed_from_the_vault_is_removed_from_memory(vault, tmp_path):
    memory = FakeMemory()
    c = client(tmp_path, memory)
    await sync_to_os(c, vault)
    (vault / "Companies" / "Northwind.md").unlink()
    result = await sync_to_os(c, vault)
    assert result["removed"] == 1 and "Companies/Northwind.md" not in memory.notes


@pytest.mark.asyncio
async def test_a_failed_upsert_is_reported_on_the_memory_page_and_raised(vault, tmp_path):
    memory = FakeMemory(fail_upsert=True)
    with pytest.raises(OsError):
        await sync_to_os(client(tmp_path, memory), vault)
    assert memory.calls[-1] == "finish" and memory.status["error"]
