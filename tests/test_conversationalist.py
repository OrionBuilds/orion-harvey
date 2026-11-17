"""Replies in assistant mode: fixed wording for the must-not-improvise cases, a checked
model draft for the rest, one acknowledgment then silence once a thread is Jordan's."""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

import harvey.persona as persona_mod
from harvey import conversation_rules as rules
from harvey.agents.conversationalist import MAX_HARVEY_TURNS, Conversationalist
from harvey.agents.handler import Handler
from harvey.config import HarveyConfig
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.persona_eval.rubric import SCENARIOS, check_reply
from tests.test_writer_assistant import PERSONA_TEXT, SIGNATURE, make_config

BY_ID = {s["id"]: s for s in SCENARIOS}
PROMPT = (
    "You are Harvey. Talking to {{prospect_name}}, {{prospect_title}} at {{company}} on {{channel}}, "
    "stage {{stage}}.\nContext:\n{{persona_files}}\nThread:\n{{thread}}\nOff-topic turns so far in this "
    "thread: {{off_topic_turns}}.\nOUTPUT JSON."
)
EXTRA = {"opinions": "# Opinions\nSimplest dependable solution first.", "faq": "# FAQ\nNone.",
         "logistics": "# Logistics\nBooking: https://owner.example/book"}


@pytest.fixture
def persona_dir(tmp_path, monkeypatch):
    d = tmp_path / "persona"
    d.mkdir()
    for name, text in {**PERSONA_TEXT, **EXTRA}.items():
        (d / f"{name}.md").write_text(text)
    (d / "conversation_prompt.md").write_text(PROMPT)
    monkeypatch.setattr(persona_mod, "PERSONA_DIR", d)
    return d


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


class FakeBrain:
    """think_json returns scripted replies; think returns an intent label."""

    def __init__(self, replies=(), intent="question"):
        self.replies, self.prompts, self.intent = list(replies), [], intent
        self.paused_reason = ""

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, **kw):
        return self.intent

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


async def add_prospect(state, **kw):
    pid = await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="CIO", company="Acme Insurance",
        email="jane@acme-insurance.com", email_status="verified", status="contacted", **kw))
    return await state.get_prospect(pid)


def convo(*messages, stage="engaged", cid="c1"):
    return Conversation(id=cid, prospect_id="p", channel="email", stage=stage,
                        thread=[Message(sender=s, content=c) for s, c in messages])


# ── routing rules ──

EXPECTED_ROUTE = {1: "ai_question", 2: "identity_challenge", 3: None, 4: "escalate", 5: "price_question", 6: "escalate",
                  7: None, 8: None, 9: "escalate", 10: None, 11: "escalate", 12: None, 13: "escalate",
                  14: "escalate", 15: "escalate", 16: "escalate", 17: "escalate", 18: "contact_request", 19: "escalate",
                  20: "escalate"}


@pytest.mark.parametrize("sid", sorted(EXPECTED_ROUTE))
def test_every_eval_scenario_routes_as_decided(sid):
    route = rules.route(BY_ID[sid]["message"])
    assert (route.kind if route else None) == EXPECTED_ROUTE[sid]


@pytest.mark.parametrize("text", [
    "We use Salesforce and the team is stretched thin.",
    "Our CTO left last month and we are hiring.",
    "What does Jordan actually build for companies like ours?",
    "We're a regulated insurer. How do you think about audit trails?",
    "Happy to chat next week if the timing works.",
    "That's interesting, tell me more about the coaching platform.",
    "Tell me about Jordan's background.",
    "Interesting, say more.",
    "Thanks, sounds good.",
    "We run on SAP and Workday, and the café chain we bought last year uses neither.",
])
def test_ordinary_messages_are_not_routed(text):
    assert rules.route(text) is None


@pytest.mark.parametrize("text, kind", [
    ("Are you a real person? Just checking this isn't automated.", "ai_question"),
    ("is this written by AI?", "ai_question"),
    ("What do you charge for something like this?", "price_question"),
    ("Do you have pricing?", "price_question"),
    ("We were a client of Example Labs back in 2023.", "escalate"),
    ("Jordan and I go way back, tell them I said hi.", "escalate"),
    ("Can you send a quote?", "escalate"),
    ("Would Jordan do a free assessment first?", "escalate"),
    ("Is this actually Jordan writing to me?", "identity_challenge"),
    ("Does your work meet HIPAA?", "escalate"),
])
def test_phrasing_variants(text, kind):
    assert rules.route(text).kind == kind


@pytest.mark.parametrize("text", [
    "Hola, me interesa. ¿Pueden escribirme en español?",
    "Bonjour, merci pour votre message, nous sommes intéressés par votre offre.",
    "Guten Tag, vielen Dank für Ihre Nachricht, wir würden gern mehr erfahren.",
    "Gracias por escribir pero ahora mismo no tenemos tiempo para esto porque estamos ocupados con otros proyectos",
])
def test_non_english_messages_go_to_owner(text):
    assert rules.route(text).kind == "escalate"


def test_strip_quoted_keeps_only_what_the_person_wrote():
    raw = ("Sounds good, send details.\n\n> On the question of cost\n> we should talk\n\n"
           "On Mon, Oct 19, 2026 at 9:14 AM Harvey <harvey@go.example.com> wrote:\n> Hey Jane,\n")
    assert rules.strip_quoted(raw) == "Sounds good, send details."
    assert rules.strip_quoted("Plain reply.") == "Plain reply."


def test_fixed_answers_pass_the_persona_rubric():
    assert check_reply(BY_ID[1], rules.AI_DISCLOSURE) == []
    assert check_reply(BY_ID[2], rules.IDENTITY_LINE) == []
    assert check_reply(BY_ID[18], rules.CONTACT_LINE) == []
    assert check_reply(BY_ID[5], rules.PRICE_LINE) == []
    assert check_reply({**BY_ID[5], "channel": "linkedin"}, rules.PRICE_LINE_SHORT) == []
    for sid in (6, 9, 14):
        assert check_reply(BY_ID[sid], rules.ESCALATION_LINE) == []


# ── Conversationalist ──

@pytest.mark.asyncio
@pytest.mark.parametrize("message, text, handoff", [
    ("Are you a bot or AI?", rules.AI_DISCLOSURE, False),
    ("Sounds interesting. How much does this cost?", rules.PRICE_LINE, False),
    ("Can you send over a proposal and an SOW?", rules.ESCALATION_LINE, True),
    ("Prove you're really Jordan.", rules.IDENTITY_LINE, False),
    ("Can we move this to WhatsApp?", rules.CONTACT_LINE, False),
])
async def test_fixed_routes_never_call_the_model(state, persona_dir, message, text, handoff):
    brain = FakeBrain()
    reply = await Conversationalist(brain, state, make_config()).respond(
        message, await add_prospect(state), convo(("prospect", message)), "email")
    assert (reply.text, reply.handoff, reply.fixed) == (text, handoff, True) and brain.prompts == []


@pytest.mark.asyncio
async def test_linkedin_price_answer_fits_the_linkedin_limit(state, persona_dir):
    reply = await Conversationalist(FakeBrain(), state, make_config()).respond(
        "How much?", await add_prospect(state), convo(("prospect", "How much?")), "linkedin")
    assert reply.text == rules.PRICE_LINE_SHORT and len(reply.text.split()) <= 60


@pytest.mark.asyncio
async def test_an_ordinary_reply_is_drafted_from_the_persona_files_and_the_thread(state, persona_dir):
    p = await add_prospect(state)
    await state.add_outbox_item(prospect_id=p.id, to_email=p.email, subject="ai access ownership",
                                body="Regulated teams often lack one owner for AI data access.",
                                send_at="2026-10-20T00:00:00", status="sent", step=1, campaign_id="c")
    good = {"reply": "Jordan's general view is to start with the simplest dependable setup. "
                     "What does your team use today?", "off_topic": False, "handoff": False}
    brain = FakeBrain([good])
    c = convo(("prospect", "What would you do first at a company like ours?"))
    reply = await Conversationalist(brain, state, make_config()).respond(c.thread[0].content, p, c, "email")

    assert reply.text.startswith("Jordan's general view") and not reply.handoff
    prompt = brain.prompts[0]
    assert "Jane Doe, CIO at Acme Insurance on email" in prompt
    assert "Jordan Example is an AI consultant" in prompt                    # persona files
    assert "What would you do first" in prompt                             # the thread


@pytest.mark.asyncio
async def test_a_rule_breaking_draft_is_retried_with_the_reasons_then_handed_to_owner(state, persona_dir):
    bad = {"reply": "Jordan cut review time by 40%! Worth a chat?", "off_topic": False}
    brain = FakeBrain([bad, bad])
    c = convo(("prospect", "Tell me more."))
    reply = await Conversationalist(brain, state, make_config()).respond("Tell me more.", await add_prospect(state), c, "email")
    assert reply.text == "" and reply.handoff and len(brain.prompts) == 2
    assert "exclamation mark" in brain.prompts[1] and "number not in the provided facts: 40%" in brain.prompts[1]


@pytest.mark.asyncio
async def test_the_model_can_hand_off_but_only_with_the_approved_line(state, persona_dir):
    brain = FakeBrain([{"reply": "Let me think about that!", "handoff": True}])
    c = convo(("prospect", "Tell me more."))
    reply = await Conversationalist(brain, state, make_config()).respond("Tell me more.", await add_prospect(state), c, "email")
    assert reply.text == rules.ESCALATION_LINE and reply.handoff


@pytest.mark.asyncio
async def test_off_topic_turns_are_counted_and_the_sixth_goes_to_owner(state, persona_dir):
    p = await add_prospect(state)
    conv = Conversationalist(FakeBrain(), state, make_config())
    for n in range(6):
        conv.brain = FakeBrain([{"reply": "Ha, I'm an AI assistant. What are you trying to fix?", "off_topic": True}])
        reply = await conv.respond("lol who won the game", p, convo(("prospect", "lol who won the game")), "email")
        assert not reply.handoff, n
    assert "off_topic_turns" not in conv.brain.prompts[0] and "so far in this thread: 5" in conv.brain.prompts[0]
    last = await conv.respond("another joke", p, convo(("prospect", "another joke")), "email")
    assert last.handoff and last.reason == "kept going off topic"


@pytest.mark.asyncio
async def test_a_long_thread_goes_to_owner(state, persona_dir):
    msgs = []
    for _ in range(MAX_HARVEY_TURNS):
        msgs += [("prospect", "ok"), ("harvey", "Sure.")]
    c = convo(*msgs, ("prospect", "and another thing"))
    reply = await Conversationalist(FakeBrain(), state, make_config()).respond("and another thing", await add_prospect(state), c, "email")
    assert reply.handoff and reply.reason == "long thread"


@pytest.mark.asyncio
async def test_missing_persona_files_mean_no_draft(state, persona_dir):
    (persona_dir / "boundaries.md").unlink()
    brain = FakeBrain([{"reply": "Hello."}])
    c = convo(("prospect", "Tell me more."))
    reply = await Conversationalist(brain, state, make_config()).respond("Tell me more.", await add_prospect(state), c, "email")
    assert reply.text == "" and reply.handoff and brain.prompts == []


# ── Handler integration ──

def make_handler(state, brain, config=None):
    h = Handler(brain=brain, state=state, config=config or make_config(), env=SimpleNamespace(instantly_api_key=""))
    h.provider = SimpleNamespace(name="smtp")
    return h


async def pending(state):
    return await state.get_outbox(status="pending_review")


@pytest.mark.asyncio
async def test_an_ordinary_email_reply_is_queued_composed_in_the_approval_ladder(state, persona_dir):
    p = await add_prospect(state)
    good = {"reply": "Jordan's general view is to start with the simplest dependable setup. What does your team use today?",
            "off_topic": False}
    h = make_handler(state, FakeBrain([good], intent="question"))
    await h._process_reply(p.email, "What would you do first?", "", reply_meta={"subject": "ai access ownership", "message_id": "<m1>"})

    rows = await pending(state)
    assert len(rows) == 1 and rows[0]["kind"] == "reply" and rows[0]["subject"] == "Re: ai access ownership"
    assert rows[0]["body"].startswith("Hey Jane,\n\nJordan's general view") and rows[0]["body"].endswith(SIGNATURE)
    assert "Thanks," in rows[0]["body"]


@pytest.mark.asyncio
async def test_ai_question_gets_owner_s_exact_answer(state, persona_dir):
    p = await add_prospect(state)
    h = make_handler(state, FakeBrain(intent="question"))
    await h._process_reply(p.email, "Are you a bot or AI?", "", reply_meta={"subject": "x"})
    rows = await pending(state)
    assert len(rows) == 1 and rules.AI_DISCLOSURE in rows[0]["body"] and h.brain.prompts == []


@pytest.mark.asyncio
async def test_escalation_sends_one_acknowledgment_then_the_thread_is_owner_s(state, persona_dir):
    p = await add_prospect(state)
    h = make_handler(state, FakeBrain(intent="question"))
    await h._process_reply(p.email, "Can you send over a proposal and an SOW?", "", reply_meta={"subject": "x"})
    rows = await pending(state)
    assert len(rows) == 1 and rules.ESCALATION_LINE in rows[0]["body"]
    held = await state.get_conversations_by_status("needs_human")
    assert len(held) == 1

    # They write again: recorded for Jordan, no second reply, no second conversation.
    # (Stop-on-reply cancels the still-unsent acknowledgment; nothing replaces it.)
    await h._process_reply(p.email, "Hello? Any update on that proposal?", "", reply_meta={"subject": "x"})
    assert await pending(state) == [] and len(await state.get_outbox(status="cancelled")) == 1
    held = await state.get_conversations_by_status("needs_human")
    assert len(held) == 1 and held[0].thread[-1].content.startswith("Hello?")
    assert await state.get_conversations_by_status("open") == []


@pytest.mark.asyncio
async def test_an_opt_out_is_still_honored_on_a_thread_that_is_owner_s(state, persona_dir):
    p = await add_prospect(state)
    h = make_handler(state, FakeBrain(intent="question"))
    await h._process_reply(p.email, "Can you send a proposal?", "", reply_meta={"subject": "x"})
    await h._process_reply(p.email, "Please stop emailing me.", "", reply_meta={"subject": "x"})
    assert (await state.get_prospect(p.id)).status == "opted_out"
    assert await pending(state) == []                              # no reply to an opt-out; the old ack was cancelled


@pytest.mark.asyncio
async def test_a_draft_that_cannot_pass_hands_the_thread_over_without_replying(state, persona_dir):
    p = await add_prospect(state)
    bad = {"reply": "Free audit, game-changer!", "off_topic": False}
    h = make_handler(state, FakeBrain([bad, bad], intent="question"))
    await h._process_reply(p.email, "Tell me more about it.", "", reply_meta={"subject": "x"})
    assert await pending(state) == []
    assert len(await state.get_conversations_by_status("needs_human")) == 1


@pytest.mark.asyncio
async def test_quoted_text_in_a_reply_is_not_treated_as_the_message(state, persona_dir):
    p = await add_prospect(state)
    h = make_handler(state, FakeBrain([{"reply": "Good question. What tools do you use today?", "off_topic": False}], intent="question"))
    raw = "Interesting, say more.\n\nOn Mon, Oct 19, 2026 at 9:14 AM Harvey wrote:\n> We can send a proposal and a quote.\n"
    await h._process_reply(p.email, raw, "", reply_meta={"subject": "x"})
    rows = await pending(state)
    assert len(rows) == 1 and rules.ESCALATION_LINE not in rows[0]["body"]       # the quoted "proposal" did not trip a hand-off


@pytest.mark.asyncio
async def test_self_mode_still_uses_the_upstream_reply_path(state):
    p = await add_prospect(state)
    cfg = make_config(mode="self", greeting="", closer="", signature="")
    brain = FakeBrain(intent="question")
    brain.think = lambda prompt, **kw: _async("Sure, happy to help.")      # upstream path calls think()
    h = make_handler(state, brain, cfg)
    await h._process_reply(p.email, "Are you a bot or AI?", "", reply_meta={"subject": "x"})
    rows = await pending(state)
    assert rows and rules.AI_DISCLOSURE not in rows[0]["body"]


async def _async(value):
    return value
