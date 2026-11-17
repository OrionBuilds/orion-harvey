"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.

Assistant mode (persona.mode: assistant) drops the shared template entirely:
one call drafts all three emails for one person, each checked against the
persona rules, and all three are staged on a day 0 / 3 / 7 schedule.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from harvey import owner
from harvey.brain import Brain
from harvey.config import HarveyConfig
from harvey.gate import deliverable_statuses
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.models.campaign import Campaign, EmailStep
from harvey.persona import compose_email, is_assistant, load_persona_context
from harvey.persona_guard import draft_problems, violations
from harvey.state import StateManager

logger = logging.getLogger("harvey.writer")

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50

# Assistant persona: body word limits per step, and the only link ever allowed.
STEP_WORD_LIMITS = {1: 75, 2: 75, 3: 40}
BOOKING_LINK = owner.BOOKING_URL
MAX_DRAFT_ATTEMPTS = 2
# Days between assistant-mode emails: day 0, day 3, day 7.
ASSISTANT_DELAYS = (0, 3, 4)
# A prospect whose drafts keep failing the rules is given up on, not retried forever.
MAX_DRAFT_CYCLES = 3

# Email 1 only asks whether the problem is real. These catch the pitch creeping in.
_EMAIL1_ASK = re.compile(
    r"\b(?:short|quick|brief)\s+(?:conversation|call|chat)\b|\b(?:conversation|call|chat|meeting)s?\s+with\b|\bbook(?:ing)?\b",
    re.I)
_OWNER = re.compile(rf"\b{re.escape(owner.FIRST_CAP)}\b")


# Generalisations about why companies hire or how searches go are not facts we hold.
_HIRING_GUESS = re.compile(
    r"\b(usually|typically|tend(s)? to|often|always|everywhere|most companies|months?|"
    r"while the search|search runs|outgrown|inherit|created when|means that|suggests?)\b", re.I)
# Claims that the COMPANY is hiring or has an open seat. Only allowed when FACTS carry a public job posting;
# otherwise it is an invention (local models in the 2026-11-05 bake-off did this for prospects with no posting).
_INVENTED_OPENING = re.compile(
    r"\b(is|are)\s+(currently\s+|now\s+|actively\s+)?(hiring|recruiting|looking for|searching for|seeking)\b"
    r"|\b(has|have)\s+an?\s+(open|vacant)\b|\bopen (role|seat|position|opening)\b|\b(role|seat|position) is (open|vacant)\b"
    r"|\b(posted|advertised)\s+(a|an|the)\s+(role|job|position|opening)\b|\bwhile the role (is|sits) (open|vacant)\b", re.I)
_ROLE_MENTION = re.compile(r"\b(hiring|open role|role (is )?open|role open|opening|posted|posting)\b", re.I)

_KEEP_UPPER = {"AI", "ML", "IT", "VP", "SVP", "EVP", "AVP", "CTO", "CIO", "COO", "CEO", "CDO", "CFO",
               "CISO", "HR", "QA", "SRE", "API", "GRC", "BI", "US", "II", "III", "IV"}
_SMALL = {"of", "and", "the", "for", "to", "in", "a", "an", "at", "on", "&"}


def tidy_title(title: str) -> str:
    """Job boards shout ('DIRECTOR, HEAD OF AI ENGINEERING'). Say it the way a person would."""
    if not title.isupper():
        return title
    words = []
    for i, token in enumerate(title.split(" ")):
        core = token.strip(",.;:()")
        if core.upper() in _KEEP_UPPER:
            words.append(token)
        elif core.lower() in _SMALL and i > 0:
            words.append(token.lower())
        else:
            words.append(token.capitalize())
    return " ".join(words)


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    @property
    def assistant(self) -> bool:
        return is_assistant(self.config)

    def _head(self) -> str:
        """The system prompt, filled in, plus reference skills and (assistant
        mode) the persona files."""
        prompt = self.brain.load_prompt(
            "writer_assistant" if self.assistant else "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
            owner_name=owner.FIRST,
            owner_full_name=owner.NAME,
            booking_url=owner.BOOKING_URL,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills
        if self.assistant and getattr(self, "persona_text", ""):
            prompt += ("\n\n---\n## THE OWNER'S PERSONA FILES (approved facts and rules; "
                       "these win over any framework above)\n\n" + self.persona_text)
        return prompt

    # ── Assistant-mode drafting: check, retry once, compose ──

    @staticmethod
    def _subject_problems(subject: str) -> list[str]:
        found = []
        if subject != subject.lower():
            found.append("subject must be lowercase")
        n = len(subject.split())
        if not 2 <= n <= 5:
            found.append(f"subject must be 2 to 5 words (got {n})")
        if "?" in subject or "!" in subject:
            found.append("no punctuation marks in the subject")
        found += violations(subject)
        return found

    def _body_problems(self, body: str, step: int, allowed_text: str) -> list[str]:
        found = draft_problems(body, allowed_text=allowed_text)
        if step == 1 and "Public job posting" in allowed_text:
            # The posting is the only thing we know. Why they are hiring, how long a
            # search takes, what such roles "usually" mean: that is guessing.
            guess = _HIRING_GUESS.search(body)
            if guess:
                found.append(f'email 1 guesses about hiring or the role ("{guess.group(0)}"): state only that '
                             "the role is open, then ask who owns that work today")
            if not _ROLE_MENTION.search(body):
                found.append("email 1 must say the company is hiring the role from the job posting")
        if "Public job posting" not in allowed_text and _INVENTED_OPENING.search(body):
            found.append("claims the company is hiring or has an open role, but FACTS contain no job posting: "
                         "that is an invention. Open with a general observation about their role or industry instead")
        limit = STEP_WORD_LIMITS.get(step, 75)
        if len(body.split()) > limit:
            found.append(f"email {step} body must be under {limit} words (got {len(body.split())})")
        questions = body.count("?")
        has_link = "http" in body
        if step == 1:
            if questions != 1:
                found.append("email 1 needs exactly one question")
            if _OWNER.search(body) or _EMAIL1_ASK.search(body):
                found.append(f"email 1 must not mention {owner.FIRST}, offer a call or conversation, or carry proof; "
                             "it is one observation and one question")
            if has_link:
                found.append("email 1 must contain no link")
        elif step == 2:
            if questions:
                found.append("email 2 must not ask a question; the booking link is its ask")
            if BOOKING_LINK not in body:
                found.append(f"email 2 must contain the booking link {BOOKING_LINK}")
            elif re.sub(re.escape(BOOKING_LINK), "", body).count("http"):
                found.append(f"the only link allowed is {BOOKING_LINK}")
            if not _OWNER.search(body):
                found.append(f"email 2 must name {owner.FIRST} (the proof line and the conversation are about them)")
        else:
            if questions:
                found.append("the breakup email must not ask a question")
            if has_link:
                found.append(f"email {step} must contain no link")
        return found

    @staticmethod
    def _repeated_sentences(bodies: list[str]) -> list[str]:
        """Sentences (over five words) that appear in more than one email."""
        seen: dict[str, int] = {}
        for i, body in enumerate(bodies):
            for sentence in {re.sub(r"\W+", " ", x).strip().lower() for x in re.split(r"[.?\n]", body)}:
                if len(sentence.split()) > 5:
                    seen.setdefault(sentence, 0)
                    seen[sentence] += 1
        return [x for x, n in seen.items() if n > 1]

    def _allowed_text(self, facts_text: str) -> str:
        return facts_text + "\n" + getattr(self, "persona_text", "")

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")

        # The assistant persona's rules live in its files. Drafting without them
        # would mean drafting without the rules, so refuse rather than improvise.
        self.persona_text = ""
        if is_assistant(self.config):
            self.persona_text, missing = load_persona_context()
            if missing:
                logger.error(
                    "Writer: persona.mode is 'assistant' but these persona files are "
                    f"missing: {missing}. Not writing anything until they exist."
                )
                return
            if not self.is_native:
                logger.error(
                    "Writer: persona.mode is 'assistant' but the mail provider is not native. "
                    "Assistant mode writes every email per prospect, which a shared-template "
                    "provider cannot send. Not writing anything."
                )
                return

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        deliverable = set(deliverable_statuses(self.config))
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in deliverable
        ]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # Batch prospects into campaign groups (by industry/title for relevance)
        batches = self._group_prospects(prospects_with_email)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            if self.assistant:
                await self._write_assistant_campaign(batch_name, prospects)
                continue

            # Generate the email sequence
            sequence = await self._write_sequence(prospects)
            if not sequence:
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.update_prospect_status(p.id, "queued")

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                await self._personalize_first_emails(campaign_id, prospects)

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    async def _personalize_first_emails(self, campaign_id: str, prospects: list):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        drafted = 0
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
            if not draft:
                continue
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=draft["body"],
                send_at=now,
                status=status,
                provider=provider,
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def _gather_facts(self, prospect) -> list[str]:
        """Everything known about one person. The only things a draft may claim."""
        facts = [
            f"- Name: {prospect.full_name()}",
            f"- Title: {prospect.title}",
            f"- Company: {prospect.company}",
        ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        if company:
            if company.description:
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        facts.extend(await self._posting_facts(prospect))
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")
        return facts

    async def _posting_facts(self, prospect) -> list[str]:
        """Open roles the company has posted on a public job board. These came from
        Discovery (the job-board source) with the posting's own link as evidence, so
        they are the one signal an email may state outright. Scraped 'Apply Here'
        style fragments from careers pages are not postings and are skipped."""
        if not prospect.company_id:
            return []
        try:
            rows = await self.state.get_observations(
                company_id=prospect.company_id, signal_code="HIRING_ROLE", limit=20)
        except Exception:
            return []
        seen, facts = set(), []
        for row in rows:
            title = tidy_title((row.get("value_text") or "").strip())
            url = (row.get("evidence_url") or "").strip()
            if row.get("collector") != "discover" or not url.startswith("http") or len(title) < 8:
                continue
            if title.lower() in seen:
                continue
            seen.add(title.lower())
            facts.append(f'- Public job posting: {prospect.company} is hiring "{title}" '
                         f"(posted on a public job board: {url})")
        return facts[:2]

    async def _write_personal_email(self, prospect) -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        facts = await self._gather_facts(prospect)

        prompt = self._head()

        prompt += f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS — everything you know about them. Every claim about the prospect in
your email must come from these facts. If a fact isn't listed here, you
don't know it — do not invent funding rounds, mutual contacts, metrics,
or anything else:
{chr(10).join(facts)}

Requirements:
- Under 75 words. One specific observation from the FACTS above, one
  question. No pitch, no product name.
- Write the actual text (no merge variables — you know their name/company).
- Subject: lowercase, 2-5 words.
- Follow every STRICT EMAIL RULE above.

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000]}

    # ── Assistant mode: all three emails per person, checked, retried once ──

    async def _write_assistant_campaign(self, batch_name: str, prospects: list):
        """Draft the whole sequence for each person and stage it in the outbox.

        There is no shared template in this mode, so a person whose draft fails
        the rules gets no email at all. They are retried on later cycles, then
        given up on."""
        require_approval = getattr(self.config.channels.email, "require_approval", True)
        status = "pending_review" if require_approval else "approved"
        provider = self.config.channels.email.provider
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        drafted: list[tuple] = []
        for prospect in prospects:
            try:
                emails = await self._draft_assistant_sequence(prospect)
            except Exception as e:
                logger.warning(f"Writer: draft failed for {prospect.email}: {e}")
                emails = None
            if emails:
                drafted.append((prospect, emails))
            else:
                await self._note_draft_failure(prospect)
        if not drafted:
            logger.warning(f"Writer: no drafts passed the rules for '{batch_name}'.")
            return

        # Active from the start: the Sender stages only 'draft' campaigns from their
        # template, and this campaign has none. The stored steps are structure only.
        campaign = Campaign(
            id="", name=batch_name, channel="email", status="active",
            sequence=[EmailStep(step=i + 1, subject="(written per prospect)",
                                body="(written per prospect, see the outbox)", delay_days=d)
                      for i, d in enumerate(ASSISTANT_DELAYS)],
            prospect_ids=[p.id for p, _ in drafted],
        )
        campaign_id = await self.state.add_campaign(campaign)

        staged = 0
        for prospect, emails in drafted:
            days = 0
            for i, email in enumerate(emails):
                days += ASSISTANT_DELAYS[i]
                item_id = await self.state.add_outbox_item(
                    prospect_id=prospect.id, campaign_id=campaign_id, step=i + 1,
                    to_email=prospect.email, subject=email["subject"], body=email["body"],
                    send_at=(now + timedelta(days=days)).isoformat(),
                    status=status, provider=provider,
                )
                staged += 1 if item_id else 0
            await self.state.update_prospect_status(prospect.id, "queued")

        await self.state.log_action(
            action_type="write_campaign", agent="writer",
            details={"campaign_id": campaign_id, "campaign_name": batch_name,
                     "prospect_count": len(drafted), "emails": staged, "mode": "per_prospect"},
        )
        logger.info(
            f"Writer: '{batch_name}': {staged} email(s) drafted for {len(drafted)} "
            f"prospect(s) ({'awaiting approval' if require_approval else 'approved'})."
        )

    async def _note_draft_failure(self, prospect):
        key = f"writer_draft_failures:{prospect.id}"
        try:
            count = int(await self.state.get_setting(key, "0") or 0) + 1
        except ValueError:
            count = 1
        await self.state.set_setting(key, str(count))
        if count >= MAX_DRAFT_CYCLES:
            await self.state.update_prospect_status(prospect.id, "lost")
            await self.state.log_action(
                action_type="writer_gave_up", agent="writer",
                details={"prospect_id": prospect.id, "email": prospect.email, "failures": count},
            )
            logger.warning(f"Writer: gave up on {prospect.email} after {count} failed cycles.")

    async def redraft_first_emails(self, prospect_ids: list[str]) -> dict[str, str]:
        """Rewrite step 1 for people who have not been emailed yet, using everything
        known now (e.g. an open-role posting added after the first draft). Steps 2 and
        3 are left exactly as they are, including any approval. A rewritten step 1
        goes back to pending_review: the owner approved the OLD words, not these.
        Returns {prospect_id: outcome}."""
        self.persona_text = ""
        if is_assistant(self.config):
            self.persona_text, missing = load_persona_context()
            if missing:
                return {pid: f"persona files missing: {missing}" for pid in prospect_ids}
        self.skills = self.brain.load_skills_for_agent("writer")
        out: dict[str, str] = {}
        for pid in prospect_ids:
            prospect = await self.state.get_prospect(pid)
            if prospect is None:
                out[pid] = "unknown prospect"
                continue
            first = None
            async with self.state._connect() as db:
                async with db.execute(
                        "SELECT id, status FROM outbox WHERE prospect_id = ? AND step = 1 "
                        "AND kind = 'sequence' AND channel = 'email'", (pid,)) as cur:
                    first = await cur.fetchone()
            if first is None:
                out[pid] = "no step-1 email staged"
                continue
            if first[1] == "sent":
                out[pid] = "already sent; left alone"
                continue
            drafted = await self._draft_assistant_sequence(prospect)
            if not drafted:
                out[pid] = "draft failed the rules; kept the old one"
                continue
            await self.state.update_outbox_item(
                first[0], subject=drafted[0]["subject"], body=drafted[0]["body"], status="pending_review")
            out[pid] = f"step 1 rewritten: {drafted[0]['subject']}"
        return out

    async def redraft_followups(self, prospect_ids: list[str]) -> dict[str, str]:
        """Rewrite emails 2 and 3 for people whose email 1 has ALREADY been sent, under the
        current positioning, as a continuation of what they received. Email 1 is never
        touched. The rewritten emails go back to pending_review (the owner approved the OLD
        words, not these) and keep their scheduled times. Returns {prospect_id: outcome}."""
        self.persona_text = ""
        if is_assistant(self.config):
            self.persona_text, missing = load_persona_context()
            if missing:
                return {pid: f"persona files missing: {missing}" for pid in prospect_ids}
        self.skills = self.brain.load_skills_for_agent("writer")
        out: dict[str, str] = {}
        for pid in prospect_ids:
            prospect = await self.state.get_prospect(pid)
            if prospect is None:
                out[pid] = "unknown prospect"
                continue
            rows: dict[int, tuple] = {}
            async with self.state._connect() as db:
                async with db.execute(
                        "SELECT step, id, status, subject, body FROM outbox WHERE prospect_id = ? "
                        "AND kind = 'sequence' AND channel = 'email' AND step IN (1, 2, 3)", (pid,)) as cur:
                    for r in await cur.fetchall():
                        rows[r[0]] = tuple(r)
            first = rows.get(1)
            if first is None or first[2] != "sent":
                out[pid] = "email 1 not sent yet; use redraft_first_emails"
                continue
            todo = [n for n in (2, 3) if n in rows and rows[n][2] in ("approved", "pending_review")]
            if len(todo) != 2:
                out[pid] = "emails 2 and 3 are not both waiting; left alone"
                continue
            drafted = await self._draft_assistant_sequence(
                prospect, sent_first={"subject": first[3], "body": first[4]})
            if not drafted:
                out[pid] = "draft failed the rules; kept the old ones"
                continue
            for n, email in zip((2, 3), drafted):
                await self.state.update_outbox_item(
                    rows[n][1], subject=email["subject"], body=email["body"], status="pending_review")
            out[pid] = f"emails 2 and 3 rewritten: {drafted[0]['subject']} / {drafted[1]['subject']}"
        return out

    async def _draft_assistant_sequence(self, prospect, sent_first: dict | None = None) -> list[dict] | None:
        """One call, three emails for one person, as Harvey. Every email must pass
        the rules; one retry with the reasons appended; anything still failing is
        dropped, not sent. Greeting and sign-off are composed in code.

        With sent_first ({"subject", "body"}) email 1 has already gone out, so only
        emails 2 and 3 are written, as a continuation of it, and returned in that order."""
        facts = "\n".join(await self._gather_facts(prospect))
        if sent_first:
            lead = (f"""Email 1 was ALREADY SENT to this person, so write ONLY emails 2 and 3, as a
continuation of it. Subject and body of the email already sent:
SUBJECT: {sent_first["subject"]}
{sent_first["body"]}

Do not repeat its observation, its question or any of its sentences. Return exactly two
emails, labelled step 2 and step 3.
""")
        else:
            lead = "Write the three-email sequence (day 0, day 3, day 7) for this ONE person.\n"
        prompt = self._head() + f"""

{lead}

FACTS: everything you know about them. Every claim about the prospect must come
from these facts. If a fact is not listed, you do not know it. Do not invent
funding rounds, mutual contacts, metrics or anything else:
{facts}

Requirements:
- Email 1: under 75 words. One observation from the FACTS, then one question asking
  whether that problem is real for them. No proof, no mention of {owner.FIRST}, no offer of a
  call, no link.
  If the FACTS include a "Public job posting", build the observation on it: say the
  company is hiring that role, using the title exactly as written, without quotation
  marks. State only that the role is open. Do not guess why they are hiring, how long
  a search takes, what such roles usually mean, what is going wrong, or what they have
  decided. No words like usually, often, tends to, typically. Do not include the
  posting's link. Then ask ONE plain question about who owns that work today.
- Email 2: under 75 words. A different angle. One proof sentence about {owner.FIRST}'s work,
  from the story whose "Best fit" matches this person (use an exact count from the
  story's Email line where it has one), and you may say {owner.FIRST} starts every engagement
  with a written assessment they review personally. Then a short conversation with {owner.FIRST},
  ending with {BOOKING_LINK} on its own line. No question mark.
- Email 3: under 40 words. Breakup. No proof, no question, no link.
- Write the actual text. No merge variables, no greeting, no sign-off.
- Each subject: lowercase, 2 to 5 words, different from the others.
- Follow every STRICT EMAIL RULE above.

Return ONLY JSON: {{"emails": [{{"step": 1, "subject": "...", "body": "..."}}, {{"step": 2, "subject": "...", "body": "..."}}, {{"step": 3, "subject": "...", "body": "..."}}]}}"""

        allowed = self._allowed_text(facts)
        note = ""
        for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
            steps = self._parse_sequence(await self.brain.think_json(
                prompt + note, session_id="harvey-writer",
                agent="writer", task="personal_sequence"))
            want = 2 if sent_first else 3
            if sent_first:                      # the parser numbers from 1; these are emails 2 and 3
                for st in steps:
                    st.step += 1
            problems: list[str] = []
            if len(steps) != want:
                problems.append(f"need exactly {want} emails (got {len(steps)})")
            for step in steps:
                problems += [f"email {step.step}: {p}" for p in
                             self._subject_problems(step.subject)
                             + self._body_problems(step.body, step.step, allowed)]
            if len(steps) == want:
                subjects = [st.subject for st in steps] + ([sent_first["subject"]] if sent_first else [])
                if len(set(subjects)) < len(subjects):
                    problems.append("the three subjects must differ" if not sent_first else "the subjects must differ from each other and from email 1")
                bodies = [st.body for st in steps]
                if sent_first:
                    bodies.append(sent_first["body"])
                problems += [f"a sentence repeats across emails: {x!r}"
                             for x in self._repeated_sentences(bodies)]
            if not problems:
                return [
                    {"subject": st.subject,
                     "body": compose_email(st.body, first_name=prospect.first_name, step=st.step,
                                           last_step=3, persona=self.config.persona)}
                    for st in steps
                ]
            logger.warning(f"Writer: sequence for {prospect.email} rejected (attempt {attempt}): {problems}")
            note = ("\n\nYour last reply broke these rules, so it was rejected:\n- "
                    + "\n- ".join(problems) + "\nWrite the emails again, fixing every one.")
        return None

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )

        prompt = self._head()

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{prospect_summary}

Requirements:
- Email 1: Personalized cold observation + one question. Under 75 words. No pitch.
- Email 2: Follow-up 3 days later. Different angle, one concrete proof point. Under 75 words.
- Email 3: Break-up email 4 days after that. Under 40 words. Gracious, not guilt-tripping.
- Use {{{{first_name}}}}, {{{{company}}}}, {{{{title}}}} as merge variables — every email
  must use at least one, and email 1 must reference something specific to
  these prospects' industry or role (use the notes above).
- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-5 words.
- Follow every rule in the STRICT EMAIL RULES above. No exceptions.

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="write_sequence",
        )
        return self._parse_sequence(result)

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = prospect.industry or "general"
            key = f"{industry}-outreach"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}
