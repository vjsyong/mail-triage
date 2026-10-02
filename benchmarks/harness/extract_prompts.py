#!/usr/bin/env python3
"""Extract the EXACT production prompt strings from the app's engine.py.

Run with the app venv from the repo worktree root (engine.py @ master 5374d26):
    cd ~/mail-triage-bench && DATA_DIR=/tmp/benchextract IMAP_USER=seanyong@ust.hk \
        ~/mail-triage/.venv/bin/python benchmarks/harness/extract_prompts.py

Writes benchmarks/harness/prompts.json — used by the benchmark runner so the
same strings production uses are replayed against every model.
"""
import json
import os
import sys

os.environ.setdefault("DATA_DIR", "/tmp/benchextract")
os.environ.setdefault("IMAP_USER", "seanyong@ust.hk")
sys.path.insert(0, os.path.abspath("."))

import engine  # noqa: E402

engine.store.init_db()  # scratch DATA_DIR — creates empty tables for settings reads

OUT = os.path.join(os.path.dirname(__file__), "prompts.json")
captured = {"classify": [], "draft": []}

_real_chat = engine.LLMClient._chat


def fake_chat(self, system, user, json_mode=True, history=None, max_tokens=None,
              full=False, thinking=False):
    captured.setdefault("calls", []).append({
        "system": system, "user": user, "json_mode": bool(json_mode),
        "max_tokens": max_tokens, "thinking": bool(thinking)})
    if "category" in system:
        return '{"category":"Action","needs_reply":true,"confidence":0.9,"summary":"s","reason":"r"}'
    return "REPLY BODY"


engine.LLMClient._chat = fake_chat

CATS = ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"]
marker_msg = {
    "from_addr": "FROMMARKER@example.com", "to_addr": "TOMARKER@example.com",
    "subject": "SUBJMARKER", "date": "DATEMARKER",
    "snippet": "BODYMARKER" + "x" * 1600,
}
try:
    engine.LLMClient().classify(marker_msg, CATS, "Sean")
except Exception as exc:
    print("classify capture failed:", exc)
try:
    engine.LLMClient().draft_reply(marker_msg, "DRAFTBODYMARKER" + "y" * 100,
                                   None, {"my_name": "Sean"}, "")
except Exception as exc:
    print("draft capture failed:", exc)

caps = captured.get("calls", [])
classify_call = next((c for c in caps if "triage incoming email" in c["system"]
                      and "FROMMARKER" in c["user"]), None)
draft_call = next((c for c in caps if c["user"].startswith("Original message")), None)

out = {
    "extracted_from": "engine.py @ master 5374d26 (mail-triage worktree, 2026-10-02)",
    "classify_system": classify_call["system"] if classify_call else None,
    "classify_user": classify_call["user"] if classify_call else None,
    "classify_payload": {"temperature": 0, "json_mode": True, "thinking": True,
                          "max_tokens": 4096, "max_tokens_retry": 8192,
                          "note": "one 8192 retry when finish_reason==length and no JSON"},
    "draft_system": draft_call["system"] if draft_call else None,
    "draft_user": draft_call["user"] if draft_call else None,
    "assistant_system_template": engine.ASSISTANT_SYSTEM,
    "assistant_tools": engine.ASSISTANT_TOOLS,
    "assistant_loop": {"MAX_STEPS": engine.AssistantAgent.MAX_STEPS,
                        "MAX_CALLS_PER_TURN": engine.AssistantAgent.MAX_CALLS_PER_TURN,
                        "RESULT_CHARS": engine.AssistantAgent.RESULT_CHARS,
                        "TRANSCRIPT_BUDGET": engine.AssistantAgent.TRANSCRIPT_BUDGET,
                        "stream": True, "temperature": 0, "max_tokens": 2500,
                        "repetition_penalty": 1.05, "thinking": True},
    "learn_system": engine.LEARN_SYSTEM,
    "example_draft_system": engine.EXAMPLE_DRAFT_SYSTEM,
    "agent_caps": getattr(engine, "AGENT_CAPS", None),
    "permissions_text_default": engine.agent_permissions_text(),
    "assistant_context_format": engine._assistant_context(),
    "summarize_thoughts_system": None,  # filled from a direct call below
}

# summarize_thoughts prompt (inline in AssistantAgent._summarize_thoughts)
captured["calls"].clear()
try:
    agent = object.__new__(engine.AssistantAgent)
    engine.AssistantAgent._summarize_thoughts(agent, "some reasoning text about rules")
except Exception as exc:
    print("summarize capture failed:", exc)
sc = next((c for c in captured.get("calls", [])), None)
if sc:
    out["summarize_thoughts_system"] = sc["system"]
    out["summarize_thoughts_payload"] = {"max_tokens": 60, "json_mode": False}

engine.LLMClient._chat = _real_chat

with open(OUT, "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("wrote", OUT)
print("classify_system captured:", bool(out["classify_system"]))
print("classify_user captured:", bool(out["classify_user"]))
print("draft_system captured:", bool(out["draft_system"]))
print("tools:", len(out["assistant_tools"] or []))
assert out["classify_system"] and out["classify_user"] and out["draft_system"]
assert "FROMMARKER" in json.dumps(out["classify_user"])
