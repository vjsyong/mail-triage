"""Per-turn trace persistence for the mail SFT slice (acceptance AC4).

Records exactly what the model saw and emitted at each turn:

* the **full** model-visible input (every prior turn, not just a summary);
* the complete completion text, ``think`` reasoning, parsed tool calls and their
  **raw** argument strings;
* each tool response (untrusted wrapper), the sandbox events, the finish reason,
  per-turn usage and the aggregate usage;
* a mailbox state snapshot per turn and the final state;
* the model/prompt/schema/template identities.

A malformed tool call is recorded and rejected **before** execution; it is never
silently turned into ``{}`` and its assistant turn is masked out of training.

Export reconstructs context from the recorded per-turn input, so nothing depends
on prior tool history that was not actually visible.
"""
from __future__ import annotations

import copy

from ..common.hashing import canonical, sha256_text
from . import minicpm
from .messages import (TRAIN_SCHEMA_VERSION, apply_mask, assistant_message,
                       canonical_valid, message, tool_message)


class TraceError(ValueError):
    """Raised when a trace cannot be built or violates its contract."""


def _usage_add(total, turn_usage):
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = (turn_usage or {}).get(key)
        if isinstance(val, (int, float)):
            total[key] = total.get(key, 0) + int(val)
    return total


def _usage_of_last(total):
    return {"prompt_tokens": total.get("prompt_tokens", 0),
            "completion_tokens": total.get("completion_tokens", 0),
            "total_tokens": total.get("total_tokens", 0)}


class TraceRecorder(object):
    """Collects one multi-turn trace with exact per-turn evidence."""

    def __init__(self, *, case_id, task, source_id="", lineage_id="",
                 model_identity=None, prompt_sha256="", schema_sha256="",
                 tools=None, sandbox=None):
        self.case_id = case_id
        self.task = task
        self.source_id = source_id
        self.lineage_id = lineage_id
        self.model_identity = dict(model_identity or {})
        self.tools = list(tools or [])
        self.tools_sha256 = sha256_text(_canonical(self.tools))
        self.prompt_sha256 = prompt_sha256
        self.schema_sha256 = schema_sha256 or sha256_text(TRAIN_SCHEMA_VERSION)
        self.template_sha256 = sha256_text(_TEMPLATE_FINGERPRINT)
        self.sandbox = sandbox
        self.messages = []
        self.turns = []
        self.total_usage = {}
        self.bad_indices = []

    # --------------------------------------------------------------- building
    def set_context(self, system_content, user_content):
        self.messages = [message("system", system_content, supervised=False),
                         message("user", user_content, supervised=False)]
        return self.messages

    def _state(self):
        return self.sandbox.snapshot() if self.sandbox is not None else None

    def record_assistant(self, *, content="", think=None, tool_calls=None,
                         rejected=False):
        """Append an assistant turn and return its index in ``self.messages``."""
        idx = len(self.messages)
        self.messages.append(assistant_message(
            content=content, think=think, tool_calls=tool_calls,
            supervised=not rejected, rejected=rejected))
        if rejected:
            self.bad_indices.append(idx)
        return idx

    def record_tool(self, name, wrapped_result, tool_call_id):
        self.messages.append(tool_message(name, wrapped_result, tool_call_id))
        return len(self.messages) - 1

    def record_turn(self, *, model_visible_input, completion, think, tool_calls,
                    tool_results, events, finish_reason, usage, state_snapshot,
                    state_after, rejected=False):
        """Persist one complete turn cycle with its exact evidence."""
        turn = {
            "index": len(self.turns),
            "model_visible_input": copy.deepcopy(model_visible_input),
            "completion": completion,
            "think": think,
            "tool_calls": copy.deepcopy(tool_calls or []),
            "tool_results": copy.deepcopy(tool_results or []),
            "events": copy.deepcopy(events or []),
            "finish_reason": finish_reason,
            "usage": dict(usage or {}),
            "state_snapshot": copy.deepcopy(state_snapshot),
            "state_after": copy.deepcopy(state_after),
            "rejected": bool(rejected),
        }
        _usage_add(self.total_usage, usage)
        self.turns.append(turn)
        return turn

    # ------------------------------------------------------------- finalise
    def finalize(self):
        masked = apply_mask(self.messages, self.bad_indices)
        problems = canonical_valid(masked)
        trace = {
            "schema_version": TRAIN_SCHEMA_VERSION,
            "trace_id": "trace_%s" % self.case_id,
            "case_id": self.case_id,
            "task": self.task,
            "source_id": self.source_id,
            "lineage_id": self.lineage_id,
            "identities": {
                "model": dict(self.model_identity),
                "prompt_sha256": self.prompt_sha256,
                "schema_sha256": self.schema_sha256,
                "tools_sha256": self.tools_sha256,
                "template_sha256": self.template_sha256,
            },
            "turns": self.turns,
            "usage": _usage_of_last(self.total_usage),
            "messages": masked,
            "state_final": (self.sandbox.final_state()
                            if self.sandbox is not None else None),
            "review_status": "draft",
            "human_seal": False,
            "test_qualified": False,
        }
        if problems:
            raise TraceError("finalized trace is not a valid message stream: %s"
                             % "; ".join(problems))
        return trace


def reconstruct_context(trace, upto_turn):
    """Return the exact model-visible input recorded for turn ``upto_turn``.

    This is the recorded per-turn snapshot -- never a hindsight reconstruction
    that could include tool history the model never saw.
    """
    turns = trace.get("turns") or []
    if not 0 <= upto_turn < len(turns):
        raise TraceError("turn %r out of range" % upto_turn)
    return copy.deepcopy(turns[upto_turn]["model_visible_input"])


def _canonical(obj):
    return canonical(obj)


_TEMPLATE_FINGERPRINT = "minicpm5-xml-tools-think-v1:%s" % (
    "tool_response" in minicpm.render_messages(
        [{"role": "user", "content": "x"},
         {"role": "tool", "content": "y", "tool_call_id": "c1"}],
        tools=None) or "")
