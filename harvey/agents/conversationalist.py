"""Conversationalist: Harvey's replies once a prospect answers (assistant mode).

The owner's rules split the work. Code decides, before any model runs, the replies
that must never be improvised (hand-offs, "are you an AI?", "how much?"), using
fixed wording. The model writes only ordinary replies, from the owner's persona files,
and every one of its drafts is checked against the same hard rules the send gates
use. A draft that still breaks them after one retry is not sent: the thread goes
to the owner instead.

Returns the message BODY only. Greeting and sign-off are composed by the caller.
"""

import logging
import re
from dataclasses import dataclass

from harvey import conversation_rules as rules
from harvey.persona import CONVERSATION_FILES, load_conversation_prompt, load_persona_context
from harvey.persona_guard import reply_problems

logger = logging.getLogger("harvey.conversationalist")

MAX_ATTEMPTS = 2
# A thread this long is no longer a first conversation; the owner should take it.
MAX_HARVEY_TURNS = 8
OFF_TOPIC_LIMIT = 6
THREAD_MESSAGES = 12
THREAD_CHARS = 6000


@dataclass
class Reply:
    text: str                 # the body to send; "" means send nothing
    handoff: bool = False     # the thread now belongs to the owner
    reason: str = ""
    fixed: bool = False       # fixed wording, no model involved


class Conversationalist:
    def __init__(self, brain, state, config):
        self.brain = brain
        self.state = state
        self.config = config

    # ── routing ──

    async def respond(self, reply_text: str, prospect, convo, channel: str) -> Reply:
        route = rules.route(reply_text)
        if route and route.kind == "escalate":
            return Reply(rules.ESCALATION_LINE, handoff=True, reason=route.reason, fixed=True)

        harvey_turns = sum(1 for m in convo.thread if m.sender == "harvey")
        if harvey_turns >= MAX_HARVEY_TURNS:
            return Reply(rules.ESCALATION_LINE, handoff=True, reason="long thread", fixed=True)

        off_topic = await self._off_topic_turns(prospect, channel)
        if off_topic >= OFF_TOPIC_LIMIT:
            return Reply(rules.ESCALATION_LINE, handoff=True, reason="kept going off topic", fixed=True)

        if route and route.kind == "ai_question":
            return Reply(rules.AI_DISCLOSURE, reason=route.reason, fixed=True)
        if route and route.kind == "identity_challenge":
            return Reply(rules.IDENTITY_LINE, reason=route.reason, fixed=True)
        if route and route.kind == "contact_request":
            return Reply(rules.CONTACT_LINE, reason=route.reason, fixed=True)
        if route and route.kind == "price_question":
            line = rules.PRICE_LINE_SHORT if channel == "linkedin" else rules.PRICE_LINE
            return Reply(line, reason=route.reason, fixed=True)

        return await self._draft(prospect, convo, channel, off_topic)

    # ── the model-written path ──

    @staticmethod
    def _off_topic_key(prospect, channel: str) -> str:
        # Keyed by person and channel: a brand-new conversation has no id yet.
        return f"convo_off_topic:{prospect.id}:{channel}"

    async def _off_topic_turns(self, prospect, channel: str) -> int:
        try:
            return int(await self.state.get_setting(self._off_topic_key(prospect, channel), "0") or 0)
        except ValueError:
            return 0

    async def _history(self, prospect, convo) -> str:
        """What was said, oldest first: Harvey's earlier outreach, then the thread."""
        lines: list[str] = []
        try:
            for row in await self.state.get_sent_for_prospect(prospect.id):
                if row["kind"] == "sequence":
                    lines.append(f"Harvey (earlier outreach, {row['channel']}): {row['body'].strip()}")
        except Exception:
            pass
        for m in convo.thread[-THREAD_MESSAGES:]:
            who = "Harvey" if m.sender == "harvey" else prospect.full_name()
            lines.append(f"{who}: {m.content.strip()}")
        text = "\n\n".join(lines)
        return text[-THREAD_CHARS:]

    def _prompt(self, prospect, convo, channel: str, off_topic: int, thread: str) -> tuple[str, str] | None:
        template = load_conversation_prompt()
        persona_text, missing = load_persona_context(CONVERSATION_FILES)
        if not template or missing:
            logger.error(f"Conversationalist: persona files missing ({missing or 'conversation_prompt'}); "
                         "not drafting.")
            return None
        values = {
            "prospect_name": prospect.full_name(), "prospect_title": prospect.title,
            "company": prospect.company, "channel": channel, "stage": convo.stage,
            "persona_files": persona_text, "thread": thread, "off_topic_turns": str(off_topic),
        }
        prompt = re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)
        return prompt, persona_text

    async def _draft(self, prospect, convo, channel: str, off_topic: int) -> Reply:
        thread = await self._history(prospect, convo)
        built = self._prompt(prospect, convo, channel, off_topic, thread)
        if built is None:
            return Reply("", handoff=True, reason="persona files missing")
        prompt, persona_text = built
        allowed = persona_text + "\n" + thread

        note = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = await self.brain.think_json(
                prompt + note, session_id="harvey-conversation",
                agent="conversationalist", task="reply")
            if not isinstance(result, dict):
                note = "\n\nYour last reply was not valid JSON. Return ONLY the JSON object."
                continue
            text = str(result.get("reply") or "").strip()
            if result.get("handoff") is True:
                return Reply(rules.ESCALATION_LINE, handoff=True, reason="the drafter was unsure", fixed=True)
            problems = reply_problems(text, channel=channel, allowed_text=allowed) if text else ["empty reply"]
            if not problems:
                if result.get("off_topic") is True:
                    await self.state.set_setting(self._off_topic_key(prospect, channel), str(off_topic + 1))
                return Reply(text)
            logger.warning(f"Conversationalist: draft for {prospect.email or prospect.full_name()} "
                           f"rejected (attempt {attempt}): {problems}")
            note = ("\n\nYour last reply broke these rules, so it was rejected:\n- "
                    + "\n- ".join(problems) + "\nWrite a new reply that fixes every one.")
        return Reply("", handoff=True, reason="no draft passed the rules")
