"""Inbound webhooks from the sending provider (Resend, signed with Svix).

Resend reports bounces and spam complaints for mail it sent by webhook;
they never reach the reply mailbox. Verification is mandatory and fails
closed: no secret configured means every delivery is refused.
"""

import base64
import hashlib
import hmac
import logging
import time

from harvey import optout
from harvey.bounce_policy import record_policy_block
from harvey.integrations.mail_provider import classify_bounce

logger = logging.getLogger("harvey.webhooks")

TOLERANCE_SECONDS = 300


def verify_svix(secret: str, headers, body: bytes, *, now: float | None = None) -> bool:
    """Verify a Svix-signed delivery (Resend). `headers` is any case-insensitive mapping."""
    if not secret:
        return False
    msg_id = headers.get("svix-id", "")
    timestamp = headers.get("svix-timestamp", "")
    signatures = headers.get("svix-signature", "")
    if not (msg_id and timestamp and signatures):
        return False
    try:
        if abs((now if now is not None else time.time()) - int(timestamp)) > TOLERANCE_SECONDS:
            return False  # replayed or badly skewed
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, TypeError):
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    # The header may carry several space-separated "v1,<sig>" entries (key rotation).
    for entry in signatures.split():
        version, _, sig = entry.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return True
    return False


async def handle_resend_event(state, event: dict) -> str:
    """Apply one Resend event. Returns what was done, for logging and tests."""
    kind = event.get("type", "")
    data = event.get("data") or {}
    if kind not in ("email.bounced", "email.complained"):
        return "ignored"

    outbox_item = await state.find_outbox_by_provider_id(data.get("email_id", ""))
    prospect = None
    if outbox_item:
        prospect = await state.get_prospect(outbox_item["prospect_id"])
    if prospect is None:
        for address in data.get("to") or []:
            prospect = await state.get_prospect_by_email(address)
            if prospect:
                break

    # The Resend account is shared: the OS sends its own mail (bookings, notices, tests) through
    # it, and every bounce lands on this webhook. Only mail Harvey sent is Harvey's concern: it
    # must trace to one of his outbox rows, or name someone he has actually emailed. Anything else
    # must not pause his sending, count against his health, or suppress anyone.
    ours = outbox_item is not None or (
        prospect is not None and bool(await state.get_sent_for_prospect(prospect.id, limit=1)))
    if not ours:
        logger.info(f"{kind} for {data.get('to')} is not Harvey's mail; ignored")
        return "not_ours"

    if kind == "email.complained":
        await optout.complaint(state, outbox_item, prospect, source="resend")
        return "complaint"

    bounce_type = ((data.get("bounce") or {}).get("type") or "").lower()
    if "transient" in bounce_type:
        # A full mailbox or greylisting says nothing about whether the
        # address exists; don't burn a good prospect on it.
        logger.info(f"soft bounce for {data.get('to')}; no action")
        return "soft_bounce"
    bounce = data.get("bounce") or {}
    detail = classify_bounce(bounce.get("subType", ""), bounce.get("message", ""))
    if detail == "policy":
        # The receiver refused OUR mail (reputation/authentication), not the address.
        if outbox_item:
            await state.set_send_outcome(outbox_item["id"], "policy_block")
        await record_policy_block(state)
        logger.warning(f"policy block for {data.get('to')}; address left valid")
        return "policy_block"
    if detail == "soft":
        logger.info(f"soft bounce for {data.get('to')}; no action")
        return "soft_bounce"
    await optout.hard_bounce(state, outbox_item, prospect, source="resend")
    return "hard_bounce"
