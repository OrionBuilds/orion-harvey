"""Actions taken when someone opts out, bounces, or complains.

One implementation shared by the Handler (replies, IMAP bounces), the
unsubscribe page, and the Resend webhook, so every route into "do not
contact" behaves identically: a "no" follows the person across channels,
cancels everything queued, and is recorded where the send gate looks.
"""

import logging

logger = logging.getLogger("harvey.optout")


async def suppress_person(state, prospect, reason: str, source: str) -> None:
    """Block every way we know to reach this person, on every channel."""
    for kind, value in (
        ("email", prospect.email),
        ("phone", prospect.phone),
        ("linkedin", prospect.linkedin_url),
    ):
        if value:
            await state.add_suppression(
                kind, value, channel="all", reason=reason, source=source)


async def opt_out(state, prospect, *, reason: str, source: str) -> int:
    """Honor an opt-out or complaint. Returns how many queued messages were cancelled."""
    await state.update_prospect_status(prospect.id, "opted_out")
    await suppress_person(state, prospect, reason, source)
    cancelled = await state.cancel_pending_outbox_for_prospect(
        prospect.id, reason=reason)
    await state.log_action(
        action_type="opt_out", agent="optout",
        details={"prospect_email": prospect.email, "reason": reason,
                 "source": source, "cancelled": cancelled})
    logger.info(f"{prospect.email} opted out ({reason} via {source}); "
                f"cancelled {cancelled} queued message(s).")
    return cancelled


async def hard_bounce(state, outbox_item: dict | None, prospect, *, source: str) -> None:
    """A permanent delivery failure: fix the data and protect reputation."""
    if outbox_item:
        await state.set_send_outcome(outbox_item["id"], "hard_bounce")
    if prospect:
        await state.update_prospect_email(prospect.id, prospect.email, "invalid")
        await state.add_suppression(
            "email", prospect.email, channel="all", reason="bounce_hard", source=source)
        await state.cancel_pending_outbox_for_prospect(prospect.id, reason="bounced")
    await state.log_action(
        action_type="bounce", agent="optout",
        details={"prospect": prospect.email if prospect else "unknown",
                 "source": source})


async def complaint(state, outbox_item: dict | None, prospect, *, source: str) -> None:
    """A spam complaint is an opt-out, and the worst deliverability signal."""
    if outbox_item:
        await state.set_send_outcome(outbox_item["id"], "complaint")
    if prospect:
        await opt_out(state, prospect, reason="complaint", source=source)
