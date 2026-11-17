"""Configuration loader for Harvey. Reads harvey.yaml + .env."""

import logging
import os
import re
from datetime import time as _time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.config")


class ConfigError(Exception):
    """Raised when Harvey's configuration is missing or invalid."""


class ConfigFileNotFoundError(ConfigError, FileNotFoundError):
    """Config file is missing. Subclasses FileNotFoundError for
    backward compatibility with existing callers/tests."""


class PersonaConfig(BaseModel):
    name: str
    company: str
    role: str
    email: str
    linkedin: str
    tone: str
    # "self": the message speaks as `name` (upstream behavior).
    # "assistant": every message says it is from an AI assistant that works for
    # someone else. The model writes only the body; greeting and sign-off are
    # added by code, and persona_guard enforces the hard rules before sending.
    mode: str = "self"
    greeting: str = ""    # e.g. "Hey {{first_name}},"; blank = none
    closer: str = ""      # e.g. "Thanks,"; used only on middle sequence steps
    signature: str = ""   # multi-line sign-off; blank = none
    # Assistant mode: the person the assistant works for (full name). Messages use the first
    # name, in the third person. Read by harvey/owner.py; OWNER_NAME in .env overrides it.
    owner_name: str = ""

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        v = (v or "self").strip().lower()
        if v not in ("self", "assistant"):
            raise ValueError("persona.mode must be 'self' or 'assistant'")
        return v


class OfferConfig(BaseModel):
    primary: str = ""
    entry: str = ""
    goal: str = "book_call"  # book_call, start_trial, get_reply
    booking_method: str = "calendar_link"  # calendar_link, suggest_times, ask_preference
    booking_url: str = ""
    meeting_duration: str = "15 minutes"
    meeting_owner: str = ""


class ProductConfig(BaseModel):
    name: str
    description: str
    pricing: str
    key_benefits: list[str]
    objection_responses: dict[str, str]
    offer: OfferConfig = OfferConfig()


class ICPConfig(BaseModel):
    industries: list[str]
    company_size: str
    titles: list[str]
    geography: list[str]
    # Role keywords that indicate a company is in-market right now (a company
    # hiring a "Head of Growth" is buying growth tooling). Empty → falls back
    # to `titles`. Used for careers-page scanning and job-board discovery.
    hiring_signals: list[str] = []
    # Discovery needs a radius, not a place name. Maps each entry in
    # `geography` to "lat,lng,radius_km" — e.g.
    #   "Denver, CO": "39.7392,-104.9903,50"
    # Without one, listings providers can only match on the business name.
    geo_coordinates: dict[str, str] = {}


class EmailChannelConfig(BaseModel):
    enabled: bool = True
    # "instantly" (legacy), "gmail" (Gmail/Workspace via API — recommended),
    # "smtp" (any SMTP+IMAP mailbox: AgentMail, Fastmail, ...), "m365"
    # (Microsoft 365 mailbox over Graph OAuth, no passwords), or "resend"
    # (send via the Resend API, read replies over IMAP)
    provider: str = "instantly"
    max_daily_sends: int = 50
    # When True, also send to catch-all ("risky") domains, not just verified
    # mailboxes. Off by default — catch-alls accept everything, so a bad
    # guess still bounces.
    send_to_risky: bool = False
    # Copilot mode (native providers): every outgoing email waits in the
    # outbox for your approval (dashboard → Outbox, or `harvey outbox`).
    # Set false for full autopilot once you trust the output.
    require_approval: bool = True
    # Kill switch: pause all sending when bounces exceed this fraction of
    # sent mail (measured over the trailing sends). 0 disables the switch.
    max_bounce_rate: float = 0.05
    # CAN-SPAM + cold-outreach hygiene for sequence mail (replies are exempt):
    # a physical postal address in every footer, and no mega-domain, role
    # (info@, sales@...) or free-mail recipients. With enforcement on and no
    # physical_address set, cold sends are held — by design, not by accident.
    enforce_compliance: bool = True
    physical_address: str = ""
    allow_role_addresses: bool = False
    # Domain mail is sent from (per-domain bounce/complaint breakers). Blank =
    # the domain of persona.email.
    sender_domain: str = ""
    # Resend: where replies should land if not the From address (usually the
    # same mailbox, so leave blank).
    reply_to: str = ""
    # Public base URL that reaches this machine's dashboard (e.g. a Cloudflare
    # Tunnel hostname). Enables the one-click unsubscribe link and header; blank
    # means mail carries only the reply-STOP footer.
    unsubscribe_base_url: str = ""

    @field_validator("max_daily_sends")
    @classmethod
    def _sends_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_daily_sends must be >= 0")
        return v


class LinkedInChannelConfig(BaseModel):
    enabled: bool = True
    # off:    no LinkedIn sending or inbox reading.
    # assist: Harvey drafts connection requests and messages and queues them in
    #         the dashboard; you send them yourself. No automation, no ban risk.
    # auto:   Harvey sends from your real account through a headless browser,
    #         inside the limits below. Outside LinkedIn's Terms at any volume.
    mode: str = "assist"
    # Ceilings, not targets. linkedin_policy clamps these to hard maximums.
    max_daily_connections: int = 7
    max_daily_messages: int = 5
    max_weekly_connections: int = 30
    require_approval: bool = True
    # First day of the 14-day warm-up ramp (YYYY-MM-DD). Blank = day 1 (cautious).
    warmup_start_date: str = ""
    timezone: str = "America/New_York"

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in ("off", "assist", "auto"):
            raise ValueError("linkedin.mode must be 'off', 'assist' or 'auto'")
        return v

    @field_validator("warmup_start_date")
    @classmethod
    def _valid_start(cls, v: str) -> str:
        v = (v or "").strip()
        if v:
            from datetime import date as _date
            try:
                _date.fromisoformat(v)
            except ValueError:
                raise ValueError("linkedin.warmup_start_date must be YYYY-MM-DD")
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"'{v}' is not a valid timezone. Use an IANA name like 'America/New_York'.")
        return v

    @field_validator("max_daily_connections", "max_daily_messages", "max_weekly_connections")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("LinkedIn limits must be >= 0")
        return v


class SmsChannelConfig(BaseModel):
    """Consent-gated texting. Off by default; never cold. A text goes only to someone
    who texted first, booked with a phone number, or opted in, and STOP ends it for good."""
    enabled: bool = False
    require_approval: bool = True
    max_daily_sms: int = 20
    # TCPA: no texts before 9 am or after 6 pm in the recipient's time, weekdays only.
    window_start: str = "09:00"
    window_end: str = "18:00"
    timezone: str = "America/New_York"
    from_number: str = ""     # E.164, a number registered for business texting (A2P 10DLC)
    # os:     send through Orion from a number the organization owns (e.g. its Clara assistant's).
    # twilio: send straight through Twilio with TWILIO_* keys and your own number.
    transport: str = "os"
    # With transport "os", Orion (Clara) files and answers inbound texts, and records every STOP in
    # its do-not-contact list, which Harvey checks before each text. True only while that is so.
    inbound_ready: bool = False

    @field_validator("max_daily_sms")
    @classmethod
    def _cap(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_daily_sms must be >= 0")
        return v


class ChannelsConfig(BaseModel):
    email: EmailChannelConfig = EmailChannelConfig()
    linkedin: LinkedInChannelConfig = LinkedInChannelConfig()
    sms: SmsChannelConfig = SmsChannelConfig()


class VaultConfig(BaseModel):
    """Harvey's Obsidian vault (a browsable copy of what it knows). Off until a path is set."""
    enabled: bool = True
    path: str = ""
    # Also mirror the vault into Orion's Memory module (SuperAdmin-readable).
    os_memory: bool = False


class QuietHoursConfig(BaseModel):
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "America/New_York"

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            _time.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"'{v}' is not a valid time. Use 24h HH:MM format, e.g. '22:00'."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        import pytz

        if v not in pytz.all_timezones_set:
            raise ValueError(
                f"'{v}' is not a valid timezone. Use an IANA name like 'America/New_York'."
            )
        return v


class UsageConfig(BaseModel):
    max_daily_claude_percent: float = 80.0
    heartbeat_interval_minutes: int = 15
    quiet_hours: QuietHoursConfig = QuietHoursConfig()

    @field_validator("max_daily_claude_percent")
    @classmethod
    def _valid_percent(cls, v: float) -> float:
        if not 0 < v <= 100:
            raise ValueError("max_daily_claude_percent must be between 0 and 100")
        return v

    @field_validator("heartbeat_interval_minutes")
    @classmethod
    def _valid_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("heartbeat_interval_minutes must be at least 1")
        return v


BRAIN_PROVIDERS = ("claude", "codex", "local")
CODEX_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

# Model names are passed to a CLI, so they must look like a model name and
# never like a flag ("-x") or anything a shell would treat specially.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$")


class BrainConfig(BaseModel):
    """Which subscription Harvey thinks with.

    ``claude`` (default) runs Claude Code headless on your Claude plan.
    ``codex`` runs Codex on your ChatGPT plan. There is never an automatic
    fallback between them, and an explicit ``model`` only affects Harvey —
    your own Claude Code / Codex settings are never read or changed.
    """

    provider: str = "claude"
    model: str = ""  # empty = the provider's own default
    reasoning_effort: str = ""  # codex only; empty = model default
    # ``local`` only: an OpenAI-compatible server on THIS machine (LM Studio). Prompts carry prospect
    # and mailbox text, so the address must be loopback; nothing leaves the box.
    base_url: str = "http://127.0.0.1:1235/v1"
    keep_loaded_seconds: int = 600     # LM Studio unloads the model after this long idle, freeing the GPU
    temperature: float = 0.3
    max_tokens: int = 3000
    # How much the model "thinks" before answering: none | low | medium | high | default (the model's own).
    # Writing and classifying do not need it; "none" is faster and keeps answers short.
    local_reasoning: str = "none"
    context_length: int = 32768        # Harvey's prompts carry persona files; LM Studio's default 8K is too small
    parallel: int = 2                  # simultaneous requests the loaded model accepts
    call_timeout_seconds: int = 600    # covers a cold model load

    @field_validator("provider", mode="before")
    @classmethod
    def _valid_provider(cls, v) -> str:
        v = str(v or "claude").strip().lower()
        if v not in BRAIN_PROVIDERS:
            raise ValueError(
                f"'{v}' is not a supported brain provider. "
                f"Use one of: {', '.join(BRAIN_PROVIDERS)}."
            )
        return v

    @field_validator("local_reasoning", mode="before")
    @classmethod
    def _valid_local_reasoning(cls, v) -> str:
        v = str(v or "none").strip().lower()
        if v not in ("none", "low", "medium", "high", "default"):
            raise ValueError("brain.local_reasoning must be none, low, medium, high or default.")
        return v

    @field_validator("base_url", mode="before")
    @classmethod
    def _loopback_only(cls, v) -> str:
        from urllib.parse import urlparse
        v = str(v or "http://127.0.0.1:1235/v1").strip().rstrip("/")
        host = urlparse(v).hostname or ""
        if urlparse(v).scheme not in ("http", "https") or host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("brain.base_url must point at this machine (127.0.0.1 or localhost).")
        return v

    @field_validator("model", mode="before")
    @classmethod
    def _valid_model(cls, v) -> str:
        v = str(v or "").strip()
        if v and not _MODEL_NAME_RE.match(v):
            raise ValueError(f"'{v}' is not a valid model name.")
        return v

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _valid_effort(cls, v) -> str:
        v = str(v or "").strip().lower()
        if v and v not in CODEX_REASONING_EFFORTS:
            raise ValueError(
                f"'{v}' is not a valid reasoning effort. "
                f"Use one of: {', '.join(CODEX_REASONING_EFFORTS)}."
            )
        return v


class MicrosoftConfig(BaseModel):
    # Name of the To Do list Harvey adds hand-off tasks to (created on first use).
    todo_list: str = "Harvey"
    # Add a To Do task whenever a thread is handed to the owner. Takes effect only once
    # `harvey microsoft login` has granted the Tasks permission.
    hand_off_tasks: bool = True


class IntegrationsConfig(BaseModel):
    microsoft: MicrosoftConfig = MicrosoftConfig()


class ScoutConfig(BaseModel):
    """Which prospecting strategies the background loop may run.

    ``strategies: null`` (the default) runs all of them. A list names the ones
    allowed; an empty list turns automatic prospecting off, so only prospects
    added deliberately (e.g. from ``harvey discover``) are worked.
    """
    strategies: list[str] | None = None
    # Autopilot (2026-11-05). `cohorts` lists the ICP cohorts (skills/persona/icp.md) whose open
    # job postings are searched on the boards, once every `discovery_every_hours`. New people
    # added per rolling day are capped so the approval queue and the Claude quota stay sane.
    cohorts: list[str] = []
    discovery_every_hours: int = 24
    job_limit: int = 25
    max_new_prospects_per_day: int = 5
    max_prospects_per_company: int = 1   # one buyer per company at a time; 0 = no limit
    # Companies Harvey never prospects. Domains are exact;
    # keywords are regexes matched against the company's name, industry and description.
    exclude_domains: list[str] = []
    exclude_keywords: list[str] = [
        r"\bnon-?profit\b", r"501\(c\)", r"\bcharit(y|ies|able)\b", r"\bfoundation\b",
        r"\bmigrant\b", r"\bfarm ?workers?\b", r"\bsocial services?\b", r"\bcommunity action\b",
        r"\bchurch(es)?\b", r"\bministr(y|ies)\b",
    ]


class HarveyConfig(BaseModel):
    persona: PersonaConfig
    product: ProductConfig
    icp: ICPConfig
    channels: ChannelsConfig = ChannelsConfig()
    integrations: IntegrationsConfig = IntegrationsConfig()
    usage: UsageConfig = UsageConfig()
    brain: BrainConfig = BrainConfig()
    scout: ScoutConfig = ScoutConfig()
    vault: VaultConfig = VaultConfig()


class EnvConfig(BaseModel):
    instantly_api_key: str = ""
    # Discovery providers
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    dataforseo_sandbox: str = ""   # any truthy value routes to the free sandbox
    linkedin_email: str = ""
    linkedin_password: str = ""
    hunter_api_key: str = ""
    serper_api_key: str = ""
    reoon_api_key: str = ""
    zerobounce_api_key: str = ""
    # Native mail providers (channels.email.provider: gmail | smtp)
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    # Resend (channels.email.provider: resend)
    resend_api_key: str = ""
    resend_webhook_secret: str = ""
    # Microsoft 365 (channels.email.provider: m365; also calendar and To Do).
    # Public client: an ID, no secret. Run `harvey microsoft login`.
    ms_client_id: str = ""
    ms_tenant_id: str = ""
    # Twilio, for the consent-gated SMS channel (channels.sms.enabled).
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    # Orion, the business OS (Supabase). Harvey signs in as its own Orion user.
    os_supabase_url: str = ""
    os_supabase_anon_key: str = ""
    os_expected_email: str = ""


def _format_validation_error(e: ValidationError) -> str:
    """Turn a pydantic ValidationError into a readable, actionable message."""
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(config_path: str | None = None) -> HarveyConfig:
    """Load Harvey configuration from YAML file.

    Raises ConfigError with a clear, actionable message on any problem.
    """
    if config_path is None:
        config_path = _find_config_file()

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigFileNotFoundError(
            f"Config file not found: {config_path}. "
            "Create one from harvey.yaml.example or run 'harvey setup'."
        )
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}:\n  {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {config_path}: {e}")

    if data is None:
        raise ConfigError(f"{config_path} is empty. Run 'harvey setup' to configure Harvey.")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{config_path} must contain a YAML mapping (key: value pairs), "
            f"got {type(data).__name__}."
        )

    try:
        return HarveyConfig(**data)
    except ValidationError as e:
        # Log a friendly, actionable summary, then re-raise the original
        # ValidationError so callers (and tests) keep the pydantic type.
        logger.error(
            f"Invalid configuration in {config_path}:\n{_format_validation_error(e)}\n"
            "Fix the fields above or re-run 'harvey setup'."
        )
        raise


def load_brain_config(config_path: str | None = None) -> BrainConfig:
    """Read just the ``brain:`` section, without validating the rest.

    Training runs before a product is configured (the template harvey.yaml
    still has placeholder values), but it must still think with the
    provider the user chose. A missing file or section means the Claude
    default; an invalid section raises rather than silently switching
    providers.
    """
    if config_path is None:
        try:
            config_path = _find_config_file()
        except ConfigFileNotFoundError:
            return BrainConfig()
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return BrainConfig()
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"Could not read the brain settings in {config_path}: {e}")
    section = data.get("brain") if isinstance(data, dict) else None
    if section is None:
        return BrainConfig()
    if not isinstance(section, dict):
        raise ConfigError(f"'brain' in {config_path} must be a mapping.")
    try:
        return BrainConfig(**section)
    except ValidationError as e:
        raise ConfigError(
            f"Invalid brain settings in {config_path}:\n{_format_validation_error(e)}"
        )


def load_env() -> EnvConfig:
    """Load environment variables from .env file."""
    load_dotenv()
    env = EnvConfig(
        instantly_api_key=os.getenv("INSTANTLY_API_KEY", "").strip(),
        dataforseo_login=os.getenv("DATAFORSEO_LOGIN", "").strip(),
        dataforseo_password=os.getenv("DATAFORSEO_PASSWORD", "").strip(),
        dataforseo_sandbox=os.getenv("DATAFORSEO_SANDBOX", "").strip(),
        linkedin_email=os.getenv("LINKEDIN_EMAIL", "").strip(),
        linkedin_password=os.getenv("LINKEDIN_PASSWORD", "").strip(),
        hunter_api_key=os.getenv("HUNTER_API_KEY", "").strip(),
        serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
        reoon_api_key=os.getenv("REOON_API_KEY", "").strip(),
        zerobounce_api_key=os.getenv("ZEROBOUNCE_API_KEY", "").strip(),
        gmail_client_id=os.getenv("GMAIL_CLIENT_ID", "").strip(),
        gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587").strip() or 587),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        imap_host=os.getenv("IMAP_HOST", "").strip(),
        imap_port=int(os.getenv("IMAP_PORT", "993").strip() or 993),
        imap_username=os.getenv("IMAP_USERNAME", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        resend_api_key=os.getenv("RESEND_API_KEY", "").strip(),
        resend_webhook_secret=os.getenv("RESEND_WEBHOOK_SECRET", "").strip(),
        ms_client_id=os.getenv("MS_CLIENT_ID", "").strip(),
        ms_tenant_id=os.getenv("MS_TENANT_ID", "").strip(),
        twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
        twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
        os_supabase_url=os.getenv("ORION_SUPABASE_URL", "").strip(),
        os_supabase_anon_key=os.getenv("ORION_SUPABASE_ANON_KEY", "").strip(),
        os_expected_email=os.getenv("ORION_EXPECTED_EMAIL", "").strip(),
    )
    return env


def _find_config_file() -> str:
    """Search for Harvey's config.

    ``harvey.local.yaml`` wins when present. It is gitignored, so a fork can
    carry a real product configuration (trained on an actual company) while
    the tracked ``harvey.yaml`` stays an untrained template — nobody
    publishes their positioning, pricing, and prospect targeting by accident.
    """
    candidates = [
        Path.cwd() / "harvey.local.yaml",
        PROJECT_ROOT / "harvey.local.yaml",
        Path.cwd() / "harvey.yaml",
        Path.cwd().parent / "harvey.yaml",
        PROJECT_ROOT / "harvey.yaml",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise ConfigFileNotFoundError(
        "harvey.yaml not found in "
        + ", ".join(str(p.parent) for p in candidates)
        + ". Create one from harvey.yaml.example or run 'harvey setup'."
    )
