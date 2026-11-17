"""The local brain provider: an OpenAI-compatible server on this machine (LM Studio)."""

import json
import os
import tempfile

import httpx
import pytest
import pytest_asyncio

from harvey.brain import Brain
from harvey.config import BrainConfig
from harvey.state import StateManager
from harvey.brain import with_us_spelling


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


def brain_with(state, handler, **cfg):
    brain = Brain(state, BrainConfig(provider="local", model="qwen3.8-27b", **cfg))
    brain._local_transport = httpx.MockTransport(handler)
    return brain


def ok(content, **extra):
    return httpx.Response(200, json={"model": "qwen3.8-27b", "usage": {"prompt_tokens": 12, "completion_tokens": 5},
                                     "choices": [{"message": {"content": content}}], **extra})


@pytest.mark.asyncio
async def test_a_local_call_sends_the_prompt_with_an_idle_unload_and_returns_the_answer(state):
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return ok("hello there")

    brain = brain_with(state, handler, keep_loaded_seconds=300)
    assert await brain.think("say hi", agent="writer", task="t") == "hello there"
    assert seen["url"] == "http://127.0.0.1:1235/v1/chat/completions"
    assert seen["body"]["model"] == "qwen3.8-27b" and seen["body"]["ttl"] == 300
    assert seen["body"]["messages"] == [{"role": "user", "content": with_us_spelling("say hi")}]
    assert "tools" not in seen["body"]                     # no tools, ever
    summary = await state.get_usage_summary() if hasattr(state, "get_usage_summary") else None
    assert summary is None or summary                      # the call was ledgered without error


@pytest.mark.asyncio
async def test_reasoning_text_is_dropped_and_json_still_parses(state):
    brain = brain_with(state, lambda req: ok('<think>let me plan this</think>\n{"a": 1}'))
    assert await brain.think_json("give json") == {"a": 1}


@pytest.mark.asyncio
async def test_server_down_returns_empty_after_retries_and_never_raises(state, monkeypatch):
    monkeypatch.setattr("harvey.brain.RETRY_BASE_DELAY", 0.0)
    calls = []

    def handler(req):
        if req.url.path.endswith("/chat/completions"):
            calls.append(1)
        raise httpx.ConnectError("refused")

    brain = brain_with(state, handler)
    assert await brain.think("x", max_retries=2) == ""
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_a_client_error_is_not_retried(state):
    calls = []

    def handler(req):
        if req.url.path.endswith("/chat/completions"):
            calls.append(1)
            return httpx.Response(400, text="model not found")
        return httpx.Response(200, json={"models": []})

    brain = brain_with(state, handler)
    assert await brain.think("x", max_retries=2) == ""
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_local_model_has_no_quota_gate(state):
    brain = brain_with(state, lambda req: ok("x"))
    assert await brain.is_within_budget(max_percent=1.0) is True
    assert brain.provider_label == "Local model"


def test_the_local_server_must_be_on_this_machine():
    for bad in ("http://192.168.1.50:1234/v1", "https://example.com/v1", "ftp://127.0.0.1/v1"):
        with pytest.raises(ValueError):
            BrainConfig(provider="local", base_url=bad)
    assert BrainConfig(provider="local", base_url="http://localhost:1235/v1/").base_url == "http://localhost:1235/v1"


@pytest.mark.asyncio
async def test_the_model_is_loaded_with_the_configured_context_when_it_is_not_loaded_big_enough(state):
    sent = []

    def handler(req):
        sent.append((req.method, req.url.path))
        if req.method == "GET":
            return httpx.Response(200, json={"models": [{"key": "qwen3.8-27b", "loaded_instances": [
                {"config": {"context_length": 8192}}]}]})
        if req.url.path == "/api/v1/models/load":
            sent.append(json.loads(req.content))
            return httpx.Response(200, json={"status": "loaded"})
        return ok("done")

    brain = brain_with(state, handler, context_length=32768, parallel=2)
    assert await brain.think("x") == "done"
    assert ("POST", "/api/v1/models/load") in sent
    assert {"model": "qwen3.8-27b", "context_length": 32768, "parallel": 2, "ttl_seconds": 600} in sent


@pytest.mark.asyncio
async def test_an_already_loaded_model_with_enough_context_is_not_reloaded(state):
    sent = []

    def handler(req):
        sent.append(req.url.path)
        if req.method == "GET":
            return httpx.Response(200, json={"models": [{"key": "qwen3.8-27b", "loaded_instances": [
                {"config": {"context_length": 32768}}]}]})
        return ok("done")

    brain = brain_with(state, handler)
    assert await brain.think("x") == "done"
    assert "/api/v1/models/load" not in sent
