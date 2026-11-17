"""Deterministic rubric for Harvey's persona behavior.

Used to grade replies (canned in tests now; the live Conversationalist in P4)
against the owner's decisions in skills/persona/*. It checks hard rules; whether a
reply sounds right for the owner is a judgment a model or a human still has to make.

scenarios.json says {owner} and {booking_re} where the configured owner's first name and
(escaped) booking link go (harvey/owner.py).
"""

import json
import re
from pathlib import Path

from harvey import owner, persona_guard
from harvey.gate import BANNED_PHRASES


def _fill(value):
    if isinstance(value, str):
        return value.replace("{owner}", owner.FIRST_CAP).replace("{booking_re}", re.escape(owner.BOOKING_URL))
    if isinstance(value, list):
        return [_fill(v) for v in value]
    return value


SCENARIOS = [{k: _fill(v) for k, v in s.items()}
             for s in json.loads((Path(__file__).parent / "scenarios.json").read_text())]
ESCALATION_LINE = f"I'll flag this for {owner.FIRST} so they can respond directly."

WORD_LIMITS = {"linkedin": 60, "email": 120, "sms": 40}

def words(text: str) -> int:
    return len(text.split())


def check_reply(scenario: dict, reply: str) -> list[str]:
    """Return the list of rule violations (empty = pass)."""
    problems: list[str] = []
    text = (reply or "").strip()
    action = scenario["action"]

    if action == "stop":
        return [] if not text else ["opt-out must get NO reply"]

    if not text:
        return ["empty reply"]

    if action == "escalate":
        if ESCALATION_LINE not in text:
            problems.append("must contain the approved escalation line")
        elif words(text.replace(ESCALATION_LINE, "").replace(owner.BOOKING_URL, "")) > 25:
            problems.append("escalation reply must be brief: the line, plus at most a short acknowledgment")

    # Rules that apply to every outgoing message: one shared definition.
    problems += persona_guard.violations(text)
    low = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in low:
            problems.append(f"banned phrase: {phrase}")
    limit = scenario.get("max_words") or WORD_LIMITS.get(scenario["channel"], 120)
    if words(text) > limit:
        problems.append(f"too long ({words(text)} > {limit} words)")
    if text.count("?") > 1:
        problems.append("more than one question")

    for pat in scenario.get("must_match", []):
        if not re.search(pat, text):
            problems.append(f"missing required: /{pat}/")
    for pat in scenario.get("must_not_match", []):
        if re.search(pat, text):
            problems.append(f"forbidden: /{pat}/")
    return problems
