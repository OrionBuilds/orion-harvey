"""LinkedIn relationship bookkeeping shared by every path that changes it:
the automated channel, the assist-mode dashboard buttons, and the Handler."""

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def apply_sent(state, item: dict, prospect) -> None:
    """Record that a LinkedIn connect/message actually went out."""
    kind = item["kind"]
    now = _now()
    thread = await state.get_linkedin_thread(prospect.id) or {}
    if kind == "connect":
        await state.set_linkedin_thread(prospect.id, state="requested", requested_at=now)
    elif kind == "message":
        await state.set_linkedin_thread(
            prospect.id, state="messaged", last_message_at=now,
            messages_sent=int(thread.get("messages_sent") or 0) + 1)
    else:  # reply to something they sent
        await state.set_linkedin_thread(prospect.id, state="replied", last_message_at=now)


async def mark_accepted(state, prospect_id: str) -> None:
    thread = await state.get_linkedin_thread(prospect_id) or {}
    if thread.get("state") in ("replied",):
        return
    await state.set_linkedin_thread(prospect_id, state="connected", accepted_at=_now())


async def mark_replied(state, prospect_id: str) -> None:
    await state.set_linkedin_thread(prospect_id, state="replied")


async def mark_sent_manually(state, item_id: str) -> str:
    """Assist mode: the owner sent this personally; record it exactly as an automated
    send would be (status, caps ledger, relationship state).

    Returns '' on success, else why it was refused."""
    item = await state.get_outbox_item(item_id)
    if not item or item["channel"] != "linkedin":
        return "not a LinkedIn item"
    if item["status"] != "approved":
        return f"item is '{item['status']}', approve it first"
    prospect = await state.get_prospect(item["prospect_id"])
    if prospect is None:
        return "prospect missing"
    await state.update_outbox_item(item_id, status="sent", sent_at=_now())
    await state.record_send(channel="linkedin", prospect_id=prospect.id,
                            company_id=prospect.company_id, ref=item_id, kind=item["kind"])
    await apply_sent(state, item, prospect)
    if prospect.status in ("new", "queued"):
        await state.update_prospect_status(prospect.id, "contacted")
    return ""
