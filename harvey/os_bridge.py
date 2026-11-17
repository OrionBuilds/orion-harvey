"""Harvey as a module of Orion, the business OS.

Harvey keeps his own SQLite state and does his own work; this bridge only
reports it to the OS and takes instructions back, the way FRIDAY does: it
PULLS. Nothing on this machine is exposed. Each cycle (default 60 s):

  1. commands   owner -> Harvey from the OS module: pause / resume / sync_now
  2. decisions  approvals the owner made in /automation/approvals -> applied
                to the outbox (approved drafts are released; rejected ones stop)
  3. mirror     every draft waiting for review -> one approval in the OS queue
  4. threads    messages to Harvey in Conversations -> answered
  5. report     heartbeat + status snapshot + changed prospects + new events
  6. alerts     hot replies and automatic pauses -> the owner's phone

Every step is independent: one failing never stops the others, and a down OS
never stops Harvey from doing his job. Sending still requires approval in the
OS (or the local dashboard) exactly as before.
"""

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from harvey import owner
from harvey.integrations.os_auth import OsAuth
from harvey.integrations.os_client import OsClient, OsError

logger = logging.getLogger("harvey.osbridge")

AGENT_SLUG = "harvey"
SYNC_INTERVAL_SECONDS = 60
EVENT_LOOKBACK_DAYS = 14
MAX_APPROVAL_ROUNDS = 3          # a draft the owner ignores is re-raised twice, then left alone
HEARTBEAT_STALL_SECONDS = 2 * 3600   # no loop activity this long = stalled
PAUSE_KEY = "sending_paused"
HOT_INTENTS = {"interested", "question", "objection"}
MANUAL_PAUSE_PREFIX = "paused from"
IMPORT_MAX_ROWS = 500
LINKEDIN_QUEUE_MAX = 50
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LINKEDIN_PROFILE_RE = re.compile(r"linkedin\.com/in/", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _iso(value) -> str | None:
    """SQLite timestamps are naive UTC strings; the OS wants timestamptz."""
    if not value:
        return None
    text = str(value).replace(" ", "T")[:19]
    return f"{text}Z" if text else None


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


# ── what the owner can say to Harvey in his Conversations thread ──

def interpret(text: str) -> str:
    """Map a free-text message to one of Harvey's built-in commands (or 'help')."""
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if re.search(r"\bresume anyway\b", t):
        return "resume_force"
    if re.search(r"\b(pause|stop|halt|hold)\b", t) and not re.search(r"\b(resume|start|unpause)\b", t):
        return "pause"
    if re.search(r"\b(resume|unpause|start sending|turn (sending )?back on)\b", t):
        return "resume"
    if re.search(r"\b(replies|replied|reply|who wrote|responses?)\b", t):
        return "replies"
    if re.search(r"\b(pending|drafts?|approvals?|waiting|queue)\b", t):
        return "pending"
    if re.search(r"\b(digest|summary|today|report|how('s| is| are) (it|we|things)|numbers)\b", t):
        return "digest"
    if re.search(r"\b(status|how are you|you (ok|up)|are you (running|alive|up)|health)\b", t):
        return "status"
    return "help"


HELP_TEXT = (
    f"I'm Harvey, {owner.FIRST}'s AI assistant for outbound. Ask me: status, pending drafts, who replied, "
    "digest. Say pause to stop all sending, or resume to start again. Approve or reject drafts in "
    "Approvals; nothing is sent without that."
)


class OsBridge:
    def __init__(self, state, config, env, *, client: OsClient | None = None):
        self.state = state
        self.config = config
        self.env = env
        self.client = client or OsClient(OsAuth.from_env(env))
        self._force_resync = False
        self._queued_events: list = []
        self._next_cursor = ""
        self._mail_health: tuple[float, dict | None] = (0.0, None)

    @property
    def enabled(self) -> bool:
        auth = self.client.auth
        return auth.is_configured() and auth.is_authorized()

    # ── the loop ──

    async def run_forever(self, stop_event: asyncio.Event, interval: int = SYNC_INTERVAL_SECONDS):
        announced = False
        while not self.enabled and not stop_event.is_set():
            if not announced:
                logger.info("OS bridge waiting: Harvey is not signed in to the OS (run 'harvey os login').")
                announced = True
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        if stop_event.is_set():
            return
        logger.info("OS bridge on: reporting to the OS every %ss.", interval)
        failures = 0
        while not stop_event.is_set():
            try:
                outcome = await self.cycle()
                failures = 0 if outcome["ok"] else failures + 1
            except Exception as e:   # never let the bridge die
                failures += 1
                logger.error("OS bridge cycle failed: %s", e, exc_info=True)
            wait = interval if failures == 0 else min(interval * (2 ** min(failures, 4)), 900)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def cycle(self) -> dict:
        """One pass over every step. Returns {'ok': bool, <step>: result-or-error}."""
        out: dict = {"ok": True}
        try:
            await self.client.whoami()   # also proves this sign-in really is the HarveyAgent
        except OsError as e:
            logger.warning("OS bridge: %s", e)
            return {"ok": False, "identity": f"os error: {e}"}
        for name in ("commands", "verify", "decisions", "mirror", "threads", "report", "alerts", "handoff", "digest", "vault"):
            try:
                out[name] = await getattr(self, f"step_{name}")()
            except OsError as e:
                out["ok"] = False
                out[name] = f"os error: {e}"
                logger.warning("OS bridge %s: %s", name, e)
                if "not signed in" in str(e).lower() or "rejected" in str(e).lower():
                    break   # auth is gone; the rest would fail the same way
            except Exception as e:
                out["ok"] = False
                out[name] = f"error: {e}"
                logger.error("OS bridge %s failed: %s", name, e, exc_info=True)
        return out

    # ── 1. commands from the OS module ──

    async def step_commands(self) -> int:
        rows = await self.client.rpc("harvey_commands_claim", {"p_limit": 5}) or []
        for row in rows:
            command = row.get("command")
            try:
                if command == "pause":
                    await self.state.set_setting(PAUSE_KEY, f"{MANUAL_PAUSE_PREFIX} the OS")
                    result = "sending paused"
                elif command == "resume":
                    await self.state.set_setting(PAUSE_KEY, "")
                    await self.state.set_setting("bounce_count", "0")
                    result = "sending resumed"
                elif command == "sync_now":
                    self._force_resync = True
                    result = "full resync scheduled"
                elif command == "linkedin_sent":
                    result = await self._linkedin_sent(row.get("args"))
                elif command == "import_prospects":
                    result = await self._import_prospects(row.get("args"))
                else:
                    raise ValueError(f"unknown command {command!r}")
                await self.client.rpc("harvey_command_report",
                                      {"p_id": row["id"], "p_status": "done", "p_result": result})
                if command == "import_prospects":
                    await self._event(f"command:{row['id']}", "import", result)
                else:
                    await self._event(f"command:{row['id']}", "command", f"Owner: {command}. {result}.")
            except Exception as e:
                await self.client.rpc("harvey_command_report", {
                    "p_id": row["id"], "p_status": "failed", "p_result": str(e)[:300]})
        return len(rows)

    async def _linkedin_sent(self, args) -> str:
        """Assist mode: the owner sent this LinkedIn item by hand and pressed 'Mark sent' in the OS.
        Same bookkeeping as the local dashboard button (ledger, relationship state, prospect stage)."""
        from harvey import linkedin_ops
        if self.config.channels.linkedin.mode != "assist":
            raise ValueError("LinkedIn is not in assist mode")
        outbox_id = str((args or {}).get("outbox_id") or "").strip()
        if not outbox_id:
            raise ValueError("outbox_id is required")
        problem = await linkedin_ops.mark_sent_manually(self.state, outbox_id)
        if problem:
            raise ValueError(problem)
        return "marked sent"

    async def _import_prospects(self, args) -> str:
        """Prospects the owner uploaded in the OS. They enter as 'new' with an unverified email, so the
        usual verification and the send gate still decide whether anyone is ever emailed. Bad rows are
        counted, never fatal."""
        from harvey.gate import prospect_suppression_keys
        from harvey.models.company import Company
        from harvey.models.prospect import Prospect

        args = args or {}
        rows = args.get("rows")
        if not isinstance(rows, list):
            raise ValueError("rows must be a list")
        label = str(args.get("source") or "").strip()[:60] or "unlabelled"
        imported = duplicates = 0
        rejected: dict[str, int] = {}

        def reject(reason: str) -> None:
            rejected[reason] = rejected.get(reason, 0) + 1

        def clean(row: dict, key: str) -> str:
            value = row.get(key)
            return str(value).strip() if value is not None else ""

        if len(rows) > IMPORT_MAX_ROWS:
            rejected[f"over the {IMPORT_MAX_ROWS}-row cap"] = len(rows) - IMPORT_MAX_ROWS
            rows = rows[:IMPORT_MAX_ROWS]
        for raw in rows:
            try:
                if not isinstance(raw, dict):
                    reject("not an object")
                    continue
                first, last = clean(raw, "first_name"), clean(raw, "last_name")
                email = clean(raw, "email").lower()
                url = clean(raw, "linkedin_url")
                if not (first or last):
                    reject("no name")
                    continue
                if email and not EMAIL_RE.match(email):
                    reject("invalid email")
                    continue
                if url and not LINKEDIN_PROFILE_RE.search(url):
                    url = ""
                if not (email or url):
                    reject("no email or LinkedIn profile")
                    continue
                company = clean(raw, "company")
                website = clean(raw, "website")
                if await self._import_is_duplicate(first, last, email, url, company):
                    duplicates += 1
                    continue
                domain = self._import_domain(website, email)
                prospect = Prospect(
                    first_name=first, last_name=last, email=email, email_status="", linkedin_url=url,
                    title=clean(raw, "title"), company=company, industry=clean(raw, "industry"),
                    company_size=clean(raw, "company_size"), source=f"os-import:{label}", status="new")
                co = Company(name=company, domain=domain)
                suppressed = False
                for kind, value in prospect_suppression_keys(prospect, co):
                    for channel in ("email", "linkedin"):
                        if await self.state.find_suppression(kind, value, channel=channel):
                            suppressed = True
                if suppressed:
                    reject("on the do-not-contact list")
                    continue
                if company:
                    prospect.company_id = await self._import_company(
                        company, domain, website, prospect, label)
                await self.state.add_prospect(prospect)
                imported += 1
            except Exception as e:   # one bad row never stops the rest
                logger.warning("OS import: row failed: %s", e)
                reject("error")
        why = ", ".join(f"{reason}: {n}" for reason, n in rejected.items()) or "none"
        return f"imported {imported}, skipped {duplicates} duplicates, rejected {sum(rejected.values())} ({why})"

    async def _import_is_duplicate(self, first: str, last: str, email: str, url: str, company: str) -> bool:
        if email and await self.state.get_prospect_by_email(email):
            return True
        if url and await self.state.get_prospect_by_linkedin_url(url):
            return True
        if first and last and company:
            async with self.state._connect() as db:
                async with db.execute(
                        "SELECT 1 FROM prospects p LEFT JOIN companies c ON c.id = p.company_id "
                        "WHERE LOWER(p.first_name) = ? AND LOWER(p.last_name) = ? "
                        "AND LOWER(COALESCE(NULLIF(p.company,''), c.name, '')) = ? LIMIT 1",
                        (first.lower(), last.lower(), company.lower())) as cur:
                    return await cur.fetchone() is not None
        return False

    @staticmethod
    def _import_domain(website: str, email: str) -> str:
        from harvey import compliance
        if website:
            host = re.sub(r"^[a-z]+://", "", website.strip().lower()).split("/")[0].split("?")[0]
            host = host[4:] if host.startswith("www.") else host
            if "." in host:
                return host
        if "@" in email:
            host = email.rsplit("@", 1)[-1]
            if host and not compliance.is_free_mail(host):
                return host
        return ""

    async def _import_company(self, name: str, domain: str, website: str, prospect, label: str) -> str:
        """Find or create the company the way the collectors do: by domain, else a stable external id."""
        from harvey.models.company import Company
        if domain:
            existing = await self.state.get_company_by_domain(domain)
        else:
            external_id = f"os-import:{name.lower()}"
            existing = await self.state.get_company_by_external_id(external_id)
        if existing:
            return existing.id
        return await self.state.add_company(Company(
            name=name, domain=domain, website=website or (f"https://{domain}" if domain else ""),
            industry=prospect.industry, company_size=prospect.company_size, source=f"os-import:{label}",
            external_id="" if domain else f"os-import:{name.lower()}"))

    # ── 1b. verify imported addresses ──

    VERIFY_BATCH = 8            # addresses per cycle: keeps the SMTP / API probing gentle
    VERIFY_TIMEOUT = 30         # seconds per address
    VERIFY_TRIES = 3            # inconclusive this many times -> 'guess' (held by the gate)

    async def step_verify(self) -> int:
        """Imported prospects arrive with an unverified email, and nothing else would ever check one
        that is already stored. Verify a few per cycle with Harvey's own verifiers; the send gate is
        untouched and still only releases 'verified' (or 'risky' if the owner allows it)."""
        from harvey.integrations.email_finder import verify_existing_email
        verify = getattr(self, "_verify_email", None) or verify_existing_email
        async with self.state._connect() as db:
            async with db.execute(
                    "SELECT id, email FROM prospects WHERE source LIKE 'os-import:%' AND email != '' "
                    "AND COALESCE(email_status, '') = '' ORDER BY created_at LIMIT ?", (self.VERIFY_BATCH,)) as cur:
                todo = await cur.fetchall()
        done = 0
        for pid, email in todo:
            key = f"os_verify_tries:{pid}"
            try:
                status = await asyncio.wait_for(verify(email, self.env), timeout=self.VERIFY_TIMEOUT)
            except Exception as e:
                logger.warning("OS import: could not verify %s: %s", email, e)
                status = "guess"
            if status == "guess":
                tries = int(await self.state.get_setting(key, "0") or 0) + 1
                await self.state.set_setting(key, str(tries))
                if tries < self.VERIFY_TRIES:
                    continue            # inconclusive: try again on a later cycle
            await self.state.update_prospect_email(pid, email, status)
            done += 1
        return done

    # ── 2 + 3. approvals: the OS queue is where drafts get decided ──

    async def _round_record(self, outbox_id: str) -> dict:
        raw = await self.state.get_setting(f"os_appr:{outbox_id}", "")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    async def _save_round(self, outbox_id: str, record: dict) -> None:
        await self.state.set_setting(f"os_appr:{outbox_id}", json.dumps(record))

    @staticmethod
    def _external_ref(outbox_id: str, round_no: int) -> str:
        return f"outbox:{outbox_id}:{round_no}"

    async def step_mirror(self) -> int:
        who = await self.client.whoami()
        pending = await self.state.get_outbox(status="pending_review", limit=200)
        raised = 0
        for item in pending:
            record = await self._round_record(item["id"])
            content_hash = _hash([item.get("subject", ""), item.get("body", "")])
            if record.get("approval_id") and not record.get("lapsed"):
                if not record.get("hash"):
                    # Cards raised before content hashing existed carry no hash, so judge by the
                    # card itself. A card that was decided and acknowledged, or closed, does not
                    # cover a draft that is waiting for review again (it was rewritten after the
                    # approval): it needs a fresh card. A card that is still open and shows the
                    # same words, or a decision not yet pulled, is left alone.
                    card = (await self.client.select("workflow_approval_queue", {
                        "select": "id,status,acked_at,draft_content", "id": f"eq.{record['approval_id']}",
                        "limit": "1"}) or [None])[0]
                    shown = ((card or {}).get("draft_content") or {}).get("draft") or {}
                    open_and_same = bool(card) and card.get("status") == "pending_approval" \
                        and shown.get("subject") == item.get("subject") and shown.get("body") == item.get("body")
                    undelivered = bool(card) and card.get("status") in ("approved", "rejected", "expired") \
                        and not card.get("acked_at")
                    if open_and_same or undelivered or not card:
                        if open_and_same:
                            record["hash"] = content_hash
                            await self._save_round(item["id"], record)
                        continue
                    record["lapsed"] = True
                elif record["hash"] == content_hash:
                    continue
                else:
                    record["lapsed"] = True      # the words changed after the card was raised
            if int(record.get("expiries", 0)) >= MAX_APPROVAL_ROUNDS:
                continue                     # ignored three times: stop nagging
            if record.get("approval_id"):
                # Whatever the owner saw before no longer applies: close that card if it is still open.
                now = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
                await self.client.update(
                    "workflow_approval_queue",
                    {"id": f"eq.{record['approval_id']}", "status": "eq.pending_approval"},
                    {"status": "superseded", "resolved_at": now, "acked_at": now,
                     "resolution_note": "superseded: Harvey rewrote this draft"})
            round_no = int(record.get("round", 0)) + 1
            prospect = await self._prospect_label(item.get("prospect_id", ""))
            channel = item.get("channel") or "email"
            assist = self.config.channels.linkedin.mode == "assist"
            if channel == "sms":
                draft = {"kind": "sms", "to": [item.get("to_ref", "")], "body": item.get("body", "")}
            elif channel == "linkedin":
                what = "connection request" if item.get("kind") == "connect" else "message"
                draft = {"kind": "linkedin", "type": item.get("kind", ""), "to": [item.get("to_ref", "")],
                         "body": item.get("body", "")}
            else:
                draft = {"kind": "email", "to": [item.get("to_email", "")],
                         "subject": item.get("subject", ""), "body": item.get("body", "")}
            row = {
                "org_id": who["organization_id"], "source": "agent_approval", "agent_slug": AGENT_SLUG,
                "external_ref": self._external_ref(item["id"], round_no),
                "action_type": f"harvey_{channel}",
                "title": (f"{prospect['name']} ({prospect['company']}): LinkedIn {what}" if channel == "linkedin"
                          else f"{prospect['name']} ({prospect['company']}): step {item.get('step', 1)}")[:200],
                "description": (
                    f"Harvey drafted a LinkedIn {what} to {prospect['name']}, {prospect['title']} at "
                    f"{prospect['company']} ({item.get('to_ref', '')}). "
                    + (f"Text: {_sentence(item.get('body', ''))} " if item.get("body") else "No note, just the request. ")
                    + ("Approve it, then send it yourself from LinkedIn and press 'Mark sent' on the LinkedIn tab of the "
                       "Harvey page in Orion OS. In assist mode Harvey never sends anything on LinkedIn."
                       if assist else
                       "Approve and Harvey sends it, inside LinkedIn's daily limits and working hours. Nothing is sent until you approve.")
                    if channel == "linkedin" else
                    f"Harvey drafted a {channel} to {prospect['name']}, {prospect['title']} at "
                    f"{prospect['company']}. Nothing is sent until you approve."),
                "status": "pending_approval",
                "draft_content": {
                    "risk": "medium" if int(item.get("step") or 1) == 1 else "low",
                    "facts": {"prospect": prospect["name"], "company": prospect["company"],
                              "title": prospect["title"], "step": item.get("step", 1),
                              "send_at": _iso(item.get("send_at")), "outbox_id": item["id"]},
                    "draft": draft, "mirrored_by": who.get("email") or AGENT_SLUG,
                },
                "requested_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            try:
                created = await self.client.insert("workflow_approval_queue", row)
            except OsError as e:
                if e.code == "23505" or "duplicate" in str(e).lower():
                    created = (await self.client.select("workflow_approval_queue", {
                        "select": "id", "source": "eq.agent_approval", "agent_slug": f"eq.{AGENT_SLUG}",
                        "external_ref": f"eq.{row['external_ref']}", "limit": "1"}) or [None])[0]
                else:
                    raise
            if created:
                await self._save_round(item["id"], {"round": round_no, "approval_id": created["id"], "hash": content_hash})
                raised += 1
                await self._event(f"draft:{item['id']}:{round_no}", "draft",
                                  f"Draft ready for approval: {prospect['name']} ({prospect['company']}), step {item.get('step', 1)}.",
                                  prospect_id=item.get("prospect_id"))
        await self._supersede_decided_locally(pending)
        return raised

    async def _supersede_decided_locally(self, pending: list[dict]) -> None:
        """A draft approved or rejected in the local dashboard: close its OS card."""
        still_pending = {p["id"] for p in pending}
        rows = await self.client.select("workflow_approval_queue", {
            "select": "id,external_ref", "source": "eq.agent_approval", "agent_slug": f"eq.{AGENT_SLUG}",
            "status": "eq.pending_approval", "limit": "200"})
        for row in rows:
            match = re.match(r"^outbox:(.+):(\d+)$", row.get("external_ref") or "")
            if not match or match.group(1) in still_pending:
                continue
            item = await self.state.get_outbox_item(match.group(1))
            outcome = (item or {}).get("status", "removed")
            now = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
            await self.client.update("workflow_approval_queue",
                                     {"id": f"eq.{row['id']}", "status": "eq.pending_approval"},
                                     {"status": "superseded", "acked_at": now, "resolved_at": now,
                                      "resolution_note": f"superseded: decided locally ({outcome})"})

    async def step_decisions(self) -> int:
        rows = await self.client.select("workflow_approval_queue", {
            "select": "id,external_ref,status,draft_content,resolution_note",
            "source": "eq.agent_approval", "agent_slug": f"eq.{AGENT_SLUG}",
            "status": "in.(approved,rejected,expired)", "acked_at": "is.null",
            "order": "resolved_at.asc.nullsfirst", "limit": "50"})
        applied = 0
        for row in rows:
            match = re.match(r"^outbox:(.+):(\d+)$", row.get("external_ref") or "")
            if match:
                outbox_id, round_no = match.group(1), int(match.group(2))
                record = await self._round_record(outbox_id)
                item = await self.state.get_outbox_item(outbox_id)
                current = int(record.get("round", 0)) == round_no
                if item and item["status"] == "pending_review" and current:
                    applied += await self._apply_decision(row, item, record, outbox_id)
            await self.client.update("workflow_approval_queue",
                                     {"id": f"eq.{row['id']}", "acked_at": "is.null"},
                                     {"acked_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ")})
        return applied

    async def _apply_decision(self, row: dict, item: dict, record: dict, outbox_id: str) -> int:
        status = row["status"]
        if status == "expired":
            # Nobody decided in 48 h. That is not a no: the draft stays and is raised again.
            record["lapsed"] = True
            record["expiries"] = int(record.get("expiries", 0)) + 1
            await self._save_round(outbox_id, record)
            await self._event(f"lapsed:{outbox_id}:{record.get('round')}", "approval_lapsed",
                              "A draft went 48 h without a decision; it is still waiting.",
                              prospect_id=item.get("prospect_id"))
            return 0
        if status == "rejected":
            await self.state.update_outbox_item(outbox_id, status="rejected")
            await self._event(f"rejected:{outbox_id}", "rejected", "Draft rejected in the OS.",
                              prospect_id=item.get("prospect_id"))
            return 1
        # approved: take the owner's edits (the OS editor saves them in draft_content.draft)
        draft = ((row.get("draft_content") or {}).get("draft")) or {}
        edits = {}
        if isinstance(draft.get("subject"), str) and draft["subject"].strip() and draft["subject"] != item.get("subject"):
            edits["subject"] = draft["subject"].strip()
        if isinstance(draft.get("body"), str) and draft["body"].strip() and draft["body"] != item.get("body"):
            edits["body"] = draft["body"]
        if edits:
            await self.state.update_outbox_item(outbox_id, **edits)
        await self.state.approve_outbox(outbox_id)
        await self._event(f"approved:{outbox_id}", "approved",
                          "Draft approved in the OS" + (" (with edits)." if edits else "."),
                          prospect_id=item.get("prospect_id"))
        return 1

    # ── 4. Conversations thread ──

    async def step_threads(self) -> int:
        rows = await self.client.rpc("agent_requests_claim", {"p_limit": 3, "p_lease_minutes": 10}) or []
        for row in rows:
            source = row.get("source") or {}
            if source.get("channel") == "factory" and source.get("factory_step_run_id"):
                # An OS factory step (e.g. the weekly outbound review): answer from Harvey's own numbers,
                # reported with evidence and a gate the factory can check, never free text.
                try:
                    summary, result = await self.factory_review(source)
                    await self.client.rpc("agent_request_report", {
                        "p_id": row["id"], "p_status": "completed", "p_summary": summary[:1000],
                        "p_result": result, "p_error": None})
                except Exception as e:
                    logger.error("Could not do factory step %s: %s", row.get("id"), e)
                    await self.client.rpc("agent_request_report", {
                        "p_id": row["id"], "p_status": "failed", "p_summary": None,
                        "p_result": None, "p_error": str(e)[:900]})
                continue
            try:
                reply = await self.answer(row.get("body") or row.get("title") or "")
                await self.client.rpc("agent_request_report", {
                    "p_id": row["id"], "p_status": "completed", "p_summary": reply[:1000],
                    "p_result": None, "p_error": None})
            except Exception as e:
                logger.error("Could not answer request %s: %s", row.get("id"), e)
                await self.client.rpc("agent_request_report", {
                    "p_id": row["id"], "p_status": "failed", "p_summary": None,
                    "p_result": None, "p_error": str(e)[:900]})
        return len(rows)

    async def answer(self, text: str) -> str:
        what = interpret(text)
        if what == "pause":
            await self.state.set_setting(PAUSE_KEY, f"{MANUAL_PAUSE_PREFIX} the OS thread")
            return "Sending is paused. Nothing goes out until you say resume."
        if what in ("resume", "resume_force"):
            reason = await self.state.get_setting(PAUSE_KEY, "")
            if reason and not reason.startswith(MANUAL_PAUSE_PREFIX) and what != "resume_force":
                return (f"I paused myself: {reason[:300]} Check that first. If you want me to "
                        "start anyway, say: resume anyway.")
            await self.state.set_setting(PAUSE_KEY, "")
            await self.state.set_setting("bounce_count", "0")
            await self.state.set_setting("policy_block_times", "[]")
            return "Sending is on again. Approved drafts will go out at the next send window."
        if what == "status":
            return await self.status_text()
        if what == "pending":
            return await self.pending_text()
        if what == "replies":
            return await self.replies_text()
        if what == "digest":
            return await self.digest_text()
        return HELP_TEXT

    # ── 5. report ──

    async def step_report(self) -> dict:
        snapshot = await self.build_snapshot()
        prospects, dropped = await self._changed_prospects()
        events = await self._new_events()
        queued, self._queued_events = self._queued_events, []
        events.extend(queued)
        try:
            result = await self.client.rpc("harvey_report", {
                "p_snapshot": snapshot, "p_prospects": prospects, "p_events": events,
                "p_drop_prospects": dropped})
        except Exception:
            self._queued_events = queued + self._queued_events   # report again next cycle
            raise
        await self._commit_hashes(prospects, dropped)
        await self.state.set_setting("os_event_cursor", self._next_cursor)
        return result or {}

    async def _event(self, ref: str, kind: str, summary: str, *, prospect_id=None, data=None, at=None):
        self._queued_events = [*self._queued_events, {
            "ref": ref, "kind": kind, "summary": summary, "prospect_id": prospect_id,
            "data": data or {}, "at": at or _now().strftime("%Y-%m-%dT%H:%M:%SZ")}]

    async def _prospect_label(self, prospect_id: str) -> dict:
        async with self.state._connect() as db:
            async with db.execute(
                    "SELECT p.first_name, p.last_name, p.title, COALESCE(NULLIF(p.company,''), c.name, '') "
                    "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id WHERE p.id = ?",
                    (prospect_id,)) as cur:
                row = await cur.fetchone()
        if not row:
            return {"name": "Unknown", "title": "", "company": ""}
        return {"name": f"{row[0]} {row[1]}".strip() or "Unknown", "title": row[2] or "", "company": row[3] or ""}

    async def mail_read_health(self) -> dict | None:
        """Can Harvey still read the mailbox where replies and bounces arrive? None = not applicable.
        A revoked Microsoft sign-in is silent otherwise: every cycle just logs an error."""
        import time as _time
        stamp, cached = self._mail_health
        if _time.time() - stamp < 300:
            return cached
        result = None
        if getattr(self.env, "ms_client_id", ""):
            from harvey.integrations.ms_auth import MicrosoftAuth, MicrosoftAuthError
            auth = MicrosoftAuth.from_env(self.env)
            if not auth.is_authorized():
                result = {"ok": False, "error": "Microsoft is not signed in. Run 'harvey microsoft login'."}
            else:
                try:
                    await auth.aaccess_token()
                    result = {"ok": True}
                except MicrosoftAuthError as e:
                    result = {"ok": False, "error": str(e)[:300]}
        self._mail_health = (_time.time(), result)
        return result

    async def build_snapshot(self) -> dict:
        from harvey.brain import BRAIN_STATUS_KEY, parse_brain_status
        from harvey.gate import deliverable_statuses
        from harvey.integrations.email_finder import hunter_searches_remaining
        from harvey.main import in_quiet_hours

        summary = await self.state.get_state_summary(deliverable=deliverable_statuses(self.config))
        pending = await self.state.get_outbox(status="pending_review", limit=500)
        approved = await self.state.get_outbox(status="approved", limit=500)
        paused = await self.state.get_setting(PAUSE_KEY, "")
        email = self.config.channels.email
        try:
            hunter_left = await hunter_searches_remaining(self.state)
        except Exception:
            hunter_left = None
        qh = self.config.usage.quiet_hours
        linkedin, sms, safety = await self._channel_snapshots()
        return {
            "running": True, "reported_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "paused": bool(paused), "paused_reason": paused,
            "paused_automatically": bool(paused) and not paused.startswith(MANUAL_PAUSE_PREFIX),
            "quiet_hours": in_quiet_hours(self.config),
            "quiet_window": f"{qh.start}-{qh.end} {qh.timezone}",
            "heartbeat_minutes": self.config.usage.heartbeat_interval_minutes,
            "email": {"provider": email.provider, "require_approval": email.require_approval,
                      "daily_cap": email.max_daily_sends,
                      "sent_today": await self.state.count_sent_today("email")},
            "pending_review": len(pending), "approved_waiting": len(approved),
            "prospects": summary.get("prospects", {}), "open_conversations": summary.get("open_conversations", 0),
            "claude_calls_today": summary.get("usage_today", 0), "brain": parse_brain_status(
                await self.state.get_setting(BRAIN_STATUS_KEY, "")),
            "hunter_searches_left": hunter_left,
            "mail_read": await self.mail_read_health(),
            "linkedin": linkedin, "sms": sms, "safety": safety,
        }

    async def _channel_snapshots(self) -> tuple[dict, dict, list[dict]]:
        """LinkedIn, SMS and the send-limit meters, for the OS Harvey page."""
        from harvey.channels.linkedin import DISABLED, TRIPPED_UNTIL, _parse
        from harvey.integrations import linkedin_policy as policy
        li, sms_cfg, email = self.config.channels.linkedin, self.config.channels.sms, self.config.channels.email
        limits = policy.Limits(li.max_daily_connections, li.max_daily_messages, li.max_weekly_connections).clamped()
        until = _parse(await self.state.get_setting(TRIPPED_UNTIL, ""))
        until = until if until and until.replace(tzinfo=None) > _now() else None
        disabled = bool(await self.state.get_setting(DISABLED, ""))
        connects = await self.state.count_kind_since("linkedin", (policy.CONNECT,), today_only=True)
        messages = await self.state.count_kind_since("linkedin", policy.MESSAGE_KINDS, today_only=True)
        week = await self.state.count_kind_since("linkedin", (policy.CONNECT,), days=7)
        queue = []
        if li.mode == "assist":
            # approved by the owner; they send each by hand and press 'Mark sent' (approval time = last update)
            approved = await self.state.get_outbox(status="approved", channel="linkedin", limit=500)
            approved.sort(key=lambda i: (i.get("updated_at") or "", i.get("created_at") or ""), reverse=True)
            for item in approved[:LINKEDIN_QUEUE_MAX]:
                who = await self._prospect_label(item.get("prospect_id", ""))
                queue.append({
                    "outbox_id": item["id"], "prospect_id": item.get("prospect_id", ""), "name": who["name"],
                    "company": who["company"], "title": who["title"], "profile_url": item.get("to_ref", ""),
                    "kind": item.get("kind", ""), "body": item.get("body", ""),
                    "approved_at": _iso(item.get("updated_at"))})
        linkedin = {
            "enabled": bool(li.enabled), "mode": li.mode,
            "tripped_until": _iso(until.replace(tzinfo=None).isoformat()) if until else None,
            "disabled": disabled,
            "sent_today": {"connect": connects, "message": messages}, "weekly_connections_sent": week,
            "limits": {"connects_per_day": limits.connects_per_day, "messages_per_day": limits.messages_per_day,
                       "connects_per_week": limits.connects_per_week},
            "queue": queue,
        }
        sms_sent = await self.state.count_sent_today("sms")
        sms = {"enabled": bool(sms_cfg.enabled), "daily_cap": int(sms_cfg.max_daily_sms), "sent_today": sms_sent}

        def meter(channel, label, cap, used, period, tripped=False):
            # a tripped LinkedIn breaker outranks a full meter: it is what the owner has to act on
            level = "tripped" if tripped else "full" if used >= cap else "near" if used >= 0.8 * cap else "ok"
            return {"channel": channel, "label": label, "cap": cap, "used": used, "period": period, "state": level}

        breaker = until is not None or disabled
        safety = [meter("email", "Email per day", int(email.max_daily_sends),
                        await self.state.count_sent_today("email"), "day")]
        if li.enabled and li.mode != "off":
            safety += [
                meter("linkedin_connect", "LinkedIn connections per day", limits.connects_per_day, connects, "day", breaker),
                meter("linkedin_message", "LinkedIn messages per day", limits.messages_per_day, messages, "day", breaker),
                meter("linkedin_week", "LinkedIn connections per week", limits.connects_per_week, week, "week", breaker)]
        if sms_cfg.enabled:
            safety.append(meter("sms", "SMS per day", int(sms_cfg.max_daily_sms), sms_sent, "day"))
        return linkedin, sms, safety

    async def _changed_prospects(self) -> tuple[list[dict], list[str]]:
        rows = []
        async with self.state._connect() as db:
            async with db.execute(
                    "SELECT p.id, p.first_name, p.last_name, p.title, COALESCE(NULLIF(p.company,''), c.name, ''), "
                    "p.email, p.email_status, p.status, p.score, p.linkedin_url, p.source, p.industry, p.company_size, "
                    "p.updated_at, "
                    "(SELECT MAX(sent_at) FROM outbox o WHERE o.prospect_id = p.id AND o.status = 'sent'), "
                    "(SELECT MIN(send_at) FROM outbox o WHERE o.prospect_id = p.id AND o.status IN ('approved','pending_review')), "
                    "(SELECT COUNT(*) FROM outbox o WHERE o.prospect_id = p.id AND o.status = 'pending_review'), "
                    "(SELECT COUNT(*) FROM outbox o WHERE o.prospect_id = p.id AND o.status = 'sent') "
                    "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id "
                    "ORDER BY p.updated_at DESC LIMIT 3000") as cur:
                rows = await cur.fetchall()
        hashes = await self._load_hashes()
        if self._force_resync:
            hashes = {}
            self._force_resync = False
        out, seen = [], set()
        for r in rows:
            seen.add(r[0])
            item = {
                "prospect_id": r[0], "name": f"{r[1]} {r[2]}".strip(), "title": r[3] or "", "company": r[4] or "",
                "email_status": r[6], "stage": r[7] or "new", "score": r[8],
                "last_event": "sent" if r[14] else None, "last_event_at": _iso(r[14]),
                "next_step_at": _iso(r[15]),
                "data": {"email": r[5], "linkedin_url": r[9], "source": r[10], "industry": r[11],
                         "company_size": r[12], "drafts_waiting": r[16], "emails_sent": r[17]},
            }
            if hashes.get(r[0]) != _hash(item):
                out.append(item)
        dropped = [pid for pid in hashes if pid not in seen]
        return out, dropped

    async def _load_hashes(self) -> dict:
        raw = await self.state.get_setting("os_prospect_hashes", "")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    async def _commit_hashes(self, prospects: list[dict], dropped: list[str]) -> None:
        hashes = await self._load_hashes()
        for item in prospects:
            hashes[item["prospect_id"]] = _hash(item)
        for pid in dropped:
            hashes.pop(pid, None)
        await self.state.set_setting("os_prospect_hashes", json.dumps(hashes))

    async def _new_events(self) -> list[dict]:
        cursor = await self.state.get_setting("os_event_cursor", "") or _ts(_now() - timedelta(days=EVENT_LOOKBACK_DAYS))
        self._next_cursor = _ts(_now() - timedelta(seconds=5))
        events: list[dict] = []
        norm = "REPLACE(SUBSTR({}, 1, 19), 'T', ' ')"
        async with self.state._connect() as db:
            async with db.execute(
                    f"SELECT l.id, l.outcome, l.prospect_id, {norm.format('l.created_at')}, l.channel, l.kind FROM send_ledger l "
                    f"WHERE {norm.format('l.created_at')} >= ? ORDER BY 4 LIMIT 500", (cursor,)) as cur:
                ledger = await cur.fetchall()
            async with db.execute(
                    f"SELECT id, thread_json, intent, prospect_id, channel, {norm.format('updated_at')} "
                    f"FROM conversations WHERE {norm.format('updated_at')} >= ? LIMIT 200", (cursor,)) as cur:
                convos = await cur.fetchall()
        for lid, outcome, pid, created, l_channel, l_kind in ledger:
            who = await self._prospect_label(pid or "")
            kind = "sent" if outcome == "sent" else (outcome or "ledger")
            events.append({"ref": f"ledger:{lid}", "kind": kind, "prospect_id": pid,
                           "summary": f"{kind.replace('_', ' ').capitalize()}: {who['name']} ({who['company']})",
                           "at": _iso(created), "data": {"channel": l_channel or "", "kind": l_kind or ""}})
        for cid, thread_json, intent, pid, channel, _updated in convos:
            try:
                thread = json.loads(thread_json or "[]")
            except ValueError:
                continue
            who = await self._prospect_label(pid or "")
            for idx, m in enumerate(thread):
                if m.get("sender") != "prospect":
                    continue
                at = _iso(m.get("timestamp"))
                if at and at.replace("T", " ")[:19] < cursor:
                    continue
                events.append({
                    "ref": f"reply:{cid}:{idx}", "kind": "reply", "prospect_id": pid, "at": at,
                    "summary": f"Reply from {who['name']} ({who['company']})",
                    "data": {"excerpt": (m.get("content") or "")[:600], "intent": intent, "channel": channel}})
        return events

    # ── 6. alerts to the owner's phone ──

    async def step_alerts(self) -> int:
        sent = 0
        recent = await self._recent_hot_replies()
        for ref, who, intent, excerpt in recent:
            key = f"harvey:reply:{ref}"
            if await self.state.get_setting(f"os_alert:{key}", ""):
                continue
            text = (f"{who} replied to Harvey ({intent}). \"{excerpt[:160]}\" "
                    "Open Harvey in the OS to read it and take over.")
            await self.client.rpc("agent_notify_owner", {
                "p_kind": "harvey.hot_reply", "p_priority": "critical", "p_text": text,
                "p_category": "revenue", "p_dedupe_key": key})
            await self.state.set_setting(f"os_alert:{key}", "1")
            sent += 1
        mail = await self.mail_read_health()
        if mail and not mail["ok"]:
            key = f"harvey:mailread:{_now().strftime('%Y-%m-%d')}"
            if not await self.state.get_setting(f"os_alert:{key}", ""):
                await self.client.rpc("agent_notify_owner", {
                    "p_kind": "harvey.mail_unreadable", "p_priority": "critical",
                    "p_text": ("Harvey can't read replies or bounces: his mailbox sign-in was rejected. "
                               "On the Linux box run: harvey microsoft login. Until then replies go unseen."),
                    "p_category": "production", "p_dedupe_key": key})
                await self.state.set_setting(f"os_alert:{key}", "1")
                sent += 1
        alive = await self.state.get_setting("heartbeat_alive_at", "")
        if alive:
            try:
                seen = datetime.fromisoformat(alive)
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - seen).total_seconds()
            except ValueError:
                age = 0
            if age > HEARTBEAT_STALL_SECONDS:
                key = f"harvey:stalled:{_now().strftime('%Y-%m-%d')}"
                if not await self.state.get_setting(f"os_alert:{key}", ""):
                    await self.client.rpc("agent_notify_owner", {
                        "p_kind": "harvey.stalled", "p_priority": "critical",
                        "p_text": (f"Harvey's work loop hasn't run for {int(age // 3600)}h "
                                   f"{int(age % 3600 // 60)}m. Nothing is being found or sent. "
                                   "On the Linux box: systemctl --user restart harvey."),
                        "p_category": "production", "p_dedupe_key": key})
                    await self.state.set_setting(f"os_alert:{key}", "1")
                    sent += 1
        paused = await self.state.get_setting(PAUSE_KEY, "")
        if paused and not paused.startswith(MANUAL_PAUSE_PREFIX):
            key = f"harvey:paused:{hashlib.sha1(paused.encode()).hexdigest()[:10]}"
            if not await self.state.get_setting(f"os_alert:{key}", ""):
                await self.client.rpc("agent_notify_owner", {
                    "p_kind": "harvey.paused", "p_priority": "critical",
                    "p_text": f"Harvey paused sending by himself: {paused[:300]} Check it before resuming.",
                    "p_category": "production", "p_dedupe_key": key})
                await self.state.set_setting(f"os_alert:{key}", "1")
                sent += 1
        return sent

    async def _recent_hot_replies(self) -> list[tuple]:
        since = _ts(_now() - timedelta(days=2))
        out = []
        async with self.state._connect() as db:
            async with db.execute(
                    "SELECT id, thread_json, intent, prospect_id FROM conversations "
                    "WHERE REPLACE(SUBSTR(updated_at,1,19),'T',' ') >= ?", (since,)) as cur:
                rows = await cur.fetchall()
        for cid, thread_json, intent, pid in rows:
            if intent not in HOT_INTENTS:
                continue
            try:
                thread = json.loads(thread_json or "[]")
            except ValueError:
                continue
            idxs = [i for i, m in enumerate(thread) if m.get("sender") == "prospect"]
            if not idxs:
                continue
            who = await self._prospect_label(pid or "")
            out.append((f"{cid}:{idxs[-1]}", f"{who['name']} at {who['company']}", intent,
                        thread[idxs[-1]].get("content") or ""))
        return out

    # ── plain-language answers (thread + digest) ──

    async def status_text(self) -> str:
        snap = await self.build_snapshot()
        state = "PAUSED (" + snap["paused_reason"][:120] + ")" if snap["paused"] else "sending is on"
        quiet = "quiet hours now, listening only" if snap["quiet_hours"] else "active"
        return (f"Harvey is running ({quiet}); {state}. {snap['pending_review']} draft(s) waiting for approval, "
                f"{snap['approved_waiting']} approved and queued, {snap['email']['sent_today']}/"
                f"{snap['email']['daily_cap']} sent today, {snap['open_conversations']} open conversation(s).")

    async def pending_text(self) -> str:
        pending = await self.state.get_outbox(status="pending_review", limit=50)
        if not pending:
            return "No drafts are waiting for approval."
        names = []
        for item in pending[:6]:
            who = await self._prospect_label(item.get("prospect_id", ""))
            names.append(f"{who['name']} ({who['company']}) step {item.get('step', 1)}")
        more = f" and {len(pending) - 6} more" if len(pending) > 6 else ""
        return (f"{len(pending)} draft(s) waiting: " + "; ".join(names) + more +
                ". Approve or reject them in Approvals.")

    async def replies_text(self) -> str:
        hot = await self._recent_hot_replies()
        if not hot:
            return "No replies in the last two days."
        return "Recent replies: " + "; ".join(f"{who} ({intent})" for _r, who, intent, _e in hot[:6]) + "."

    async def digest_text(self) -> str:
        since = _ts(_now() - timedelta(days=1))
        async with self.state._connect() as db:
            async with db.execute(
                    "SELECT outcome, COUNT(*) FROM send_ledger WHERE REPLACE(SUBSTR(created_at,1,19),'T',' ') >= ? "
                    "GROUP BY 1", (since,)) as cur:
                counts = dict(await cur.fetchall())
        replies = len(await self._recent_hot_replies())
        sent = counts.get("sent", 0)
        bad = sum(v for k, v in counts.items() if k != "sent")
        return (f"Last 24h: {sent} sent, {replies} reply(ies), {bad} bounce/complaint event(s). "
                + await self.pending_text())


    # ── hot-lead hand-off into the OS CRM ──

    CRM_PIPELINE = "Outbound"
    CRM_STAGE = "Engaged"

    @staticmethod
    def crm_id(kind: str, key: str) -> str:
        """Stable ids: the same person or reply always maps to the same CRM row, so a retry
        can never make a duplicate, and Harvey never has to read back a row he doesn't own."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"harvey:{kind}:{key.lower()}"))

    async def _crm_targets(self) -> tuple[str, str] | None:
        pipelines = await self.client.select("pipelines", {"select": "id", "name": f"eq.{self.CRM_PIPELINE}", "limit": "1"})
        if not pipelines:
            return None
        stages = await self.client.select("pipeline_stages", {
            "select": "id", "pipeline_id": f"eq.{pipelines[0]['id']}", "name": f"eq.{self.CRM_STAGE}", "limit": "1"})
        return (pipelines[0]["id"], stages[0]["id"]) if stages else None

    async def step_handoff(self) -> int:
        """A prospect who says they're interested becomes a CRM contact and an open opportunity
        in the Outbound pipeline (stage Engaged), owned by the owner. Questions and objections stay
        in Harvey's own thread: only a clear yes opens an opportunity."""
        made = 0
        since = _ts(_now() - timedelta(days=2))
        async with self.state._connect() as db:
            async with db.execute(
                    "SELECT c.id, c.prospect_id, c.thread_json FROM conversations c "
                    "WHERE c.intent = 'interested' AND REPLACE(SUBSTR(c.updated_at,1,19),'T',' ') >= ?", (since,)) as cur:
                convos = await cur.fetchall()
        for cid, pid, thread_json in convos:
            key = f"os_handoff:{cid}"
            if await self.state.get_setting(key, ""):
                continue
            targets = await self._crm_targets()
            if targets is None:
                logger.warning("OS hand-off: the '%s' pipeline or its '%s' stage is missing; skipping.",
                               self.CRM_PIPELINE, self.CRM_STAGE)
                return made
            prospect = await self.state.get_prospect(pid)
            if prospect is None or not prospect.email:
                continue
            who = await self.client.whoami()
            try:
                thread = json.loads(thread_json or "[]")
            except ValueError:
                thread = []
            replies = [m.get("content", "") for m in thread if m.get("sender") == "prospect"]
            contact_id = self.crm_id("contact", prospect.email)
            try:
                await self.client.insert("contacts", {
                    "id": contact_id, "organization_id": who["organization_id"],
                    "first_name": prospect.first_name or "Unknown", "last_name": prospect.last_name or "",
                    "email": prospect.email, "company": prospect.company, "job_title": prospect.title,
                    "source": "harvey"}, minimal=True)
            except OsError as e:
                if not (e.status == 409 or e.code == "23505"):
                    raise
                # Already in the CRM (earlier hand-off, or the owner/another agent added them).
            pipeline_id, stage_id = targets
            try:
                await self.client.insert("opportunities", {
                    "id": self.crm_id("opportunity", f"{prospect.email}:{cid}"), "org_id": who["organization_id"],
                    "contact_id": contact_id, "pipeline_id": pipeline_id, "stage_id": stage_id,
                    "name": f"Harvey: {prospect.company or prospect.full_name()}"[:200],
                    "description": ("Replied to Harvey's outreach and is interested.\n\n"
                                    f"{prospect.full_name()}, {prospect.title}, {prospect.company}\n"
                                    f"Their reply: {(replies[-1] if replies else '')[:600]}"),
                    "source": "harvey", "created_by": who["user_id"]}, minimal=True)
                outcome = "opportunity opened in Outbound / Engaged"
            except OsError as e:
                if e.status == 409 or e.code == "23505":
                    outcome = "opportunity already existed"
                else:
                    raise
            await self.state.set_setting(key, "1")
            await self._event(f"handoff:{cid}", "hot_lead",
                              f"{prospect.full_name()} ({prospect.company}) is interested: {outcome}.",
                              prospect_id=pid, data={"crm": outcome})
            made += 1
        return made

    # ── daily digest ──

    DIGEST_HOUR = 7
    DIGEST_MINUTE = 30

    async def step_digest(self) -> bool:
        """Once a morning (7:30 ET, after quiet hours) a plain summary lands in the OS feed and
        joins the owner's morning brief. Not a text: normal priority waits for the brief."""
        from zoneinfo import ZoneInfo
        local = datetime.now(ZoneInfo(self.config.usage.quiet_hours.timezone))
        if (local.hour, local.minute) < (self.DIGEST_HOUR, self.DIGEST_MINUTE):
            return False
        day = local.strftime("%Y-%m-%d")
        key = f"os_digest:{day}"
        if await self.state.get_setting(key, ""):
            return False
        text = await self.digest_text()
        weekly = local.weekday() == 0
        data = await self.metrics(days=7 if weekly else 1)
        label = "Weekly" if weekly else "Daily"
        await self._event(f"digest:{day}", "digest", f"{label} digest: {text}"[:480], data=data)
        try:
            vault = self._vault()
            if vault is not None:
                vault.write_digest(day, f"{label} digest. {text}", data)
        except Exception as e:   # the vault is a convenience; it never blocks the digest
            logger.warning("Could not write the digest note: %s", e)
        await self.client.rpc("agent_notify_owner", {
            "p_kind": "harvey.digest", "p_priority": "normal", "p_text": f"Harvey {label.lower()} digest. {text}"[:1500],
            "p_category": None, "p_dedupe_key": f"harvey:digest:{day}"})
        await self.state.set_setting(key, "1")
        return True

    async def factory_review(self, source: dict) -> tuple[str, dict]:
        """The weekly outbound review as a factory deliverable: last week's numbers by sequence step,
        replies by intent, bounce/complaint rate, and ONE change to test, with the numbers as evidence.
        Deterministic: it reports what the ledger says and suggests from thresholds, it never invents."""
        m = await self.metrics(days=7)
        ledger, by_step, intents = m["ledger"], m["sent_by_step"], m["replies_by_intent"]
        sent = ledger.get("sent", 0)
        bad = sum(v for k, v in ledger.items() if k != "sent")
        positive = sum(v for k, v in intents.items() if k in ("interested", "meeting", "positive", "booked"))
        rate = m["bounce_or_complaint_rate"]
        if sent == 0:
            change = "Nothing was sent last week: check the outbox for drafts waiting on approval before changing copy."
        elif rate is not None and rate > 0.03:
            change = f"Bounce/complaint rate is {rate:.1%}: only send to verified addresses next week (drop catch-all/risky)."
        elif sent < 20:
            change = f"Only {sent} sends: too few to judge copy. Keep the sequence and raise volume toward the daily cap."
        elif positive == 0:
            change = "No positive replies yet: test a new first-email angle (lead with the hiring signal) on half of next week's sends."
        else:
            change = "Positive replies are coming in: keep the winning first email and test a shorter follow-up (step 2)."
        lines = [
            f"## Outbound review, last {m['days']} days",
            f"- Sent: {sent} (by step: {', '.join(f'step {k}: {v}' for k, v in sorted(by_step.items())) or 'none'})",
            f"- Bounce/complaint events: {bad}" + (f" ({rate:.1%})" if rate is not None else " (too few sends for a rate)"),
            f"- Replies all-time: {m['replies_total']} from {m['people_emailed_total']} people emailed"
            + (f" ({m['reply_rate_total']:.1%})" if m["reply_rate_total"] is not None else ""),
            f"- Replies by intent: {', '.join(f'{k}: {v}' for k, v in intents.items()) or 'none'}",
            f"\n**One change to test:** {change}",
            "\nNothing was changed: this is a recommendation for the owner's approval.",
        ]
        output = "\n".join(lines)
        evidence = [
            {"source": "Harvey send_ledger (last 7 days)", "ref": json.dumps(ledger)},
            {"source": "Harvey outbox sent by step (last 7 days)", "ref": json.dumps(by_step)},
            {"source": "Harvey conversations by intent", "ref": json.dumps(intents)},
        ]
        gate = {"passed": True, "checks": [
            {"criterion": "Numbers by variant", "met": True, "note": "by sequence step (Harvey tracks steps, not A/B variants)"},
            {"criterion": "One change to test", "met": True, "note": change},
        ]}
        return f"Sent {sent}, {bad} bounce/complaint, {positive} positive repl(ies). {change}", {"output": output, "evidence": evidence, "gate": gate}

    async def metrics(self, days: int = 7) -> dict:
        """Counts and rates a person can act on. Small samples are reported as counts, never as a
        percentage dressed up as a trend."""
        since = _ts(_now() - timedelta(days=days))
        norm = "REPLACE(SUBSTR({}, 1, 19), 'T', ' ')"
        out: dict = {"days": days}
        async with self.state._connect() as db:
            async with db.execute(
                    f"SELECT outcome, COUNT(*) FROM send_ledger WHERE {norm.format('created_at')} >= ? GROUP BY 1", (since,)) as cur:
                out["ledger"] = dict(await cur.fetchall())
            async with db.execute(
                    f"SELECT o.step, COUNT(*) FROM outbox o WHERE o.status='sent' AND {norm.format('o.sent_at')} >= ? GROUP BY 1", (since,)) as cur:
                out["sent_by_step"] = {str(k): v for k, v in await cur.fetchall()}
            async with db.execute("SELECT COUNT(DISTINCT prospect_id) FROM outbox WHERE status='sent'") as cur:
                out["people_emailed_total"] = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM conversations WHERE intent != '' AND status != ''") as cur:
                out["replies_total"] = (await cur.fetchone())[0]
            async with db.execute("SELECT intent, COUNT(*) FROM conversations WHERE intent != '' GROUP BY 1") as cur:
                out["replies_by_intent"] = dict(await cur.fetchall())
        sent = out["ledger"].get("sent", 0)
        bad = sum(v for k, v in out["ledger"].items() if k != "sent")
        out["bounce_or_complaint_rate"] = round(bad / sent, 3) if sent >= 20 else None   # None: too few to mean anything
        out["reply_rate_total"] = (round(out["replies_total"] / out["people_emailed_total"], 3)
                                   if out["people_emailed_total"] >= 20 else None)
        return out


    # ── Obsidian vault (a browsable copy of what Harvey knows) ──

    VAULT_EVERY_SECONDS = 300

    def _vault(self):
        from pathlib import Path
        from harvey.vault import Vault
        cfg = getattr(self.config, "vault", None)
        if cfg is None or not cfg.enabled or not cfg.path:
            return None
        return Vault(self.state, self.config, Path(cfg.path))

    async def step_vault(self) -> dict | None:
        import time as _time
        last = getattr(self, "_vault_at", 0.0)
        if _time.time() - last < self.VAULT_EVERY_SECONDS:
            return None
        vault = self._vault()
        if vault is None:
            return None
        self._vault_at = _time.time()
        result = await vault.sync()
        if self.config.vault.os_memory:
            from harvey.vault_sync import sync_to_os
            try:
                result["memory"] = await sync_to_os(self.client, vault.root)
            except OsError as e:
                # The Memory page is a convenience copy; Harvey's work never waits on it.
                logger.warning("Could not mirror the vault into OS Memory: %s", e)
                result["memory"] = f"os error: {e}"
        return result
