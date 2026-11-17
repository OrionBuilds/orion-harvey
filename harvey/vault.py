"""Harvey's Obsidian vault: everything he knows, browsable, linked, and kept current.

    OBSIDIAN-VAULT/HARVEY/
      Home.md  Pipeline.md  README.md
      Prospects/   one note per person: why Harvey wrote, the emails, the conversation
      Companies/   one note per company: open roles, site signals, the people
      Digests/     one note per morning digest
      Persona/     read-only mirror of the persona files (source of truth stays in ~/harvey/skills/persona)
      Notes/       YOURS. Harvey never writes here and never reads it as instructions.

Rules that keep it trustworthy:
  - Generated notes are rewritten from Harvey's database; a manual edit there is overwritten.
    Write in Notes/.
  - Nothing in the vault ever steers what Harvey says. Claims come from the persona files only.
  - Files are only written when their content changed, and nothing is ever deleted.
  - No secrets: no tokens, no API keys. It does hold prospect emails and reply text. It stays on
    this machine unless vault.os_memory is on, in which case the generated notes (never Notes/)
    are mirrored into the OS Memory module, which only SuperAdmins can read.
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from harvey import owner
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.vault")

GENERATED_DIRS = ("Prospects", "Companies", "Digests", "Persona")
BAD_FILENAME = re.compile(r'[\\/:*?"<>|#^\[\]]')
HTML = re.compile(r"<[^>]+>")
NOISE_ROLES = {"apply here", "apply now", "careers", "jobs"}
SITE_FLAGS = {
    "RUNNING_GOOGLE_ADS": "runs Google Ads", "RUNNING_META_ADS": "runs Meta ads",
    "NO_ONLINE_BOOKING": "no online booking", "NO_SCHEMA_MARKUP": "no schema markup",
    "BLOCKS_AI_CRAWLERS": "blocks AI crawlers",
}

README = f"""# Harvey: vault

This is Harvey's memory of his outbound work for {owner.NAME}. He writes it; you read it.

## Structure

- **Home** and **Pipeline**: where things stand, who is at which stage.
- **Prospects/**: one note per person. Why Harvey wrote to them, each email, the conversation.
- **Companies/**: one note per company. Open roles (with the posting link), site signals, the people.
- **Digests/**: the morning digest, one per day.
- **Persona/**: a read-only copy of the rules Harvey writes under. Change them in
  `~/harvey/skills/persona/`, not here.
- **Notes/**: yours. Harvey never writes or reads here.

Notes outside Notes/ are regenerated from Harvey's database every few minutes. If you edit one,
the edit is overwritten. Nothing here changes what Harvey is allowed to say.
"""


def safe(name: str) -> str:
    name = BAD_FILENAME.sub("", (name or "").strip())
    return re.sub(r"\s+", " ", name)[:90] or "Unnamed"


def link(folder: str, filename: str, label: str | None = None) -> str:
    return f"[[{folder}/{filename}|{label}]]" if label else f"[[{folder}/{filename}]]"


def y(value) -> str:
    """A YAML-safe scalar."""
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def clean(text: str, limit: int = 140) -> str:
    return HTML.sub("", text or "").replace("\n", " ").strip()[:limit]


def stamp(value) -> str:
    return str(value or "")[:16].replace("T", " ")


class Vault:
    def __init__(self, state, config, root: Path, persona_dir: Path | None = None):
        self.state, self.config, self.root = state, config, Path(root)
        self.persona_dir = persona_dir or (PROJECT_ROOT / "skills" / "persona")
        self.written = 0
        self.unchanged = 0

    # ── writing ──

    def _put(self, rel: str, text: str) -> None:
        path = self.root / rel
        if path.exists() and path.read_text(encoding="utf-8") == text:
            self.unchanged += 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".vault.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.written += 1

    def scaffold(self) -> None:
        for d in (*GENERATED_DIRS, "Notes"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        if not (self.root / "README.md").exists():
            self._put("README.md", README)
        obsidian = self.root / ".obsidian"
        obsidian.mkdir(exist_ok=True)
        if not (obsidian / "app.json").exists():
            (obsidian / "app.json").write_text(json.dumps({"alwaysUpdateLinks": True, "newFileLocation": "folder",
                                                            "newFileFolderPath": "Notes"}, indent=2))
        if not (obsidian / "graph.json").exists():
            (obsidian / "graph.json").write_text(json.dumps({"colorGroups": [
                {"query": "path:Prospects", "color": {"a": 1, "rgb": 0x2F81F7}},
                {"query": "path:Companies", "color": {"a": 1, "rgb": 0x3FB950}},
                {"query": "path:Digests", "color": {"a": 1, "rgb": 0xD29922}}]}, indent=2))

    # ── reading Harvey's database ──

    async def _rows(self, sql: str, params=()) -> list[dict]:
        import aiosqlite
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def sync(self) -> dict:
        self.written = self.unchanged = 0
        self.scaffold()
        prospects = await self._rows(
            "SELECT p.*, COALESCE(NULLIF(p.company,''), c.name, '') AS co_name, c.id AS co_id "
            "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id ORDER BY p.created_at")
        companies = await self._rows("SELECT * FROM companies ORDER BY name")
        outbox = await self._rows("SELECT * FROM outbox ORDER BY prospect_id, step, created_at")
        convos = await self._rows("SELECT * FROM conversations ORDER BY created_at")
        observations = await self._rows(
            "SELECT company_id, signal_code, collector, value_text, evidence_url, observed_at FROM observations "
            "ORDER BY observed_at DESC")

        names: dict[str, str] = {}      # prospect id -> filename stem
        used: set[str] = set()
        for p in prospects:
            stem = safe(f"{p['first_name']} {p['last_name']} - {p['co_name']}")
            if stem in used:
                stem = f"{stem} ({p['id'][:4]})"
            used.add(stem)
            names[p["id"]] = stem
        co_names = {c["id"]: safe(c["name"]) for c in companies}
        by_company: dict[str, list[dict]] = {}
        for p in prospects:
            by_company.setdefault(p.get("company_id") or "", []).append(p)

        for p in prospects:
            sends = [o for o in outbox if o["prospect_id"] == p["id"]]
            thread = [c for c in convos if c["prospect_id"] == p["id"]]
            obs = [o for o in observations if o["company_id"] == (p.get("company_id") or "~")]
            self._put(f"Prospects/{names[p['id']]}.md",
                      self._prospect_note(p, sends, thread, obs, co_names.get(p.get("company_id") or "")))
        for c in companies:
            people = by_company.get(c["id"], [])
            obs = [o for o in observations if o["company_id"] == c["id"]]
            self._put(f"Companies/{co_names[c['id']]}.md", self._company_note(c, people, obs, names))
        self._put("Pipeline.md", self._pipeline_note(prospects, names))
        self._put("Home.md", await self._home_note(prospects, outbox))
        self._mirror_persona()
        result = {"written": self.written, "unchanged": self.unchanged,
                  "prospects": len(prospects), "companies": len(companies)}
        logger.info("Vault synced: %s", result)
        return result

    # ── notes ──

    def _prospect_note(self, p, sends, thread, obs, co_file) -> str:
        postings = self._postings(obs)
        stage = p["status"] or "new"
        fm = ["---", "type: prospect", f"id: {y(p['id'])}", f"stage: {y(stage)}", f"email: {y(p['email'])}",
              f"email_status: {y(p['email_status'])}", f"title: {y(p['title'])}",
              f"company: {y(f'[[Companies/{co_file}]]' if co_file else p['co_name'])}",
              f"source: {y(p['source'])}", f"linkedin: {y(p['linkedin_url'])}",
              f"tags: [harvey/prospect, stage/{re.sub(r'[^a-z0-9_]', '_', stage.lower())}]",
              f"updated: {y(stamp(p['updated_at']))}", "---", ""]
        who = f"{p['first_name']} {p['last_name']}".strip()
        body = [f"# {who}", f"{p['title']} at " + (link("Companies", co_file, p["co_name"]) if co_file else p["co_name"]), ""]
        body += ["## Why Harvey is writing", ""]
        if postings:
            body += [f"- Public job posting: **{t}** ([posting]({u}))" for t, u in postings]
        else:
            body += ["- No specific signal recorded."]
        if p.get("personalization_notes"):
            body += [f"- Notes: {clean(p['personalization_notes'], 300)}"]
        if p.get("source"):
            body += [f"- Found via: {p['source']}"]
        body += ["", "## Emails", ""]
        if not sends:
            body += ["Nothing drafted yet."]
        else:
            body += ["| Step | Subject | Status | Scheduled | Sent |", "|---|---|---|---|---|"]
            for o in sends:
                body += [f"| {o['step']} | {o['subject']} | {o['status']} | {stamp(o['send_at'])} | {stamp(o['sent_at'])} |"]
            for o in sends:
                quoted = "\n".join("> " + line for line in (o["body"] or "").splitlines())
                body += ["", f"> [!note]- Step {o['step']}: {o['subject']} ({o['status']})", quoted]
        body += ["", "## Conversation", ""]
        messages = []
        for c in thread:
            try:
                messages += [(m.get("timestamp", ""), m.get("sender", ""), m.get("content", "")) for m in json.loads(c["thread_json"] or "[]")]
            except ValueError:
                continue
        if not messages:
            body += ["No replies yet."]
        for ts, sender, text in sorted(messages):
            who_said = "Harvey" if sender == "harvey" else (who or "Prospect")
            quoted = "\n".join("> " + line for line in (text or "").splitlines())
            body += [f"**{who_said}** ({stamp(ts)})", quoted, ""]
        if thread and thread[-1].get("intent"):
            body += [f"Latest intent: `{thread[-1]['intent']}`"]
        return "\n".join(fm + body).rstrip() + "\n"

    @staticmethod
    def _postings(obs) -> list[tuple[str, str]]:
        seen, out = set(), []
        for o in obs:
            title = clean(o["value_text"], 160)
            if (o["signal_code"] != "HIRING_ROLE" or o["collector"] != "discover"
                    or title.lower() in NOISE_ROLES or len(title) < 8 or title.lower() in seen
                    or not (o["evidence_url"] or "").startswith("http")):
                continue
            seen.add(title.lower())
            from harvey.agents.writer import tidy_title
            out.append((tidy_title(title), o["evidence_url"]))
        return out

    def _company_note(self, c, people, obs, names) -> str:
        fm = ["---", "type: company", f"id: {y(c['id'])}", f"domain: {y(c['domain'])}", f"website: {y(c['website'])}",
              f"size: {y(c['company_size'])}",
              "tags: [harvey/company]", f"updated: {y(stamp(c['updated_at']))}", "---", ""]
        body = [f"# {c['name']}", ""]
        if c.get("description"):
            body += [clean(c["description"], 400), ""]
        body += ["## Open roles", ""]
        postings = self._postings(obs)
        body += [f"- **{t}** ([posting]({u}))" for t, u in postings] or ["None recorded."]
        body += ["", "## People Harvey is working", ""]
        body += [f"- {link('Prospects', names[p['id']], p['first_name'] + ' ' + p['last_name'])}, {p['title']} ({p['status']})"
                 for p in people] or ["Nobody yet."]
        flags = sorted({SITE_FLAGS[o["signal_code"]] for o in obs if o["signal_code"] in SITE_FLAGS})
        stack = next((o["value_text"] for o in obs if o["signal_code"] == "TECH_STACK"), "")
        body += ["", "## Website", ""]
        if stack:
            body += [f"- Tools detected: {clean(stack, 200)}"]
        body += [f"- {f}" for f in flags] or ([] if stack else ["Nothing recorded."])
        found, seen = [], set()
        for o in obs:
            text = clean(o["value_text"], 100)
            if o["signal_code"] == "CONTACT_FOUND" and text and text not in seen and "—" in text:
                seen.add(text)
                found.append(text)
        if found:
            body += ["", "## Leaders listed on their site", ""] + [f"- {t}" for t in found[:8]]
        return "\n".join(fm + body).rstrip() + "\n"

    def _pipeline_note(self, prospects, names) -> str:
        order = ["new", "queued", "drafted", "contacted", "replied", "meeting_booked", "opted_out", "invalid"]
        groups: dict[str, list[dict]] = {}
        for p in prospects:
            groups.setdefault(p["status"] or "new", []).append(p)
        stages = [s for s in order if s in groups] + sorted(s for s in groups if s not in order)
        body = ["---", "type: pipeline", "tags: [harvey/pipeline]", "---", "", "# Pipeline", ""]
        if not stages:
            body += ["No prospects yet."]
        for s in stages:
            body += [f"## {s.replace('_', ' ').capitalize()} ({len(groups[s])})", ""]
            body += [f"- {link('Prospects', names[p['id']], p['first_name'] + ' ' + p['last_name'])}, {p['title']}, {p['co_name']}"
                     for p in groups[s]] + [""]
        return "\n".join(body).rstrip() + "\n"

    async def _home_note(self, prospects, outbox) -> str:
        paused = await self.state.get_setting("sending_paused", "")
        counts: dict[str, int] = {}
        for o in outbox:
            counts[o["status"]] = counts.get(o["status"], 0) + 1
        body = ["---", "type: home", "tags: [harvey/home]", "---", "", "# Harvey", "",
                f"Sending: **{'PAUSED: ' + paused if paused else 'on'}**", "",
                f"- Prospects: {len(prospects)} ([[Pipeline]])",
                "- Emails: " + (", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(counts.items())) or "none"),
                "", "## Recent digests", ""]
        digests = sorted((self.root / "Digests").glob("*.md"), reverse=True)[:7]
        body += [f"- [[Digests/{d.stem}]]" for d in digests] or ["None yet."]
        body += ["", "## Persona", ""] + [f"- [[Persona/{p.stem}]]" for p in sorted(self.persona_dir.glob("*.md"))
                                           if self.persona_dir.exists()]
        return "\n".join(body).rstrip() + "\n"

    def _mirror_persona(self) -> None:
        if not self.persona_dir.exists():
            return
        for src in sorted(self.persona_dir.glob("*.md")):
            text = src.read_text(encoding="utf-8")
            banner = (f"---\ntype: persona\ngenerated: true\ntags: [harvey/persona]\n---\n"
                      f"> [!warning] Read-only copy. Edit `~/harvey/skills/persona/{src.name}`; changes here are overwritten.\n\n")
            self._put(f"Persona/{src.stem}.md", banner + text)

    # ── digests ──

    def write_digest(self, day: str, text: str, metrics: dict | None = None) -> None:
        self.scaffold()
        lines = ["---", "type: digest", f"date: {y(day)}", "tags: [harvey/digest]", "---", "", f"# Digest {day}", "", text, ""]
        if metrics:
            lines += ["## Numbers", "", "```json", json.dumps(metrics, indent=2, default=str), "```", ""]
        self._put(f"Digests/{day}.md", "\n".join(lines))


# ── registering the vault with Obsidian so it shows up in the vault list ──

def register_with_obsidian(path: Path, config_file: Path | None = None) -> str:
    """Add the folder to Obsidian's vault list. Idempotent. Returns 'added' | 'present' | 'no obsidian config'."""
    import hashlib
    import shutil
    import time
    cfg = config_file or Path.home() / ".config" / "obsidian" / "obsidian.json"
    if not cfg.exists():
        return "no obsidian config"
    data = json.loads(cfg.read_text())
    vaults = data.setdefault("vaults", {})
    resolved = str(Path(path).resolve())
    if any(v.get("path") == resolved for v in vaults.values()):
        return "present"
    shutil.copy2(cfg, cfg.with_suffix(".json.harvey-bak"))
    vault_id = hashlib.sha1(resolved.encode()).hexdigest()[:16]
    vaults[vault_id] = {"path": resolved, "ts": int(time.time() * 1000)}
    fd, tmp = tempfile.mkstemp(dir=cfg.parent, prefix=".obsidian.")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, cfg)
    return "added"
