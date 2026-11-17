"""Deterministic routing for inbound replies in assistant mode.

Decides, before any model is asked, the replies where the owner's rules leave no
room for improvising: hand-offs, the AI-disclosure answer and the "how much?"
answer. Those get fixed wording, so the worst a bad model day can do is write
a bad answer to an *ordinary* question, never invent a price or a promise.

Source of the rules: skills/persona/boundaries.md (escalation list and trigger
keywords, AI disclosure wording) and offers_pricing.md ("how much?" wording).
The keywords are a floor, not the whole rule: when in doubt the match goes to
the owner. The owner's name and booking link come from harvey/owner.py (config).
Pure functions, no I/O.
"""

import re
from dataclasses import dataclass

from harvey import owner

_O, _OC, _BOOK = owner.FIRST, owner.FIRST_CAP, owner.BOOKING_URL

# The owner's wording. Do not paraphrase.
ESCALATION_LINE = f"I'll flag this for {_O} so they can respond directly."
AI_DISCLOSURE = (f"Yes, I'm Harvey, {_O}'s AI assistant. {_OC} builds systems like this for clients. "
                 f"I help with initial conversations, and {_O} handles consultations and project "
                 "decisions personally.")
PRICE_LINE = (f"{_OC} scopes engagements around the problem you need solved, the systems involved, "
              "and how much implementation or ongoing leadership you need. What are you trying to "
              "improve, and what tools are you using today? With that context they can recommend an "
              "approach and give you a realistic price.")
IDENTITY_LINE = (f"I'm Harvey, {_O}'s AI assistant, so I can't verify things for them or answer personal "
                 f"questions on their behalf. If you'd like to speak with {_O} directly, you can book a "
                 f"conversation here: {_BOOK}")
CONTACT_LINE = ("I can't share a phone number or move this to another app. The easiest way to reach "
                f"{_O} directly is to book a conversation here: {_BOOK}")
# LinkedIn messages are capped at 60 words; the same answer, shorter.
PRICE_LINE_SHORT = (f"{_OC} scopes engagements around the problem, the systems involved and how much "
                    "build or ongoing leadership you need. What are you trying to improve, and what "
                    "tools do you use today?")

# The owner's first name and company, for the hand-off patterns below.
_NAME = re.escape(_O.lower())
_COMPANIES = "|".join(re.escape(c.lower()) for c in owner.COMPANIES) or "(?!x)x"


@dataclass(frozen=True)
class Route:
    kind: str       # "escalate" | "ai_question" | "identity_challenge" | "contact_request" | "price_question"
    reason: str


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


ESCALATE = {
    "pricing negotiation, proposal or contract": _rx(
        r"\$\s?\d", r"\b\d[\d,.]*\s?k\b", r"\bdiscounts?\b", r"\bquotes?\b", r"\bproposals?\b",
        r"\bsow\b", r"\bcontracts?\b", r"\bmsa\b", r"\bnda\b", r"\bretainers?\b", r"\binvoices?\b",
        r"\brefunds?\b", r"\bbudget\b", r"\bcan you do it for\b", r"\b(?:hourly|day) rates?\b",
    ),
    "legal, regulatory or compliance assurance": _rx(
        r"\blawyers?\b", r"\battorneys?\b", r"\blegal\b", r"\blawsuit\b", r"\bsue\b", r"\bcease\b",
        r"\bgdpr\b", r"\bhipaa\b", r"\bsoc ?2\b", r"\bccpa\b", r"\bcan-?spam\b", r"\bregulators?\b",
        r"\bfinra\b", r"\bharass", r"\bspam", r"\bcompliance (?:cert|guarantee)",
    ),
    "partnership, investment, press or speaking": _rx(
        r"\bacqui(?:re|sition)", r"\binvest(?:or|ors|ment|ing)?\b", r"\bpartner(?:ship|ships)?\b",
        r"\bpodcast", r"\bpress\b", r"\binterview", r"\bspeak(?:ing|er)\b", r"\bkeynote\b", r"\bsponsor",
    ),
    "recruiter or vendor pitch": _rx(
        r"\brecruit", r"\bjob opening\b", r"\bwe offer (?:services|development)\b", r"\bwhite.?label\b",
        r"\bagency partner\b", r"\bdev shop\b", r"\bstaff (?:your|the) ", r"\bvp engineering role\b",
        r"\b(?:we|i)(?:'re| am| are) (?:a |an )?(?:recruiter|agency|vendor)\b",
    ),
    "asks for an immediate call or personal contact": _rx(
        r"\bcall me\b", r"\bcall now\b", r"\bcall (?:you|him|her|them) (?:now|right now)\b", r"\btext me\b",
        r"\bright now\b", r"\basap\b", rf"\b{_NAME}'?s? (?:cell|personal)", r"\b\d{3}[-.\s]\d{4}\b",
    ),
    "current or past client": _rx(
        r"\b(?:we|i)(?:'re| are| am|'m| were| was) (?:a |an )?(?:current |past |former )?clients?\b",
        rf"\bwe work(?:ed)? with (?:you|{_COMPANIES}|{_NAME})\b",
        rf"\b(?:your|{_NAME}'?s?) (?:current|past|former) client",
    ),
    "claims a personal relationship with the owner": _rx(
        rf"\b(?:i )?know {_NAME}\b", rf"\bfriends? (?:of|with) {_NAME}\b", rf"\b{_NAME} and i\b", r"\bold friend\b",
        rf"\bserved with (?:you|{_NAME}|him|her|them)\b", r"\bmutual (?:friend|connection)\b",
    ),
    "asks for free work or a custom scope": _rx(
        r"\bfree (?:\w+ )?(?:review|audit|assessment|consult\w*|teardown|work|trial|proof of concept|poc|pilot|estimate)\b",
        r"\b(?:poc|proof of concept|pilot) (?:first|before)\b",
    ),
    "competitor probing for non-public information": _rx(
        r"\bevaluating similar tools\b", r"\bexact stack\b", r"\bclients (?:are )?using it\b",
    ),
}

AI_QUESTION = _rx(
    r"\b(?:are|r) (?:you|u) (?:a |an |actually |really )?(?:real |human |live )?(?:bot|robot|ai|human|person|automated|machine|chatgpt|gpt|llm)\b",
    r"\bis this (?:a |an |really |actually )?(?:bot|ai|automated|robot|machine|human|real person)\b",
    r"\bam i (?:talking|speaking|chatting|writing) (?:to|with) (?:a |an )?(?:bot|ai|human|real|machine|person)\b",
    r"\b(?:ai|bot|robot)[- ]generated\b", r"\bwritten by (?:ai|a bot|chatgpt|a robot)\b",
    r"\bthis (?:message|email|note) (?:written|generated) by\b",
)

CONTACT_REQUEST = _rx(
    r"\bwhats ?app\b", r"\btelegram\b", r"\bwhat'?s your (?:number|cell|phone|email)\b",
    r"\b(?:your|his|her) (?:number|cell|phone|mobile)\b", r"\btext you\b", r"\bmove (?:this|the conversation) (?:to|over to)\b",
)

IDENTITY_CHALLENGE = _rx(
    r"\bprove (?:it|you|that|this)\b", rf"\b(?:this|that|you) (?:is|are|'re|'s) (?:really |actually |not |n't )?{_NAME}\b",
    rf"\bis (?:this|that) (?:really |actually )?{_NAME}\b", rf"\bare you (?:really |actually )?{_NAME}\b",
    rf"\bnot (?:really |actually )?{_NAME}\b", rf"\bverify (?:that )?(?:you|this)\b.{{0,30}}\b{_NAME}\b",
)

PRICE_QUESTION = _rx(
    r"\bhow much\b", r"\bwhat (?:does|would|will|do) (?:this|it|that|an? \w+|the \w+|you) (?:cost|charge)\b",
    r"\bpric(?:e|es|ing)\b", r"\byour (?:fees?|rates?)\b", r"\bwhat do you charge\b",
)

# Common English words. A long ASCII message with almost none of them is not English.
_ENGLISH = frozenset(
    "the a an and or but if is are was were be been to of in on for with at by from that this it its you "
    "we i your our they their them there what how can could would will do does did not no yes thanks "
    "thank hi hello hey please me my about tell more say said send details sounds good great ok okay sure "
    "interested interesting help know need want use have has had get got make made like just very much "
    "any some then than so up out who when where which why while also most other into over after before "
    "only both each few own same too now here see look think work works working team company business "
    "system systems data happy talk call meet time week next today tomorrow back well really actually".split())


def looks_non_english(text: str) -> bool:
    """Inverted punctuation, a noticeable share of accented or non-Latin letters, or a long
    plain-ASCII message with almost no common English words. Short replies are never judged
    by word counts."""
    if re.search(r"[¿¡]", text):
        return True
    letters = re.findall(r"[^\W\d_]", text)
    if letters and sum(1 for c in letters if ord(c) > 127) / len(letters) >= 0.05:
        return True
    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) < 8:
        return False
    return sum(1 for w in words if w in _ENGLISH) / len(words) < 0.10


def route(text: str) -> Route | None:
    """The first rule the message trips, or None for an ordinary message.

    Order matters: hand-offs beat everything, then the two fixed answers."""
    text = text or ""
    for reason, pattern in ESCALATE.items():
        if pattern.search(text):
            return Route("escalate", reason)
    if looks_non_english(text):
        return Route("escalate", "not written in English")
    if AI_QUESTION.search(text):
        return Route("ai_question", "asked whether Harvey is an AI")
    if IDENTITY_CHALLENGE.search(text):
        return Route("identity_challenge", "doubts it is the owner or asks Harvey to prove it")
    if CONTACT_REQUEST.search(text):
        return Route("contact_request", "wants a phone number or another app")
    if PRICE_QUESTION.search(text):
        return Route("price_question", "asked what it costs")
    return None


_QUOTE_START = re.compile(
    r"^(?:On .{5,200}wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|_{5,}\s*$|From:\s.+|Sent from my )",
    re.I)


def strip_quoted(text: str) -> str:
    """Only what the person wrote: drops the quoted thread an email client appends."""
    kept: list[str] = []
    for line in (text or "").splitlines():
        if _QUOTE_START.match(line.strip()):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()
