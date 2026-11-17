"""Handler — monitors replies and manages conversations.

Works against Instantly (legacy) or a native mail provider (Gmail/SMTP).
The native path also owns bounce handling: a bounce marks the address
invalid, cancels the prospect's queued sends, and — past a bounce-rate
threshold — flips the global kill switch so a bad list can't torch the
sending domain while nobody's watching.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from harvey import compliance, conversation_rules
from harvey.bounce_policy import record_policy_block
from harvey.agents.conversationalist import Conversationalist
from harvey.brain import Brain, BrainUnavailable
from harvey.channels.email import EmailChannel
from harvey.channels.linkedin import LinkedInChannel
from harvey.channels.sms import SmsChannel, keyword as sms_keyword
from harvey.config import HarveyConfig, EnvConfig
from harvey import linkedin_ops
from harvey.optout import suppress_person
from harvey.persona import compose_email, is_assistant
from harvey.integrations.instantly import InstantlyClient
from harvey.integrations.mail_provider import NATIVE_PROVIDERS, classify_bounce, get_mail_provider
from harvey.models.conversation import Conversation, Message
from harvey.state import StateManager

EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

logger = logging.getLogger("harvey.handler")

CHANNEL_POLL_DEADLINE_SECONDS = 300


class ChannelReadTimeout(Exception):
    """A channel's inbox read did not finish in time."""

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"
# Kill switch only engages after this many sends (a tiny sample lies).
MIN_SENDS_FOR_KILL_SWITCH = 10

# "Not interested" from one person cools the whole company for this long.
COMPANY_COOLDOWN_DAYS = 180

INTENT_LABELS = {
    "interested",
    "objection",
    "not_interested",
    "ooo",
    "wrong_person",
    "question",
    "unsubscribe",
    "escalate",
}

# Intents that get an automated reply. Everything else is closed,
# deferred, or escalated to a human.
AUTO_REPLY_INTENTS = {"interested", "objection", "question", "wrong_person"}

# Hard opt-out phrases. If any of these appear, we ALWAYS honor the
# opt-out regardless of what the LLM classifier says.
OPT_OUT_PATTERNS = (
    "unsubscribe",
    "opt out",
    "opt-out",
    "remove me",
    "take me off",
    "stop emailing",
    "stop contacting",
    "stop sending",
    "do not contact",
    "do not email",
    "don't contact",
    "don't email",
    "never contact",
    "stop messaging",
    "stop texting",
    "leave me alone",
    "delete my info",
    "delete my data",
)

# Phrases that mean a human must take over. Never auto-reply to these.
ESCALATION_PATTERNS = (
    "lawyer",
    "attorney",
    "legal action",
    "lawsuit",
    "cease and desist",
    "harassment",
    "harassing",
    "report you",
    "reporting you",
    "spam complaint",
    "ftc",
    "gdpr",
    "can-spam",
    "blacklist",
)


class Handler:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env
        self.instantly = InstantlyClient(env.instantly_api_key)
        self.provider = get_mail_provider(config, env)
        self.skills = ""
        self.conversationalist = Conversationalist(brain, state, config)
        self.todo = self._build_todo(config, env)

    @property
    def assistant(self) -> bool:
        return is_assistant(self.config)

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def run(self):
        """Check for new replies and handle them.

        Returns True only if every reply could be fetched and handled. The
        heartbeat sends nothing in a cycle where this isn't True, because a
        reply that wasn't read can't have cancelled its queued follow-ups.
        """
        logger.info("Handler: Checking for replies...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("handler")

        if not self.config.channels.email.enabled:
            return True

        if self.is_native:
            return await self._run_native()

        # 1. Fetch active campaigns
        active_campaigns = await self.state.get_campaigns_by_status("active")
        if not active_campaigns:
            logger.info("Handler: No active campaigns to monitor.")
            return True

        total_handled = 0
        complete = True

        for campaign in active_campaigns:
            if not campaign.instantly_campaign_id:
                continue

            # 2. Get replies from Instantly
            try:
                replies = await self.instantly.get_replies(campaign.instantly_campaign_id)
            except Exception as e:
                logger.error(f"Handler: Failed to fetch replies for {campaign.name}: {e}")
                complete = False
                continue
            if not replies:
                continue

            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                try:
                    handled = await self._handle_reply(reply, campaign)
                    if handled:
                        total_handled += 1
                except BrainUnavailable as e:
                    logger.error(f"Handler: stopping; replies are left for later ({e}).")
                    return False
                except Exception as e:
                    logger.error(f"Handler: Error processing reply: {e}")
                    complete = False

        if total_handled:
            logger.info(f"Handler: Processed {total_handled} replies.")
        else:
            logger.info("Handler: No new replies.")
        return complete

    async def _handle_reply(self, reply: dict, campaign) -> bool:
        """Process a single reply. Returns True if a new reply was handled."""
        lead_email = (reply.get("lead_email") or reply.get("from_email") or "").strip().lower()
        reply_text = (reply.get("body") or reply.get("text") or "").strip()
        reply_uuid = str(reply.get("uuid") or reply.get("id") or "")

        if not lead_email or not reply_text:
            return False

        # Dedup: skip if we already processed this reply
        if reply_uuid and await self.state.is_reply_processed(reply_uuid):
            logger.debug(f"Handler: Reply {reply_uuid} already processed. Skipping.")
            return False

        logger.info(f"Handler: Reply from {lead_email}")

        consumed = True
        try:
            await self._process_reply(lead_email, reply_text, reply_uuid, campaign.id)
        except BrainUnavailable:
            # Nothing was decided or recorded about this reply yet, so leave
            # it for the next cycle instead of losing it to a provider outage.
            consumed = False
            raise
        finally:
            # Otherwise ALWAYS mark processed — even on early exits (opt-out,
            # OOO, unknown prospect) — so the same reply is never re-handled
            # or double-replied on the next cycle.
            if reply_uuid and consumed:
                await self.state.mark_reply_processed(reply_uuid)
        return True

    async def _process_reply(
        self,
        lead_email: str,
        reply_text: str,
        reply_uuid: str,
        campaign_id: str = "",
        reply_meta: dict | None = None,
        channel: str = "email",
    ):
        """Classify, record, and respond to a single reply.

        ``lead_email`` is the sender's address on the channel it arrived on
        (email address, LinkedIn profile URL, or phone number).
        """
        prospect = await self._find_prospect(channel, lead_email)
        if not prospect:
            logger.warning(f"Handler: No prospect found for {lead_email} ({channel})")
            return

        # Update prospect status + stop-on-reply: a human answered, so every
        # queued sequence email for them is now wrong to send.
        await self.state.update_prospect_status(prospect.id, "replied")
        if channel == "linkedin":
            await linkedin_ops.mark_replied(self.state, prospect.id)
        try:
            cancelled = await self.state.cancel_pending_outbox_for_prospect(prospect.id)
            if cancelled:
                logger.info(
                    f"Handler: cancelled {cancelled} queued email(s) for "
                    f"{lead_email} (they replied)."
                )
        except Exception as e:
            logger.debug(f"Handler: outbox cancel failed: {e}")

        # 1. Classify intent. Hard keyword checks run FIRST and override
        # the LLM — opt-outs and legal threats must never be missed.
        if self.assistant:
            reply_text = conversation_rules.strip_quoted(reply_text) or reply_text
        text_lower = reply_text.lower()
        opted_out = any(p in text_lower for p in OPT_OUT_PATTERNS)
        route = conversation_rules.route(reply_text) if self.assistant else None
        # Once a thread is the owner's, Harvey stops answering it. (An opt-out is still honored.)
        if self.assistant and not opted_out and await self._append_to_held(prospect, channel, reply_text):
            return
        if opted_out:
            intent = "unsubscribe"
        elif any(p in text_lower for p in ESCALATION_PATTERNS):
            intent = "escalate"
        elif route and route.kind == "escalate":
            intent = "escalate"
        else:
            intent = await self._classify_intent(reply_text, prospect)
        logger.info(f"Handler: Intent for {lead_email}: {intent}")

        # 2. The conversation as it will be, in memory only. Nothing about
        # this reply is stored until every model call it needs has succeeded:
        # if the provider goes down part-way (BrainUnavailable), the reply is
        # left unprocessed and the retry starts from the same stored state,
        # so the thread never gets the message twice or its stage advanced
        # twice. (Stop-on-reply above is idempotent and stays first.)
        existing_convos = await self.state.get_conversations_by_status("open")
        stored = next(
            (c for c in existing_convos
             if c.prospect_id == prospect.id and (c.channel or "email") == channel),
            None
        )
        if stored is not None:
            convo = stored.model_copy(deep=True)
        else:
            convo = Conversation(
                id="",
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                channel=channel,
                intent=intent,
                status="open",
            )
        convo.thread.append(Message(sender="prospect", content=reply_text))
        convo.intent = intent
        old_stage = convo.stage
        convo.stage = self._determine_stage(intent, convo.stage, reply_text)

        # 2b. Draft the reply now, while nothing is stored yet.
        response = ""
        handoff, handoff_reason = False, ""
        if intent in AUTO_REPLY_INTENTS:
            if self.assistant:
                drafted = await self.conversationalist.respond(reply_text, prospect, convo, channel)
                response, handoff, handoff_reason = drafted.text, drafted.handoff, drafted.reason
            else:
                response = await self._generate_response(
                    intent, reply_text, prospect, convo, channel=channel)

        # 2c. Store the conversation in one write.
        if stored is None:
            convo.id = await self.state.add_conversation(convo)
        else:
            await self.state.update_conversation(
                convo.id,
                thread_json=convo.thread_json(),
                intent=intent,
                stage=convo.stage,
            )
        if convo.stage != old_stage:
            logger.info(f"Handler: Stage for {lead_email}: {old_stage} -> {convo.stage}")

        # 3. Route based on intent
        if intent == "unsubscribe":
            # Honor opt-outs ALWAYS. No reply, no future contact.
            await self.state.update_conversation(convo.id, status="closed", stage="closed_lost")
            await self.state.update_prospect_status(prospect.id, "opted_out")
            await self._suppress_person(prospect, reason="unsubscribe")
            await self.state.log_action(
                action_type="opt_out",
                agent="handler",
                details={"prospect_email": lead_email},
            )
            logger.info(f"Handler: {lead_email} opted out. Suppressed permanently. No reply sent.")
            return

        if intent == "escalate":
            # Angry / legal / compliance replies go to a human. Never auto-reply.
            await self.state.update_conversation(convo.id, status="needs_human")
            await self.state.log_action(
                action_type="escalation",
                agent="handler",
                details={
                    "prospect_email": lead_email,
                    "reply_preview": reply_text[:200],
                },
            )
            await self._hand_off_task(
                prospect, channel, route.reason if route else "flagged for a person", reply_text)
            if self.assistant:
                # The owner's rule: ONE brief acknowledgment, then the thread is theirs.
                reason = route.reason if route else "flagged for a person"
                await self._deliver_assistant_reply(
                    conversation_rules.ESCALATION_LINE, prospect, convo, channel,
                    reply_meta or {}, intent)
                logger.warning(f"Handler: ESCALATED {lead_email} ({reason}); acknowledged, thread is the owner's.")
            else:
                logger.warning(
                    f"Handler: ESCALATED reply from {lead_email} — needs human review. No auto-reply sent."
                )
            return

        if intent == "not_interested":
            await self.state.update_conversation(convo.id, status="closed", stage="closed_lost")
            await self.state.update_prospect_status(prospect.id, "lost")
            await self._suppress_company(
                prospect, reason="not_interested", days=COMPANY_COOLDOWN_DAYS)
            logger.info(f"Handler: {lead_email} not interested. Closing. One no is enough.")
            return

        if intent == "ooo":
            logger.info(f"Handler: {lead_email} is OOO. Will follow up later.")
            return

        if intent not in AUTO_REPLY_INTENTS:
            logger.warning(f"Handler: No auto-reply policy for intent '{intent}'. Skipping reply.")
            return

        # For interested, objection, question, wrong_person — the reply drafted above
        if not response:
            # No usable draft from a working provider: hand it to a human
            # rather than leave an answered-looking thread with no reply.
            await self.state.update_conversation(convo.id, status="needs_human")
            await self.state.log_action(
                action_type="escalation",
                agent="handler",
                details={"prospect_email": lead_email, "reason": "no usable reply draft",
                         "reply_preview": reply_text[:200]},
            )
            await self._hand_off_task(prospect, channel, "no usable reply draft", reply_text)
            logger.warning(
                f"Handler: Could not generate a response for {lead_email}. "
                "Flagged for human review."
            )
            return

        # Send the response — natively through the outbox approval ladder,
        # or immediately via Instantly (legacy).
        if self.assistant:
            await self._deliver_assistant_reply(
                response, prospect, convo, channel, reply_meta or {}, intent)
            if handoff:
                await self.state.update_conversation(convo.id, status="needs_human")
                await self.state.log_action(
                    action_type="escalation", agent="handler",
                    details={"prospect_email": lead_email, "reason": handoff_reason,
                             "reply_preview": reply_text[:200]})
                await self._hand_off_task(prospect, channel, handoff_reason or "handed to the owner", reply_text)
                logger.warning(f"Handler: {lead_email} handed to the owner ({handoff_reason}).")
        elif channel == "linkedin":
            await self._queue_linkedin_reply(
                response, prospect, convo, reply_meta or {}, intent)
        elif self.is_native:
            await self._queue_native_reply(
                response, prospect, convo, reply_meta or {}, intent
            )
        elif reply_uuid:
            result = await self.instantly.send_reply(reply_uuid, response)
            if result is not None:
                # Add our response to conversation
                convo.thread.append(Message(sender="harvey", content=response))
                await self.state.update_conversation(
                    convo.id,
                    thread_json=convo.thread_json(),
                )
                logger.info(f"Handler: Replied to {lead_email}")

                await self.state.log_action(
                    action_type="reply",
                    agent="handler",
                    details={
                        "prospect_email": lead_email,
                        "intent": intent,
                        "response_preview": response[:100],
                    },
                )

    # ── Assistant-mode helpers ──

    @staticmethod
    def _build_todo(config, env):
        """A Microsoft To Do client when hand-off tasks are on and Microsoft is configured."""
        ms = getattr(getattr(config, "integrations", None), "microsoft", None)
        if ms is None or not ms.hand_off_tasks or not getattr(env, "ms_client_id", ""):
            return None
        from harvey.integrations.ms_graph import build_graph
        from harvey.integrations.ms_todo import TodoClient
        return TodoClient(build_graph(env), ms.todo_list)

    async def _hand_off_task(self, prospect, channel: str, reason: str, preview: str):
        """Put a thread Harvey won't answer on the owner's To Do list. Never raises: a task that
        can't be created must not stop reply handling (the dashboard still shows the thread)."""
        if self.todo is None or not self.todo.is_available():
            return
        try:
            company = prospect.company
            if not company and prospect.company_id:
                found = await self.state.get_company(prospect.company_id)
                company = found.name if found else ""
            who = prospect.full_name() + (f" ({company})" if company else "")
            await self.todo.add_task(
                f"Reply to {who}",
                f"Harvey is not answering this {channel} thread: {reason}.\n\n"
                f"They wrote: {preview[:400]}\n\nOpen the Harvey dashboard, Conversations tab.",
                importance="high")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Handler: could not add a To Do task for {prospect.full_name()}: {e}")

    async def _append_to_held(self, prospect, channel: str, text: str) -> bool:
        """A thread already handed to the owner: record the new message, send nothing."""
        for c in await self.state.get_conversations_by_status("needs_human"):
            if c.prospect_id == prospect.id and (c.channel or "email") == channel:
                c.thread.append(Message(sender="prospect", content=text))
                await self.state.update_conversation(c.id, thread_json=c.thread_json())
                await self.state.log_action(
                    action_type="held_for_owner", agent="handler",
                    details={"prospect": prospect.full_name(), "channel": channel,
                             "reply_preview": text[:200]})
                logger.info(f"Handler: {prospect.full_name()} wrote again; the thread is the owner's, no reply sent.")
                return True
        return False

    async def _deliver_assistant_reply(self, body: str, prospect, convo, channel: str,
                                       reply_meta: dict, intent: str):
        """Compose (email gets greeting and sign-off) and queue through the outbox."""
        if channel == "email":
            body = compose_email(body, first_name=prospect.first_name, step=2, last_step=3,
                                 persona=self.config.persona)
        if channel == "linkedin":
            await self._queue_linkedin_reply(body, prospect, convo, reply_meta, intent)
        elif channel == "sms":
            await self._queue_sms_reply(body, prospect, convo, intent)
        else:
            await self._queue_native_reply(body, prospect, convo, reply_meta, intent)

    async def _queue_sms_reply(self, body: str, prospect, convo, intent: str):
        """A text goes back as a text, through the outbox like any other channel."""
        require_approval = getattr(self.config.channels.sms, "require_approval", True)
        item_id = await self.state.add_outbox_item(
            prospect_id=prospect.id, conversation_id=convo.id, kind="reply", channel="sms",
            to_email="", to_ref=prospect.phone, subject="", body=body,
            send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            status="pending_review" if require_approval else "approved", provider="twilio")
        if item_id:
            logger.info(f"Handler: text reply to {prospect.full_name()} "
                        f"{'queued for your approval' if require_approval else 'queued to send'} ({intent}).")

    # ── Channels + suppression ──

    def _channels(self):
        """Every channel whose inbox must be read before any send happens."""
        channels = []
        if self.provider is not None:
            channels.append(EmailChannel(self.config, self.provider))
        li = getattr(self.config.channels, "linkedin", None)
        if li is not None and li.enabled and getattr(li, "mode", "") == "auto":
            channels.append(LinkedInChannel(self.config, self.state))
        sms = getattr(self.config.channels, "sms", None)
        if sms is not None and sms.enabled:
            channels.append(SmsChannel(self.config, self.env, self.state))
        return channels

    async def _handle_sms_keyword(self, msg) -> bool:
        """STOP ends texting to that number for good; START restores it. Both are handled in
        code before any model sees the message. Returns True when the text was a keyword."""
        word = sms_keyword(msg.body)
        if not word:
            return False
        number = msg.from_ref
        prospect = await self.state.get_prospect_by_phone(number)
        if word == "stop":
            await self.state.add_suppression("phone", number, channel="sms", reason="stop_keyword", source="sms")
            if prospect:
                await self.state.cancel_pending_outbox_for_prospect(prospect.id, reason="sms_stop")
                consent = prospect.consent()
                consent.pop("sms", None)
                await self.state.set_prospect_consent(prospect.id, consent)
            logger.warning(f"Handler: {number} texted STOP. No more texts to that number.")
        else:
            await self.state.remove_suppression("phone", number, channel="sms")
            if prospect:
                await self._record_sms_consent(prospect, "inbound_contact")
            logger.info(f"Handler: {number} texted START; texting allowed again.")
        return True

    async def _record_sms_consent(self, prospect, basis: str) -> None:
        """Texting us first is consent to be texted back about that conversation."""
        if prospect.phone_status != "verified" and prospect.phone:
            # They just texted from this number, which proves it reaches them.
            await self.state.set_prospect_phone(prospect.id, prospect.phone, "verified")
        consent = prospect.consent()
        if (consent.get("sms") or {}).get("basis") in ("inbound_contact", "booked", "opted_in"):
            return
        consent["sms"] = {"basis": basis, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        await self.state.set_prospect_consent(prospect.id, consent)

    async def _find_prospect(self, channel: str, ref: str):
        if channel == "linkedin":
            return await self.state.get_prospect_by_linkedin_url(ref)
        if channel == "sms":
            return await self.state.get_prospect_by_phone(ref)
        return await self.state.get_prospect_by_email(ref)

    async def _suppress_person(self, prospect, reason: str, source: str = "reply"):
        await suppress_person(self.state, prospect, reason, source)

    async def _suppress_company(self, prospect, reason: str, days: int):
        company = (await self.state.get_company(prospect.company_id)
                   if prospect.company_id else None)
        domain = (getattr(company, "domain", "") or "").strip().lower()
        if not domain and prospect.email and not compliance.is_free_mail(prospect.email):
            domain = prospect.email.rsplit("@", 1)[-1].lower()
        if not domain:
            return
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S")
        await self.state.add_suppression(
            "company", domain, channel="all", reason=reason, source="reply",
            expires_at=expires)

    # ── Native provider path (Gmail / SMTP) ──

    async def _run_native(self) -> bool:
        channels = [ch for ch in self._channels() if ch.is_configured()]
        try:
            return await self._read_channels(channels)
        finally:
            for ch in channels:   # a browser left open here leaked one Chromium per poll
                closer = getattr(ch, 'close', None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception as e:
                        logger.warning(f'Handler: closing {ch.name} failed: {e}')

    async def _poll_with_deadline(self, ch):
        """poll_inbound() with a hard deadline that does not depend on the poll honouring
        cancellation: if it hangs, it is cancelled and abandoned rather than awaited."""
        task = asyncio.ensure_future(ch.poll_inbound())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # never "unretrieved"
        done, _ = await asyncio.wait({task}, timeout=CHANNEL_POLL_DEADLINE_SECONDS)
        if not done:
            task.cancel()
            raise ChannelReadTimeout(
                f"reading {ch.name} replies took over {CHANNEL_POLL_DEADLINE_SECONDS}s; abandoned")
        return task.result()

    async def _read_channels(self, channels) -> bool:
        """Poll every channel's inbox, split bounces from human replies, handle
        both. Returns True only if every inbox was read and every message
        handled: a channel that can't be read must hold sending, because a
        reply we didn't see can't have cancelled its queued follow-ups."""
        if not channels:
            logger.debug("Handler: no configured channel to read yet.")
            return True  # nothing to read; the sender can't send through it either

        handled = 0
        complete = True
        # Email first: it is the one that gates sending, so a slow browser channel can't delay it.
        for ch in sorted(channels, key=lambda c: c.name != "email"):
            try:
                inbound = await self._poll_with_deadline(ch)
            except ChannelReadTimeout as e:
                # A hung browser must not hold email hostage; LinkedIn has its own freshness gate.
                logger.error(f"Handler: {e}")
                complete = complete and ch.name != "email"
                continue
            except Exception as e:
                logger.error(f"Handler: reading {ch.name} replies failed: {e}")
                complete = False
                continue

            stop = False
            for msg in inbound:
                dedup_key = msg.provider_id or msg.message_id
                if not dedup_key or await self.state.is_reply_processed(dedup_key):
                    continue
                consumed = True
                sender = msg.from_ref or msg.from_email
                try:
                    if msg.is_bounce:
                        await self._handle_bounce(msg)
                    elif (msg.channel or ch.name) == "sms" and await self._handle_sms_keyword(msg):
                        pass
                    elif msg.body.strip():
                        if (msg.channel or ch.name) == "sms":
                            known = await self.state.get_prospect_by_phone(sender)
                            if known:
                                await self._record_sms_consent(known, "inbound_contact")
                        await self._process_reply(
                            sender,
                            msg.body.strip(),
                            reply_uuid="",
                            reply_meta={
                                "thread_ref": msg.thread_ref,
                                "message_id": msg.message_id,
                                "subject": msg.subject,
                            },
                            channel=msg.channel or ch.name,
                        )
                    handled += 1
                except BrainUnavailable as e:
                    consumed = False
                    complete = False
                    stop = True
                    logger.error(f"Handler: stopping; replies are left for later ({e}).")
                    break
                except Exception as e:
                    logger.error(f"Handler: error processing {sender}: {e}")
                    complete = False
                finally:
                    if consumed:
                        await self.state.mark_reply_processed(dedup_key)
            if stop:
                break

        if handled:
            logger.info(f"Handler: processed {handled} inbound message(s).")
        else:
            logger.info("Handler: no new replies.")
        return complete

    async def _handle_bounce(self, msg):
        """A bounce is a data bug AND a reputation threat. Fix both, but only
        blame the address when the bounce says the address is bad."""
        outbox_item = await self.state.find_outbox_by_message_id(msg.in_reply_to)
        prospect = None
        if outbox_item:
            prospect = await self.state.get_prospect(outbox_item["prospect_id"])
        if prospect is None:
            # The mailbox is shared with other software (smoke tests, other agents),
            # so an undeliverable notice is only ours if it names someone we emailed.
            prospect = await self._prospect_named_in(msg)
        if prospect is None:
            logger.info(
                f"Handler: ignoring undeliverable notice not tied to anything Harvey "
                f"sent ({msg.subject[:60]}).")
            return

        kind = classify_bounce(msg.subject, msg.body)
        if kind in ("policy", "soft"):
            await self._handle_non_address_bounce(kind, msg, outbox_item, prospect)
            return

        if outbox_item:
            await self.state.set_send_outcome(outbox_item["id"], "hard_bounce")
        if prospect:
            await self.state.update_prospect_email(
                prospect.id, prospect.email, "invalid"
            )
            await self.state.add_suppression(
                "email", prospect.email, channel="all",
                reason="bounce_hard", source="mailbox")
            cancelled = await self.state.cancel_pending_outbox_for_prospect(
                prospect.id, reason="bounced"
            )
            logger.warning(
                f"Handler: BOUNCE for {prospect.email} — marked invalid, "
                f"cancelled {cancelled} queued email(s)."
            )
        else:
            logger.warning(
                f"Handler: bounce received ({msg.subject[:60]}) but couldn't "
                "match it to a sent email."
            )

        bounces = await self.state.increment_setting(BOUNCE_COUNT_KEY)
        total_sent = await self.state.count_outbox_sent()
        await self.state.log_action(
            action_type="bounce",
            agent="handler",
            details={"prospect": prospect.email if prospect else "unknown",
                     "bounces": bounces, "total_sent": total_sent},
        )

        max_rate = getattr(self.config.channels.email, "max_bounce_rate", 0.05)
        if (
            max_rate > 0
            and total_sent >= MIN_SENDS_FOR_KILL_SWITCH
            and bounces / total_sent > max_rate
        ):
            reason = (
                f"bounce rate {bounces}/{total_sent} exceeded "
                f"{max_rate:.0%} — check list quality before resuming"
            )
            await self.state.set_setting(KILL_SWITCH_KEY, reason)
            logger.error(f"Handler: KILL SWITCH ENGAGED — {reason}")

    async def _prospect_named_in(self, msg):
        """The prospect we have actually emailed whose address appears in this notice."""
        seen = set()
        for found in EMAIL_IN_TEXT.findall(f"{msg.subject}\n{msg.body}"):
            address = found.lower()
            if address in seen:
                continue
            seen.add(address)
            prospect = await self.state.get_prospect_by_email(address)
            if prospect and await self.state.get_sent_for_prospect(prospect.id, limit=1):
                return prospect
        return None

    async def _handle_non_address_bounce(self, kind: str, msg, outbox_item, prospect):
        """A policy block (5.7.x: IP/domain reputation, authentication) or a soft
        bounce (full mailbox, deferral) says nothing bad about the address.
        Keep the prospect valid and unsuppressed, leave the hard-bounce counter
        alone, and treat repeated policy blocks as a sender-health problem."""
        who = prospect.email if prospect else "unknown"
        if outbox_item:
            await self.state.set_send_outcome(
                outbox_item["id"], "policy_block" if kind == "policy" else "soft_bounce")
        await self.state.log_action(
            action_type="bounce_policy" if kind == "policy" else "bounce_soft",
            agent="handler",
            details={"prospect": who, "subject": msg.subject[:120]},
        )
        if kind == "soft":
            logger.warning(f"Handler: soft bounce for {who}; address left valid.")
            return

        logger.warning(
            f"Handler: POLICY BLOCK for {who} ({msg.subject[:60]}); the recipient's server "
            "refused our mail. Address left valid.")
        await record_policy_block(self.state)

    async def _queue_linkedin_reply(
        self, response: str, prospect, convo, reply_meta: dict, intent: str
    ):
        """A LinkedIn reply goes back on LinkedIn, through the outbox like any other."""
        require_approval = getattr(self.config.channels.linkedin, "require_approval", True)
        item_id = await self.state.add_outbox_item(
            prospect_id=prospect.id, conversation_id=convo.id, kind="reply",
            channel="linkedin", to_email="", to_ref=prospect.linkedin_url,
            subject="", body=response,
            send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            status="pending_review" if require_approval else "approved",
            provider="linkedin", thread_ref=reply_meta.get("thread_ref", ""),
        )
        if item_id:
            mode = "queued for your approval" if require_approval else "queued to send"
            logger.info(f"Handler: LinkedIn reply to {prospect.full_name()} {mode} ({intent}).")
            await self.state.log_action(
                action_type="reply_queued", agent="handler",
                details={"prospect": prospect.full_name(), "channel": "linkedin",
                         "intent": intent, "response_preview": response[:100]})

    async def _queue_native_reply(
        self, response: str, prospect, convo, reply_meta: dict, intent: str
    ):
        """Route Harvey's reply through the outbox (approval ladder applies)."""
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        subject = reply_meta.get("subject", "")
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        item_id = await self.state.add_outbox_item(
            prospect_id=prospect.id,
            conversation_id=convo.id,
            kind="reply",
            to_email=prospect.email,
            subject=subject or "Re: your note",
            body=response,
            send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            status="pending_review" if require_approval else "approved",
            provider=self.provider.name if self.provider else "",
            thread_ref=reply_meta.get("thread_ref", ""),
            in_reply_to=reply_meta.get("message_id", ""),
        )
        if item_id:
            mode = "queued for your approval" if require_approval else "queued to send"
            logger.info(f"Handler: reply to {prospect.email} {mode} ({intent}).")
            await self.state.log_action(
                action_type="reply_queued",
                agent="handler",
                details={"prospect_email": prospect.email, "intent": intent,
                         "response_preview": response[:100]},
            )

    async def _classify_intent(self, reply_text: str, prospect) -> str:
        """Ask the brain to classify the reply's intent."""
        prompt = f"""Classify this email reply into exactly ONE category:
- "interested" — wants to learn more, open to a meeting, positive response
- "objection" — has concerns but hasn't said no (price, timing, competition)
- "not_interested" — a clear no, not now, or "we're all set"
- "unsubscribe" — asks to stop being contacted, remove from list, opt out
- "escalate" — angry, hostile, threatens legal action, or mentions spam/compliance
- "ooo" — out of office / auto-reply
- "wrong_person" — not the right contact, suggests someone else
- "question" — asking for more information before deciding

When in doubt between "not_interested" and "unsubscribe", choose "unsubscribe".
When in doubt between anything and "escalate", choose "escalate".

Reply from {prospect.full_name()} ({prospect.title} at {prospect.company}):
\"\"\"{reply_text}\"\"\"

Respond with ONLY the category label, nothing else."""

        result = await self.brain.think(
            prompt, session_id="harvey-handler",
            agent="handler", task="classify_intent",
        )
        if not result:
            paused = getattr(self.brain, "paused_reason", "")
            if paused:
                # The provider is down (login, setup, usage limit), not this
                # reply: don't escalate it, leave it unprocessed.
                raise BrainUnavailable(paused)
            # Classifier failed. Do NOT auto-reply blind — flag for a human.
            logger.warning("Handler: Intent classifier returned nothing. Escalating.")
            return "escalate"

        # Robust extraction: take the first recognized label anywhere in
        # the response (models sometimes add explanation despite instructions).
        cleaned = result.strip().strip('"').strip("'").lower()
        if cleaned in INTENT_LABELS:
            return cleaned
        for label in sorted(INTENT_LABELS, key=len, reverse=True):
            if label in cleaned:
                return label

        logger.warning(f"Handler: Unknown intent '{cleaned[:80]}'. Defaulting to 'question'.")
        return "question"

    def _determine_stage(self, intent: str, current_stage: str, reply_text: str) -> str:
        """Advance the conversation stage based on intent and context."""
        text_lower = reply_text.lower()

        # Terminal states
        if intent in ("not_interested", "unsubscribe"):
            return "closed_lost"

        if intent == "escalate":
            return current_stage  # Human decides what happens next

        # Stage advancement rules
        if intent == "interested":
            # Meeting/call signals from an interested prospect → closing
            if any(w in text_lower for w in ["let's meet", "schedule", "calendar", "book a call", "free on", "available"]):
                return "closing"
            if current_stage == "initial_outreach":
                return "engaged"
            if current_stage == "engaged":
                # Check if they're asking about specifics → presenting
                if any(w in text_lower for w in ["price", "cost", "how much", "pricing", "demo", "trial"]):
                    return "presenting"
                return "qualifying"
            if current_stage in ("qualifying", "presenting"):
                return "negotiating"
            if current_stage == "negotiating":
                return "closing"

        if intent == "objection":
            # Objections typically happen during presenting or negotiating
            if current_stage in ("initial_outreach", "engaged"):
                return "qualifying"
            # Stay in current stage during objection handling

        if intent == "question":
            if current_stage == "initial_outreach":
                return "engaged"
            if current_stage == "engaged":
                return "qualifying"

        return current_stage

    async def _generate_response(
        self, intent: str, reply_text: str, prospect, convo: Conversation,
        channel: str = "email",
    ) -> str:
        """Generate an appropriate response based on intent."""
        # Build conversation history for context
        history = "\n".join(
            f"{'Harvey' if m.sender == 'harvey' else prospect.full_name()}: {m.content}"
            for m in convo.thread[-6:]  # Last 6 messages for context
        )

        objection_context = ""
        if intent == "objection":
            # Check if we have a pre-configured response
            for trigger, response in self.config.product.objection_responses.items():
                if trigger.lower() in reply_text.lower():
                    objection_context = f"\nSuggested approach for this objection: {response}"
                    break

        prompt = self.brain.load_prompt("handler", stage=convo.stage)
        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}
Product: {self.config.product.name} — {self.config.product.description}"""

        # Inject objection handling + sales methodology skills
        if self.skills:
            prompt += "\n\n" + self.skills

        prompt += f"""

Conversation so far:
{history}

The prospect's intent is: {intent}
{objection_context}

Write a reply that:"""

        if intent == "interested":
            prompt += """
- Acknowledges their interest warmly
- Suggests a specific next step (brief call or meeting)
- Keeps it short (under 80 words)
- Includes a clear CTA with flexibility on timing"""
        elif intent == "objection":
            prompt += """
- Addresses the concern directly and empathetically
- Provides evidence or a reframe
- Doesn't argue — redirect toward value
- Keeps it under 100 words"""
        elif intent == "question":
            prompt += """
- Answers their question clearly and concisely
- Ties the answer back to value for them
- Ends with a soft CTA
- Under 100 words"""
        elif intent == "wrong_person":
            prompt += """
- Thanks them politely
- Asks who the right person would be
- Makes it easy for them to refer (one-line ask)
- Under 50 words"""

        if channel == "linkedin":
            prompt += ("\n\nThis is a LinkedIn direct message. Keep it to 60 words or fewer, "
                       "plain text, no links, no signature, no subject, no markdown.")
        else:
            prompt += "\n\nWrite ONLY the email body. No subject line, no greeting label, no signature block, no markdown."

        response = await self.brain.think(
            prompt, session_id="harvey-handler",
            agent="handler", task="generate_response",
        )
        if not response:
            paused = getattr(self.brain, "paused_reason", "")
            if paused:
                # The provider went down mid-reply; nothing is stored yet,
                # so leave the reply for a clean retry.
                raise BrainUnavailable(paused)
            return ""
        response = response.strip()
        # Guard against the model returning meta-text instead of an email
        if response.lower().startswith(("i can't", "i cannot", "as an ai", "sorry,")):
            logger.warning("Handler: Model returned meta-text instead of an email. Discarding.")
            return ""
        return response
