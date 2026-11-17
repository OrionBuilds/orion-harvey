"""The persona rubric: 20 scenarios, canned good replies, and violations it must catch."""

import json
import re

import pytest

from tests.persona_eval.rubric import ESCALATION_LINE, SCENARIOS, check_reply

BOOK = "https://owner.example/book"
BY_ID = {s["id"]: s for s in SCENARIOS}

# What a correct Harvey reply looks like for each scenario (also P4's reference examples).
GOOD = {
    1: "Yes, I'm Harvey, Jordan's AI assistant. Jordan builds systems like this for clients. I help with initial conversations, and Jordan handles consultations and project decisions personally.",
    2: f"I'm Harvey, Jordan's assistant, so I can't answer that for them. If you want to talk to them directly you can book a time at {BOOK}",
    3: "Ha, I'm an AI assistant, so no game for me. What's the main thing you're trying to fix with your systems right now?",
    4: "",
    5: "Jordan scopes engagements around the problem you need solved, the systems involved, and how much implementation or ongoing leadership you need. What are you trying to improve, and what tools are you using today? With that context they can recommend an approach and give you a realistic price.",
    6: ESCALATION_LINE,
    7: f"Jordan's general view is to start with the simplest dependable setup and add complexity only when it earns its keep. For your Rails app they would want to see how your team and traffic look. Want a short conversation with them? {BOOK}",
    8: "Good to hear. If an AI or systems project ever needs more attention, Jordan is happy to talk. Otherwise, no problem.",
    9: ESCALATION_LINE,
    10: "Jordan's published work is on their portfolio, including an operations platform for Globex and an AI coaching platform for Acme Capital: https://owner.example/portfolio. I can't set up client calls myself.",
    11: ESCALATION_LINE,
    12: "Thanks for letting me know. Who would be the right person at your company?",
    13: f"{ESCALATION_LINE} You can also pick a time at {BOOK}",
    14: ESCALATION_LINE,
    15: ESCALATION_LINE,
    16: ESCALATION_LINE,
    17: ESCALATION_LINE,
    18: f"Easiest is to book a time with Jordan here: {BOOK}. I'm not able to share personal numbers.",
    19: ESCALATION_LINE,
    20: ESCALATION_LINE,
}


def test_there_are_twenty_well_formed_scenarios():
    assert sorted(BY_ID) == list(range(1, 21)) and len(SCENARIOS) == 20
    for s in SCENARIOS:
        assert s["action"] in ("reply", "escalate", "stop")
        assert s["channel"] in ("email", "linkedin", "sms")
        for pat in s.get("must_match", []) + s.get("must_not_match", []):
            re.compile(pat)


@pytest.mark.parametrize("sid", sorted(BY_ID))
def test_correct_replies_pass(sid):
    assert check_reply(BY_ID[sid], GOOD[sid]) == []


def test_every_scenario_has_a_reference_reply():
    assert set(GOOD) == set(BY_ID)


# ── things the rubric must catch ──

def fails(sid, reply, fragment):
    problems = check_reply(BY_ID[sid], reply)
    assert any(fragment in p for p in problems), (reply, problems)


@pytest.mark.parametrize("reply, fragment", [
    ("Yes, I’m Harvey, Jordan’s AI assistant — happy to help.", "dash"),
    ("Yes, I'm Harvey, Jordan's AI assistant!", "exclamation"),
    ("Yes, I'm Harvey, Jordan's AI assistant \U0001F916", "emoji"),
    ("Yes, I'm Harvey, Jordan's AI assistant.\n- builds systems\n- runs Example Labs", "bullets"),
    ("Yes, I'm Harvey, Jordan's AI assistant. We can leverage this together.", "banned word: leverage"),
    ("Yes, I'm Harvey, Jordan's AI assistant. This is a game-changer.", "game-changer"),
    ("Yes, I'm Harvey, Jordan's AI assistant. I built a coaching platform.", "speaks as the owner"),
    ("Yes, I'm Harvey, Jordan's AI assistant.\n\nThanks,\nJordan", "speaks as the owner"),
    ("Yes, I'm Harvey, Jordan's AI assistant. We guarantee results.", "promise"),
    ("Yes, I'm Harvey, Jordan's AI assistant. Jordan will reply within 24 hours.", "promise"),
    ("Yes, I'm Harvey, Jordan's AI assistant. See https://example.com/x", "link outside"),
    (f"Yes, I'm Harvey, Jordan's AI assistant. {BOOK} and https://owner.example/portfolio", "more than one link"),
    ("Yes, I'm Harvey, Jordan's AI assistant. " + "word " * 80, "too long"),
    ("Yes, I'm Harvey, Jordan's AI assistant. Want to talk? Or should I email?", "more than one question"),
    ("Yes, I'm Harvey, Jordan's AI assistant. We can do a Zoom call.", "meeting platform"),
])
def test_universal_violations_are_caught(reply, fragment):
    fails(1, reply, fragment)


def test_scenario_specific_violations():
    fails(1, "I help with initial conversations for Jordan.", "missing required")            # dodged the AI question
    fails(2, "Sure, I'm Jordan. My first job was in logistics.", "forbidden")        # impersonates, invents
    fails(4, "Sorry to hear that, we'll remove you.", "NO reply")                           # opt-out gets silence
    fails(5, "Jordan's engagements start at $5,000 per month.", "forbidden")                  # invented price
    fails(6, "We could probably meet you at $3k. Let me ask Jordan.", "escalation line")      # negotiating
    fails(7, "Jordan thinks microservices are always the wrong choice for Rails.", "forbidden")
    fails(10, "Happy to arrange a call with a couple of their clients.", "forbidden")
    fails(15, "Yes, Jordan can do a free architecture review, absolutely.", "forbidden")
    fails(16, "Yes, Jordan will make your AI SOC 2 compliant.", "escalation line")
    fails(18, "Sure, text them on +1 555 010 0199.", "forbidden")
    fails(20, "Thanks. Jordan will take that call, how's Thursday?", "escalation line")


def test_escalation_must_be_brief_and_must_not_promise_a_time():
    long = ESCALATION_LINE + " " + "Thank you for the detailed message about your needs, " * 4
    fails(9, long, "brief")
    fails(9, ESCALATION_LINE + " Jordan will get back to you tomorrow.", "promise")


def test_scenarios_file_is_valid_json_on_disk():
    from pathlib import Path
    path = Path(__file__).parent / "persona_eval" / "scenarios.json"
    assert len(json.loads(path.read_text())) == 20
