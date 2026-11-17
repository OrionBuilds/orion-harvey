"""Mirror Harvey's Obsidian vault into the OS Memory module, the way FRIDAY and CLARA do.

Protocol (the OS `vault-sync` function), under Harvey's own sign-in:
  manifest {vault, entries:[{path, hash}]}  -> the OS answers which notes it needs (new/changed)
                                               and deletes the ones that no longer exist
  upsert   {vault, notes:[...]}             -> up to 100 notes per call, with their outgoing links
  finish   {vault, error?}                  -> note/link counts and the sync time for the Memory page

What is sent: the generated notes only (Home, Pipeline, Prospects, Companies, Digests, Persona).
`Notes/` is yours and `.obsidian/` is app config: neither leaves this machine.
Memory is readable by SuperAdmins only, because these notes hold prospect emails and reply text.
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from harvey.integrations.os_client import OsClient, OsError

logger = logging.getLogger("harvey.vaultsync")

VAULT = "harvey"
FUNCTION = "/functions/v1/vault-sync"
BATCH = 100
MAX_NOTE_BYTES = 512 * 1024
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
SKIP_TOP = {".obsidian", ".trash", "Notes"}


def collect(root: Path) -> dict[str, dict]:
    """{vault-relative path: note payload} for every markdown file that may leave the machine."""
    notes: dict[str, dict] = {}
    for file in sorted(root.rglob("*.md")):
        rel = file.relative_to(root)
        if rel.parts[0] in SKIP_TOP or any(part.startswith(".") for part in rel.parts):
            continue
        data = file.read_bytes()
        if len(data) > MAX_NOTE_BYTES:
            logger.warning("vault note too large to sync, skipped: %s", rel)
            continue
        text = data.decode("utf-8", errors="replace")
        heading = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "")
        notes[rel.as_posix()] = {
            "path": rel.as_posix(), "title": heading or file.stem,
            "folder": rel.parts[0] if len(rel.parts) > 1 else "root",
            "content": text, "hash": hashlib.sha256(data).hexdigest(),
            "mtime": datetime.fromtimestamp(file.stat().st_mtime, timezone.utc).isoformat(),
        }
    paths = set(notes)
    by_stem: dict[str, list[str]] = {}
    for p in paths:
        by_stem.setdefault(Path(p).stem.lower(), []).append(p)
    for note in notes.values():
        note["links"] = _links(note["content"], note["path"], paths, by_stem)
    return notes


def _links(content: str, source: str, paths: set[str], by_stem: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for target in WIKILINK.findall(content):
        target = target.strip()
        candidate = target if target.lower().endswith(".md") else f"{target}.md"
        if candidate in paths:
            resolved = candidate
        else:
            hits = by_stem.get(Path(candidate).stem.lower(), [])
            resolved = hits[0] if len(hits) == 1 else ""
        if resolved and resolved != source and resolved not in out:
            out.append(resolved)
    return out[:500]


async def sync_to_os(client: OsClient, root: Path) -> dict:
    """One full manifest/upsert/finish pass. Returns the OS's counts; raises OsError on failure
    after recording the failure on the Memory page."""
    notes = collect(root)
    try:
        plan = await client._send("POST", FUNCTION, json={
            "action": "manifest", "vault": VAULT,
            "entries": [{"path": n["path"], "hash": n["hash"]} for n in notes.values()]})
        need = [p for p in (plan or {}).get("need", []) if p in notes]
        for i in range(0, len(need), BATCH):
            await client._send("POST", FUNCTION, json={
                "action": "upsert", "vault": VAULT, "notes": [notes[p] for p in need[i:i + BATCH]]})
        done = await client._send("POST", FUNCTION, json={"action": "finish", "vault": VAULT})
    except OsError as e:
        try:
            await client._send("POST", FUNCTION, json={"action": "finish", "vault": VAULT, "error": str(e)[:400]})
        except OsError:
            pass
        raise
    return {"sent": len(need), "removed": (plan or {}).get("removed", 0), **(done or {})}
