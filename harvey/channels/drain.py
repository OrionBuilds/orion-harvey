"""The shared send loop: drain due, approved outbox rows through any Channel.

Order of defenses per item: stop-on-reply -> channel content gate -> suppression
and frequency gate -> send -> ledger. Anything that can't be verified blocks
the send (fail closed); temporary blocks leave the item queued for a later
cycle, permanent ones cancel or fail it with the reason recorded.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

from harvey.channels import Channel
from harvey.gate import stateful_gate
from harvey.paths import stop_requested

logger = logging.getLogger("harvey.drain")

# Prospect pipeline statuses that mean "stop contacting this person".
STOP_STATUSES = {"replied", "opted_out", "lost", "meeting", "closed"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def drain_channel(state, channel: Channel, *, pacing_enabled: bool = True) -> int:
    """Send what's due on one channel. Returns the number sent."""
    try:
        return await _drain(state, channel, pacing_enabled)
    finally:
        try:
            await channel.close()
        except Exception as e:  # noqa: BLE001 — never mask the drain's own outcome
            logger.warning(f"{channel.name}: close failed: {e}")


async def _drain(state, channel: Channel, pacing_enabled: bool) -> int:
    name = channel.name

    if stop_requested():
        logger.warning(f"{name}: STOP file present; nothing is sent until it is removed.")
        return 0

    paused = await state.get_setting(channel.pause_key)
    if paused:
        logger.warning(
            f"{name}: SENDING PAUSED ({paused}). Resume from the dashboard "
            "or `harvey sending resume` once the cause is fixed.")
        return 0

    if not channel.is_configured():
        logger.warning(f"{name}: channel is not configured yet; outbox is holding.")
        return 0

    reason = await channel.breaker_reason(state)
    if reason:
        if channel.pauses_on_breaker:
            await state.set_setting(channel.pause_key, f"breaker: {reason}")
            logger.error(f"{name}: delivery-health breaker tripped, pausing: {reason}")
        else:
            logger.warning(f"{name}: holding sends: {reason}")
        return 0

    cap = channel.daily_cap()
    sent_today = await state.count_sent_today(name)
    budget = min(channel.max_per_cycle(), cap - sent_today)
    if budget <= 0:
        logger.info(f"{name}: daily send cap reached ({sent_today}/{cap}).")
        return 0

    due = await state.get_outbox(
        status="approved", due_before=_now_iso(), limit=budget * 3, channel=name)
    if not due:
        return 0

    sent = 0
    for item in due:
        if sent >= budget:
            break

        prospect = await state.get_prospect(item["prospect_id"])
        if prospect is None:
            await state.update_outbox_item(
                item["id"], status="failed", error="prospect missing")
            continue

        # Stop-on-reply / opt-out: never continue outreach after contact. Only a
        # reply to an inbound message is exempt (it answers them).
        if item["kind"] != "reply" and prospect.status in STOP_STATUSES:
            await state.update_outbox_item(
                item["id"], status="cancelled",
                error=f"prospect status '{prospect.status}'")
            continue

        subject, body = await channel.prepare(item, prospect)

        gate = channel.content_gate(item, subject, body, prospect)
        if not gate:
            await state.update_outbox_item(
                item["id"], status="failed",
                error="gate: " + "; ".join(gate.reasons)[:300])
            logger.warning(f"{name}: gate blocked {item['to_ref']}: {gate.reasons}")
            continue

        compliance_gate = await stateful_gate(
            state, channel=name, prospect=prospect, kind=item["kind"],
            quotas=channel.quotas())
        if not compliance_gate:
            if compliance_gate.defer:
                logger.info(
                    f"{name}: holding {item['to_ref']} for a later cycle: "
                    f"{compliance_gate.reasons}")
            else:
                await state.update_outbox_item(
                    item["id"], status="cancelled",
                    error="compliance: " + "; ".join(compliance_gate.reasons)[:300])
                logger.warning(
                    f"{name}: compliance blocked {item['to_ref']}: "
                    f"{compliance_gate.reasons}")
            continue

        rules = await channel.pre_send(state, item, prospect)
        if not rules:
            if rules.defer:
                logger.info(f"{name}: holding {item['to_ref']}: {rules.reasons}")
            else:
                await state.update_outbox_item(
                    item["id"], status="cancelled",
                    error="; ".join(rules.reasons)[:300])
                logger.warning(f"{name}: not sending to {item['to_ref']}: {rules.reasons}")
            continue

        result = await channel.send(item, subject, body)
        if not result.ok:
            if result.defer:
                logger.warning(f"{name}: send deferred, ending this cycle: {result.error}")
                break
            await state.update_outbox_item(
                item["id"], status="failed", error=result.error[:300])
            continue

        sent += 1
        await state.update_outbox_item(
            item["id"], status="sent", sent_at=_now_iso(),
            message_id=result.message_id, thread_ref=result.thread_ref,
            provider_id=getattr(result, "provider_id", ""))
        try:
            await state.record_send(
                channel=name, prospect_id=prospect.id,
                company_id=prospect.company_id,
                sender_domain=channel.sender_domain(), ref=item["id"],
                kind=item["kind"])
        except Exception as e:  # noqa: BLE001 — the send already happened
            logger.error(f"{name}: sent but could not record in ledger: {e}")

        try:
            await channel.after_send(state, item, prospect)
        except Exception as e:  # noqa: BLE001 — the send already happened
            logger.error(f"{name}: after-send bookkeeping failed: {e}")

        if prospect.status in ("new", "queued"):
            await state.update_prospect_status(prospect.id, "contacted")

        # A sent reply belongs in its conversation thread.
        if item["kind"] == "reply" and item.get("conversation_id"):
            try:
                convo = await state.get_conversation(item["conversation_id"])
                if convo:
                    from harvey.models.conversation import Message
                    convo.thread.append(Message(sender="harvey", content=item["body"]))
                    await state.update_conversation(
                        convo.id, thread_json=convo.thread_json())
            except Exception as e:  # noqa: BLE001
                logger.debug(f"{name}: convo append failed: {e}")

        await state.log_action(
            action_type=f"{name}_sent", agent="sender",
            details={"to": item["to_ref"], "step": item["step"],
                     "kind": item["kind"], "provider": getattr(
                         getattr(channel, "provider", None), "name", name)})
        logger.info(f"{name}: sent step {item['step']} to {item['to_ref']}.")

        pause = channel.pacing()
        if pacing_enabled and pause and sent < budget:
            await asyncio.sleep(random.uniform(*pause))

    if sent:
        logger.info(f"{name}: {sent} message(s) sent this cycle ({sent_today + sent}/{cap} today).")
    return sent
