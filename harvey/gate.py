"""Deterministic pre-send gate — the last line before an email leaves.

LLM instructions are a floor, not a guarantee. Every outgoing email passes
these non-LLM checks at send time; a failure blocks the send and records
why, so a bad generation can never reach a prospect. This is the pattern
the surviving commercial AI-SDR vendors converged on after the 2025
autonomous-agent flameouts: constraints in the prompt, verification in
code.
"""

import re
from dataclasses import dataclass, field

from harvey import compliance

# Spam-filter and AI-tell phrases that must never appear in outgoing mail.
# Mirrors prompts/writer.md; enforced here because prompts can be ignored.
BANNED_PHRASES = (
    "act now", "buy now", "click here", "click below", "limited time",
    "limited offer", "exclusive deal", "special offer", "risk-free",
    "no obligation", "100% free", "make money", "double your",
    "guaranteed results", "winner", "congratulations",
    "i hope this finds you well", "hope this email finds you well",
    "i hope this email finds you", "picture this", "what if i told you",
    "in today's fast-paced world", "game-changer", "game changer",
    "cutting-edge solution", "revolutionize your", "unlock your",
    "supercharge your",
    # AI-slop vocabulary that reads as templated
    "spearheaded", "transformative", "leveraged", "results-driven", "proven track record",
    "synergy", "synergies",
    # Boilerplate leaking out of a model refusing or hedging. Harvey itself
    # says openly that it is an AI assistant, so "I'm an AI" is NOT banned.
    "as an ai language model", "as an ai model", "as a language model",
)

MAX_SUBJECT_LEN = 90
MAX_BODY_WORDS = 220        # hard ceiling; sequences aim far lower
MAX_LINKS = 1

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MERGE_TAG_RE = re.compile(r"\{\{\s*\w+\s*\}\}|\{\s*\w+\s*\}(?!\})")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

DELIVERABLE_STATUSES = {"verified", "risky"}


def deliverable_statuses(config) -> tuple[str, ...]:
    """Email statuses this config will actually send to. One definition, used by
    the Writer and the decision engine, so they never disagree with the Sender."""
    risky = bool(getattr(config.channels.email, "send_to_risky", False))
    return ("verified", "risky") if risky else ("verified",)


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    # True when the block is temporary (a frequency cap, a lookup that failed):
    # the item stays queued and is retried instead of being marked failed.
    defer: bool = False

    def __bool__(self):
        return self.ok


def pre_send_check(
    to_email: str,
    subject: str,
    body: str,
    prospect=None,
    allow_risky: bool = False,
    kind: str = "sequence",
    *,
    enforce_compliance: bool = False,
    physical_address: str = "",
    allow_role: bool = False,
    allow_free_mail: bool = False,
) -> GateResult:
    """Run every deterministic check. Returns ok=False with reasons on any hit.

    With ``enforce_compliance`` (cold sequence mail only; replies go to people
    who wrote to us first) the CAN-SPAM and address-hygiene rules also apply:
    no mega-domains, role mailboxes or free-mail, a configured physical
    address, and that address present in the body footer.
    """
    reasons: list[str] = []
    subject = (subject or "").strip()
    body = (body or "").strip()
    to_email = (to_email or "").strip().lower()

    # Recipient sanity
    if not EMAIL_RE.match(to_email):
        reasons.append(f"invalid recipient address '{to_email}'")
    if prospect is not None:
        if (prospect.email or "").strip().lower() != to_email:
            reasons.append("recipient does not match the prospect record")
        allowed = DELIVERABLE_STATUSES if allow_risky else {"verified"}
        # Replies go back to someone who emailed US — deliverability proven.
        if kind == "sequence" and (prospect.email_status or "guess") not in allowed:
            reasons.append(
                f"email_status '{prospect.email_status or 'guess'}' is not deliverable"
            )

    # Content sanity
    if not subject:
        reasons.append("empty subject")
    if not body:
        reasons.append("empty body")
    if len(subject) > MAX_SUBJECT_LEN:
        reasons.append(f"subject too long ({len(subject)} > {MAX_SUBJECT_LEN})")
    word_count = len(body.split())
    if word_count > MAX_BODY_WORDS:
        reasons.append(f"body too long ({word_count} words > {MAX_BODY_WORDS})")

    # Unrendered merge tags — the classic mass-mail embarrassment
    leftover = MERGE_TAG_RE.findall(subject + " " + body)
    if leftover:
        reasons.append(f"unrendered merge tags: {sorted(set(leftover))[:3]}")

    # Banned phrases (spam triggers + AI tells)
    lowered = (subject + " " + body).lower()
    hits = [p for p in BANNED_PHRASES if p in lowered]
    if hits:
        reasons.append(f"banned phrases: {hits[:3]}")

    # Link budget: cold email with links goes to spam
    links = URL_RE.findall(body)
    if len(links) > MAX_LINKS:
        reasons.append(f"too many links ({len(links)} > {MAX_LINKS})")

    # No HTML — Harvey sends plain text only
    if re.search(r"<\s*(html|body|div|table|img|a)\b", body, re.IGNORECASE):
        reasons.append("HTML markup in body (plain text only)")

    if enforce_compliance and kind == "sequence":
        decision = compliance.check_address(
            to_email, allow_role=allow_role, allow_free_mail=allow_free_mail)
        if not decision.ok:
            reasons.append(f"address not allowed for cold outreach: {decision.reason}")
        address = (physical_address or "").strip()
        if not address:
            reasons.append("no physical address configured (CAN-SPAM requires one)")
        elif address.lower() not in body.lower():
            reasons.append("CAN-SPAM footer missing from body")

    return GateResult(ok=not reasons, reasons=reasons)


def prospect_suppression_keys(prospect, company=None) -> list[tuple[str, str]]:
    """Every (kind, value) that could suppress this prospect on email."""
    keys: list[tuple[str, str]] = []
    email = (getattr(prospect, "email", "") or "").strip().lower()
    if email:
        keys.append(("email", email))
        domain = email.rsplit("@", 1)[-1]
        if domain and not compliance.is_free_mail(domain):
            keys.append(("domain", domain))
    if getattr(prospect, "phone", ""):
        keys.append(("phone", prospect.phone))
    if getattr(prospect, "linkedin_url", ""):
        keys.append(("linkedin", prospect.linkedin_url))
    company_domain = (getattr(company, "domain", "") or "").strip().lower()
    if company_domain:
        keys.append(("company", company_domain))
    return keys


async def stateful_gate(
    state,
    *,
    channel: str,
    prospect,
    kind: str = "sequence",
    quotas: "compliance.EmailQuotas | None" = None,
) -> GateResult:
    """Checks that need the database: suppression list and frequency limits.

    Fail-closed by construction: any lookup error blocks the send.
    """
    hard: list[str] = []
    soft: list[str] = []
    try:
        company = None
        if getattr(prospect, "company_id", ""):
            company = await state.get_company(prospect.company_id)

        for sup_kind, value in prospect_suppression_keys(prospect, company):
            hit = await state.find_suppression(sup_kind, value, channel=channel)
            if hit:
                hard.append(
                    f"suppressed ({sup_kind}: {hit.get('reason') or 'no reason recorded'})")
                break

        # Frequency limits apply to cold sequence touches, not replies.
        if kind == "sequence":
            q = quotas or compliance.EmailQuotas()
            counts = compliance.QuotaCounts(
                global_today=await state.count_sent_today(channel),
                contact_today=await state.count_sent_for(
                    channel=channel, prospect_id=prospect.id, hours=24),
                company_today=await state.count_sent_for(
                    channel=channel, company_id=prospect.company_id, hours=24),
                company_this_week=await state.count_sent_for(
                    channel=channel, company_id=prospect.company_id, hours=24 * 7),
            )
            capped = compliance.check_email_quota(counts, q)
            if capped:
                soft.append(f"frequency limit: {capped}")
    except Exception as e:  # noqa: BLE001 — fail closed on any lookup error
        soft.append(f"compliance lookup failed, holding send: {e}")

    reasons = hard + soft
    return GateResult(ok=not reasons, reasons=reasons, defer=bool(soft) and not hard)
