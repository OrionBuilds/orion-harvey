"""SQLite state manager. All of Harvey's memory lives here.

Concurrency: the DB runs in WAL mode (set persistently at init) so the
dashboard can read while the agent writes. Every connection gets a busy
timeout so concurrent writers wait instead of raising "database is locked".

Migrations: schema changes are applied via a linear, idempotent migration
list tracked with SQLite's ``PRAGMA user_version`` so existing user DBs
upgrade cleanly in place.
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import sqlite3

import aiosqlite

from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.models.campaign import Campaign, EmailStep  # noqa: F401 (EmailStep re-exported)
from harvey.models.conversation import Conversation, Message  # noqa: F401

from harvey.paths import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "harvey.db"

# How long (seconds) a connection waits on a locked database before failing.
BUSY_TIMEOUT_SECONDS = 30.0


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow() -> datetime:
    """Naive UTC now (matches how timestamps are stored in the DB)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm(value: str | None) -> str:
    """Normalize an identity key (email/domain) for dedup: strip + lowercase."""
    return (value or "").strip().lower()


def norm_phone(value: str | None) -> str:
    """Digits only, US numbers canonicalized to 10 digits, so formats compare equal."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def norm_linkedin(value: str | None) -> str:
    """Canonical LinkedIn profile key: lowercase, no scheme/www/query/trailing slash."""
    v = (value or "").strip().lower().split("?")[0].split("#")[0].rstrip("/")
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    if v.startswith("www."):
        v = v[4:]
    return v


SUPPRESSION_KINDS = ("email", "domain", "company", "phone", "linkedin")


# ── Schema migrations ─────────────────────────────────────────────────
# Each entry is an idempotent SQL script. The index into this list + 1 is
# the schema version stored in PRAGMA user_version. Never edit or reorder
# released migrations — append new ones.

MIGRATIONS: list[str] = [
    # ── v1: base schema (idempotent, so pre-migration DBs adopt cleanly) ──
    """
    CREATE TABLE IF NOT EXISTS companies (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        domain TEXT DEFAULT '',
        website TEXT DEFAULT '',
        description TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        location TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS prospects (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '' REFERENCES companies(id),
        first_name TEXT DEFAULT '',
        last_name TEXT DEFAULT '',
        email TEXT DEFAULT '',
        email_verified INTEGER DEFAULT 0,
        phone TEXT DEFAULT '',
        phone_verified INTEGER DEFAULT 0,
        linkedin_url TEXT DEFAULT '',
        title TEXT DEFAULT '',
        seniority TEXT DEFAULT '',
        department TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        score INTEGER DEFAULT 0,
        personalization_notes TEXT DEFAULT '',
        company TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        instantly_campaign_id TEXT DEFAULT '',
        sequence_json TEXT DEFAULT '[]',
        prospect_ids_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'draft',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        prospect_id TEXT REFERENCES prospects(id),
        campaign_id TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        thread_json TEXT DEFAULT '[]',
        intent TEXT DEFAULT '',
        stage TEXT DEFAULT 'initial_outreach',
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY,
        entity_type TEXT DEFAULT '',
        entity_id TEXT DEFAULT '',
        comment TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY,
        action_type TEXT,
        agent TEXT,
        details_json TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS usage_log (
        id TEXT PRIMARY KEY,
        date TEXT UNIQUE,
        claude_calls INTEGER DEFAULT 0,
        usage_percent REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS processed_replies (
        reply_id TEXT PRIMARY KEY,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
    CREATE INDEX IF NOT EXISTS idx_prospects_company_id ON prospects(company_id);
    CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
    CREATE INDEX IF NOT EXISTS idx_prospects_email ON prospects(email);
    CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
    CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
    CREATE INDEX IF NOT EXISTS idx_feedback_entity ON feedback(entity_type, entity_id);
    CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_log(date);
    """,
    # ── v2: normalize identity keys, dedup, uniqueness, hot-path indexes ──
    """
    -- Normalize emails/domains so uniqueness is case-insensitive going forward.
    UPDATE prospects SET email = LOWER(TRIM(email)) WHERE email != LOWER(TRIM(email));
    UPDATE companies SET domain = LOWER(TRIM(domain)) WHERE domain != LOWER(TRIM(domain));

    -- Dedup existing rows (keep the earliest) so unique indexes can be built.
    DELETE FROM prospects WHERE email != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM prospects WHERE email != '' GROUP BY email
    );
    DELETE FROM prospects WHERE linkedin_url != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM prospects WHERE linkedin_url != '' GROUP BY linkedin_url
    );
    DELETE FROM companies WHERE domain != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM companies WHERE domain != '' GROUP BY domain
    );

    -- Enforce uniqueness at the DB level (partial: blank values allowed).
    CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_email
        ON prospects(email) WHERE email != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_linkedin
        ON prospects(linkedin_url) WHERE linkedin_url != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_domain
        ON companies(domain) WHERE domain != '';

    -- Hot-path indexes: campaign stats subqueries, reply handling, dedup checks.
    CREATE INDEX IF NOT EXISTS idx_conversations_campaign_id ON conversations(campaign_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_prospect_id ON conversations(prospect_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_intent ON conversations(intent);
    CREATE INDEX IF NOT EXISTS idx_prospects_name_company
        ON prospects(LOWER(first_name), LOWER(last_name), LOWER(company));
    CREATE INDEX IF NOT EXISTS idx_prospects_status_updated ON prospects(status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_actions_created_at ON actions(created_at);
    """,
    # ── v3: per-call usage accounting (tokens, cost, attribution) ──
    """
    CREATE TABLE IF NOT EXISTS usage_events (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        agent TEXT DEFAULT '',
        task TEXT DEFAULT '',
        session_id TEXT DEFAULT '',
        request_key TEXT DEFAULT '',
        model TEXT DEFAULT '',
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        duration_ms INTEGER DEFAULT 0,
        num_turns INTEGER DEFAULT 0,
        is_error INTEGER DEFAULT 0,
        source TEXT DEFAULT 'result_json'
    );

    CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at);
    CREATE INDEX IF NOT EXISTS idx_usage_events_agent ON usage_events(agent);
    CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
    -- Transcript-backfilled rows carry a request_key; uniqueness makes
    -- reconciliation idempotent (INSERT OR IGNORE).
    CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_events_request
        ON usage_events(request_key) WHERE request_key != '';
    """,
    # ── v4: honest email statuses + per-domain pattern cache ──
    """
    -- verified: a provider confirmed the mailbox exists
    -- risky:    catch-all / accept-all domain — sendable only in low volume
    -- guess:    pattern guess, never verified — never auto-sent
    -- invalid:  provider said undeliverable
    ALTER TABLE prospects ADD COLUMN email_status TEXT DEFAULT '';

    -- Backfill: the old email_verified flag over-reported (pattern guesses
    -- at any MX-bearing domain were marked verified), so every existing
    -- address is downgraded to an honest 'guess' and must re-verify.
    UPDATE prospects SET email_status = 'guess', email_verified = 0
        WHERE email != '';

    CREATE TABLE IF NOT EXISTS email_patterns (
        domain TEXT PRIMARY KEY,
        pattern TEXT DEFAULT '',
        source TEXT DEFAULT '',
        confidence REAL DEFAULT 0.0,
        mx_type TEXT DEFAULT '',
        is_catch_all INTEGER DEFAULT -1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── v5: buying signals on companies (tech stack, hiring, etc.) ──
    """
    ALTER TABLE companies ADD COLUMN tech_stack_json TEXT DEFAULT '[]';
    ALTER TABLE companies ADD COLUMN signals_json TEXT DEFAULT '[]';
    """,
    # ── v6: outbox (approval ladder + native sending) and settings KV ──
    """
    -- Every outgoing email becomes an outbox row first. Status ladder:
    --   pending_review -> approved -> sent
    --   (or rejected / cancelled / failed)
    -- The unique index on (campaign_id, prospect_id, step) is the
    -- double-send guard: retries and re-stages physically cannot
    -- duplicate a send.
    CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY,
        campaign_id TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        conversation_id TEXT DEFAULT '',
        step INTEGER DEFAULT 1,
        kind TEXT DEFAULT 'sequence',
        to_email TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        status TEXT DEFAULT 'pending_review',
        send_at TIMESTAMP,
        sent_at TIMESTAMP,
        provider TEXT DEFAULT '',
        message_id TEXT DEFAULT '',
        thread_ref TEXT DEFAULT '',
        in_reply_to TEXT DEFAULT '',
        error TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_campaign_step
        ON outbox(campaign_id, prospect_id, step)
        WHERE campaign_id != '' AND kind = 'sequence';
    CREATE INDEX IF NOT EXISTS idx_outbox_status_send_at ON outbox(status, send_at);
    CREATE INDEX IF NOT EXISTS idx_outbox_prospect ON outbox(prospect_id);

    -- Simple key/value store for operational flags (kill switch, counters).
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── v7: observation model — every fact is a row, never a column ──
    """
    -- The governed vocabulary. A collector may only emit a code that exists
    -- here (enforced by the observations FK), so a typo fails loudly instead
    -- of quietly inventing a junk signal. `status` is the user-confirmation
    -- gate: Harvey PROPOSES signals, the user confirms which ones to
    -- prospect against, and only confirmed signals get collected.
    CREATE TABLE IF NOT EXISTS signal_codes (
        code TEXT PRIMARY KEY,
        label TEXT DEFAULT '',
        description TEXT DEFAULT '',
        category TEXT DEFAULT '',
        value_type TEXT DEFAULT 'text',
        collector TEXT DEFAULT '',
        cost_note TEXT DEFAULT '',
        status TEXT DEFAULT 'proposed',
        confidence_floor REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- One row per fact. Confidence and provenance travel WITH the fact, and
    -- re-observing the same signal over time is a free time series.
    CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        signal_code TEXT NOT NULL,
        collector TEXT DEFAULT '',
        value_num REAL,
        value_text TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        evidence_url TEXT DEFAULT '',
        observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        run_id TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_obs_company ON observations(company_id);
    CREATE INDEX IF NOT EXISTS idx_obs_signal ON observations(signal_code);
    CREATE INDEX IF NOT EXISTS idx_obs_company_signal
        ON observations(company_id, signal_code, observed_at);
    CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);

    -- The governed-vocabulary guarantee. A trigger (not a foreign key) so it
    -- holds regardless of the per-connection foreign_keys pragma, and applies
    -- only here: the legacy tables default several id columns to '' and would
    -- break under blanket FK enforcement.
    CREATE TRIGGER IF NOT EXISTS trg_observations_signal_known
    BEFORE INSERT ON observations
    FOR EACH ROW
    WHEN NEW.signal_code NOT IN (SELECT code FROM signal_codes)
    BEGIN
        SELECT RAISE(ABORT, 'unknown signal_code: not in signal_codes vocabulary');
    END;

    -- A log of what ran, when, how much it produced and cost. Purely a
    -- record: spend is capped inside the collector, never here.
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        stage TEXT DEFAULT '',
        status TEXT DEFAULT 'running',
        provider TEXT DEFAULT '',
        records INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        params_json TEXT DEFAULT '{}',
        error TEXT DEFAULT '',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_runs_stage_started ON runs(stage, started_at);
    """,
    # ── v8: discovery — entity resolution for businesses without a website ──
    """
    -- The best prospect for anyone selling websites is a business that has
    -- none, so `domain` cannot be the only identity key. `external_id` holds
    -- the provider's stable id ("dataforseo:ChIJ...", "osm:node/123") and is
    -- what dedups a re-run for those records.
    ALTER TABLE companies ADD COLUMN external_id TEXT DEFAULT '';
    ALTER TABLE companies ADD COLUMN phone TEXT DEFAULT '';

    CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_external
        ON companies(external_id) WHERE external_id != '';
    """,

    # ── v9: track prospect-backfill attempts on known companies ──
    """
    -- Without this, a company that gets scraped for contacts and comes up
    -- empty (no team page, site blocked, etc.) is indistinguishable from one
    -- never attempted, so the backfill pass would retry the same handful of
    -- companies forever instead of rotating through the rest.
    ALTER TABLE companies ADD COLUMN prospects_checked_at TIMESTAMP DEFAULT NULL;
    """,

    # ── v10: same fix, for the profile collector ──
    """
    -- The profile collector only ever wrote an observation on a SUCCESSFUL
    -- fetch; an unreachable site produced zero rows despite the code's own
    -- comment claiming "a failure IS an observation." Staleness was judged
    -- purely from observations, so an unreachable company never looked
    -- checked and the same handful got retried every cycle forever.
    -- Backfill from existing observations so already-profiled companies
    -- aren't treated as unchecked just because this column is new.
    ALTER TABLE companies ADD COLUMN profile_checked_at TIMESTAMP DEFAULT NULL;

    UPDATE companies SET profile_checked_at = (
        SELECT MAX(observed_at) FROM observations
        WHERE observations.company_id = companies.id
          AND observations.collector = 'profile'
    ) WHERE id IN (
        SELECT DISTINCT company_id FROM observations WHERE collector = 'profile'
    );
    """,
    # ── v11: which subscription each call ran on (claude | codex) ──
    """
    -- Every row before this migration was a Claude call.
    ALTER TABLE usage_events ADD COLUMN provider TEXT DEFAULT 'claude';
    CREATE INDEX IF NOT EXISTS idx_usage_events_provider ON usage_events(provider);

    -- claude_calls keeps meaning Claude calls; Codex calls count separately
    -- so one subscription's budget never consumes the other's.
    ALTER TABLE usage_log ADD COLUMN codex_calls INTEGER DEFAULT 0;
    """,
    # ── v12: channel-neutral outbox, suppressions, send ledger ──
    """
    BEGIN;

    -- The outbox was email-shaped. `to_ref` is the channel-neutral address
    -- (email | LinkedIn profile URL | E.164 phone); `payload_json` carries
    -- channel extras (e.g. a LinkedIn connection note).
    ALTER TABLE outbox ADD COLUMN channel TEXT DEFAULT 'email';
    ALTER TABLE outbox ADD COLUMN to_ref TEXT DEFAULT '';
    ALTER TABLE outbox ADD COLUMN payload_json TEXT DEFAULT '{}';
    UPDATE outbox SET to_ref = to_email WHERE to_ref = '';

    -- Double-send guard now includes the channel: the same step number on
    -- email and LinkedIn are different touches.
    DROP INDEX IF EXISTS uq_outbox_campaign_step;
    CREATE UNIQUE INDEX uq_outbox_campaign_step
        ON outbox(channel, campaign_id, prospect_id, step)
        WHERE campaign_id != '' AND kind = 'sequence';
    CREATE INDEX IF NOT EXISTS idx_outbox_channel_status
        ON outbox(channel, status, send_at);

    ALTER TABLE prospects ADD COLUMN phone_status TEXT DEFAULT '';
    ALTER TABLE prospects ADD COLUMN consent_json TEXT DEFAULT '{}';
    CREATE INDEX IF NOT EXISTS idx_prospects_phone ON prospects(phone);
    CREATE INDEX IF NOT EXISTS idx_prospects_linkedin ON prospects(linkedin_url);

    ALTER TABLE conversations ADD COLUMN external_thread_id TEXT DEFAULT '';

    -- Do-not-contact list. kind: email | domain | company | phone | linkedin.
    -- channel 'all' blocks every channel. Rows may expire (e.g. a 180-day
    -- "not interested" company cooldown).
    CREATE TABLE IF NOT EXISTS suppressions (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        value_key TEXT NOT NULL,
        channel TEXT DEFAULT 'all',
        reason TEXT DEFAULT '',
        source TEXT DEFAULT '',
        expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE UNIQUE INDEX IF NOT EXISTS uq_suppressions
        ON suppressions(kind, value_key, channel);

    -- One row per send action. Caps, per-contact/company frequency and the
    -- bounce/complaint breakers all read this; `outcome` is updated when a
    -- bounce or complaint arrives for the send.
    CREATE TABLE IF NOT EXISTS send_ledger (
        id TEXT PRIMARY KEY,
        channel TEXT NOT NULL,
        prospect_id TEXT DEFAULT '',
        company_id TEXT DEFAULT '',
        sender_domain TEXT DEFAULT '',
        ref TEXT DEFAULT '',
        outcome TEXT DEFAULT 'sent',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_ledger_channel_created
        ON send_ledger(channel, created_at);
    CREATE INDEX IF NOT EXISTS idx_ledger_prospect ON send_ledger(prospect_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_ledger_company ON send_ledger(company_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_ledger_ref ON send_ledger(ref);

    -- Sends that happened before the ledger existed still count toward caps.
    INSERT INTO send_ledger (id, channel, prospect_id, ref, outcome, created_at)
        SELECT 'bf' || id, 'email', prospect_id, id, 'sent',
               REPLACE(SUBSTR(COALESCE(sent_at, created_at), 1, 19), 'T', ' ')
        FROM outbox WHERE status = 'sent';

    COMMIT;
    """,
    # ── v13: provider message ids + one-click unsubscribe tokens ──
    """
    BEGIN;

    -- The sending provider's own id for a message (e.g. Resend's email id).
    -- Bounce and complaint webhooks identify mail by it, not by Message-ID.
    ALTER TABLE outbox ADD COLUMN provider_id TEXT DEFAULT '';
    CREATE INDEX IF NOT EXISTS idx_outbox_provider_id
        ON outbox(provider_id) WHERE provider_id != '';

    -- One token per sent message, embedded in its unsubscribe link. It maps
    -- back to the prospect without putting an email address in a URL.
    CREATE TABLE IF NOT EXISTS unsub_tokens (
        token TEXT PRIMARY KEY,
        prospect_id TEXT NOT NULL,
        outbox_id TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        used_at TIMESTAMP
    );

    COMMIT;
    """,
    # ── v14: LinkedIn relationship state + per-kind send ledger ──
    """
    BEGIN;

    -- Where each prospect stands on LinkedIn. One row per prospect.
    --   staged -> requested -> connected -> messaged -> replied
    --   (or withdrawn: the invite sat unanswered and expired)
    CREATE TABLE IF NOT EXISTS linkedin_threads (
        prospect_id TEXT PRIMARY KEY,
        state TEXT DEFAULT 'staged',
        requested_at TIMESTAMP,
        accepted_at TIMESTAMP,
        last_checked_at TIMESTAMP,
        messages_sent INTEGER DEFAULT 0,
        last_message_at TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_linkedin_threads_state ON linkedin_threads(state);

    -- LinkedIn has separate daily limits for connection requests and messages,
    -- so the ledger has to say which a row was.
    ALTER TABLE send_ledger ADD COLUMN kind TEXT DEFAULT '';
    CREATE INDEX IF NOT EXISTS idx_ledger_channel_kind
        ON send_ledger(channel, kind, created_at);

    COMMIT;
    """,
]

def _utc_today() -> str:
    """The usage_log day. UTC, to match SQLite's date('now') used by the dashboard."""
    return datetime.now(timezone.utc).date().isoformat()


BRAIN_PROVIDERS = ("claude", "codex", "local")
_USAGE_LOG_COLUMN = {"claude": "claude_calls", "codex": "codex_calls"}

# When a company was last profiled: the newer of the explicit check stamp
# (set on every attempt, reachable or not) and its latest profile observation
# (written by any path, including ones that never stamp the column).
_LAST_PROFILED_JOIN = """LEFT JOIN (
                       SELECT company_id, MAX(observed_at) AS last_seen
                       FROM observations WHERE collector = 'profile'
                       GROUP BY company_id
                   ) p ON p.company_id = c.id"""
_LAST_PROFILED = (
    "(CASE WHEN p.last_seen IS NULL OR c.profile_checked_at > p.last_seen"
    " THEN c.profile_checked_at ELSE p.last_seen END)"
)

# Column whitelists for dynamic UPDATEs (prevents SQL injection via kwargs).
_CAMPAIGN_COLUMNS = frozenset({
    "name", "channel", "instantly_campaign_id",
    "sequence_json", "prospect_ids_json", "status",
})
_CONVERSATION_COLUMNS = frozenset({
    "prospect_id", "campaign_id", "channel",
    "thread_json", "intent", "stage", "status",
})


class StateManager:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(DB_PATH)

    @asynccontextmanager
    async def _connect(self):
        """Open a connection with sane concurrency settings.

        `timeout` maps to SQLite's busy handler, so writers wait for locks
        (e.g. while the dashboard holds a read) instead of erroring.
        """
        db = await aiosqlite.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            yield db
        finally:
            await db.close()

    async def init_db(self):
        """Create/upgrade the schema. Safe to call on every startup."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            # WAL is persistent in the DB file: readers (dashboard) never
            # block the writer (agent) and vice versa.
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")

            async with db.execute("PRAGMA user_version") as cursor:
                (version,) = await cursor.fetchone()

            for target, script in enumerate(MIGRATIONS, start=1):
                if version < target:
                    await db.executescript(script)
                    await db.execute(f"PRAGMA user_version = {target}")
                    await db.commit()

    # ── Companies ──

    @staticmethod
    def _company_from_row(row: aiosqlite.Row) -> Company:
        d = dict(row)
        for json_col, field in (("tech_stack_json", "tech_stack"), ("signals_json", "signals")):
            raw = d.pop(json_col, None)
            try:
                parsed = json.loads(raw) if raw else []
            except (json.JSONDecodeError, TypeError):
                parsed = []
            d[field] = parsed if isinstance(parsed, list) else []
        return Company(**d)

    async def add_company(self, company: Company) -> str:
        """Insert a company, or return the id of the one already recorded.

        Identity is the normalised domain when there is one, and the
        provider's ``external_id`` when there isn't — a business with no
        website still has to dedup across re-runs, and for anyone selling
        websites those are the best prospects on the list.
        """
        if not company.id:
            company.id = _new_id()
        company.domain = _norm(company.domain)
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO companies
                   (id, name, domain, website, description, industry,
                    company_size, location, phone, source, source_url,
                    external_id, notes, tech_stack_json, signals_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    company.id, company.name, company.domain, company.website,
                    company.description, company.industry, company.company_size,
                    company.location, company.phone, company.source,
                    company.source_url, company.external_id, company.notes,
                    json.dumps(company.tech_stack), json.dumps(company.signals),
                    company.created_at.isoformat(),
                    company.updated_at.isoformat(),
                ),
            )
            await db.commit()
            if cursor.rowcount == 0:
                # Uniqueness conflict: hand back the existing record's id.
                for column, value in (("domain", company.domain),
                                      ("external_id", company.external_id)):
                    if not value:
                        continue
                    async with db.execute(
                        f"SELECT id FROM companies WHERE {column} = ?", (value,)
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            company.id = row[0]
                            break
        return company.id

    async def update_company_signals(
        self,
        company_id: str,
        tech_stack: list[str] | None = None,
        new_signals: list[dict] | None = None,
    ):
        """Merge freshly-detected tech + signals into a company record.

        Signals are appended with dedup on (type, detail) so re-scans
        don't multiply the same finding.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT tech_stack_json, signals_json FROM companies WHERE id = ?",
                (company_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return

            def _load(raw):
                try:
                    parsed = json.loads(raw) if raw else []
                except (json.JSONDecodeError, TypeError):
                    parsed = []
                return parsed if isinstance(parsed, list) else []

            tech = _load(row["tech_stack_json"])
            signals = _load(row["signals_json"])

            for t in tech_stack or []:
                if t not in tech:
                    tech.append(t)
            seen = {(s.get("type"), s.get("detail")) for s in signals if isinstance(s, dict)}
            for s in new_signals or []:
                if not isinstance(s, dict):
                    continue
                if (s.get("type"), s.get("detail")) in seen:
                    continue
                signals.append(s)
                seen.add((s.get("type"), s.get("detail")))

            await db.execute(
                "UPDATE companies SET tech_stack_json = ?, signals_json = ?, "
                "updated_at = ? WHERE id = ?",
                (json.dumps(tech), json.dumps(signals),
                 _utcnow().isoformat(), company_id),
            )
            await db.commit()

    async def get_company(self, company_id: str) -> Company | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE id = ?", (company_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._company_from_row(row) if row else None

    async def get_company_by_domain(self, domain: str) -> Company | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                row = await cursor.fetchone()
                return self._company_from_row(row) if row else None

    async def get_company_by_external_id(self, external_id: str) -> Company | None:
        """Look a company up by its provider-stable id.

        The identity path for businesses with no website — which is exactly
        the cohort worth the most to anyone selling one.
        """
        if not external_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE external_id = ?", (external_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._company_from_row(row) if row else None

    async def get_contacts_for_company(self, company_id: str) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
                (company_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def company_exists(self, domain: str) -> bool:
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM companies WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                return bool(await cursor.fetchone())

    # ── Prospects (Contacts) ──

    @staticmethod
    def _prospect_from_row(row: aiosqlite.Row) -> Prospect:
        d = dict(row)
        d["email_verified"] = bool(d.get("email_verified", 0))
        d["phone_verified"] = bool(d.get("phone_verified", 0))
        d["email_status"] = d.get("email_status") or ""
        return Prospect(**d)

    async def add_prospect(self, prospect: Prospect) -> str:
        """Insert a prospect. Duplicates (same email or LinkedIn URL) are not
        re-inserted; the existing record's id is returned instead."""
        if not prospect.id:
            prospect.id = _new_id()
        prospect.email = _norm(prospect.email)
        prospect.linkedin_url = (prospect.linkedin_url or "").strip()
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO prospects
                   (id, company_id, first_name, last_name, email, email_verified,
                    email_status, phone, phone_verified, linkedin_url, title,
                    seniority, department, source, source_url, status, score,
                    personalization_notes, company, industry, company_size,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prospect.id, prospect.company_id,
                    prospect.first_name, prospect.last_name,
                    prospect.email, int(prospect.email_verified),
                    prospect.email_status,
                    prospect.phone, int(prospect.phone_verified),
                    prospect.linkedin_url, prospect.title,
                    prospect.seniority, prospect.department,
                    prospect.source, prospect.source_url,
                    prospect.status, prospect.score,
                    prospect.personalization_notes,
                    prospect.company, prospect.industry, prospect.company_size,
                    prospect.created_at.isoformat(),
                    prospect.updated_at.isoformat(),
                ),
            )
            await db.commit()
            if cursor.rowcount == 0:
                # Unique-constraint conflict: resolve to the existing record.
                for column, value in (
                    ("email", prospect.email),
                    ("linkedin_url", prospect.linkedin_url),
                    ("id", prospect.id),
                ):
                    if not value:
                        continue
                    async with db.execute(
                        f"SELECT id FROM prospects WHERE {column} = ?", (value,)
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            prospect.id = row[0]
                            break
        return prospect.id

    async def get_prospect(self, prospect_id: str) -> Prospect | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE id = ?", (prospect_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def get_prospects_by_status(self, status: str) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def update_prospect_status(self, prospect_id: str, status: str):
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ?",
                (status, _utcnow().isoformat(), prospect_id),
            )
            await db.commit()

    async def get_prospect_by_email(self, email: str) -> Prospect | None:
        """Look up a prospect by email address (indexed, case-insensitive)."""
        email = _norm(email)
        if not email:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE email = ?", (email,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def update_prospect_email(
        self, prospect_id: str, email: str, email_status: str
    ):
        """Set a prospect's email + honesty status (verified/risky/guess/invalid)."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE prospects
                   SET email = ?, email_status = ?, email_verified = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    _norm(email), email_status,
                    1 if email_status == "verified" else 0,
                    _utcnow().isoformat(), prospect_id,
                ),
            )
            await db.commit()

    # ── Email pattern cache (per-domain) ──

    async def get_email_pattern(self, domain: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM email_patterns WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def save_email_pattern(
        self,
        domain: str,
        pattern: str = "",
        source: str = "",
        confidence: float = 0.0,
        mx_type: str = "",
        is_catch_all: int | None = None,
    ):
        """Upsert what we've learned about a domain's email conventions.

        Only overwrites the stored pattern when the new one has equal or
        higher confidence; mx_type/is_catch_all always refresh.
        """
        domain = _norm(domain)
        if not domain:
            return
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO email_patterns
                       (domain, pattern, source, confidence, mx_type, is_catch_all, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(domain) DO UPDATE SET
                       pattern = CASE WHEN excluded.confidence >= email_patterns.confidence
                                       AND excluded.pattern != ''
                                      THEN excluded.pattern ELSE email_patterns.pattern END,
                       source = CASE WHEN excluded.confidence >= email_patterns.confidence
                                      AND excluded.pattern != ''
                                     THEN excluded.source ELSE email_patterns.source END,
                       confidence = MAX(email_patterns.confidence, excluded.confidence),
                       mx_type = CASE WHEN excluded.mx_type != ''
                                      THEN excluded.mx_type ELSE email_patterns.mx_type END,
                       is_catch_all = CASE WHEN excluded.is_catch_all != -1
                                           THEN excluded.is_catch_all
                                           ELSE email_patterns.is_catch_all END,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    domain, pattern, source, float(confidence), mx_type,
                    -1 if is_catch_all is None else int(is_catch_all),
                ),
            )
            await db.commit()

    # ── Outbox (approval ladder + native sending) ──

    async def add_outbox_item(
        self,
        *,
        prospect_id: str,
        to_email: str,
        subject: str,
        body: str,
        send_at: str,
        status: str = "pending_review",
        campaign_id: str = "",
        conversation_id: str = "",
        step: int = 1,
        kind: str = "sequence",
        provider: str = "",
        thread_ref: str = "",
        in_reply_to: str = "",
        channel: str = "email",
        to_ref: str = "",
        payload: dict | None = None,
    ) -> str | None:
        """Queue one outgoing message. Returns its id, or None when the
        (channel, campaign, prospect, step) slot already exists — the
        double-send guard. Email callers pass `to_email`; other channels pass
        `to_ref` (profile URL / E.164 phone) and may leave `to_email` empty."""
        item_id = _new_id()
        to_email = _norm(to_email)
        to_ref = to_ref or to_email
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO outbox
                   (id, campaign_id, prospect_id, conversation_id, step, kind,
                    to_email, subject, body, status, send_at, provider,
                    thread_ref, in_reply_to, channel, to_ref, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id, campaign_id, prospect_id, conversation_id,
                    int(step), kind, to_email, subject, body,
                    status, send_at, provider, thread_ref, in_reply_to,
                    channel, to_ref, json.dumps(payload or {}),
                ),
            )
            await db.commit()
            return item_id if cursor.rowcount > 0 else None

    async def get_outbox(
        self,
        status: str | None = None,
        due_before: str | None = None,
        limit: int = 200,
        channel: str | None = None,
    ) -> list[dict]:
        where, params = [], []
        if channel:
            where.append("channel = ?")
            params.append(channel)
        if status:
            where.append("status = ?")
            params.append(status)
        if due_before:
            where.append("(send_at IS NULL OR send_at <= ?)")
            params.append(due_before)
        sql = "SELECT * FROM outbox"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY send_at ASC, created_at ASC LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def get_sent_for_prospect(self, prospect_id: str, limit: int = 6) -> list[dict]:
        """The messages already sent to one person, oldest first (their context for a reply)."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT channel, kind, step, subject, body, sent_at FROM outbox "
                "WHERE prospect_id = ? AND status = 'sent' ORDER BY sent_at DESC, rowid DESC LIMIT ?",
                (prospect_id, limit),
            ) as cursor:
                rows = [dict(r) for r in await cursor.fetchall()]
        return rows[::-1]

    async def get_sequence_item(self, campaign_id: str, prospect_id: str, step: int) -> dict | None:
        """One step of one prospect's sequence (None if it was never staged)."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE campaign_id = ? AND prospect_id = ? AND step = ? "
                "AND kind = 'sequence'", (campaign_id, prospect_id, step),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_outbox_item(self, item_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE id = ?", (item_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    _OUTBOX_COLUMNS = frozenset({
        "status", "error", "message_id", "thread_ref", "sent_at",
        "subject", "body", "send_at", "provider", "payload_json", "provider_id",
    })

    async def update_outbox_item(self, item_id: str, **kwargs):
        fields = {k: v for k, v in kwargs.items() if k in self._OUTBOX_COLUMNS}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE outbox SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), _utcnow().isoformat(), item_id),
            )
            await db.commit()

    async def approve_outbox(self, item_id: str | None = None) -> int:
        """Approve one pending item, or ALL pending when item_id is None."""
        async with self._connect() as db:
            if item_id:
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'approved', updated_at = ? "
                    "WHERE id = ? AND status = 'pending_review'",
                    (_utcnow().isoformat(), item_id),
                )
            else:
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'approved', updated_at = ? "
                    "WHERE status = 'pending_review'",
                    (_utcnow().isoformat(),),
                )
            await db.commit()
            return cursor.rowcount

    async def cancel_pending_outbox_for_prospect(
        self, prospect_id: str, reason: str = "stop_on_reply"
    ) -> int:
        """Stop-on-reply: cancel everything queued for a prospect who replied."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
                "WHERE prospect_id = ? AND status IN ('pending_review', 'approved')",
                (reason, _utcnow().isoformat(), prospect_id),
            )
            await db.commit()
            return cursor.rowcount

    async def count_outbox_sent(self) -> int:
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'sent'"
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_outbox_sent_today(self) -> int:
        # Local business day, not UTC: after 8pm Eastern a UTC "today" would
        # reset the daily cap in the middle of the sending window.
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'sent' "
                "AND date(sent_at, 'localtime') = date('now', 'localtime')"
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def find_outbox_by_message_id(self, message_id: str) -> dict | None:
        if not message_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def find_outbox_by_provider_id(self, provider_id: str) -> dict | None:
        if not provider_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE provider_id = ?", (provider_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    # ── One-click unsubscribe tokens ──

    async def mint_unsub_token(self, prospect_id: str, outbox_id: str) -> str:
        """The unsubscribe token for one outgoing message. Idempotent: a message
        that is prepared twice (deferred, then retried) keeps the same link."""
        import secrets
        async with self._connect() as db:
            async with db.execute(
                "SELECT token FROM unsub_tokens WHERE outbox_id = ?", (outbox_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                return row[0]
            token = secrets.token_urlsafe(16)
            await db.execute(
                "INSERT INTO unsub_tokens (token, prospect_id, outbox_id) VALUES (?, ?, ?)",
                (token, prospect_id, outbox_id),
            )
            await db.commit()
            return token

    async def get_unsub_token(self, token: str) -> dict | None:
        if not token:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM unsub_tokens WHERE token = ?", (token,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def mark_unsub_token_used(self, token: str):
        async with self._connect() as db:
            await db.execute(
                "UPDATE unsub_tokens SET used_at = COALESCE(used_at, CURRENT_TIMESTAMP) "
                "WHERE token = ?", (token,))
            await db.commit()

    # ── Send ledger (caps, frequency limits, breakers) ──

    async def record_send(
        self,
        *,
        channel: str,
        prospect_id: str = "",
        company_id: str = "",
        sender_domain: str = "",
        ref: str = "",
        outcome: str = "sent",
        kind: str = "",
    ) -> str:
        entry_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO send_ledger
                   (id, channel, prospect_id, company_id, sender_domain, ref, outcome, kind)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry_id, channel, prospect_id, company_id,
                 _norm(sender_domain), ref, outcome, kind),
            )
            await db.commit()
        return entry_id

    async def set_send_outcome(self, ref: str, outcome: str) -> int:
        """Mark a recorded send as bounced/complained (ref = outbox item id)."""
        if not ref:
            return 0
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE send_ledger SET outcome = ? WHERE ref = ?", (outcome, ref)
            )
            await db.commit()
            return cursor.rowcount

    async def count_sent_today(self, channel: str) -> int:
        """Sends on a channel during the current local business day."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM send_ledger WHERE channel = ? "
                "AND date(created_at, 'localtime') = date('now', 'localtime')",
                (channel,),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_kind_since(self, channel: str, kinds: tuple[str, ...],
                               *, today_only: bool = False, days: int = 0) -> int:
        """Ledger rows of the given kinds: this local business day, or a rolling window."""
        marks = ",".join("?" for _ in kinds)
        sql = f"SELECT COUNT(*) FROM send_ledger WHERE channel = ? AND kind IN ({marks})"
        params: list = [channel, *kinds]
        if today_only:
            sql += " AND date(created_at, 'localtime') = date('now', 'localtime')"
        elif days:
            sql += " AND created_at >= datetime('now', ?)"
            params.append(f"-{int(days)} days")
        async with self._connect() as db:
            async with db.execute(sql, params) as cursor:
                return (await cursor.fetchone())[0]

    async def count_sent_for(
        self,
        *,
        channel: str,
        prospect_id: str = "",
        company_id: str = "",
        hours: int = 24,
    ) -> int:
        """Rolling-window sends to one contact or company on a channel."""
        if not prospect_id and not company_id:
            return 0
        col, val = ("prospect_id", prospect_id) if prospect_id else ("company_id", company_id)
        async with self._connect() as db:
            async with db.execute(
                f"SELECT COUNT(*) FROM send_ledger WHERE channel = ? AND {col} = ? "
                "AND created_at >= datetime('now', ?)",
                (channel, val, f"-{int(hours)} hours"),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_outcomes_today(self, channel: str, outcome: str,
                                   sender_domain: str = "") -> int:
        sql = ("SELECT COUNT(*) FROM send_ledger WHERE channel = ? AND outcome = ? "
               "AND date(created_at, 'localtime') = date('now', 'localtime')")
        params: list = [channel, outcome]
        if sender_domain:
            sql += " AND sender_domain = ?"
            params.append(_norm(sender_domain))
        async with self._connect() as db:
            async with db.execute(sql, params) as cursor:
                return (await cursor.fetchone())[0]

    async def recent_send_health(self, channel: str, last_n: int = 100) -> tuple[int, int]:
        """(sends, bad) over the most recent `last_n` sends on a channel."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN outcome != 'sent' THEN 1 ELSE 0 END), 0) "
                "FROM (SELECT outcome FROM send_ledger WHERE channel = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (channel, int(last_n)),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0], row[1]

    # ── LinkedIn relationship state ──

    _LINKEDIN_THREAD_COLUMNS = frozenset({
        "state", "requested_at", "accepted_at", "last_checked_at",
        "messages_sent", "last_message_at",
    })

    async def get_linkedin_thread(self, prospect_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM linkedin_threads WHERE prospect_id = ?", (prospect_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def set_linkedin_thread(self, prospect_id: str, **fields) -> None:
        """Create or update a prospect's LinkedIn row. Unknown fields are ignored."""
        fields = {k: v for k, v in fields.items() if k in self._LINKEDIN_THREAD_COLUMNS}
        cols = ["prospect_id", *fields, "updated_at"]
        marks = ",".join("?" for _ in cols)
        updates = ", ".join(f"{k} = excluded.{k}" for k in [*fields, "updated_at"])
        async with self._connect() as db:
            await db.execute(
                f"INSERT INTO linkedin_threads ({','.join(cols)}) VALUES ({marks}) "
                f"ON CONFLICT(prospect_id) DO UPDATE SET {updates}",
                (prospect_id, *fields.values(), _utcnow().isoformat()),
            )
            await db.commit()

    async def list_linkedin_threads(self, state: str, limit: int = 50) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM linkedin_threads WHERE state = ? "
                "ORDER BY COALESCE(last_checked_at, requested_at) ASC LIMIT ?",
                (state, int(limit)),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def get_linkedin_candidates(self, limit: int) -> list[Prospect]:
        """Prospects worth a connection request: a profile URL, still in play,
        and never staged on LinkedIn before. Best score first."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT p.* FROM prospects p
                   LEFT JOIN linkedin_threads t ON t.prospect_id = p.id
                   WHERE p.linkedin_url != ''
                     AND p.status IN ('new', 'queued', 'contacted')
                     AND t.prospect_id IS NULL
                   ORDER BY p.score DESC, p.created_at ASC LIMIT ?""",
                (int(limit),),
            ) as cursor:
                return [self._prospect_from_row(r) for r in await cursor.fetchall()]

    async def count_outbox_open(self, channel: str, kinds: tuple[str, ...]) -> int:
        """Queued-but-unsent rows (pending review or approved) of the given kinds."""
        marks = ",".join("?" for _ in kinds)
        async with self._connect() as db:
            async with db.execute(
                f"SELECT COUNT(*) FROM outbox WHERE channel = ? AND kind IN ({marks}) "
                "AND status IN ('pending_review', 'approved')",
                (channel, *kinds),
            ) as cursor:
                return (await cursor.fetchone())[0]

    # ── Suppressions (do-not-contact) ──

    @staticmethod
    def _suppression_key(kind: str, value: str) -> str:
        if kind == "phone":
            return norm_phone(value)
        if kind == "linkedin":
            return norm_linkedin(value)
        return _norm(value)

    async def add_suppression(
        self,
        kind: str,
        value: str,
        *,
        channel: str = "all",
        reason: str = "",
        source: str = "",
        expires_at: str | None = None,
    ) -> bool:
        """Block a contact/domain/company. Idempotent; refreshes reason + expiry."""
        if kind not in SUPPRESSION_KINDS:
            raise ValueError(f"unknown suppression kind '{kind}'")
        key = self._suppression_key(kind, value)
        if not key:
            return False
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO suppressions
                   (id, kind, value_key, channel, reason, source, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(kind, value_key, channel) DO UPDATE SET
                     reason = excluded.reason, source = excluded.source,
                     expires_at = excluded.expires_at""",
                (_new_id(), kind, key, channel, reason, source, expires_at),
            )
            await db.commit()
        return True

    async def find_suppression(self, kind: str, value: str,
                               channel: str = "all") -> dict | None:
        """The active suppression covering this value on this channel, if any."""
        key = self._suppression_key(kind, value)
        if not key:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM suppressions
                   WHERE kind = ? AND value_key = ?
                     AND channel IN ('all', ?)
                     AND (expires_at IS NULL OR expires_at > datetime('now'))
                   ORDER BY created_at DESC LIMIT 1""",
                (kind, key, channel),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def is_suppressed(self, kind: str, value: str, channel: str = "all") -> bool:
        return await self.find_suppression(kind, value, channel) is not None

    async def remove_suppression(self, kind: str, value: str, channel: str = "all") -> int:
        key = self._suppression_key(kind, value)
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM suppressions WHERE kind = ? AND value_key = ? AND channel = ?",
                (kind, key, channel),
            )
            await db.commit()
            return cursor.rowcount

    # ── Prospect lookups for non-email channels ──

    async def get_prospect_by_linkedin_url(self, url: str) -> Prospect | None:
        key = norm_linkedin(url)
        if not key:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE linkedin_url != ''"
            ) as cursor:
                async for row in cursor:
                    if norm_linkedin(row["linkedin_url"]) == key:
                        return self._prospect_from_row(row)
        return None

    async def set_prospect_phone(self, prospect_id: str, phone: str, status: str = "") -> None:
        """Record a number and how far we trust it ('verified' = they texted from it, or a lookup passed)."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET phone = ?, phone_status = ?, updated_at = ? WHERE id = ?",
                (phone, status, _utcnow().isoformat(), prospect_id))
            await db.commit()

    async def set_prospect_consent(self, prospect_id: str, consent: dict) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET consent_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(consent), _utcnow().isoformat(), prospect_id))
            await db.commit()

    async def get_prospect_by_phone(self, phone: str) -> Prospect | None:
        key = norm_phone(phone)
        if not key:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE phone != ''"
            ) as cursor:
                async for row in cursor:
                    if norm_phone(row["phone"]) == key:
                        return self._prospect_from_row(row)
        return None

    # ── Signal vocabulary (governed; user-confirmed before collection) ──

    async def upsert_signal_code(
        self,
        code: str,
        *,
        label: str = "",
        description: str = "",
        category: str = "",
        value_type: str = "text",
        collector: str = "",
        cost_note: str = "",
        confidence_floor: float = 0.0,
        status: str | None = None,
    ):
        """Register a signal in the vocabulary. Never downgrades a user's
        decision: an existing row's ``status`` is preserved unless explicitly
        passed, so re-seeding can't silently re-enable a rejected signal."""
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO signal_codes
                       (code, label, description, category, value_type,
                        collector, cost_note, confidence_floor, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'proposed'))
                   ON CONFLICT(code) DO UPDATE SET
                       label = excluded.label,
                       description = excluded.description,
                       category = excluded.category,
                       value_type = excluded.value_type,
                       collector = excluded.collector,
                       cost_note = excluded.cost_note,
                       confidence_floor = excluded.confidence_floor,
                       status = COALESCE(?, signal_codes.status),
                       updated_at = CURRENT_TIMESTAMP""",
                (code, label, description, category, value_type, collector,
                 cost_note, float(confidence_floor), status, status),
            )
            await db.commit()

    async def get_signal_codes(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM signal_codes"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY category, code"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def set_signal_status(self, code: str, status: str) -> bool:
        """Confirm / reject a proposed signal. Only confirmed signals are
        collected — Harvey proposes, the user decides."""
        if status not in ("proposed", "confirmed", "rejected"):
            raise ValueError(f"invalid signal status: {status}")
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE signal_codes SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE code = ?",
                (status, code),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def confirmed_signal_codes(self) -> set[str]:
        return {r["code"] for r in await self.get_signal_codes(status="confirmed")}

    # ── Observations (every fact is a row, never a column) ──

    async def add_observation(
        self,
        signal_code: str,
        *,
        company_id: str = "",
        prospect_id: str = "",
        collector: str = "",
        value_num: float | None = None,
        value_text: str = "",
        confidence: float = 1.0,
        evidence_url: str = "",
        run_id: str = "",
        observed_at: str | None = None,
    ) -> str:
        """Record one fact. Raises if signal_code isn't in the vocabulary —
        a typo must fail loudly rather than create a junk signal."""
        obs_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO observations
                       (id, company_id, prospect_id, signal_code, collector,
                        value_num, value_text, confidence, evidence_url,
                        observed_at, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                           COALESCE(?, CURRENT_TIMESTAMP), ?)""",
                (obs_id, company_id, prospect_id, signal_code, collector,
                 value_num, value_text, float(confidence), evidence_url,
                 observed_at, run_id),
            )
            await db.commit()
        return obs_id

    async def add_observations(self, rows: list[dict], run_id: str = "") -> int:
        """Batch insert. Flushed per batch by callers — a long run that dies
        must not lose everything it observed."""
        if not rows:
            return 0
        payload = [
            (
                _new_id(), r.get("company_id", ""), r.get("prospect_id", ""),
                r["signal_code"], r.get("collector", ""), r.get("value_num"),
                r.get("value_text", ""), float(r.get("confidence", 1.0)),
                r.get("evidence_url", ""), r.get("observed_at"),
                r.get("run_id", run_id),
            )
            for r in rows
        ]
        async with self._connect() as db:
            await db.executemany(
                """INSERT INTO observations
                       (id, company_id, prospect_id, signal_code, collector,
                        value_num, value_text, confidence, evidence_url,
                        observed_at, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                           COALESCE(?, CURRENT_TIMESTAMP), ?)""",
                payload,
            )
            await db.commit()
        return len(payload)

    async def get_observations(
        self,
        company_id: str = "",
        signal_code: str = "",
        latest_only: bool = False,
        limit: int = 500,
    ) -> list[dict]:
        where, params = [], []
        if company_id:
            where.append("company_id = ?")
            params.append(company_id)
        if signal_code:
            where.append("signal_code = ?")
            params.append(signal_code)
        sql = "SELECT * FROM observations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if latest_only:
            # Newest observation per (company, signal) — the current view of
            # the world, with history still on disk underneath.
            sql = (
                "SELECT * FROM (" + sql + " ORDER BY observed_at DESC) "
                "GROUP BY company_id, signal_code"
            )
        else:
            sql += " ORDER BY observed_at DESC"
        sql += " LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    # A boolean signal is recorded either way: "checked, not running ads" is a
    # real finding, and a later flip from 0 to 1 is a real event. But a COHORT
    # asks who *has* the signal, so it must read the value, not merely the
    # presence of a row. Without this, "companies running Google Ads" silently
    # means "companies we checked for Google Ads" — which is everyone.
    #
    # `latest` is the current view of the world (newest observation per company
    # and signal, with history still on disk underneath). `positive` is the
    # subset where the finding is actually true — value_num IS NULL covers text
    # signals like INCUMBENT_AGENCY, where the row's existence IS the finding.
    _CURRENT_CTE = """
    WITH latest AS (
        SELECT o.* FROM observations o
        JOIN (SELECT company_id, signal_code, MAX(observed_at) AS t
              FROM observations GROUP BY company_id, signal_code) newest
          ON newest.company_id = o.company_id
         AND newest.signal_code = o.signal_code
         AND newest.t = o.observed_at
    ),
    positive AS (
        SELECT * FROM latest WHERE value_num IS NULL OR value_num != 0
    )
    """

    async def signal_counts(self) -> list[dict]:
        """How many entities actually carry each signal — the cohort sizes."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                self._CURRENT_CTE + """
                SELECT p.signal_code,
                       COALESCE(sc.label, p.signal_code) AS label,
                       sc.status AS status,
                       COUNT(DISTINCT p.company_id) AS companies,
                       COUNT(*) AS observations
                FROM positive p
                LEFT JOIN signal_codes sc ON sc.code = p.signal_code
                GROUP BY p.signal_code
                ORDER BY companies DESC"""
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def cohort(
        self,
        require: list[str],
        exclude: list[str] | None = None,
        min_confidence: float = 0.0,
        limit: int = 500,
    ) -> list[str]:
        """Company IDs carrying ALL required signals and none excluded.

        Set intersection happens in SQL — doing it in application code over a
        capped SELECT silently returns the wrong cohort.
        """
        if not require:
            return []
        req_ph = ",".join("?" for _ in require)
        sql = self._CURRENT_CTE + (
            f"SELECT company_id FROM positive "
            f"WHERE signal_code IN ({req_ph}) AND company_id != '' "
            "AND confidence >= ? "
            "GROUP BY company_id HAVING COUNT(DISTINCT signal_code) = ?"
        )
        params: list = [*require, float(min_confidence), len(set(require))]
        if exclude:
            exc_ph = ",".join("?" for _ in exclude)
            sql += (
                " AND company_id NOT IN ("
                f"SELECT company_id FROM positive WHERE signal_code IN ({exc_ph})"
                ")"
            )
            params.extend(exclude)
        sql += " LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            async with db.execute(sql, params) as cursor:
                return [row[0] for row in await cursor.fetchall()]

    async def companies_needing_profile(
        self, limit: int = 100, stale_days: int = 90
    ) -> list[dict]:
        """Companies a profile run should visit next.

        Never checked, or last checked longer ago than ``stale_days`` —
        ordered nulls-first so it rotates through the whole backlog instead
        of retrying the same top-N-by-created_at companies every cycle.
        ``profile_checked_at`` is set on every attempt regardless of outcome
        (see ProfileCollector.run), so an unreachable site still counts as
        checked and rotates out instead of being retried forever. A profile
        observation written any other way still counts too, whichever is newer.

        Businesses with no domain are excluded — there is no site to read.
        They are not a failure, they are the NO_WEBSITE cohort.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT c.id, c.name, c.domain
                   FROM companies c
                   {_LAST_PROFILED_JOIN}
                   WHERE c.domain != ''
                     AND ({_LAST_PROFILED} IS NULL
                          OR {_LAST_PROFILED} < datetime('now', ?))
                   ORDER BY {_LAST_PROFILED} IS NOT NULL, {_LAST_PROFILED}
                   LIMIT ?""",
                (f"-{int(stale_days)} days", int(limit)),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def count_companies_needing_profile(self, stale_days: int = 90) -> int:
        async with self._connect() as db:
            async with db.execute(
                f"""SELECT COUNT(*) FROM companies c
                   {_LAST_PROFILED_JOIN}
                   WHERE c.domain != ''
                     AND ({_LAST_PROFILED} IS NULL
                          OR {_LAST_PROFILED} < datetime('now', ?))""",
                (f"-{int(stale_days)} days",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def mark_company_profiled(self, company_id: str):
        """Record that a profile pass looked at this company, whether or
        not it was reachable — advances the rotation regardless."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE companies SET profile_checked_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (company_id,),
            )
            await db.commit()

    async def companies_needing_prospects(
        self, limit: int = 10, stale_days: int = 14
    ) -> list[dict]:
        """Companies with a domain, zero prospects, and no recent check.

        `harvey discover` (and other company-only sources) can add companies
        without ever finding a named contact at them. This is what lets a
        prospecting pass go back and fill that in, instead of only ever
        looking at companies it stumbles into fresh in the same cycle.

        Ordered by ``prospects_checked_at`` (nulls first) rather than
        recency, so a backfill pass rotates through the whole backlog
        instead of retrying the same top-N-by-created_at companies every
        cycle — a company a team page didn't exist for last time is worth
        another look after ``stale_days``, not a permanent skip.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id, c.name, c.domain, c.industry
                   FROM companies c
                   LEFT JOIN prospects p ON p.company_id = c.id
                   WHERE c.domain != ''
                     AND (c.prospects_checked_at IS NULL
                          OR c.prospects_checked_at < datetime('now', ?))
                   GROUP BY c.id
                   HAVING COUNT(p.id) = 0
                   ORDER BY c.prospects_checked_at IS NOT NULL, c.prospects_checked_at
                   LIMIT ?""",
                (f"-{int(stale_days)} days", int(limit)),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def companies_needing_domain(self, limit: int = 15) -> list[dict]:
        """Companies known only by name (job boards often give no website) not yet tried."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT id, name FROM companies
                   WHERE COALESCE(domain, '') = '' AND prospects_checked_at IS NULL
                   ORDER BY created_at DESC LIMIT ?""", (limit,)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def set_company_domain(self, company_id: str, domain: str) -> bool:
        """Attach a resolved website. False when another company already owns that domain."""
        domain = _norm(domain)
        async with self._connect() as db:
            try:
                await db.execute(
                    """UPDATE companies SET domain = ?, website = ?, prospects_checked_at = NULL,
                           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (domain, f"https://{domain}", company_id))
                await db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    async def mark_prospects_checked(self, company_id: str):
        """Record that a prospect-backfill pass looked at this company,
        whether or not it found anyone — advances the rotation regardless."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE companies SET prospects_checked_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (company_id,),
            )
            await db.commit()

    # ── Run log (what ran, when, what it produced and cost) ──

    async def start_run(
        self, stage: str, provider: str = "", params: dict | None = None
    ) -> str:
        run_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO runs (id, stage, provider, params_json, status) "
                "VALUES (?, ?, ?, ?, 'running')",
                (run_id, stage, provider, json.dumps(params or {})),
            )
            await db.commit()
        return run_id

    async def finish_run(
        self,
        run_id: str,
        status: str = "completed",
        records: int = 0,
        cost_usd: float = 0.0,
        error: str = "",
    ):
        async with self._connect() as db:
            await db.execute(
                "UPDATE runs SET status = ?, records = ?, cost_usd = ?, "
                "error = ?, ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, int(records), float(cost_usd), error[:500], run_id),
            )
            await db.commit()

    async def sweep_stale_runs(self, older_than_hours: int = 6) -> int:
        """A killed collector can't close its own run — never trust it to."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE runs SET status = 'stale', ended_at = CURRENT_TIMESTAMP "
                "WHERE status = 'running' "
                "AND started_at < datetime('now', ?)",
                (f"-{int(older_than_hours)} hours",),
            )
            await db.commit()
            return cursor.rowcount

    async def get_runs(self, limit: int = 25) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    # ── Settings (operational flags: kill switch, counters) ──

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._connect() as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else default

    async def set_setting(self, key: str, value: str):
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                (key, str(value)),
            )
            await db.commit()

    async def increment_setting(self, key: str, by: int = 1) -> int:
        current = await self.get_setting(key, "0")
        try:
            value = int(current) + by
        except ValueError:
            value = by
        await self.set_setting(key, str(value))
        return value

    async def prospect_exists(
        self, email: str = "", linkedin_url: str = "",
        first_name: str = "", last_name: str = "", company: str = "",
    ) -> bool:
        async with self._connect() as db:
            if email:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE email = ?", (_norm(email),)
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            if linkedin_url:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE linkedin_url = ?",
                    (linkedin_url.strip(),),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            # Name + company dedup (case-insensitive, expression-indexed)
            if first_name and last_name and company:
                async with db.execute(
                    """SELECT 1 FROM prospects
                       WHERE LOWER(first_name) = ? AND LOWER(last_name) = ?
                         AND LOWER(company) = ?""",
                    (first_name.lower(), last_name.lower(), company.lower()),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
        return False

    async def count_prospects_by_status(self) -> dict[str, int]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM prospects GROUP BY status"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Feedback ──

    async def add_feedback(
        self, entity_type: str, entity_id: str, comment: str
    ) -> str:
        feedback_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO feedback (id, entity_type, entity_id, comment)
                   VALUES (?, ?, ?, ?)""",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
        return feedback_id

    async def get_feedback(
        self, entity_type: str, entity_id: str
    ) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM feedback
                   WHERE entity_type = ? AND entity_id = ?
                   ORDER BY created_at DESC""",
                (entity_type, entity_id),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_feedback(self) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feedback ORDER BY created_at DESC LIMIT 100"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    # ── Reply Deduplication ──

    async def is_reply_processed(self, reply_id: str) -> bool:
        """Check if a reply has already been processed."""
        if not reply_id:
            return False
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM processed_replies WHERE reply_id = ?", (reply_id,)
            ) as cursor:
                return bool(await cursor.fetchone())

    async def mark_reply_processed(self, reply_id: str):
        """Mark a reply as processed to avoid double-handling."""
        if not reply_id:
            return
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO processed_replies (reply_id) VALUES (?)",
                (reply_id,),
            )
            await db.commit()

    # ── Campaigns ──

    async def add_campaign(self, campaign: Campaign) -> str:
        if not campaign.id:
            campaign.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO campaigns
                   (id, name, channel, instantly_campaign_id, sequence_json,
                    prospect_ids_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign.id, campaign.name, campaign.channel,
                    campaign.instantly_campaign_id, campaign.sequence_json(),
                    json.dumps(campaign.prospect_ids), campaign.status,
                    campaign.created_at.isoformat(),
                ),
            )
            await db.commit()
        return campaign.id

    async def get_campaigns_by_status(self, status: str) -> list[Campaign]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                campaigns = []
                for r in rows:
                    d = dict(r)
                    d["sequence"] = Campaign.sequence_from_json(d.pop("sequence_json"))
                    d["prospect_ids"] = json.loads(d.pop("prospect_ids_json") or "[]")
                    campaigns.append(Campaign(**d))
                return campaigns

    async def update_campaign(self, campaign_id: str, **kwargs):
        """Update whitelisted campaign columns in a single atomic statement."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CAMPAIGN_COLUMNS
        if invalid:
            raise ValueError(f"Invalid campaign column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE campaigns SET {set_clause} WHERE id = ?",
                (*kwargs.values(), campaign_id),
            )
            await db.commit()

    # ── Conversations ──

    async def add_conversation(self, convo: Conversation) -> str:
        if not convo.id:
            convo.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO conversations
                   (id, prospect_id, campaign_id, channel, thread_json,
                    intent, stage, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    convo.id, convo.prospect_id, convo.campaign_id,
                    convo.channel, convo.thread_json(), convo.intent,
                    convo.stage, convo.status, convo.created_at.isoformat(),
                    convo.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return convo.id

    async def get_conversation(self, convo_id: str) -> Conversation | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE id = ?", (convo_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                return Conversation(**d)

    async def get_conversations_by_status(self, status: str) -> list[Conversation]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                convos = []
                for r in rows:
                    d = dict(r)
                    d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                    convos.append(Conversation(**d))
                return convos

    async def update_conversation(self, convo_id: str, **kwargs):
        """Update whitelisted conversation columns atomically (bumps updated_at)."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CONVERSATION_COLUMNS
        if invalid:
            raise ValueError(f"Invalid conversation column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE conversations SET {set_clause}, updated_at = ? WHERE id = ?",
                (*kwargs.values(), _utcnow().isoformat(), convo_id),
            )
            await db.commit()

    # ── Actions Log ──

    async def log_action(self, action_type: str, agent: str, details: dict | None = None):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO actions (id, action_type, agent, details_json) VALUES (?, ?, ?, ?)",
                (_new_id(), action_type, agent, json.dumps(details or {})),
            )
            await db.commit()

    # ── Analytics ──

    async def get_campaign_stats(self) -> list[dict]:
        """Get performance stats for each campaign (one indexed pass over conversations)."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id, c.name, c.status, c.prospect_ids_json,
                          COUNT(v.id) as reply_count,
                          SUM(CASE WHEN v.intent = 'interested' THEN 1 ELSE 0 END) as interested_count,
                          SUM(CASE WHEN v.intent = 'objection' THEN 1 ELSE 0 END) as objection_count,
                          SUM(CASE WHEN v.intent = 'not_interested' THEN 1 ELSE 0 END) as not_interested_count
                   FROM campaigns c
                   LEFT JOIN conversations v ON v.campaign_id = c.id
                   WHERE c.status IN ('active', 'completed')
                   GROUP BY c.id
                   ORDER BY c.created_at DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                stats = []
                for r in rows:
                    d = dict(r)
                    for key in ("interested_count", "objection_count", "not_interested_count"):
                        d[key] = d[key] or 0
                    prospect_ids = json.loads(d.get("prospect_ids_json") or "[]")
                    d["leads_count"] = len(prospect_ids)
                    d["reply_rate"] = (
                        round(d["reply_count"] / len(prospect_ids) * 100, 1)
                        if prospect_ids else 0
                    )
                    stats.append(d)
                return stats

    async def get_intent_distribution(self) -> dict[str, int]:
        """Count conversations by intent."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT intent, COUNT(*) FROM conversations WHERE intent != '' GROUP BY intent"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def get_stage_distribution(self) -> dict[str, int]:
        """Count conversations by sales stage."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT stage, COUNT(*) FROM conversations WHERE stage != '' GROUP BY stage"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Usage Tracking ──

    async def get_usage_today(self, provider: str = "claude") -> int:
        """Brain calls made today on one provider's subscription."""
        column = _USAGE_LOG_COLUMN[provider]
        today = _utc_today()
        async with self._connect() as db:
            async with db.execute(
                f"SELECT {column} FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                row = await cursor.fetchone()
                return (row[0] or 0) if row else 0

    async def increment_usage(self, provider: str = "claude"):
        column = _USAGE_LOG_COLUMN[provider]
        today = _utc_today()
        async with self._connect() as db:
            await db.execute(
                f"""INSERT INTO usage_log (id, date, {column})
                   VALUES (?, ?, 1)
                   ON CONFLICT(date) DO UPDATE SET {column} = {column} + 1""",
                (_new_id(), today),
            )
            await db.commit()

    # ── Per-call usage accounting (usage_events) ──

    async def record_usage_event(
        self,
        *,
        agent: str = "",
        task: str = "",
        session_id: str = "",
        request_key: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        num_turns: int = 0,
        is_error: bool = False,
        source: str = "result_json",
        created_at: str | None = None,
        provider: str = "claude",
    ) -> bool:
        """Insert one usage row (one model within one brain call).

        Returns False when the row was skipped as a duplicate (request_key
        uniqueness makes transcript reconciliation idempotent).
        """
        if provider not in BRAIN_PROVIDERS:
            raise ValueError(f"unknown brain provider: {provider!r}")
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO usage_events
                   (id, created_at, agent, task, session_id, request_key, model,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, cost_usd, duration_ms, num_turns,
                    is_error, source, provider)
                   VALUES (?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _new_id(), created_at, agent, task, session_id, request_key,
                    model, int(input_tokens or 0), int(output_tokens or 0),
                    int(cache_read_tokens or 0), int(cache_creation_tokens or 0),
                    float(cost_usd or 0.0), int(duration_ms or 0),
                    int(num_turns or 0), 1 if is_error else 0, source, provider,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    _USAGE_SUM = (
        "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
        "SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens, "
        "SUM(cache_read_tokens) AS cache_read_tokens, "
        "SUM(cache_creation_tokens) AS cache_creation_tokens, "
        "SUM(cost_usd) AS cost_usd"
    )

    @staticmethod
    def _usage_row_to_dict(row: aiosqlite.Row) -> dict:
        d = dict(row)
        for key, value in d.items():
            if value is None and key != "period":
                d[key] = 0
        if "cost_usd" in d:
            d["cost_usd"] = round(float(d["cost_usd"] or 0.0), 6)
        return d

    async def _usage_grouped(self, group_expr: str, alias: str, days: int) -> list[dict]:
        sql = (
            f"SELECT {group_expr} AS {alias}, {self._USAGE_SUM} "
            f"FROM usage_events "
            f"WHERE created_at >= datetime('now', ?) "
            f"GROUP BY {alias} ORDER BY cost_usd DESC"
        )
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (f"-{int(days)} days",)) as cursor:
                return [self._usage_row_to_dict(r) for r in await cursor.fetchall()]

    async def usage_by_agent(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent", days
        )

    async def usage_by_task(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN task = '' THEN 'other' ELSE task END", "task", days
        )

    async def usage_by_model(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN model = '' THEN 'unknown' ELSE model END", "model", days
        )

    async def usage_by_provider(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped("provider", "provider", days)

    async def usage_by_day(self, days: int = 30) -> list[dict]:
        sql = (
            f"SELECT date(created_at) AS day, {self._USAGE_SUM} "
            f"FROM usage_events "
            f"WHERE created_at >= datetime('now', ?) "
            f"GROUP BY day ORDER BY day ASC"
        )
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (f"-{int(days)} days",)) as cursor:
                return [self._usage_row_to_dict(r) for r in await cursor.fetchall()]

    async def usage_totals(self) -> dict:
        """Rollups for today / last 7 days / last 30 days (UTC)."""
        totals = {}
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            for label, where in (
                ("today", "date(created_at) = date('now')"),
                ("week", "created_at >= datetime('now', '-7 days')"),
                ("month", "created_at >= datetime('now', '-30 days')"),
            ):
                async with db.execute(
                    f"SELECT {self._USAGE_SUM} FROM usage_events WHERE {where}"
                ) as cursor:
                    row = await cursor.fetchone()
                    totals[label] = self._usage_row_to_dict(row) if row else {}
        return totals

    # ── Summary for Decision Making ──

    async def count_writable_prospects(self, statuses=("verified",)) -> int:
        """New prospects the Writer can actually write for: deliverable email
        only. Counting all `new` rows stalls the loop on unverifiable ones."""
        marks = ",".join("?" for _ in statuses)
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE status = 'new' "
                f"AND email != '' AND COALESCE(email_status, 'guess') IN ({marks})",
                tuple(statuses),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def get_state_summary(self, deliverable=("verified",)) -> dict:
        prospect_counts = await self.count_prospects_by_status()
        today = _utc_today()
        async with self._connect() as db:
            async with db.execute(
                """SELECT
                     SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END)
                   FROM campaigns"""
            ) as cursor:
                row = await cursor.fetchone()
                draft_campaigns = row[0] or 0
                active_campaigns = row[1] or 0
            async with db.execute(
                "SELECT COUNT(*) FROM conversations WHERE status = 'open'"
            ) as cursor:
                open_conversations = (await cursor.fetchone())[0]
            # Open threads that still need Harvey: a prospect wrote and no answer has been drafted since
            # (a queued, sent or cancelled reply counts as an answer). A thread waiting on the owner must
            # not keep the heartbeat on "handle replies" forever and starve writing and prospecting.
            # Stale ones (a day with no draft) are left to the owner's own review.
            async with db.execute(
                """SELECT COUNT(*) FROM conversations c
                   WHERE c.status = 'open'
                     AND datetime(c.updated_at) > datetime('now', '-1 day')
                     AND NOT EXISTS (
                       SELECT 1 FROM outbox o
                        WHERE o.conversation_id = c.id AND o.kind = 'reply'
                          AND datetime(o.created_at) >= datetime(c.updated_at, '-5 seconds'))"""
            ) as cursor:
                conversations_to_handle = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT claude_calls, codex_calls FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                usage_row = await cursor.fetchone()
                usage_today = (usage_row[0] or 0) if usage_row else 0
                codex_today = (usage_row[1] or 0) if usage_row else 0

        return {
            "prospects": prospect_counts,
            "writable": await self.count_writable_prospects(tuple(deliverable)),
            "draft_campaigns": draft_campaigns,
            "active_campaigns": active_campaigns,
            "open_conversations": open_conversations,
            "conversations_to_handle": conversations_to_handle,
            "usage_today": usage_today,  # Claude calls (kept for compatibility)
            "usage_today_by_provider": {"claude": usage_today, "codex": codex_today},
            # Companies discovered but not yet read. The heartbeat profiles
            # these every cycle — it is free, so it never competes with the
            # Claude budget the rest of the loop is rationing.
            "unprofiled": await self.count_companies_needing_profile(),
        }
