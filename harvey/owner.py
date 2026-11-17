"""Who Harvey works for in assistant mode (persona.mode: assistant).

Every name and link the assistant-mode rules use comes from here, so nothing about a
real person is hardcoded. Values come from the environment first (OWNER_NAME,
OWNER_BOOKING_URL, OWNER_COMPANY), then from harvey.local.yaml / harvey.yaml
(persona.owner_name, product.offer.booking_url, persona.company). Read once, at
import. The owner is always referred to in the third person, with neutral pronouns.
"""

import os
from urllib.parse import urlparse

import yaml


def _from_config() -> tuple[str, str, str]:
    try:
        from harvey.config import _find_config_file
        with open(_find_config_file()) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return "", "", ""
    persona = data.get("persona") or {}
    offer = (data.get("product") or {}).get("offer") or {}
    return (str(persona.get("owner_name") or ""), str(offer.get("booking_url") or ""),
            str(persona.get("company") or ""))


_cfg_name, _cfg_url, _cfg_company = _from_config()

# Full name, for calendar titles and the vault ("Jordan Example").
NAME = (os.getenv("OWNER_NAME") or _cfg_name or "").strip() or "the team"
# What messages call them ("Jordan"). Falls back to neutral wording when unset.
FIRST = NAME.split()[0] if NAME != "the team" else NAME
# The same, for the start of a sentence.
FIRST_CAP = FIRST[:1].upper() + FIRST[1:]
# The one link assistant-mode messages may carry.
BOOKING_URL = (os.getenv("OWNER_BOOKING_URL") or _cfg_url or "").strip() or "https://example.com/book"
# Links in outgoing messages must point at this host (or a subdomain of it).
LINK_HOST = (urlparse(BOOKING_URL).hostname or "example.com").lower().removeprefix("www.")
# The owner's company, as prospects would write it ("we worked with Acme Co"). The
# template placeholder does not count.
_company = (os.getenv("OWNER_COMPANY") or _cfg_company or "").strip()
COMPANIES = (_company,) if _company and _company.lower() != "your company" else ()
