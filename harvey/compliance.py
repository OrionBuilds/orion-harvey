"""Outreach compliance rules shared by every channel. Pure functions only.

Ported from CLARA's prospecting engine (compliance.ts, complianceFooter.ts,
senderRotation.ts) so Harvey and Orion refuse the same
things: a "no" anywhere is a "no" everywhere. Stateful checks (suppression
lookups, frequency counts) live in ``harvey.gate``; this module has no I/O.
"""

import re
from dataclasses import dataclass
from urllib.parse import quote

# ── Mega domains ──
# Giant platforms and public companies that won't answer cold outreach.
# Identical to CLARA's MEGA_DOMAINS so both systems refuse the same domains.
MEGA_DOMAINS = frozenset({
    "appsumo.com", "capterra.com", "g2.com", "producthunt.com", "angel.co",
    "ycombinator.com", "indiehackers.com", "dev.to", "medium.com", "notion.so",
    "zapier.com", "integromat.com", "make.com", "airtable.com", "webflow.com",
    "stripe.com", "google.com", "youtube.com", "facebook.com", "meta.com",
    "instagram.com", "amazon.com", "aws.amazon.com", "apple.com",
    "microsoft.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
    "salesforce.com", "hubspot.com", "shopify.com", "adobe.com", "oracle.com",
    "ibm.com", "intercom.com", "slack.com", "atlassian.com", "canva.com",
    "figma.com", "wix.com", "squarespace.com", "mailchimp.com", "semrush.com",
    "ahrefs.com", "moz.com", "wordpress.com", "godaddy.com", "cloudflare.com",
    "openai.com", "anthropic.com", "gartner.com", "forbes.com",
    "techcrunch.com", "wikipedia.org", "reddit.com", "quora.com",
    "trustpilot.com", "yelp.com", "crunchbase.com",
})

# Free-mail providers: a prospect on one of these has no company domain to
# vet, and cold mail to personal inboxes is both low-value and higher risk.
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "proton.me",
    "protonmail.com", "gmx.com", "mail.com",
})


def _domain_of(email_or_domain: str) -> str:
    value = (email_or_domain or "").strip().lower()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    return value.removeprefix("www.")


def is_mega_domain(email_or_domain: str) -> bool:
    """True for a mega domain or any subdomain of one (blog.hubspot.com)."""
    d = _domain_of(email_or_domain)
    if not d:
        return False
    return d in MEGA_DOMAINS or any(d.endswith("." + m) for m in MEGA_DOMAINS)


def is_free_mail(email_or_domain: str) -> bool:
    return _domain_of(email_or_domain) in FREE_MAIL_DOMAINS


# ── Role addresses ──
ROLE_LOCALPARTS = frozenset({
    "info", "contact", "sales", "support", "office", "admin", "hello",
    "service", "help", "team", "billing", "accounts", "noreply", "no-reply",
    "webmaster", "postmaster",
})


def is_role_address(email: str) -> bool:
    local = (email or "").split("@", 1)[0].strip().lower()
    return local in ROLE_LOCALPARTS


# ── Opt-out detection ──
OPTOUT_RE = re.compile(
    r"\b(unsubscribe|opt[\s-]?out|stop|remove me|take me off|do not (contact|email))\b",
    re.IGNORECASE,
)


def is_optout_reply(subject: str = "", snippet: str = "") -> bool:
    return bool(OPTOUT_RE.search(f"{subject or ''} {snippet or ''}"))


# ── Send-gate decision (mega / role / free-mail) ──
@dataclass(frozen=True)
class AddressDecision:
    ok: bool
    reason: str = ""


def check_address(email: str, *, allow_role: bool = False,
                  allow_free_mail: bool = False) -> AddressDecision:
    """Is this address one we may cold-email at all? (Replies skip this.)"""
    if is_mega_domain(email):
        return AddressDecision(False, "mega_domain")
    if is_role_address(email) and not allow_role:
        return AddressDecision(False, "role_address")
    if is_free_mail(email) and not allow_free_mail:
        return AddressDecision(False, "free_mail")
    return AddressDecision(True)


# ── Frequency quotas ──
@dataclass(frozen=True)
class EmailQuotas:
    global_per_day: int = 50
    contact_per_day: int = 1
    company_per_day: int = 1
    company_per_week: int = 3


@dataclass(frozen=True)
class QuotaCounts:
    global_today: int = 0
    contact_today: int = 0
    company_today: int = 0
    company_this_week: int = 0


def check_email_quota(counts: QuotaCounts,
                      quotas: EmailQuotas = EmailQuotas()) -> str:
    """Return '' if allowed, else the FIRST cap hit.

    Checked global-first, then narrowing to recipient and company, so the
    reported reason is always the first ceiling actually reached.
    """
    if counts.global_today >= quotas.global_per_day:
        return "global_daily_cap"
    if counts.contact_today >= quotas.contact_per_day:
        return "contact_daily_cap"
    if counts.company_today >= quotas.company_per_day:
        return "company_daily_cap"
    if counts.company_this_week >= quotas.company_per_week:
        return "company_weekly_cap"
    return ""


# ── Breakers ──
DOMAIN_HARD_BOUNCE_THRESHOLD = 5
DOMAIN_COMPLAINT_THRESHOLD = 1
GLOBAL_BREAKER_WINDOW = 100
GLOBAL_BREAKER_RATE = 0.03


def domain_breaker_reason(hard_bounces_today: int, complaints_today: int) -> str:
    """≥5 hard bounces/day or ≥1 complaint pauses that sending domain."""
    if complaints_today >= DOMAIN_COMPLAINT_THRESHOLD:
        return f"{complaints_today} spam complaint(s) today"
    if hard_bounces_today >= DOMAIN_HARD_BOUNCE_THRESHOLD:
        return f"{hard_bounces_today} hard bounces today (>= {DOMAIN_HARD_BOUNCE_THRESHOLD})"
    return ""


def global_breaker_reason(sent_last_n: int, bad_last_n: int) -> str:
    """>3% bad over the last 100 sends pauses the channel. Never on a small sample."""
    if sent_last_n < GLOBAL_BREAKER_WINDOW:
        return ""
    rate = bad_last_n / sent_last_n
    if rate > GLOBAL_BREAKER_RATE:
        return (f"{rate * 100:.1f}% bounce/complaint rate over the last "
                f"{sent_last_n} sends (> {GLOBAL_BREAKER_RATE * 100:.0f}%)")
    return ""


# ── CAN-SPAM footer + headers ──
def build_footer_text(physical_address: str, unsubscribe_url: str = "",
                      reply_to: str = "") -> str:
    """Plain-text footer: physical postal address (CAN-SPAM's legal minimum)
    plus a way out. With an unsubscribe URL it is a link; without one it is
    the reply-STOP form, which the Handler honors."""
    lines = ["", "", "--", physical_address.strip()]
    if unsubscribe_url:
        lines.append(f"Don't want these emails? Unsubscribe: {unsubscribe_url}")
    else:
        lines.append("Not interested? Just reply STOP and I won't email you again.")
    if reply_to and not unsubscribe_url:
        lines.append(f'You can also email {reply_to} with "unsubscribe" in the subject.')
    return "\n".join(lines)


def append_compliance_footer(body: str, physical_address: str,
                             unsubscribe_url: str = "", reply_to: str = "") -> str:
    return body.rstrip() + build_footer_text(physical_address, unsubscribe_url, reply_to)


def build_compliance_headers(unsubscribe_url: str, mailbox: str,
                             reply_to: str = "") -> dict[str, str]:
    """RFC 8058 one-click unsubscribe: both mailto and https forms.

    ``mailbox`` must be an address Harvey actually reads (the From address):
    a mailto aimed at an alias that doesn't exist would just bounce."""
    subject = quote("unsubscribe")
    headers = {
        "List-Unsubscribe": f"<mailto:{mailbox}?subject={subject}>, <{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    if reply_to:
        headers["Reply-To"] = reply_to
    return headers


# ── SMS (TCPA) ──
SMS_STOP_KEYWORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit"})
SMS_START_KEYWORDS = frozenset({"start", "yes", "unstop"})


def is_sms_stop(text: str) -> bool:
    """Exact-keyword match, as carriers define it — 'please stop' is not STOP."""
    return (text or "").strip().lower().rstrip(".!") in SMS_STOP_KEYWORDS
