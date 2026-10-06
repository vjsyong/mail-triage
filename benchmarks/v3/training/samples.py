"""Source records, decision examples and bounded tool dialogues (AC1/AC3/AC5).

A **source record** is a full synthetic email with an explicit corpus ``role`` and
generation ``domain`` plus an authored semantic intent and reply fact.  One source
record feeds both a classifier example and a bounded tool dialogue over the same
email under one ``source_id``/``lineage_id`` (cross-task lineage, M3).

The tool dialogue runs on ``NativeMailbox`` through the production-schema
``execute_tool_call``; tool calls may be authored (deterministic tests) or captured
from a real model rollout (the live path in ``training/mail``).
"""
from __future__ import annotations

import copy
import json
import os

from ..common.hashing import hash_obj, sha256_text
from . import minicpm
from .messages import TRAIN_SCHEMA_VERSION, apply_mask, message
from .native_state import NativeMailbox
from .schema import gold_answer
from .taxonomies import (assert_public_clean, load_taxonomy, public_projection,
                         render_classifier_prompt, render_workflow_prompt,
                         resolve_semantics)
from .tools_native import (CONTRACT_NOTE, CONTRACT_PARITY, execute_tool_call,
                           native_tool_schemas)
from .trace import TraceRecorder

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

SOURCE_SCHEMA = "mail-sft-source-0.1"
ROLE_TRAIN = "training"
ROLE_DEV = "development"

DEFAULT_IDENTITIES = {
    "model": "unset",
    "model_revision": "",
    "prompt_revision": "mail-sft-prompt2",
    "schema_revision": TRAIN_SCHEMA_VERSION,
    "template_revision": "minicpm5-native-v1",
    "taxonomy_revision": "mail-sft-tax1",
}


def load_workflow_scenarios(path=None):
    path = path or os.path.join(FIXTURES, "workflow_scenarios.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_dev_workflow_scenarios(path=None):
    path = path or os.path.join(FIXTURES, "workflow_scenarios_dev.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------- emails

def email_user_body(email, limit=1500):
    """The production native triage user body (headers + clipped body)."""
    body = (email.get("body") or "")[:limit]
    return ("From: %s\nTo: %s\nSubject: %s\nDate: %s\n\n%s"
            % (email.get("from_addr", ""), email.get("to_addr", ""),
               email.get("subject", ""), email.get("date", ""), body))


def mailbox_for_source(source, message_id=1, extra=None):
    """A synthetic mailbox holding the source email (integer id)."""
    email = source["email"]
    msg = {"id": int(message_id), "uid": int(message_id), "folder": "INBOX",
           "from_addr": email.get("from_addr", ""),
           "to_addr": email.get("to_addr", ""),
           "subject": email.get("subject", ""),
           "date": email.get("date", ""), "body": email.get("body", ""),
           "snippet": (email.get("body") or "")[:200], "status": "sorted"}
    messages = [msg] + [dict(e) for e in (extra or [])]
    return messages


# ----------------------------------------------------------------- decision

def _first_sentence(text, limit=120):
    text = (text or "").strip().replace("\n", " ")
    for sep in (". ", "! ", "? "):
        if sep in text:
            text = text.split(sep)[0] + sep.strip()
            break
    return text[:limit]


def build_decision_example(source, taxonomy, *, domain, identities=None):
    """One classifier example from a source record."""
    identities = dict(identities or DEFAULT_IDENTITIES)
    public = public_projection(taxonomy)
    intent = source.get("intent") or {}
    category = intent.get("category")
    definition = ""
    for c in taxonomy["categories"]:
        if c.get("name") == category:
            definition = c.get("definition") or ""
            break
    email = source["email"]
    summary = _first_sentence(email.get("body") or email.get("subject") or "")
    reason = "Matches %s: %s" % (category, definition or "its definition")
    decision = {"category": category, "needs_reply": intent.get("needs_reply"),
                "summary": summary, "reason": reason}
    system = render_classifier_prompt(public, owner=source.get("owner", ""))
    user = email_user_body(email)
    messages = [
        message("system", system, supervised=False),
        message("user", user, supervised=False),
        message("assistant", json.dumps(decision, ensure_ascii=False),
                supervised=True),
    ]
    return {
        "schema_version": TRAIN_SCHEMA_VERSION,
        "example_id": "dec_%s" % source["source_id"],
        "task": "decision",
        "role": source["role"],
        "generation_domain": domain,
        "source_id": source["source_id"],
        "lineage_id": source["lineage_id"],
        "family": source.get("family"),
        "identities": identities,
        "tools": [],
        "messages": apply_mask(messages),
        "source_text": system + "\n" + user,
        "source_email": dict(email),
        "metadata": {"review_status": "draft", "human_seal": False,
                     "test_qualified": False, "provenance": source.get("provenance")},
    }


# ----------------------------------------------------------------- dialogue

def build_dialogue_example(dialogue, taxonomy, *, domain, identities=None):
    """One bounded tool dialogue example from a captured/аuthored rollout."""
    identities = dict(identities or DEFAULT_IDENTITIES)
    public = public_projection(taxonomy)
    system = render_workflow_prompt(public)
    tools = native_tool_schemas()
    mailbox = NativeMailbox(dialogue.get("mailbox") or [],
                            rules=dialogue.get("rules"),
                            case_id=dialogue["dialogue_id"],
                            permissions=dialogue.get("permissions"))
    recorder = TraceRecorder(
        case_id=dialogue["dialogue_id"], task="workflow",
        source_id=dialogue["source_id"], lineage_id=dialogue["lineage_id"],
        model_identity=dialogue.get("model") or identities,
        prompt_sha256=hash_obj({"system": system, "request": dialogue.get("request")}),
        schema_sha256=sha256_text(TRAIN_SCHEMA_VERSION), tools=tools,
        sandbox=mailbox)
    recorder.set_context(system, dialogue.get("request") or "")

    for spec in dialogue.get("turns") or []:
        if spec.get("role") == "user":
            recorder.messages.append(message("user", spec.get("content") or "",
                                             supervised=False))
            continue
        visible = copy.deepcopy(recorder.messages)
        before = mailbox.snapshot()
        raw_calls = spec.get("tool_calls") or []
        canonical_calls = []
        for i, call in enumerate(raw_calls):
            raw = call.get("raw_arguments")
            if raw is None:
                raw = json.dumps(call.get("arguments") or {})
            canonical_calls.append({"id": call.get("id") or ("call%d" % i),
                                    "name": call.get("name"), "arguments": raw,
                                    "raw_arguments": raw})
        aidx = recorder.record_assistant(content=spec.get("content") or "",
                                         think=spec.get("think"),
                                         tool_calls=canonical_calls, rejected=False)
        events_before = len(mailbox.events)
        tool_results = []
        rejected = False
        for i, call in enumerate(raw_calls):
            outcome = execute_tool_call(mailbox, call.get("name"),
                                        canonical_calls[i]["raw_arguments"],
                                        host_approve=bool(dialogue.get("host_approve")))
            tool_results.append({
                "name": call.get("name"),
                "raw_arguments": canonical_calls[i]["raw_arguments"],
                "rejected": outcome.get("rejected"),
                "stripped_keys": outcome.get("stripped_keys")})
            if outcome.get("rejected"):
                rejected = True
                payload = {"error": outcome.get("rejected"),
                           "detail": outcome.get("detail")}
            else:
                payload = outcome.get("result") or {}
            recorder.record_tool(call.get("name"),
                                 json.dumps(payload, ensure_ascii=False,
                                            sort_keys=True),
                                 canonical_calls[i]["id"])
        if rejected:
            recorder.bad_indices.append(aidx)
        events = mailbox.events[events_before:]
        finish = spec.get("finish_reason") or ("tool_calls" if raw_calls else "stop")
        recorder.record_turn(
            model_visible_input=visible, completion=spec.get("content") or "",
            think=spec.get("think"), tool_calls=canonical_calls,
            tool_results=tool_results, events=events,
            finish_reason="rejected" if rejected else finish,
            usage=spec.get("usage") or {"prompt_tokens": 0,
                                        "completion_tokens": 0,
                                        "total_tokens": 0},
            state_snapshot=before, state_after=mailbox.snapshot(), rejected=rejected)
    trace = recorder.finalize()
    trace["usage_source"] = dialogue.get("usage_source", "model")
    return {
        "schema_version": TRAIN_SCHEMA_VERSION,
        "example_id": "dlg_%s" % dialogue["dialogue_id"],
        "task": "workflow",
        "role": dialogue["role"],
        "generation_domain": domain,
        "source_id": dialogue["source_id"],
        "lineage_id": dialogue["lineage_id"],
        "family": dialogue.get("family"),
        "identities": identities,
        "tools": tools,
        "tool_contract": {"parity": CONTRACT_PARITY, "note": CONTRACT_NOTE},
        "messages": trace["messages"],
        "trace": trace,
        "gold": dialogue.get("gold") or {},
        "mailbox": [dict(m) for m in (dialogue.get("mailbox") or [])],
        "source_email": dict(dialogue.get("source_email") or {}),
        "metadata": {"review_status": "draft", "human_seal": False,
                     "test_qualified": False,
                     "provenance": dialogue.get("provenance")},
    }


def workflow_source_text(dialogue):
    parts = []
    for msg in dialogue.get("mailbox") or []:
        parts.append(str(msg.get("subject") or ""))
        parts.append(str(msg.get("body") or ""))
    return "\n".join(parts)
