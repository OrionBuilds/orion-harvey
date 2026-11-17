"""Everything that knows what LinkedIn's pages look like, in one place.

When LinkedIn changes its UI this is the file that breaks, and it must break
loudly: callers treat an element that should be there but isn't as a signal to
stop (the circuit breaker), never to retry blindly. Nothing here has been
exercised against the live site from CI; the pure functions are unit-tested,
the Playwright locators are the part to verify in a supervised first run.
"""

import re
from dataclasses import dataclass

# Locators prefer accessible roles and visible text over CSS classes, which
# LinkedIn rewrites constantly.
CONNECT_BUTTON = re.compile(r"^(connect|invite .+ to connect)$", re.I)
PENDING_BUTTON = re.compile(r"^pending|withdraw", re.I)
MESSAGE_BUTTON = re.compile(r"^message\b", re.I)
MORE_BUTTON = re.compile(r"^more( actions)?$", re.I)
CONNECT_MENU_ITEM = re.compile(r"connect", re.I)
ADD_NOTE_BUTTON = re.compile(r"add a note", re.I)
SEND_WITHOUT_NOTE_BUTTON = re.compile(r"send without a note", re.I)
SEND_BUTTON = re.compile(r"^send( invitation)?$", re.I)
COMPOSE_BOX = re.compile(r"write a message", re.I)

# The top card is the <section> that contains PROFILE_HEADING (LinkedInAutomation._card); "main section" matched the whole page.
# LinkedIn's profile page has no <h1> any more: the name is the first <h2> inside <main> (checked against live profiles 2026-11-12;
# the old "main h1" never matched, so every profile looked "removed or restricted" and tripped the breaker).
PROFILE_HEADING = "main h2"
DIALOG_OR_ALERT = '[role="dialog"], [role="alert"], .artdeco-modal'

FEED_URL = "https://www.linkedin.com/feed/"
INBOX_URL = "https://www.linkedin.com/messaging/"

# Inbox parsing (class-based; the most fragile part of this file).
CONVERSATION_ITEM = "li.msg-conversation-listitem"
CONVERSATION_LINK = 'a[href*="/messaging/thread/"]'
UNREAD_MARKER = ".notification-badge--show, .msg-conversation-card__unread-count"
THREAD_HEADER_PROFILE_LINK = 'a[href*="/in/"]'
MESSAGE_GROUP = ".msg-s-message-group"
MESSAGE_GROUP_NAME = ".msg-s-message-group__name"
MESSAGE_BODY = ".msg-s-event-listitem__body"


@dataclass(frozen=True)
class PageVerdict:
    kind: str = ""        # "" fine | "expired" (logged out) | "trip" (stop everything)
    reason: str = ""


_LOGGED_OUT_URL = re.compile(r"/(authwall|login|uas/login|checkpoint/lg/login)", re.I)
_CHALLENGE_URL = re.compile(r"/(checkpoint|challenge)", re.I)

# Phrases in dialogs/alerts (never in page body, where a prospect's own profile
# text could contain them) that mean LinkedIn is limiting or questioning us.
_TRIP_PHRASES = (
    "unusual activity",
    "temporarily restricted",
    "we've restricted",
    "weekly invitation limit",
    "reached the weekly",
    "invitation limit",
    "verify you're not a robot",
    "let's do a quick security check",
    "too many requests",
)


def detect_trip(url: str, alert_text: str = "", status: int | None = None) -> PageVerdict:
    """Classify a page we just loaded. Logged-out and challenged are different:
    the first needs `harvey linkedin login`, the second means stop for days."""
    if _LOGGED_OUT_URL.search(url or ""):
        return PageVerdict("expired", "LinkedIn session expired (redirected to login)")
    if _CHALLENGE_URL.search(url or ""):
        return PageVerdict("trip", f"LinkedIn challenge/checkpoint page: {url}")
    if status in (429, 999):
        return PageVerdict("trip", f"LinkedIn returned HTTP {status}")
    lowered = (alert_text or "").lower()
    for phrase in _TRIP_PHRASES:
        if phrase in lowered:
            return PageVerdict("trip", f"LinkedIn notice: '{phrase}'")
    return PageVerdict()


_DEGREE_RE = re.compile(r"[·•]\s*(1st|2nd|3rd)\b", re.I)


def parse_degree(card_text: str) -> str:
    """'1st' / '2nd' / '3rd' from the profile top card, or '' if not shown."""
    m = _DEGREE_RE.search(card_text or "")
    return m.group(1).lower() if m else ""


def classify_connection(labels: list[str], degree: str = "") -> str:
    """What can be done on this profile?

    labels: the accessible names of the top card's buttons.
    Returns connected | pending | connectable | more | unknown.
    'more' means Connect may sit in the overflow menu. 'unknown' is returned
    rather than a guess: the caller treats it as unactionable and a run of them
    as a sign the UI changed.
    """
    names = [(label or "").strip() for label in labels]
    if any(PENDING_BUTTON.search(n) for n in names):
        return "pending"
    if any(CONNECT_BUTTON.match(n) for n in names):
        return "connectable"
    has_message = any(MESSAGE_BUTTON.match(n) for n in names)
    # A Message button alone doesn't prove a connection (open profiles and
    # Premium show it for strangers); the degree badge is what does.
    if degree == "1st" and has_message:
        return "connected"
    if any(MORE_BUTTON.match(n) for n in names):
        return "more"
    return "unknown"
