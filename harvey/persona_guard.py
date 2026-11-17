"""Deterministic enforcement of the assistant persona's hard rules.

The prompt asks the model to follow these; this module checks that it did,
because a prompt is a request and this is a guarantee. Used by the Writer
(reject and redo a draft), by the send gates (last line before anything leaves),
and by the persona evaluation rubric, so there is exactly one definition of
what Harvey must never say. Pure functions, no I/O.

Rules come from the persona interview (skills/persona/boundaries.md, voice.md,
identity.md). Speaker: Harvey, the owner's AI assistant, from the first message;
the owner (harvey/owner.py) is third person.
"""

import re

from harvey import owner

# Never-say (the owner's list) plus the upstream spam/AI-tell words the Writer already bans.
NEVER_WORDS = (
    "game-changer", "game changer", "unlock", "leverage", "synergy", "circle back",
    "thought leader", "revolutionary", "cutting-edge", "10x", "set it and forget it",
    "supercharge", "seamless", "delve",
)

EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")

# Harvey speaks as itself. First person must never claim the owner's life or voice.
FIRST_PERSON_OWNER = (
    r"\bI(?:'ve| have)? (?:built|founded|served|created|run)\b",
    r"\bmy (?:company|firm|clients?|career|business)\b",
    rf"\bI(?:'m| am) {re.escape(owner.FIRST_CAP)}\b",
    rf"(?m)^\s*(?:Thanks,\s*)?{re.escape(owner.FIRST_CAP)}\s*$",
)

# Things Harvey may never promise or state.
PROMISES = (
    r"\bguarantee[sd]?\b",
    r"\b100%\b",
    r"\bzero (?:errors|downtime)\b",
    r"\b(?:will|would) (?:reply|respond|get back|call you)\b.{0,40}\b(?:today|tomorrow|soon|within|shortly|tonight)\b",
    r"\bwithin \d+ (?:hours?|days?|minutes?)\b",
    r"\bSOC ?2 (?:compliant|certified)\b|\bHIPAA (?:compliant|certified)\b",
    r"\b(?:fully|completely) (?:secure|compliant)\b",
)
PRICE = (
    r"\$\s?\d",
    r"\b\d[\d,.]*\s?k\b",
    r"\b(?:per|a) (?:month|hour|week)\b.{0,20}\$|\$.{0,20}\b(?:per|a) (?:month|hour|week)\b",
    r"\bretainer (?:starts|is|of)\b",
    r"\bdiscount",
)

# Results Harvey has not been cleared to state (an hours figure in stories.md may be
# the owner's own, unpublished). Hours or time saved is never a claim Harvey makes.
RESULT_CLAIMS = (
    r"\b\d[\d,.]*\s*(?:hours?|hrs?)\b.{0,25}\b(?:week|month|saved?|saving)\b",
    r"\bsav(?:ed|es|ing)\b.{0,25}\b\d[\d,.]*\s*(?:hours?|hrs?)\b",
)

# The calendar controls the meeting length; Harvey says "a short conversation".
MEETING_LENGTH = re.compile(r"\b\d+\s*(?:-|to)?\s*\d*\s*(?:minutes?|mins?|hours?|hrs?)\b", re.I)
SELF_INTRO = re.compile(r"\bHarvey\b|\bAI assistant\b", re.I)
MEETING_PLATFORMS = re.compile(
    r"\b(?:Microsoft Teams|MS Teams|Teams (?:call|meeting|link|invite)|Zoom|Google Meet|Webex|dial-in)\b", re.I)
GREETING_AT_START = re.compile(r"^\s*(?:hey|hi|hello|dear|good (?:morning|afternoon|evening))\b", re.I)
SIGNOFF_AT_END = re.compile(
    rf"(?:thanks|thank you|best|regards|cheers|sincerely)[,.]?\s*\n?\s*(?:harvey|{re.escape(owner.FIRST.lower())})?\s*$", re.I)

ALLOWED_LINK_HOSTS = (owner.LINK_HOST,)


def violations(text: str, *, allowed_hosts: tuple[str, ...] = ALLOWED_LINK_HOSTS) -> list[str]:
    """Every hard-rule violation in an outgoing message. Empty list = clean."""
    text = text or ""
    found: list[str] = []
    if "—" in text or "–" in text:
        found.append("em/en dash")
    if "!" in text:
        found.append("exclamation mark")
    if EMOJI.search(text):
        found.append("emoji")
    if re.search(r"(?m)^\s*(?:[-*•]|\d+\.)\s", text) or "**" in text:
        found.append("bullets or markdown")
    low = text.lower()
    for word in NEVER_WORDS:
        if word in low:
            found.append(f"banned word: {word}")
    for pat in FIRST_PERSON_OWNER:
        if re.search(pat, text):
            found.append(f"speaks as the owner (/{pat}/)")
    for pat in PROMISES:
        if re.search(pat, text, re.I):
            found.append(f"promise: /{pat}/")
    for pat in PRICE:
        if re.search(pat, text, re.I):
            found.append(f"price or discount: /{pat}/")
    for pat in RESULT_CLAIMS:
        if re.search(pat, text, re.I):
            found.append(f"unapproved result claim: /{pat}/")
    links = re.findall(r"https?://([^\s/]+)[^\s]*", text)
    for host in links:
        if not any(host.lower() == h or host.lower().endswith("." + h) for h in allowed_hosts):
            found.append(f"link outside {', '.join(allowed_hosts)}: {host}")
    if len(links) > 1:
        found.append("more than one link")
    if MEETING_PLATFORMS.search(text):
        found.append("names a meeting platform (not confirmed)")
    if MEETING_LENGTH.search(text):
        found.append("states a meeting length (the calendar controls it; say 'a short conversation')")
    return found


def draft_problems(body: str, *, allowed_text: str = "", composed_by_code: bool = True) -> list[str]:
    """Checks for a model-written BODY, before greeting and sign-off are added.

    `allowed_text` is everything the draft may draw on (the prospect facts plus
    the approved persona files). Any number in the body that is not in it is
    invented, however plausible.
    """
    found = violations(body)
    if composed_by_code:
        if SELF_INTRO.search(body or ""):
            found.append("the body introduces Harvey (the signature does that)")
        if GREETING_AT_START.match(body or ""):
            found.append("the body starts with its own greeting (code adds it)")
        if SIGNOFF_AT_END.search(body or ""):
            found.append("the body ends with its own sign-off (code adds it)")
    for token in re.findall(r"\d[\d,.%]*", body or ""):
        token = token.rstrip(".,")
        if token and token not in allowed_text:
            found.append(f"number not in the provided facts: {token}")
    return found


REPLY_WORD_LIMITS = {"linkedin": 60, "email": 120, "sms": 40}


def reply_problems(text: str, *, channel: str, allowed_text: str = "") -> list[str]:
    """Checks for a model-written REPLY body, before any greeting and sign-off.

    Unlike a cold email, a reply may say who Harvey is (someone asked), so the
    self-introduction rule does not apply. Everything else does."""
    text = text or ""
    found = violations(text)
    if channel == "email":
        if GREETING_AT_START.match(text):
            found.append("the body starts with its own greeting (code adds it)")
        if SIGNOFF_AT_END.search(text):
            found.append("the body ends with its own sign-off (code adds it)")
    limit = REPLY_WORD_LIMITS.get(channel, 120)
    if len(text.split()) > limit:
        found.append(f"too long for {channel}: {len(text.split())} words (limit {limit})")
    if text.count("?") > 1:
        found.append("more than one question")
    for token in re.findall(r"\d[\d,.%]*", text):
        token = token.rstrip(".,")
        if token and token not in allowed_text:
            found.append(f"number not in the provided facts: {token}")
    return found
