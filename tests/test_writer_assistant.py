"""The Writer in assistant mode: drafts as Harvey, checked against Jordan's rules,
retried once, composed (greeting + sign-off) in code, refused without persona files."""

import os
import tempfile
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio

import harvey.persona as persona_mod
from harvey import persona_guard
from harvey.agents.writer import Writer
from harvey.brain import Brain
from harvey.channels.email import EmailChannel
from harvey.channels.linkedin import LinkedInChannel
from harvey.config import HarveyConfig
from harvey.models.prospect import Prospect
from harvey.persona import compose_email, load_persona_context
from harvey.state import StateManager

SIGNATURE = "Harvey\nJordan Example's AI assistant"
PERSONA_TEXT = {
    "identity": "# Identity\nJordan Example is an AI consultant and fractional CTO.",
    "icp": "# ICP\nRegulated mid-market. Cohort A: 250 to 2,500 employees.",
    "offers_pricing": "# Offers\nAssessment, implementation, fractional CTO. Scoped per engagement.",
    "stories": ("# Stories\nS1 Acme Capital: \"Jordan built an AI coaching platform for a financial advisory "
                "firm that scores calls against the firm's own standards.\" Published gate: 85% agreement."),
    "voice": "# Voice\nShort and plain.",
    "boundaries": "# Boundaries\nNever quote a price.",
}


def make_config(**persona):
    base = dict(name="Harvey", company="Jordan Example", role="AI assistant to Jordan Example",
                email="harvey@go.example.com", linkedin="", tone="plain, direct",
                mode="assistant", greeting="Hey {{first_name}},", closer="Thanks,", signature=SIGNATURE)
    base.update(persona)
    return HarveyConfig(
        persona=base,
        product={"name": "AI consulting", "description": "AI and fractional CTO", "pricing": "scoped",
                 "key_benefits": ["one"], "objection_responses": {}},
        icp={"industries": ["x"], "company_size": "100-2500", "titles": ["CIO"], "geography": ["US"]},
        channels={"email": {"provider": "smtp", "require_approval": True,
                            "physical_address": "1 Test St, Tampa, FL 33601"}},
    )


@pytest.fixture
def persona_dir(tmp_path, monkeypatch):
    d = tmp_path / "persona"
    d.mkdir()
    for name, text in PERSONA_TEXT.items():
        (d / f"{name}.md").write_text(text)
    monkeypatch.setattr(persona_mod, "PERSONA_DIR", d)
    return d


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


class ScriptedBrain(Brain):
    """The real Brain (real prompt loading) with the model replaced by a script."""

    def __init__(self, state, replies):
        super().__init__(state)
        self.replies, self.prompts = list(replies), []

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


EMAIL_1 = {"step": 1, "subject": "ai access ownership",
           "body": ("Regulated teams adopting AI often lack one owner for data access and audit trails. "
                    "Is that a gap at Acme Insurance?")}
EMAIL_2 = {"step": 2, "subject": "a different angle",
           "body": ("Another angle: who approves what an AI system is allowed to do. Jordan built an AI coaching "
                    "platform for a financial advisory firm that scores calls against the firm's own standards. "
                    "A short conversation with Jordan would cover it:\nhttps://owner.example/book")}
EMAIL_3 = {"step": 3, "subject": "closing the loop",
           "body": "Looks like the timing is off, so I'll leave it here. Jordan is around if it becomes a priority."}
GOOD_SEQ = {"emails": [EMAIL_1, EMAIL_2, EMAIL_3]}
SELF_1 = {"subject": "ai rollout ownership", "body": "Teams rolling out AI often lack one owner. Is that you?"}


def seq(**changes):
    """GOOD_SEQ with email N's fields replaced: seq(e1_body="...", e3_subject="...")."""
    emails = [dict(e) for e in GOOD_SEQ["emails"]]
    for key, value in changes.items():
        n, field = key.split("_", 1)
        emails[int(n[1]) - 1][field] = value
    return {"emails": emails}


def writer_for(state, replies, config=None):
    brain = ScriptedBrain(state, replies)
    writer = Writer(brain, state, config or make_config())
    writer.skills, writer.persona_text = "", load_persona_context()[0]
    return writer, brain


async def prospect(state, **kw):
    pid = await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="CIO", company="Acme Insurance",
        email="jane@acme-insurance.com", email_status="verified", status="new",
        industry="insurance", **kw))
    return await state.get_prospect(pid)


# ── composition ──

def test_greeting_body_and_signature_are_composed_in_code():
    cfg = make_config().persona
    out = compose_email("Body text.", first_name="Jane", step=1, last_step=3, persona=cfg)
    assert out == f"Hey Jane,\n\nBody text.\n\n{SIGNATURE}"


def test_closer_only_on_middle_steps():
    cfg = make_config().persona
    mid = compose_email("Body.", first_name="Jane", step=2, last_step=3, persona=cfg)
    assert mid.endswith(f"Thanks,\n{SIGNATURE}")
    for step in (1, 3):
        assert "Thanks," not in compose_email("Body.", first_name="Jane", step=step, last_step=3, persona=cfg)


def test_template_emails_keep_the_merge_variable_for_later():
    out = compose_email("Body.", first_name="{{first_name}}", step=1, last_step=3, persona=make_config().persona)
    assert out.startswith("Hey {{first_name}},")


def test_blank_greeting_and_signature_add_nothing():
    cfg = make_config(greeting="", signature="", closer="").persona
    assert compose_email("Body.", first_name="Jane", step=2, last_step=3, persona=cfg) == "Body."


def test_persona_files_report_what_is_missing(persona_dir):
    text, missing = load_persona_context()
    assert missing == [] and "persona/boundaries.md" in text
    (persona_dir / "boundaries.md").unlink()
    _, missing = load_persona_context()
    assert missing == ["boundaries"]


# ── the guard ──

@pytest.mark.parametrize("body, fragment", [
    ("Hey Jane, regulated teams need an owner.", "own greeting"),
    ("Regulated teams need an owner.\n\nThanks,\nHarvey", "own sign-off"),
    ("Jordan helped cut review time by 40% last year.", "number not in the provided facts: 40%"),
    ("Jordan built this in 12 weeks.", "number not in the provided facts: 12"),
    ("Engagements start at $5,000 a month.", "price"),
    ("I built an AI coaching platform.", "speaks as the owner"),
    ("Jordan guarantees compliance.", "promise"),
    ("Book here: https://calendly.com/jordan", "link outside"),
    ("Harvey here, Jordan's AI assistant. Regulated teams need an owner.", "introduces Harvey"),
    ("Jordan can talk it through in 15 minutes.", "meeting length"),
    ("Jordan has a 20-30 min slot.", "meeting length"),
    ("Let's do a Zoom call.", "meeting platform"),
])
def test_draft_problems(body, fragment):
    assert any(fragment in p for p in persona_guard.draft_problems(body, allowed_text="")), body


def test_numbers_that_appear_in_the_facts_are_allowed():
    ok = persona_guard.draft_problems(
        "Jordan's published quality gate is 85% agreement.", allowed_text="Published gate: 85% agreement.")
    assert ok == []


# ── Writer: all three emails per person ──

@pytest.mark.asyncio
async def test_sequence_is_checked_composed_and_uses_the_assistant_prompt(state, persona_dir):
    writer, brain = writer_for(state, [GOOD_SEQ])
    emails = await writer._draft_assistant_sequence(await prospect(state))

    assert [e["subject"] for e in emails] == ["ai access ownership", "a different angle", "closing the loop"]
    assert all(e["body"].startswith("Hey Jane,\n\n") and e["body"].endswith(SIGNATURE) for e in emails)
    assert "Thanks," not in emails[0]["body"] and "Thanks," in emails[1]["body"] and "Thanks," not in emails[2]["body"]
    assert "https://owner.example/book" in emails[1]["body"]
    prompt = brain.prompts[0]
    assert "You write cold outreach emails on Jordan Example's behalf" in prompt
    assert "THE OWNER'S PERSONA FILES" in prompt and "Acme Insurance" in prompt
    assert "{{" not in prompt.replace("{{first_name}}", "").replace("{{company}}", "").replace("{{title}}", "")


@pytest.mark.asyncio
async def test_a_rule_breaking_sequence_is_retried_once_with_the_reasons(state, persona_dir):
    bad = seq(e1_body="Jordan cut review time by 40%! A game-changer. Worth a chat?")
    writer, brain = writer_for(state, [bad, GOOD_SEQ])
    emails = await writer._draft_assistant_sequence(await prospect(state))

    assert emails is not None and len(emails) == 3
    retry = brain.prompts[1]
    assert "broke these rules" in retry and "exclamation mark" in retry and "banned word: game-changer" in retry
    assert "number not in the provided facts: 40%" in retry


@pytest.mark.asyncio
async def test_two_bad_sequences_mean_no_emails_not_a_sent_one(state, persona_dir):
    bad = seq(e1_body="Hey Jane, I built a platform. Free audit?", e1_subject="Quick Question")
    writer, brain = writer_for(state, [bad, bad])
    assert await writer._draft_assistant_sequence(await prospect(state)) is None
    assert len(brain.prompts) == 2


LONG = "word " * 45


@pytest.mark.asyncio
@pytest.mark.parametrize("changes, fragment", [
    (dict(e1_subject="Ai Access"), "lowercase"),
    (dict(e1_subject="one"), "2 to 5 words"),
    (dict(e1_body="Teams lack an owner for AI. Jordan built a platform. Is that you?"), "must not mention Jordan"),
    (dict(e1_body="Teams lack an owner for AI. Is that you, and is a short conversation useful?"), "must not mention Jordan"),
    (dict(e1_body="Teams lack an owner for AI. See https://owner.example/book. Is that you?"), "no link"),
    (dict(e1_body="word " * 80 + "Is that you?"), "under 75 words"),
    (dict(e1_body="Teams lack an owner for AI."), "email 1 needs exactly one question"),
    (dict(e1_body="Who owns it? And who approves it?"), "email 1 needs exactly one question"),
    (dict(e2_body="Jordan built an AI coaching platform for a financial advisory firm. Want to talk?"),
     "must contain the booking link"),
    (dict(e2_body="Jordan built it for an advisory firm. Does that fit? https://owner.example/book"),
     "must not ask a question"),
    (dict(e2_body="A short conversation would cover it: https://owner.example/book"), "must name Jordan"),
    (dict(e2_body="Jordan can talk it through: https://owner.example/book and https://example.com"),
     "more than one link"),
    (dict(e3_body="Timing seems off. Should I leave the door open?"), "must not ask a question"),
    (dict(e3_body=LONG), "under 40 words"),
    (dict(e3_body="Jordan is around: https://owner.example/book"), "must contain no link"),
    (dict(e3_body="Jordan's work saved 15 hours a week at one client. The door is open."), "unapproved result claim"),
    (dict(e3_subject="a different angle"), "subjects must differ"),
    (dict(e3_body="Regulated teams adopting AI often lack one owner for data access and audit trails."),
     "repeats across emails"),
])
async def test_structural_rules_are_enforced(state, persona_dir, changes, fragment):
    bad = seq(**changes)
    writer, brain = writer_for(state, [bad, bad])
    assert await writer._draft_assistant_sequence(await prospect(state)) is None
    assert fragment in brain.prompts[1]


@pytest.mark.asyncio
async def test_a_sequence_with_fewer_than_three_emails_is_rejected(state, persona_dir):
    short = {"emails": GOOD_SEQ["emails"][:2]}
    writer, brain = writer_for(state, [short, short])
    assert await writer._draft_assistant_sequence(await prospect(state)) is None
    assert "need exactly 3 emails (got 2)" in brain.prompts[1]


# ── Writer.run: staging, no template, give-up ──

@pytest.mark.asyncio
async def test_run_stages_three_personal_emails_on_the_day_0_3_7_schedule(state, persona_dir):
    p = await prospect(state)
    brain = ScriptedBrain(state, [GOOD_SEQ])
    await Writer(brain, state, make_config()).run()

    rows = sorted(await state.get_outbox(status="pending_review"), key=lambda r: r["step"])
    assert [r["step"] for r in rows] == [1, 2, 3] and {r["prospect_id"] for r in rows} == {p.id}
    assert rows[0]["body"].startswith("Hey Jane,") and rows[0]["body"].endswith(SIGNATURE)
    assert "owner.example/book" not in rows[0]["body"] and "owner.example/book" in rows[1]["body"]
    days = [datetime.fromisoformat(r["send_at"]) for r in rows]
    assert [round((d - days[0]).total_seconds() / 86400) for d in days] == [0, 3, 7]
    assert len(brain.prompts) == 1                                   # one call for all three emails
    assert (await state.get_prospect(p.id)).status == "queued"
    # No shared template for the Sender to stage later: the campaign is active, never 'draft'.
    assert await state.get_campaigns_by_status("draft") == []
    assert len(await state.get_campaigns_by_status("active")) == 1


@pytest.mark.asyncio
async def test_two_prospects_get_their_own_sequences(state, persona_dir):
    await prospect(state)
    other = await state.add_prospect(Prospect(
        first_name="Raj", last_name="Patel", title="CTO", company="Bay Mutual", email="raj@baymutual.com",
        email_status="verified", status="new", industry="insurance"))
    other2 = seq(e1_body="Insurers often hold AI projects for lack of an approval path. Is that true at Bay Mutual?")

    class PerCompanyBrain(ScriptedBrain):
        async def think_json(self, prompt, **kw):                  # prospect order is not guaranteed
            self.prompts.append(prompt)
            return other2 if "Bay Mutual" in prompt else GOOD_SEQ

    brain = PerCompanyBrain(state, [])
    await Writer(brain, state, make_config()).run()

    rows = await state.get_outbox(status="pending_review")
    assert len(rows) == 6 and len(brain.prompts) == 2
    first_bodies = {r["to_email"]: r["body"] for r in rows if r["step"] == 1}
    assert "Acme Insurance" in first_bodies["jane@acme-insurance.com"]
    assert "Bay Mutual" in first_bodies["raj@baymutual.com"]
    assert other                                                      # both were drafted


@pytest.mark.asyncio
async def test_a_failed_draft_sends_nothing_and_is_retried_then_given_up_on(state, persona_dir):
    p = await prospect(state)
    bad = seq(e1_body="Hey Jane, I built a platform. Free audit?")
    for cycle in range(1, 4):
        brain = ScriptedBrain(state, [bad, bad])
        await Writer(brain, state, make_config()).run()
        assert await state.get_outbox(status="pending_review") == []     # no template fallback, ever
        assert await state.get_campaigns_by_status("draft") == []
        expected = "new" if cycle < 3 else "lost"
        assert (await state.get_prospect(p.id)).status == expected
    assert await state.get_setting(f"writer_draft_failures:{p.id}") == "3"


@pytest.mark.asyncio
async def test_run_refuses_to_write_when_persona_files_are_missing(state, persona_dir):
    (persona_dir / "boundaries.md").unlink()
    await prospect(state)
    brain = ScriptedBrain(state, [GOOD_SEQ])
    await Writer(brain, state, make_config()).run()
    assert brain.prompts == [] and await state.get_outbox(status="pending_review") == []


@pytest.mark.asyncio
async def test_assistant_mode_refuses_a_provider_that_cannot_send_per_prospect(state, persona_dir):
    cfg = make_config()
    cfg.channels.email.provider = "instantly"
    await prospect(state)
    brain = ScriptedBrain(state, [GOOD_SEQ])
    await Writer(brain, state, cfg).run()
    assert brain.prompts == [] and await state.get_outbox(status="pending_review") == []


@pytest.mark.asyncio
async def test_self_mode_is_unchanged(state):
    cfg = make_config(mode="self", greeting="", closer="", signature="")
    brain = ScriptedBrain(state, [SELF_1])
    writer = Writer(brain, state, cfg)
    writer.skills = ""
    draft = await writer._write_personal_email(await prospect(state))
    assert draft["body"] == SELF_1["body"]                       # no greeting, no signature, no checks
    assert "behalf" not in brain.prompts[0]                       # upstream prompt, not the assistant one


# ── send gates ──

def email_item(body, subject="ai rollout ownership"):
    return {"to_email": "jane@acme-insurance.com", "kind": "sequence", "subject": subject, "body": body}


def full(body):
    return f"Hey Jane,\n\n{body}\n\n{SIGNATURE}\n\n--\n1 Test St, Tampa, FL 33601\nNot interested? Just reply STOP."


@pytest.mark.asyncio
async def test_email_gate_enforces_the_persona_rules_on_what_will_be_sent(state):
    cfg = make_config()
    ch = EmailChannel(cfg, SimpleNamespace(is_configured=lambda: True))
    p = await prospect(state)
    ok = ch.content_gate(email_item(full("Teams often lack an owner for AI access. Is that a gap?")), "ai rollout ownership",
                         full("Teams often lack an owner for AI access. Is that a gap?"), p)
    assert ok.ok, ok.reasons

    for body in ("I built a platform for a financial firm. Is that you?",
                 "Jordan guarantees compliance. Is that you?",
                 "Engagements start at $5,000. Is that you?",
                 "This is a game-changer. Is that you?",
                 "See https://calendly.com/jordan for time. Is that you?"):
        res = ch.content_gate(email_item(full(body)), "ai rollout ownership", full(body), p)
        assert not res.ok and any(r.startswith("persona:") for r in res.reasons), body


@pytest.mark.asyncio
async def test_email_gate_does_not_apply_persona_rules_in_self_mode(state):
    ch = EmailChannel(make_config(mode="self"), SimpleNamespace(is_configured=lambda: True))
    body = "I built a platform. Leverage it. Is that you?"
    res = ch.content_gate(email_item(body), "ai rollout ownership", body + "\n\n--\n1 Test St, Tampa, FL 33601", await prospect(state))
    assert not any(r.startswith("persona:") for r in res.reasons)


@pytest.mark.asyncio
async def test_footer_links_are_not_judged_as_the_message(state):
    cfg = make_config()
    cfg.channels.email.unsubscribe_base_url = "https://go.example.com"
    ch = EmailChannel(cfg, SimpleNamespace(is_configured=lambda: True))
    body = full("Teams often lack an owner for AI access. Is that a gap?").replace(
        "Not interested? Just reply STOP.", "Unsubscribe: https://go.example.com/u/abc")
    res = ch.content_gate(email_item(body), "ai rollout ownership", body, await prospect(state))
    assert not any(r.startswith("persona:") for r in res.reasons), res.reasons


@pytest.mark.asyncio
async def test_linkedin_gate_enforces_the_persona_rules(state):
    cfg = make_config()
    cfg.channels.linkedin = SimpleNamespace(enabled=True, mode="auto", max_daily_connections=7,
                                            max_daily_messages=5, max_weekly_connections=30,
                                            warmup_start_date="", timezone="America/New_York")
    ch = LinkedInChannel(cfg, state, automation_factory=object)
    item = {"kind": "message", "to_ref": "https://www.linkedin.com/in/jane", "thread_ref": ""}
    p = SimpleNamespace(id="p")
    assert ch.content_gate(item, "", "Hey Jane, thanks for connecting. What are you working on?", p).ok
    assert not ch.content_gate(item, "", "Hey Jane, I built a platform. Want to see it?", p).ok
    assert not ch.content_gate(item, "", "Hey Jane, we guarantee results.", p).ok


# ── open-role postings become a stated fact ──

@pytest.mark.asyncio
async def test_a_public_job_posting_is_a_fact_and_a_careers_page_fragment_is_not(state, persona_dir):
    from harvey.models.company import Company
    from harvey.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    company_id = await state.add_company(Company(name="Acme Insurance", domain="acme-insurance.com"))
    p = await prospect(state)
    async with state._connect() as db:
        await db.execute("UPDATE prospects SET company_id = ? WHERE id = ?", (company_id, p.id))
        await db.commit()
    await state.add_observations([
        {"company_id": company_id, "signal_code": "HIRING_ROLE", "collector": "discover",
         "value_text": "Vice President of Technology", "evidence_url": "https://www.indeed.com/viewjob?jk=abc"},
        {"company_id": company_id, "signal_code": "HIRING_ROLE", "collector": "profile",
         "value_text": "Apply Here", "evidence_url": "https://acme.com/careers"},
    ])
    writer, brain = writer_for(state, [GOOD_SEQ])
    facts = await writer._gather_facts(await state.get_prospect(p.id))
    postings = [f for f in facts if "Public job posting" in f]
    assert len(postings) == 1 and 'hiring "Vice President of Technology"' in postings[0]
    assert not any("Apply Here" in f for f in facts)

    await writer._draft_assistant_sequence(await state.get_prospect(p.id))
    assert "Public job posting" in brain.prompts[0] and "using the title exactly as written" in brain.prompts[0]


@pytest.mark.asyncio
async def test_redraft_replaces_only_step_one_and_asks_for_approval_again(state, persona_dir):
    p = await prospect(state)
    ids = []
    for n in (1, 2, 3):
        ids.append(await state.add_outbox_item(
            prospect_id=p.id, to_email=p.email, subject=f"old {n}", body=f"old body {n}",
            send_at=datetime.utcnow().isoformat(), status="approved", campaign_id="camp", step=n))
    writer, brain = writer_for(state, [GOOD_SEQ])
    result = await writer.redraft_first_emails([p.id])

    assert "step 1 rewritten" in result[p.id]
    one, two, three = [await state.get_outbox_item(i) for i in ids]
    assert one["status"] == "pending_review" and one["subject"] == "ai access ownership"
    assert (two["status"], two["body"]) == ("approved", "old body 2")
    assert (three["status"], three["subject"]) == ("approved", "old 3")


@pytest.mark.asyncio
async def test_redraft_never_touches_an_email_already_sent(state, persona_dir):
    p = await prospect(state)
    item = await state.add_outbox_item(
        prospect_id=p.id, to_email=p.email, subject="sent one", body="b",
        send_at=datetime.utcnow().isoformat(), status="approved", campaign_id="camp", step=1)
    await state.update_outbox_item(item, status="sent")
    writer, brain = writer_for(state, [GOOD_SEQ])
    result = await writer.redraft_first_emails([p.id])
    assert "already sent" in result[p.id] and brain.prompts == []
    assert (await state.get_outbox_item(item))["subject"] == "sent one"


@pytest.mark.asyncio
async def test_redraft_keeps_the_old_email_when_the_new_one_breaks_the_rules(state, persona_dir):
    p = await prospect(state)
    item = await state.add_outbox_item(
        prospect_id=p.id, to_email=p.email, subject="old one", body="old",
        send_at=datetime.utcnow().isoformat(), status="approved", campaign_id="camp", step=1)
    bad = seq(e1_body="Jordan cut review time by 40%! Worth a chat?")
    writer, _ = writer_for(state, [bad, bad])
    result = await writer.redraft_first_emails([p.id])
    assert "kept the old one" in result[p.id]
    kept = await state.get_outbox_item(item)
    assert (kept["status"], kept["subject"]) == ("approved", "old one")


def test_shouting_job_titles_are_said_like_a_person_would():
    from harvey.agents.writer import tidy_title
    assert tidy_title("DIRECTOR, HEAD OF AI ENGINEERING & AUTOMATION") == "Director, Head of AI Engineering & Automation"
    assert tidy_title("Vice President of Technology") == "Vice President of Technology"
    assert tidy_title("SVP, DATA & AI STRATEGY") == "SVP, Data & AI Strategy"


@pytest.mark.asyncio
@pytest.mark.parametrize("body, fragment", [
    ("Acme is hiring a CTO. Roles like that usually mean the founders carry the tech. Who owns it today?", "guesses"),
    ("Acme is hiring a CTO. Filling a seat takes months. Who owns it today?", "guesses"),
    ("Teams adopting AI lack an owner. Who owns it today?", "must say the company is hiring"),
])
async def test_with_a_posting_email_one_states_it_and_does_not_guess(state, persona_dir, body, fragment):
    writer, _ = writer_for(state, [])
    allowed = '- Public job posting: Acme is hiring "CTO" (posted on a public job board: https://x)'
    problems = writer._body_problems(body, 1, allowed)
    assert any(fragment in p for p in problems), problems
    good = writer._body_problems("Acme is hiring a CTO. Who owns technical direction there today?", 1, allowed)
    assert good == []


# ── follow-ups under new positioning: email 1 already sent ──

def _followup_rows(state, p, statuses=("approved", "approved")):
    async def go():
        ids = []
        first = await state.add_outbox_item(
            prospect_id=p.id, to_email=p.email, subject="sent one",
            body="Hey Jane,\n\nAcme is hiring a Head of AI. Who owns that work today?\n\nHarvey",
            send_at=datetime.utcnow().isoformat(), status="approved", campaign_id="camp", step=1)
        await state.update_outbox_item(first, status="sent")
        for n, st in zip((2, 3), statuses):
            ids.append(await state.add_outbox_item(
                prospect_id=p.id, to_email=p.email, subject=f"old {n}", body=f"old body {n}",
                send_at=datetime.utcnow().isoformat(), status=st, campaign_id="camp", step=n))
        return first, ids
    return go()


@pytest.mark.asyncio
async def test_followups_are_rewritten_as_a_continuation_and_need_fresh_approval(state, persona_dir):
    p = await prospect(state)
    first, (two_id, three_id) = await _followup_rows(state, p)
    writer, brain = writer_for(state, [{"emails": GOOD_SEQ["emails"][1:]}])
    result = await writer.redraft_followups([p.id])

    assert "emails 2 and 3 rewritten" in result[p.id]
    assert "ALREADY SENT" in brain.prompts[0] and "sent one" in brain.prompts[0]
    one, two, three = [await state.get_outbox_item(i) for i in (first, two_id, three_id)]
    assert one["status"] == "sent" and one["subject"] == "sent one"          # email 1 untouched
    assert (two["status"], two["subject"]) == ("pending_review", EMAIL_2["subject"])
    assert (three["status"], three["subject"]) == ("pending_review", EMAIL_3["subject"])
    assert two["body"].startswith("Hey Jane,") and "Thanks," in two["body"]    # composed in code as step 2


@pytest.mark.asyncio
async def test_followups_keep_the_old_emails_when_the_new_ones_break_the_rules(state, persona_dir):
    p = await prospect(state)
    _, (two_id, three_id) = await _followup_rows(state, p)
    bad = {"emails": [dict(EMAIL_2, body="Jordan cut review time by 40%! Worth a chat?"), EMAIL_3]}
    writer, _ = writer_for(state, [bad, bad])
    result = await writer.redraft_followups([p.id])
    assert "kept the old ones" in result[p.id]
    assert (await state.get_outbox_item(two_id))["status"] == "approved"
    assert (await state.get_outbox_item(three_id))["subject"] == "old 3"


@pytest.mark.asyncio
async def test_followups_refuse_when_email_one_has_not_gone_out(state, persona_dir):
    p = await prospect(state)
    await state.add_outbox_item(
        prospect_id=p.id, to_email=p.email, subject="unsent", body="b",
        send_at=datetime.utcnow().isoformat(), status="approved", campaign_id="camp", step=1)
    writer, brain = writer_for(state, [])
    result = await writer.redraft_followups([p.id])
    assert "not sent yet" in result[p.id] and brain.prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "Example Mutual Insurance is hiring a Chief AI Officer. Who owns that work today?",
    "Bell Freight Systems has an open role for a Head of AI. Who owns it today?",
    "The seat is open right now. Who owns the work?",
    "Cedar Health is currently hiring a Chief Financial Officer. Who owns that?",
])
async def test_an_invented_opening_is_rejected_when_facts_have_no_posting(state, persona_dir, body):
    writer, _ = writer_for(state, [])
    problems = writer._body_problems(body, 1, "- Name: Jane Doe\n- Title: COO")
    assert any("no job posting" in p for p in problems), problems


@pytest.mark.asyncio
async def test_a_real_posting_still_allows_the_hiring_claim(state, persona_dir):
    writer, _ = writer_for(state, [])
    allowed = '- Public job posting: Acme is hiring "Head of AI" (posted on a public job board: https://x)'
    assert writer._body_problems("Acme is hiring a Head of AI. Who owns that work today?", 1, allowed) == []
