"""Bake-off for local models: the same work Harvey and Clara would hand them, scored the way Harvey scores itself.

    python scripts/model_bakeoff.py --model <lm-studio-model-id> [--skip writer,convo,tools]

Needs LM Studio's server running (`lms server start`). Loads nothing itself; the server loads the model on the
first request. Writes results to ~/harvey/data/bakeoff/<model>.json and <model>.md (drafts, for a human read).

What is measured
  speed    cold start (first token after the model was unloaded) and steady tokens/second
  writer   Harvey's real Writer drafts 8 three-email sequences; scored by Harvey's own rule checks, first try
  convo    the 20-scenario persona rubric through Harvey's real Conversationalist
  tools    Clara-style tool calling: right tool, right arguments, and no tool when none is needed
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harvey.brain import Brain  # noqa: E402
from harvey.config import BrainConfig, load_config  # noqa: E402
from harvey.models.conversation import Conversation, Message  # noqa: E402
from harvey.models.prospect import Prospect  # noqa: E402
from harvey.state import StateManager  # noqa: E402

BASE = "http://127.0.0.1:1235/v1"
OUT = ROOT / "data" / "bakeoff"


# ── prospects for the Writer ──

PROSPECTS = [   # fictional people and companies
    ("Alex", "Example", "Chief Executive Officer", "Acme Electric", "electrical contracting", 'VP of Technology'),
    ("Sam", "Sample", "Co-President", "Example Capital", "wealth management", "Head of AI Engineering & Automation"),
    ("Pat", "Placeholder", "Chief Data and AI Officer", "Northwind Lending", "small business lending", "SVP, Data & AI Strategy and Transformation"),
    ("Dana", "Doe", "Chief Operating Officer", "Example Mutual Insurance", "insurance", ""),
    ("Priya", "Test", "Founder and CEO", "Initech", "software", "Head of AI"),
    ("Marcus", "Bell", "Chief Executive Officer", "Bell Freight Systems", "logistics", ""),
    ("Nora", "Nobody", "VP Operations", "Globex Manufacturing", "manufacturing", "Director of Technology"),
    ("Greg", "Demo", "Chief Financial Officer", "Contoso Health Administration", "healthcare administration", ""),
]


async def bench_speed(model: str) -> dict:
    out = {}
    async with httpx.AsyncClient(timeout=600) as c:
        body = {"model": model, "messages": [{"role": "user", "content": "Write two plain sentences about cold email."}],
                "max_tokens": 60, "temperature": 0.2, "ttl": 600}
        t = time.monotonic()
        r = await c.post(f"{BASE}/chat/completions", json=body)
        r.raise_for_status()
        out["first_request_seconds"] = round(time.monotonic() - t, 1)     # includes the load when it was unloaded
        body["messages"] = [{"role": "user", "content": "Explain, in about 250 words, how a fractional CTO differs from a full-time CTO."}]
        body["max_tokens"] = 400
        t = time.monotonic()
        r = await c.post(f"{BASE}/chat/completions", json=body)
        r.raise_for_status()
        dt = time.monotonic() - t
        tokens = (r.json().get("usage") or {}).get("completion_tokens") or 0
        out["tokens_per_second"] = round(tokens / dt, 1) if dt else 0
        out["sample_tokens"] = tokens
    return out


async def bench_writer(brain: Brain, config) -> tuple[dict, str]:
    from harvey.agents.writer import Writer
    from harvey.persona import load_persona_context

    with tempfile.TemporaryDirectory() as tmp:
        state = StateManager(os.path.join(tmp, "b.db"))
        await state.init_db()
        calls = {"n": 0}
        real = brain.think_json

        async def counted(*a, **k):
            calls["n"] += 1
            return await real(*a, **k)
        brain.think_json = counted
        writer = Writer(brain, state, config, None)
        writer.persona_text, missing = load_persona_context()
        if missing:
            raise SystemExit(f"persona files missing: {missing}")
        writer.skills = brain.load_skills_for_agent("writer")
        rows, notes = [], []
        for first, last, title, company, industry, posting in PROSPECTS:
            pid = await state.add_prospect(Prospect(
                first_name=first, last_name=last, title=title, company=company, industry=industry,
                email=f"{first.lower()}@{company.split()[0].lower()}.com", email_status="verified", status="new"))
            prospect = await state.get_prospect(pid)

            async def posting_facts(_p, _posting=posting, _company=company):
                return [f'- Public job posting: {_company} is hiring "{_posting}" (posted on a public job board: https://x.example/1)'] if _posting else []
            writer._posting_facts = posting_facts
            calls["n"] = 0
            t = time.monotonic()
            seq = await writer._draft_assistant_sequence(prospect)
            rows.append({"who": f"{first} {last}", "ok": bool(seq), "model_calls": calls["n"],
                         "seconds": round(time.monotonic() - t, 1)})
            notes.append(f"### {first} {last}, {title}, {company} ({'posting: ' + posting if posting else 'no posting'})\n"
                         + ("\n\n".join(f"**{e['subject']}**\n\n{e['body']}" for e in seq) if seq else "(failed Harvey's rules twice: nothing would be sent)"))
        brain.think_json = real
    ok = sum(r["ok"] for r in rows)
    first_try = sum(1 for r in rows if r["ok"] and r["model_calls"] == 1)
    return ({"drafted": ok, "of": len(rows), "passed_first_try": first_try,
             "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 1), "rows": rows},
            "\n\n".join(notes))


async def bench_convo(brain: Brain, config) -> tuple[dict, str]:
    from harvey.agents.conversationalist import Conversationalist
    import harvey.persona as persona_mod  # noqa: F401
    from tests.persona_eval.rubric import SCENARIOS, check_reply

    with tempfile.TemporaryDirectory() as tmp:
        state = StateManager(os.path.join(tmp, "c.db"))
        await state.init_db()
        convo_agent = Conversationalist(brain, state, config)
        results, notes = [], []
        for s in SCENARIOS:
            pid = await state.add_prospect(Prospect(
                first_name="Casey", last_name=f"Test{s['id']}", title="Chief Executive Officer", company="Example Co",
                email=f"j{s['id']}@example-co.com", email_status="verified", status="contacted"))
            prospect = await state.get_prospect(pid)
            convo = Conversation(id=f"bake{s['id']}", prospect_id=pid, channel=s["channel"])
            convo.thread = [Message(sender="harvey", content="Example Co is hiring a Head of AI. Who owns that work today?"),
                            Message(sender="prospect", content=s["message"])]
            t = time.monotonic()
            try:
                reply = await convo_agent.respond(s["message"], prospect, convo, s["channel"])
                text, handoff, fixed = reply.text, reply.handoff, reply.fixed
            except Exception as e:  # a model that breaks the pipeline fails the scenario
                text, handoff, fixed = "", False, False
                notes.append(f"scenario {s['id']} raised {type(e).__name__}: {e}")
            problems = check_reply(s, text) if s["action"] != "stop" else ([] if not text else ["must not reply"])
            if s["action"] == "escalate" and not handoff:
                problems.append("did not hand the thread to the owner")
            results.append({"id": s["id"], "name": s["name"], "action": s["action"], "fixed_wording": fixed,
                            "problems": problems, "seconds": round(time.monotonic() - t, 1)})
            notes.append(f"### {s['id']} {s['name']} ({s['channel']}, expects {s['action']}"
                         f"{', fixed wording' if fixed else ''})\n> {s['message']}\n\n{text or '(no reply)'}\n\n"
                         + ("PASS" if not problems else "FAIL: " + "; ".join(problems)))
    model_written = [r for r in results if not r["fixed_wording"]]
    return ({"passed": sum(1 for r in results if not r["problems"]), "of": len(results),
             "model_written": len(model_written),
             "model_written_passed": sum(1 for r in model_written if not r["problems"]),
             "failures": [{"id": r["id"], "name": r["name"], "problems": r["problems"]} for r in results if r["problems"]]},
            "\n\n".join(notes))


# ── Clara-style tool calling ──

TOOLS = [
    {"type": "function", "function": {"name": "calendar_list", "description": "List the owner's calendar events in a date range.",
     "parameters": {"type": "object", "properties": {"start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"}}, "required": ["start_date", "end_date"]}}},
    {"type": "function", "function": {"name": "calendar_create", "description": "Create a calendar event.",
     "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "start": {"type": "string", "description": "ISO 8601 local time"},
                    "duration_minutes": {"type": "integer"}, "attendees": {"type": "array", "items": {"type": "string"}}},
                    "required": ["title", "start", "duration_minutes"]}}},
    {"type": "function", "function": {"name": "task_create", "description": "Add a task to the owner's task list.",
     "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "due_date": {"type": "string", "description": "YYYY-MM-DD"}},
                    "required": ["title"]}}},
    {"type": "function", "function": {"name": "contact_search", "description": "Find a contact by name, company or phone number.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "send_sms", "description": "Text a phone number. Queues for the owner's approval.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string", "description": "E.164 phone number"}, "body": {"type": "string"}},
                    "required": ["to", "body"]}}},
    {"type": "function", "function": {"name": "email_draft", "description": "Draft an email for the owner's approval.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["to", "subject", "body"]}}},
]
SYSTEM = ("You are Clara, the owner's assistant. Today is Friday 2026-11-06. The owner's time zone is America/New_York. "
          "Use a tool only when the request needs one. If a required detail is missing, ask for it instead of guessing. "
          "Never invent phone numbers or email addresses.")

# (message, expected calls [(tool, {arg: regex})], or [] for "no tool call")
TOOL_CASES = [
    ("What's on my calendar Monday?", [("calendar_list", {"start_date": r"2026-11-09", "end_date": r"2026-11-09"})]),
    ("Anything on my calendar next week?", [("calendar_list", {"start_date": r"2026-11-(09|1[0-5])", "end_date": r"2026-11-1[3-6]"})]),
    ("Put a 30 minute call with jordan@example.com on the calendar tomorrow at 3pm called Kickoff.",
     [("calendar_create", {"title": r"(?i)kickoff", "start": r"2026-11-07T15:00", "duration_minutes": r"^30$", "attendees": r"jordan@example\.com"})]),
    ("Remind me to renew the domain by the 20th.", [("task_create", {"title": r"(?i)domain", "due_date": r"2026-11-20"})]),
    ("Add a task: send the Acme Co invoice.", [("task_create", {"title": r"(?i)invoice"})]),
    ("Who is Marcus at Bell Freight?", [("contact_search", {"query": r"(?i)marcus|bell"})]),
    ("Text 813-555-0142 that I'm running ten minutes late.", [("send_sms", {"to": r"\+?1?8135550142", "body": r"(?i)late|10|ten"})]),
    ("Email dana@example.org a short note saying thanks for the call today. Subject: Thanks.",
     [("email_draft", {"to": r"dana@example\.org", "subject": r"(?i)thanks", "body": r"(?i)thank"})]),
    ("Text Marcus that the meeting moved to 4.", []),   # no number: must ask or search, not invent one (a search is acceptable below)
    ("What is 17 times 23?", []),
    ("Tell me a joke.", []),
    ("Schedule lunch with Priya.", []),                 # missing day and time: ask
    ("Check my calendar for Tuesday and remind me to call the bank that day.",
     [("calendar_list", {"start_date": r"2026-11-10"}), ("task_create", {"title": r"(?i)bank", "due_date": r"2026-11-10"})]),
    ("Delete all my contacts.", []),                    # no such tool: must not pick one
]


async def bench_tools(model: str, reasoning: str = "none") -> tuple[dict, str]:
    results, notes = [], []
    async with httpx.AsyncClient(timeout=600) as c:
        for i, (msg, expected) in enumerate(TOOL_CASES, 1):
            body = {"model": model, "temperature": 0.1, "max_tokens": 1500, "ttl": 600, "tools": TOOLS,
                    **({"reasoning_effort": reasoning} if reasoning != "default" else {}),
                    "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}]}
            t = time.monotonic()
            r = await c.post(f"{BASE}/chat/completions", json=body)
            if r.status_code >= 400:
                results.append({"n": i, "ok": False, "why": f"HTTP {r.status_code}"})
                continue
            message = r.json()["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            got = []
            for tc in calls:
                try:
                    got.append((tc["function"]["name"], json.loads(tc["function"].get("arguments") or "{}")))
                except ValueError:
                    got.append((tc["function"]["name"], None))
            ok, why = True, ""
            if not expected:
                invented = [g for g in got if g[0] in ("send_sms", "email_draft", "calendar_create", "task_create")]
                if invented:
                    ok, why = False, f"acted when it should not have: {[g[0] for g in invented]}"
            else:
                names = [g[0] for g in got]
                if sorted(names) != sorted(e[0] for e in expected):
                    ok, why = False, f"expected {[e[0] for e in expected]}, got {names}"
                else:
                    for tool, patterns in expected:
                        args = next((a for n, a in got if n == tool), None)
                        if args is None:
                            ok, why = False, f"{tool}: unreadable arguments"
                            break
                        for key, pat in patterns.items():
                            val = args.get(key)
                            if val is None or not re.search(pat, str(val if not isinstance(val, list) else ",".join(map(str, val)))):
                                ok, why = False, f"{tool}.{key}={val!r} does not match {pat}"
                                break
                        if not ok:
                            break
            results.append({"n": i, "ok": ok, "why": why, "seconds": round(time.monotonic() - t, 1)})
            notes.append(f"{i}. {msg}\n   -> {got or (message.get('content') or '')[:160]!r}\n   {'PASS' if ok else 'FAIL: ' + why}")
    return ({"passed": sum(1 for r in results if r["ok"]), "of": len(results),
             "failures": [r for r in results if not r["ok"]]}, "\n".join(notes))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--skip", default="")
    ap.add_argument("--reasoning", default="none", help="none | low | medium | high | default")
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    OUT.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", args.model) + f"__{args.reasoning}"

    # Start every model from the same clean state: only it loaded, with Harvey's context size.
    subprocess.run([str(Path.home() / ".lmstudio/bin/lms"), "unload", "--all"], capture_output=True)
    load = httpx.post("http://127.0.0.1:1235/api/v1/models/load", timeout=600,
                      json={"model": args.model, "context_length": 32768, "parallel": 2, "ttl_seconds": 900})
    print("loaded:", load.json().get("status"), load.json().get("load_time_seconds"), "s")

    config = load_config()
    brain = Brain(StateManager(os.path.join(tempfile.mkdtemp(), "b.db")),
                  BrainConfig(provider="local", model=args.model, max_tokens=4000, local_reasoning=args.reasoning))
    await brain.state.init_db()
    result, report = {"model": args.model, "reasoning": args.reasoning}, [f"# Bake-off: {args.model} (reasoning: {args.reasoning})\n"]

    if "speed" not in skip:
        result["speed"] = await bench_speed(args.model)
        print("speed", result["speed"])
    if "writer" not in skip:
        result["writer"], text = await bench_writer(brain, config)
        report.append("## Writer drafts\n\n" + text)
        print("writer", {k: v for k, v in result["writer"].items() if k != "rows"})
    if "convo" not in skip:
        result["convo"], text = await bench_convo(brain, config)
        report.append("## Conversations\n\n" + text)
        print("convo", {k: v for k, v in result["convo"].items() if k != "failures"})
    if "tools" not in skip:
        result["tools"], text = await bench_tools(args.model, args.reasoning)
        report.append("## Tool calling\n\n" + text)
        print("tools", {k: v for k, v in result["tools"].items() if k != "failures"})

    (OUT / f"{slug}.json").write_text(json.dumps(result, indent=2))
    (OUT / f"{slug}.md").write_text("\n\n".join(report))
    print(f"\nWrote {OUT / (slug + '.json')} and .md")


if __name__ == "__main__":
    asyncio.run(main())
