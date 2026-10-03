"""Shared tool-call loop for workflow adapters (WP4).

The loop is deliberately tiny and transport-agnostic: the adapter supplies a
``turn_fn(system, messages, tools) -> {"content", "tool_calls", "usage",
"request_sha256"}`` and the loop drives the sandbox.  Every emitted call is
executed (or recorded as ``skipped`` when it overflows the per-turn budget), so
nothing a model emitted is invisible to scoring, and a skipped call can never
complete a task.
"""
from __future__ import annotations

import json

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_CALLS_PER_TURN = 4


def run_tool_loop(turn_fn, sandbox, system, user, tools,
                  max_steps=DEFAULT_MAX_STEPS,
                  max_calls_per_turn=DEFAULT_MAX_CALLS_PER_TURN):
    """Drive ``sandbox`` until the model stops calling tools or the budget ends."""
    messages = [{"role": "user", "content": user}]
    raw_turns = []
    steps = 0
    budget_exhausted = False
    reply = ""
    usage = {}
    request_hashes = []
    error = None
    try:
        while steps < max_steps:
            steps += 1
            res = turn_fn(system, messages, tools)
            if res.get("request_sha256"):
                request_hashes.append(res["request_sha256"])
            usage = res.get("usage") or usage
            raw_turns.append(res.get("content") or "")
            calls = res.get("tool_calls") or []
            if not calls:
                reply = res.get("content") or ""
                break
            messages.append({
                "role": "assistant",
                "content": res.get("content") or None,
                "tool_calls": [{"id": c.get("id") or ("call%d" % i),
                                "type": "function",
                                "function": {"name": c.get("name"),
                                             "arguments": c.get("arguments", "{}")}}
                               for i, c in enumerate(calls)],
            })
            for i, call in enumerate(calls):
                status = "executed" if i < max_calls_per_turn else "skipped"
                args = call.get("args")
                if args is None:
                    args = _json_args(call.get("arguments"))
                result = sandbox.execute(call.get("name"), args, status=status)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or ("call%d" % i),
                    "name": call.get("name"),
                    "content": json.dumps(result, sort_keys=True)[:4000],
                })
        else:
            budget_exhausted = True
    except Exception as exc:  # noqa: BLE001 - surfaced as a model/infra error
        error = repr(exc)
    return {
        "reply": reply,
        "steps": steps,
        "usage": usage,
        "raw_turns": raw_turns,
        "tool_events": list(sandbox.events),
        "budget_exhausted": budget_exhausted,
        "request_sha256": request_hashes[-1] if request_hashes else "",
        "error": error,
    }


def _json_args(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}
