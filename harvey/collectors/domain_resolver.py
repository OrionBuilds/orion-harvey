"""Find the website of a company known only by name (job boards often give no URL).

No search API: guess a handful of likely hostnames from the name and accept one only if the
page that answers actually names the company. A parked or unrelated domain fails that test,
so a wrong guess costs nothing and a right one unlocks the team-page and verification steps.
"""

import asyncio
import re
from urllib.parse import urlparse

import httpx

from harvey.collectors.discover import normalize_domain

TLDS = ("com", "ai", "io", "co")
MAX_CANDIDATES = 8
FETCH_TIMEOUT = 6.0

# Words that carry no identity: dropped when building hostnames and when checking the page.
_LEGAL = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "plc", "lp", "llp", "the", "of"}
_GENERIC = {"group", "holdings", "global", "operations", "services", "solutions", "international",
            "partners", "enterprises", "systems", "family", "usa", "us", "america", "american",
            "technologies", "technology", "tech"}
_PARKED = re.compile(r"domain (is )?for sale|buy this domain|hugedomains|sedo\.com|parked (free|domain)|"
                     r"this domain may be for sale|is for sale|make an offer|godaddy\.com/domains", re.I)
_BAD_NAME = re.compile(r"confidential|undisclosed|stealth|\bamazon (staffing|jobs)\b", re.I)


def name_tokens(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", re.sub(r"&", " and ", (name or "").lower()))
    return [w for w in words if w not in _LEGAL]


def candidate_hosts(name: str) -> list[str]:
    toks = name_tokens(name)
    if not toks or _BAD_NAME.search(name or ""):
        return []
    core = [t for t in toks if t not in _GENERIC] or toks
    stems: list[str] = []
    for stem in ("".join(toks), "".join(core), "-".join(core), "".join(core[:2]), core[0]):
        if len(stem) >= 4 and stem not in stems:
            stems.append(stem)
    hosts = [f"{s}.{tld}" for s in stems for tld in TLDS]
    return hosts[:MAX_CANDIDATES]


def _registered(host: str) -> str:
    parts = (host or "").lower().strip(".").split(".")
    return ".".join(parts[-2:])


def _head_text(html: str) -> str:
    head = html[:60000]
    title = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    site = re.search(r'property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)', head, re.I)
    h1 = " ".join(re.findall(r"<h1[^>]*>(.*?)</h1>", head, re.I | re.S))
    return " ".join([(title.group(1) if title else ""), (site.group(1) if site else ""), h1]).lower()


def _page_text(html: str) -> str:
    head = html[:60000]
    title = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    site = re.search(r'property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)', head, re.I)
    h1 = " ".join(re.findall(r"<h1[^>]*>(.*?)</h1>", head, re.I | re.S))
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", head, flags=re.I | re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return " ".join([(title.group(1) if title else ""), (site.group(1) if site else ""), h1, body[:4000]]).lower()


def page_matches(name: str, html: str) -> bool:
    """True when the page names the company and is not a parked or for-sale page."""
    text = _page_text(html)
    if _PARKED.search(text):
        return False
    toks = name_tokens(name)
    core = [t for t in toks if t not in _GENERIC] or toks
    if not core:
        return False
    joined = "".join(core)
    if len(joined) <= 6:   # short names collide with everything; the page must say it up front
        return bool(re.search(rf"\b{re.escape(joined)}\b|\b{re.escape(' '.join(core))}\b", _head_text(html)))
    return all(re.search(rf"\b{re.escape(t)}", text) for t in core) or joined in re.sub(r"[^a-z0-9]", "", text[:1500])


async def resolve_domain(name: str, client: httpx.AsyncClient) -> str:
    """The normalised domain of `name`'s website, or '' when none could be confirmed."""
    for host in candidate_hosts(name):
        try:
            resp = await client.get(f"https://{host}", follow_redirects=True, timeout=FETCH_TIMEOUT)
        except (httpx.HTTPError, ValueError):
            continue
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html"):
            continue
        final = normalize_domain(str(resp.url)) or host
        if _registered(final) != _registered(host):   # redirected somewhere else: marketplace, parent, parked
            continue
        if page_matches(name, resp.text):
            return final
    return ""


async def resolve_many(names: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None,
                       concurrency: int = 5) -> dict[str, str]:
    """{company_id: name} -> {company_id: domain} for the ones that resolved."""
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, str] = {}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HarveyBot/1.0)"}
    async with httpx.AsyncClient(headers=headers, transport=transport, verify=True) as client:
        async def one(cid: str, name: str):
            async with sem:
                domain = await resolve_domain(name, client)
                if domain:
                    out[cid] = domain
        await asyncio.gather(*(one(c, n) for c, n in names.items()), return_exceptions=True)
    return out
