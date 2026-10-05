"""Canonical SFT example construction (AC1/AC3/AC4/AC5).

Builds decision (classification) and workflow (bounded tool) examples from
authored inputs:

* a decision example uses the case's frozen rendered input and the authored gold;
  the summary/reason are authored from the visible subject and the category
  definition, never model-generated, and **no confidence** is supervised.
* a workflow example is an authored multi-turn scenario whose tool calls are
  executed host-side through the frozen sandbox and recorded turn by turn.

Nothing here calls a model or the network.
"""
from __future__ import annotations

import copy
import json
import os
import re

from ..common.hashing import hash_obj, sha256_text
from ..sandbox import Mailbox
from .messages import TRAIN_SCHEMA_VERSION, message
from .schema import gold_answer
from .taxonomies import (assert_public_clean, load_taxonomy, public_projection,
                         render_classifier_prompt, resolve_semantics)
from .tools_native import (CONTRACT_NOTE, CONTRACT_PARITY, native_tool_schemas,
                           execute_tool_call)
from .trace import TraceRecorder

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

DEFAULT_IDENTITIES = {
    "model": "unset",
    "model_revision": "",
    "prompt_revision": "mail-sft-prompt1",
    "schema_revision": TRAIN_SCHEMA_VERSION,
    "template_revision": "minicpm5-native-v1",
}


def load_workflow_scenarios(path=None):
    path = path or os.path.join(FIXTURES, "workflow_scenarios.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_dev_workflow_scenarios(path=None):
    """Independent development workflow scenarios (BASE vs ADAPTER eval)."""
    path = path or os.path.join(FIXTURES, "workflow_scenarios_dev.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _subject_of(user_text):
    m = re.search(r"^Subject:\s*(.*)$", user_text or "", re.M)
    return (m.group(1).strip() if m else "")


def _authored_reason(category, subject, definition):
    subject = subject or "the message"
    defn = definition or "its category definition"
    return ("%s fits the %s category: %s"
            % (subject[:80], category, defn[:120]))


def build_decision_example(case, gold, *, domain, identities=None, taxonomy=None):
    """Build one decision (classification) SFT example."""
    identities = dict(identities or DEFAULT_IDENTITIES)
    rendered = case.get("rendered_input") or {}
    system = rendered.get("system") or ""
    user = rendered.get("user") or ""
    answer = gold_answer(gold)
    category = answer.get("category")
    definition = ""
    if taxonomy is not None:
        for cat in taxonomy["categories"]:
            if cat.get("name") == category:
                definition = cat.get("definition") or ""
                break
    subject = _subject_of(user) or (case.get("family") or "")
    summary = subject[:120] or "An incoming message."
    decision = {
        "category": category,
        "needs_reply": answer.get("needs_reply"),
        "summary": summary,
        "reason": _authored_reason(category, subject, definition),
    }
    content = json.dumps(decision, ensure_ascii=False)
    messages = [
        message("system", system, supervised=False),
        message("user", user, supervised=False),
        message("assistant", content, supervised=True,
                think="Match the message to the single best authored category."),
    ]
    source_text = system + "\n" + user
    source_id = case.get("source_id") or ("src_%s" % hash_obj(
        {"case_id": case.get("case_id"), "system": system, "user": user})[:16])
    example = {
        "schema_version": TRAIN_SCHEMA_VERSION,
        "example_id": "sft_%s_%s" % (case.get("case_id"), domain),
        "task": "decision",
        "case_id": case.get("case_id"),
        "gold_id": case.get("gold_id") or (gold or {}).get("gold_id"),
        "source_id": source_id,
        "lineage_id": case.get("lineage_id") or case.get("scenario_id"),
        "generation_domain": domain,
        "input_profile": case.get("input_profile"),
        "identities": identities,
        "tools": [],
        "messages": messages,
        "decision": decision,
        "source_text": source_text,
        "metadata": {
            "review_status": "draft",
            "human_seal": False,
            "test_qualified": False,
            "generation_domain": domain,
            "provenance": "synthetic_authored",
        },
    }
    return example


def build_workflow_example(scenario, *, domain, identities=None,
                           trusted_system=None, tools=None):
    """Build one workflow SFT example by executing its authored tool calls."""
    identities = dict(identities or DEFAULT_IDENTITIES)
    trusted_system = trusted_system or scenario.get("trusted_system") or ""
    tools = tools if tools is not None else native_tool_schemas()
    sandbox = Mailbox(scenario.get("mailbox") or {},
                      permissions=scenario.get("permissions"),
                      case_id=scenario["id"])
    recorder = TraceRecorder(
        case_id=scenario["id"], task="workflow",
        source_id=scenario.get("source_id") or scenario["id"],
        lineage_id=scenario.get("lineage_id") or ("lin_%s" % scenario["id"]),
        model_identity=identities.get("model_identity") or identities,
        prompt_sha256=hash_obj({"system": trusted_system,
                                "request": scenario.get("request")}),
        schema_sha256=sha256_text(TRAIN_SCHEMA_VERSION),
        tools=tools, sandbox=sandbox)
    recorder.set_context(trusted_system, scenario.get("request") or "")

    for spec in scenario.get("turns") or []:
        if spec.get("role") == "user":
            recorder.messages.append(message("user", spec.get("content") or "",
                                             supervised=False))
            continue
        visible = copy.deepcopy(recorder.messages)
        before = sandbox.snapshot()
        raw_calls = spec.get("tool_calls") or []
        canonical_calls = []
        tool_results = []
        rejected = False
        for i, call in enumerate(raw_calls):
            raw = call.get("raw_arguments")
            if raw is None:
                raw = json.dumps(call.get("arguments") or {})
            canonical_calls.append({"id": call.get("id") or ("call%d" % i),
                                    "name": call.get("name"),
                                    "arguments": raw,
                                    "raw_arguments": raw})
        aidx = recorder.record_assistant(content=spec.get("content") or "",
                                         think=spec.get("think"),
                                         tool_calls=canonical_calls,
                                         rejected=False)
        events_before = len(sandbox.events)
        for i, call in enumerate(raw_calls):
            res = execute_tool_call(sandbox, call.get("name"),
                                    canonical_calls[i]["raw_arguments"],
                                    approve=False)
            tool_results.append({
                "name": call.get("name"),
                "raw_arguments": canonical_calls[i]["raw_arguments"],
                "rejected": res.get("rejected"),
                "self_approval_stripped": res.get("self_approval_stripped"),
            })
            if res.get("rejected"):
                rejected = True
                payload = {"trust": "untrusted",
                           "payload": {"error": res.get("rejected"),
                                       "detail": res.get("detail")}}
            else:
                payload = res["wrapped"]
            recorder.record_tool(call.get("name"), json.dumps(payload,
                                ensure_ascii=False, sort_keys=True),
                                canonical_calls[i]["id"])
        if rejected:
            recorder.bad_indices.append(aidx)
        events = sandbox.events[events_before:]
        finish = "tool_calls" if raw_calls else "stop"
        recorder.record_turn(
            model_visible_input=visible,
            completion=spec.get("content") or "",
            think=spec.get("think"),
            tool_calls=canonical_calls,
            tool_results=tool_results,
            events=events,
            finish_reason="rejected" if rejected else finish,
            usage={"prompt_tokens": max(1, len(visible)),
                   "completion_tokens": max(1, len(spec.get("content") or "") // 4),
                   "total_tokens": max(1, len(visible)
                                       + len(spec.get("content") or "") // 4)},
            state_snapshot=before,
            state_after=sandbox.snapshot(),
            rejected=rejected)
    trace = recorder.finalize()
    trace["usage_source"] = "synthetic_placeholder"
    example = {
        "schema_version": TRAIN_SCHEMA_VERSION,
        "example_id": "sft_%s_%s" % (scenario["id"], domain),
        "task": "workflow",
        "case_id": scenario["id"],
        "gold_id": "gold_%s" % scenario["id"],
        "source_id": scenario.get("source_id") or scenario["id"],
        "lineage_id": scenario.get("lineage_id") or ("lin_%s" % scenario["id"]),
        "generation_domain": domain,
        "input_profile": "workflow",
        "identities": identities,
        "tools": tools,
        "tool_contract": {"parity": CONTRACT_PARITY, "note": CONTRACT_NOTE},
        "messages": trace["messages"],
        "trace": trace,
        "metadata": {
            "review_status": "draft",
            "human_seal": False,
            "test_qualified": False,
            "generation_domain": domain,
            "provenance": "synthetic_authored",
        },
    }
    return example


def workflow_source_text(scenario):
    """All mailbox text of a scenario (for contamination checks)."""
    parts = []
    for msg in (scenario.get("mailbox") or {}).get("messages") or []:
        parts.append(str(msg.get("subject") or ""))
        parts.append(str(msg.get("body") or ""))
    return "\n".join(parts)
