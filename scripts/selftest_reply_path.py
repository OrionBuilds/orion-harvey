"""Live check of Harvey's reply path, using your own mailbox as the "prospect".

    SELFTEST_EMAIL=you@example.com python scripts/selftest_reply_path.py          # send one test email to you
    SELFTEST_EMAIL=you@example.com python scripts/selftest_reply_path.py --done   # afterwards: retire the test contact

1. Run it. Harvey sends ONE real email from its configured sender (persona.email) to
   SELFTEST_EMAIL. It is a plain test message; the unsubscribe footer is added as usual.
2. Reply to it from that mailbox with anything ("Interested, send more").
3. Within about 5 minutes Harvey should read the reply, file it as a conversation, show it
   on the Orion Harvey page (Replies tab) when Orion is connected, and queue a drafted
   answer in Approvals. Nothing is sent back without your approval.
4. Run with --done to stop any further mail to the test contact.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

from harvey.channels.drain import drain_channel
from harvey.channels.email import EmailChannel
from harvey import owner
from harvey.config import load_config, load_env
from harvey.integrations.mail_provider import get_mail_provider
from harvey.models.prospect import Prospect
from harvey.state import StateManager

TEST_EMAIL = os.getenv("SELFTEST_EMAIL", "").strip()


async def main(done: bool) -> None:
    if not TEST_EMAIL:
        sys.exit("Set SELFTEST_EMAIL to a mailbox you own.")
    env, config = load_env(), load_config()
    state = StateManager()
    await state.init_db()

    existing = await state.get_prospect_by_email(TEST_EMAIL)
    if done:
        if existing is None:
            print("No test contact found.")
            return
        await state.cancel_pending_outbox_for_prospect(existing.id, reason="selftest_done")
        await state.update_prospect_status(existing.id, "opted_out")
        print("Test contact retired: nothing more will be sent to it.")
        return

    if existing is not None and existing.status == "opted_out":
        print(f"{TEST_EMAIL} was retired with --done; not sending again.")
        return
    if existing is not None:
        pid = existing.id            # re-run: stage a fresh test message to the same contact
    else:
        pid = await state.add_prospect(Prospect(
            first_name=owner.FIRST_CAP, last_name="Selftest", title="Owner", company="Harvey self-test",
            email=TEST_EMAIL, email_status="verified", status="queued", source="selftest"))
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=TEST_EMAIL, subject="harvey reply test", send_at=now,
        status="approved", campaign_id=f"selftest-{datetime.now(timezone.utc):%Y%m%d%H%M%S}", step=1,
        kind="reply",   # a one-off message to yourself, not a cold sequence (the cold-outreach gate refuses free mail)
        body=(f"Hey {owner.FIRST_CAP},\n\nThis is a test of Harvey's reply path. Reply to this message with anything "
              f"and I will check that it reaches Orion.\n\nHarvey\n{owner.NAME}'s AI assistant"))
    provider = get_mail_provider(config, env)
    sent = await drain_channel(state, EmailChannel(config, provider, state), pacing_enabled=False)
    row = await state.get_outbox_item(item)
    print(f"sent={sent} status={row['status']} error={row.get('error') or '-'}")
    if row["status"] == "sent":
        print(f"Check {TEST_EMAIL}, reply to it, and give Harvey about 5 minutes.")


if __name__ == "__main__":
    asyncio.run(main("--done" in sys.argv))
