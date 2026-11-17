"""Quiet hours stop outreach, not listening: Harvey stays awake overnight,
reads replies and bounces, and sends / writes / prospects nothing."""

import asyncio
import tempfile
from pathlib import Path

import pytest

import harvey.main as main_mod
from harvey.config import EnvConfig, HarveyConfig
from harvey.state import StateManager
from tests.test_codex_e2e import BASE


def _run(monkeypatch, tmp_path, *, quiet: bool):
    calls: list[str] = []

    import harvey.agents.handler as handler_mod
    import harvey.agents.scout as scout_mod
    import harvey.agents.sender as sender_mod
    import harvey.agents.writer as writer_mod
    from tests.test_outbox_native import FakeProvider

    mail = FakeProvider()
    monkeypatch.setattr(handler_mod, "get_mail_provider", lambda c, e: mail)
    monkeypatch.setattr(sender_mod, "get_mail_provider", lambda c, e: mail)

    async def handler_run(_self):
        calls.append("handler")
        return True

    async def sender_run(_self):
        calls.append("sender")

    async def writer_run(_self):
        calls.append("writer")

    async def scout_run(_self):
        calls.append("scout")

    monkeypatch.setattr(handler_mod.Handler, "run", handler_run)
    monkeypatch.setattr(sender_mod.Sender, "run", sender_run)
    monkeypatch.setattr(writer_mod.Writer, "run", writer_run)
    monkeypatch.setattr(scout_mod.Scout, "run", scout_run)

    config = HarveyConfig(**BASE)
    state = StateManager(db_path=str(tmp_path / "h.db"))
    monkeypatch.setattr(main_mod, "load_config", lambda: config)
    monkeypatch.setattr(main_mod, "load_env", lambda: EnvConfig())
    monkeypatch.setattr(main_mod, "StateManager", lambda: state)
    monkeypatch.setattr(main_mod, "in_quiet_hours", lambda c: quiet)

    async def stop_after_first_sleep(seconds, stop_event):
        return True

    monkeypatch.setattr(main_mod, "_interruptible_sleep", stop_after_first_sleep)

    async def no_preflight(self):
        return ""

    monkeypatch.setattr(main_mod.Brain, "preflight", no_preflight)

    async def within_budget(self, *a, **k):
        return True   # the real check reads the live Claude quota

    monkeypatch.setattr(main_mod.Brain, "is_within_budget", within_budget)

    async def go():
        await asyncio.wait_for(main_mod.heartbeat(asyncio.Event()), 30)

    asyncio.run(go())
    return calls


def test_quiet_hours_reads_replies_but_sends_nothing(monkeypatch, tmp_path):
    calls = _run(monkeypatch, tmp_path, quiet=True)
    assert calls == ["handler"]


def test_daytime_cycle_also_sends(monkeypatch, tmp_path):
    calls = _run(monkeypatch, tmp_path, quiet=False)
    assert "sender" in calls and "handler" in calls
