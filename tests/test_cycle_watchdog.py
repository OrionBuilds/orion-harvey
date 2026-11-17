"""A task that ignores cancellation (a hung browser driver) must not freeze the heartbeat."""
import asyncio
import time

import pytest

import harvey.main as m


@pytest.mark.asyncio
async def test_run_agents_abandons_task_that_swallows_cancellation(monkeypatch):
    monkeypatch.setattr(m, "CYCLE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(m, "CANCEL_GRACE_SECONDS", 0.1)

    async def stubborn():
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass   # swallows cancel, like the wedged Playwright start

    async def fine():
        return True

    t0 = time.monotonic()
    outcomes = await m._run_agents([("stuck", stubborn()), ("ok", fine())])
    assert time.monotonic() - t0 < 2
    assert outcomes == {"ok": True}          # the stuck one has no outcome, so nothing is "handled"


@pytest.mark.asyncio
async def test_run_agents_keeps_exceptions_as_outcomes():
    async def boom():
        raise ValueError("x")
    out = await m._run_agents([("boom", boom())])
    assert isinstance(out["boom"], ValueError)



@pytest.mark.asyncio
async def test_a_hung_inbox_read_is_abandoned_and_a_non_email_channel_does_not_hold_email(monkeypatch):
    import harvey.agents.handler as handler_mod
    monkeypatch.setattr(handler_mod, "CHANNEL_POLL_DEADLINE_SECONDS", 0.05)

    class Hung:
        name = "linkedin"
        async def poll_inbound(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)          # swallows cancellation, like a stuck browser

    class Email:
        name = "email"
        async def poll_inbound(self):
            return []

    h = handler_mod.Handler.__new__(handler_mod.Handler)
    assert await asyncio.wait_for(h._read_channels([Hung(), Email()]), 2) is True

    class HungEmail(Hung):
        name = "email"
    assert await asyncio.wait_for(h._read_channels([HungEmail()]), 2) is False


def test_http_client_logging_cannot_write_api_keys_to_the_journal():
    import logging
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
