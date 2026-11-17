"""Sender-health tracking for policy blocks (5.7.x): receivers refusing OUR mail.

A policy block says nothing about the recipient's address, so it never
invalidates a prospect. Repeated blocks inside a window mean our IP/domain
reputation or authentication is the problem, so sending pauses until the owner
looks. Shared by the mailbox bounce path (Handler) and the Resend webhook.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("harvey.bounce_policy")

KILL_SWITCH_KEY = "sending_paused"
POLICY_BLOCKS_KEY = "policy_block_times"
POLICY_BLOCK_WINDOW_HOURS = 24
POLICY_BLOCKS_TO_PAUSE = 2


async def record_policy_block(state) -> bool:
    """Remember this block; pause sending once the threshold is hit.
    Returns True when this call paused sending."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=POLICY_BLOCK_WINDOW_HOURS)
    recent = []
    try:
        for t in json.loads(await state.get_setting(POLICY_BLOCKS_KEY, "[]") or "[]"):
            if datetime.fromisoformat(t) > cutoff:
                recent.append(t)
    except (ValueError, TypeError):
        recent = []
    recent.append(now.isoformat())
    await state.set_setting(POLICY_BLOCKS_KEY, json.dumps(recent))
    if len(recent) >= POLICY_BLOCKS_TO_PAUSE and not await state.get_setting(KILL_SWITCH_KEY):
        reason = (f"{len(recent)} policy blocks (5.7.x) in {POLICY_BLOCK_WINDOW_HOURS}h; "
                  "receivers are refusing our mail (IP/domain reputation or authentication). "
                  "Check the sender domain's SPF/DKIM/DMARC and the provider's logs, then resume")
        await state.set_setting(KILL_SWITCH_KEY, reason)
        logger.error(f"KILL SWITCH ENGAGED: {reason}")
        return True
    return False
