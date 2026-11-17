"""Find named leaders on a company's own website.

The plain team-page scrape only understands pages whose cards carry class names like "team" or
"member", which is a small share of real sites. This reads what a person would: schema.org Person
markup, a name followed by a title, "Name, Title" lines, and the leadership page the homepage
links to. It never invents a person: a name only counts when a leadership title sits right next
to it on the page.
"""

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

LEADERSHIP_PATHS = (
    "/leadership", "/team", "/about/leadership", "/company/leadership", "/management",
    "/about", "/about-us", "/our-team", "/people", "/executive-team", "/leadership-team",
    "/about/team", "/about-us/leadership", "/company/team", "/company/about-us",
    "/who-we-are", "/our-leadership", "/about/management", "/company", "/staff",
)

_LINK_HINT = re.compile(r"leadership|management|executive|our-?team|the-?team|/team|people|who-we-are|about", re.I)

# A title worth keeping: a seat that could own AI or technology decisions, or the founder and CEO.
TITLE_RE = re.compile(
    r"(?<![\w-])(chief\s+[a-z&,/ ]{3,40}?\s+officer|ceo|cto|cio|coo|cfo|ciso|cdo|cro|cmo|"
    r"president|founder|co-?founder|owner|managing\s+(?:director|partner)|"
    r"(?:senior\s+|executive\s+)?vice\s+president|svp|evp|vp|head\s+of\s+[a-z&/ ]{2,30}|"
    r"director\s+of\s+[a-z&/ ]{2,30}|general\s+manager|partner)(?![\w•·-])", re.I)

_CREDENTIALS = re.compile(r",?\s+(?:jr|sr|ii|iii|iv|mba|cpa|phd|dr|cissp|cism|pmp|esq|pe|csm)\.?$", re.I)
_NAME_WORD = re.compile(r"^[A-Z][a-zA-Z'’.\-]{1,20}$")
_NOT_NAME_WORDS = {
    "our", "the", "team", "about", "us", "leadership", "management", "executive", "executives", "board",
    "directors", "chief", "officer", "president", "vice", "senior", "head", "of", "and", "for", "with",
    "contact", "careers", "career", "privacy", "policy", "terms", "services", "solutions", "read", "more",
    "learn", "view", "meet", "company", "group", "bank", "banking", "credit", "union", "insurance",
    "technology", "technologies", "engineering", "operations", "finance", "financial", "marketing",
    "sales", "data", "security", "product", "products", "customer", "customers", "careers", "news",
    "press", "blog", "events", "investors", "investor", "relations", "home", "menu", "search", "login",
    "sign", "get", "started", "request", "demo", "free", "trial", "resources", "partners", "partner",
    "support", "help", "global", "international", "americas", "north", "america", "new", "york", "inc",
    "llc", "ltd", "corp", "corporation", "co", "years", "experience", "mission", "vision", "values",
    "story", "history", "overview", "team", "members", "member", "staff", "people", "leaders", "leader",
    "advisors", "advisory", "committee", "governance", "human", "resources", "legal", "general",
    "counsel", "business", "development", "strategy", "innovation", "digital", "transformation",
    "artificial", "intelligence", "ai", "cloud", "software", "platform", "network", "systems",
    "who", "are", "is", "gsa", "list", "contract", "vehicle", "idiq", "sb", "my", "your", "defined", "cooperative", "holdings", "federal", "national",
    "association", "foundation", "institute", "university", "college", "hospital", "health", "capital",
}
_NAME_LINE = re.compile(
    r"^(?P<name>[A-Z][a-zA-Z'’.\-]+(?: [A-Z][a-zA-Z'’.\-]+){1,3}(?:,? (?:Jr|Sr|II|III|IV)\.?)?)"
    r"\s*[,|–—:\-]\s*(?P<title>.{3,100})$")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def split_name(raw: str) -> tuple[str, str] | None:
    """'Dr. Jane Q. Smith, MBA' -> ('Jane', 'Smith'); None when it doesn't look like a person."""
    name = _clean(raw)
    name = re.sub(r"^(?:dr|mr|mrs|ms|prof)\.?\s+", "", name, flags=re.I)
    for _ in range(3):
        name = _CREDENTIALS.sub("", name).strip(" ,")
    words = name.split(" ")
    if not 2 <= len(words) <= 4:
        return None
    if not all(_NAME_WORD.match(w) for w in words):
        return None
    if any(w.lower().strip(".") in _NOT_NAME_WORDS for w in words):
        return None
    first, last = words[0].strip("."), words[-1].strip(".")
    if len(first) < 2 or len(last) < 2:
        return None
    return first, last


def _title_ok(text: str) -> str:
    text = _clean(text)
    if not text or len(text) > 100 or not TITLE_RE.search(text):
        return ""
    # A sentence about leadership is not a title.
    if text.count(" ") > 12 or text.endswith("."):
        return ""
    return text


def _from_json_ld(soup: BeautifulSoup) -> list[dict]:
    people: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Person" in kinds and node.get("name") and node.get("jobTitle"):
                parts = split_name(str(node["name"]))
                title = _title_ok(str(node["jobTitle"]))
                if parts and title:
                    people.append({"first_name": parts[0], "last_name": parts[1], "title": title})
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or tag.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return people


def _from_text(soup: BeautifulSoup) -> list[dict]:
    people: list[dict] = []
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()

    # "Name, Title" or "Name - Title" in a single string.
    for s in soup.stripped_strings:
        m = _NAME_LINE.match(_clean(s))
        if m:
            parts, title = split_name(m.group("name")), _title_ok(m.group("title"))
            if parts and title:
                people.append({"first_name": parts[0], "last_name": parts[1], "title": title})

    # A name element with a title right after it, inside the same card.
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "a", "p", "span", "div"]):
        if el.find(["h1", "h2", "h3", "h4", "h5", "h6", "p", "div"]):
            continue  # a wrapper, not the name itself
        parts = split_name(el.get_text(" ", strip=True))
        if not parts:
            continue
        title = ""
        node = el
        for _ in range(3):   # the element, then up to two parents
            strings = [_clean(x) for x in node.stripped_strings]
            mine = _clean(el.get_text(" ", strip=True))
            try:
                after = strings[strings.index(mine) + 1: strings.index(mine) + 4]
            except ValueError:
                after = []
            title = next((t for t in map(_title_ok, after) if t), "")
            if title or node.parent is None or node.parent.name in ("body", "html", "main", "[document]"):
                break
            node = node.parent
        if title:
            people.append({"first_name": parts[0], "last_name": parts[1], "title": title})
    return people


def extract_people(html: str) -> list[dict]:
    """Named people with a leadership title on one page, de-duplicated by name."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return []
    found = _from_json_ld(soup) + _from_text(soup)
    seen, unique = set(), []
    for p in found:
        key = (p["first_name"].lower(), p["last_name"].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def leadership_links(html: str, base_url: str, limit: int = 4) -> list[str]:
    """Same-site links whose address or text points at a team or leadership page."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return []
    host = urlparse(base_url).netloc.lower().removeprefix("www.")
    scored: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        p = urlparse(href)
        if p.scheme not in ("http", "https") or p.netloc.lower().removeprefix("www.") != host:
            continue
        label = f"{p.path} {a.get_text(' ', strip=True)}"
        if not _LINK_HINT.search(label):
            continue
        score = 3 if re.search(r"leadership|management|executive", label, re.I) else \
            2 if re.search(r"team|people|who-we-are", label, re.I) else 1
        scored.append((-score, href))
    seen, out = set(), []
    for _, href in sorted(scored):
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out[:limit]


async def find_leaders(base_url: str, fetch, max_pages: int = 8) -> list[dict]:
    """Walk the likely leadership pages of a site until one yields people.

    `fetch(url)` returns an httpx response or None (the Scout's own bounded fetch).
    """
    tried: set[str] = set()
    queue: list[str] = []
    home = await fetch(base_url)
    if home is not None:
        people = extract_people(home.text)
        if people:
            return people
        queue.extend(leadership_links(home.text, str(home.url)))
    queue.extend(f"{base_url}{p}" for p in LEADERSHIP_PATHS)
    for url in queue:
        if url in tried or len(tried) >= max_pages:
            continue
        tried.add(url)
        resp = await fetch(url)
        if resp is None:
            continue
        people = extract_people(resp.text)
        if people:
            return people
        for link in leadership_links(resp.text, str(resp.url), limit=2):
            if link not in tried and link not in queue:
                queue.append(link)
    return []
