"""Offline runner tests: event capture, overflow, state, budget (WP3)."""
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)

from harness import runner  # noqa: E402
from common.validation import load_schema, validate  # noqa: E402


class FakeClient(object):
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def chat_turn(self, system, messages, tools=None, thinking=True, max_tokens=2500,
                  stream=True, json_mode=False):
        self.seen.append({"tools": bool(tools), "messages": messages})
        t = self.turns.pop(0) if self.turns else {"content": "", "tool_calls": []}
        return {
            "content": t.get("content", ""),
            "reasoning": "",
            "tool_calls": t.get("tool_calls", []),
            "finish": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "metrics": {"wall_s": 1.0, "first_event_s": 0.2, "first_visible_s": 0.3,
                        "first_tool_call_s": 0.25, "completion_s": 1.0, "decode_s": 0.8,
                        "prompt_tokens": 10, "completion_tokens": 5},
            "request_sha256": "deadbeef",
        }


def _tool_call(i, name, args):
    import json
    return {"id": "call_%d" % i, "name": name, "arguments": json.dumps(args)}


def test_runner_captures_overflow_and_state():
    turns = [
        {"tool_calls": [
            _tool_call(0, "move_message", {"message_id": 201, "target_folder": "Receipts"}),
            _tool_call(1, "flag_message", {"message_id": 201, "seen": True}),
            _tool_call(2, "create_folder", {"name": "Invoices"}),
            _tool_call(3, "read_message", {"message_id": 201}),
            _tool_call(4, "read_message", {"message_id": 202}),  # 5th > limit -> skipped
        ]},
        {"content": "Moved it."},
    ]
    client = FakeClient(turns)
    case = {"id": "asst_x", "class": "assistant", "sub": "tool_argument_correctness",
            "split": "dev", "family": "f", "user": "Move 201 to Receipts.",
            "expect": {}}
    a = runner.run_assistant(client, case, "run1", "m")
    calls = a["output"]["calls"]
    assert len(calls) == 5, calls
    assert [c["status"] for c in calls] == ["executed"] * 4 + ["skipped"], calls
    assert a["output"]["state"]["moved"] == [{"message_id": 201, "from": "INBOX",
                                              "to": "Receipts"}]
    assert "Invoices" in a["output"]["state"]["folders"]
    # skipped call still appears as a tool event
    assert any(e["status"] == "skipped" for e in a["tool_events"])
    assert a["metrics" if "metrics" in a else "wall_s"] == 1.0
    assert a["first_visible_s"] == 0.3 and a["first_tool_call_s"] == 0.25
    # runner attempts must satisfy the attempt schema end-to-end
    assert validate(a, load_schema("attempt.schema.json")) == []


def test_runner_uses_history():
    turns = [{"content": "The one about the grant."}]
    client = FakeClient(turns)
    case = {"id": "asst_h", "class": "assistant", "sub": "multi_turn", "split": "dev",
            "family": "f", "user": "Tell me more.", "expect": {},
            "history": [{"role": "user", "content": "Any messages from alice?"},
                        {"role": "assistant", "content": "I found a few."}]}
    a = runner.run_assistant(client, case, "run1", "m")
    first_messages = client.seen[0]["messages"]
    assert any(m["content"] == "Any messages from alice?" for m in first_messages)
    assert a["output"]["reply"] == "The one about the grant."


def test_transcript_budget_trips(monkeypatch=None):
    old = runner.TRANSCRIPT_BUDGET
    runner.TRANSCRIPT_BUDGET = 50  # tiny budget
    try:
        turns = [
            {"tool_calls": [_tool_call(0, "search_messages", {"query": "invoice"})]},
            {"content": "should not be reached with an empty terse search"},
        ]
        client = FakeClient(turns)
        case = {"id": "asst_b", "class": "assistant", "sub": "transcript_budget",
                "split": "dev", "family": "f", "user": "search", "expect": {}}
        a = runner.run_assistant(client, case, "run1", "m")
        assert a["output"]["budget_tripped"] is True
    finally:
        runner.TRANSCRIPT_BUDGET = old


def test_classification_parse():
    client = FakeClient([{"content": '{"category":"Action","needs_reply":true,'
                                     '"confidence":0.9,"summary":"s","reason":"r"}'}])
    case = {"id": "cls", "class": "classification", "sub": "normal_clear", "split": "dev",
            "family": "f", "user": "x", "expect": {}}
    a = runner.run_classification(client, case, "run1", "m")
    assert a["output"]["parsed"]["category"] == "Action"
    assert a["status"] == "ok"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
