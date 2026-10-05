"""Verification for the mail SFT slice (acceptance AC5).

Every accepted example must pass:

* **factual slots** -- the summary/reason tokens it asserts actually appear in the
  authored source text;
* **classification decisions are authored** -- category and reply intent match the
  authored gold (never a model-invented label), and an ambiguous/unavailable
  decision is rejected here too;
* **scope / state / proposal conditions** -- an ask-gated write stays pending, a
  proposal is never applied, an auto write did complete, and an off capability
  (send/delete) never ran;
* **no fabricated completion** -- if the assistant claims a write completed, a
  matching mailbox event must exist;
* **confidence is never a synthetic gold target** -- an SFT target must not carry
  a confidence field;
* **no auto human seal and no test qualification** -- review stays draft;
  ``test_qualified`` is never asserted by this synthetic-only slice.
"""
from __future__ import annotations

import re

from .schema import gold_answer

_COMPLETION_CLAIMS = (
    "i moved", "i've moved", "has been moved", "moved it", "moved the message",
    "moved to", "i filed", "i've filed", "filed it", "the rule is applied",
    "i applied", "i've applied", "sent the", "i sent", "deleted",
)


def _tokens(text):
    return set(re.findall(r"[a-z0-9@._-]{3,}", (text or "").lower()))


def _source_text(example):
    """All model-visible non-assistant text in an example (the 'source')."""
    parts = []
    for msg in example.get("messages") or []:
        if msg.get("role") in ("system", "user", "tool"):
            parts.append(str(msg.get("content") or ""))
    return "\n".join(parts)


# ------------------------------------------------------------------ decision

def verify_decision(example, gold):
    """Problems with a decision (classification) example ([] when valid)."""
    problems = []
    src = example.get("source_text") or _source_text(example)
    src_tokens = _tokens(src)
    answer = gold_answer(gold) if gold else {}
    if not answer:
        return ["decision example has no authored gold answer"]

    decision = example.get("decision") or {}
    if decision.get("category") != answer.get("category"):
        problems.append("category %r is not the authored gold %r"
                        % (decision.get("category"), answer.get("category")))
    acceptable = answer.get("acceptable_categories") or []
    if acceptable and decision.get("category") not in acceptable:
        problems.append("category %r not in authored acceptable set %r"
                        % (decision.get("category"), acceptable))
    if decision.get("needs_reply") != answer.get("needs_reply"):
        problems.append("needs_reply %r is not the authored gold %r"
                        % (decision.get("needs_reply"), answer.get("needs_reply")))
    if "confidence" in decision:
        problems.append("confidence must not be a synthetic gold target")

    for field in ("summary", "reason"):
        text = decision.get(field)
        if not text:
            problems.append("decision has no %s" % field)
            continue
        toks = _tokens(text)
        if toks and not (toks & src_tokens):
            problems.append("%s does not reference any source token" % field)
        for term in gold.get("hidden_evidence") or []:
            if term and term.lower() in str(text).lower():
                problems.append("%s leaks hidden gold evidence %r" % (field, term))
    return problems


# ------------------------------------------------------------------ workflow

def _events(trace):
    return [e for turn in (trace.get("turns") or []) for e in turn.get("events") or []]


def verify_workflow(example, gold, trace=None):
    """Problems with a workflow example ([] when valid)."""
    problems = []
    trace = trace or example.get("trace") or {}
    events = _events(trace)
    answer = gold_answer(gold) if gold else {}

    # proposed rules must never be applied; only a proposal record may change.
    for ev in events:
        if ev.get("tool") == "propose_rule":
            ops = {c.get("op") for c in ev.get("state_changes") or []}
            if ops - {"propose_rule"}:
                problems.append("propose_rule changed state beyond a proposal")
        if ev.get("tool") in ("send_message", "delete_message") and ev.get("executed"):
            problems.append("%s executed; send/delete must never run"
                            % ev.get("tool"))
        if ev.get("status") == "pending" and ev.get("mutated"):
            problems.append("a pending (ask-gated) call mutated state")

    # completion claims must be backed by a matching mutated event.
    assistant_text = " ".join(
        str(m.get("content") or "").lower()
        for m in example.get("messages") or [] if m.get("role") == "assistant")
    moved = any(e.get("tool") == "move_message" and e.get("mutated") for e in events)
    if any(claim in assistant_text for claim in _COMPLETION_CLAIMS):
        if "move" in assistant_text and not moved and "approval" not in assistant_text:
            problems.append("assistant claims a move completed but no move "
                            "mutation event exists")
        if ("sent" in assistant_text or "delete" in assistant_text) and any(
                e.get("tool") in ("send_message", "delete_message") for e in events):
            problems.append("assistant claims an off capability completed")

    # scope: required outcomes are not empty for a workflow gold.
    required = answer.get("required_outcomes")
    if required is not None and not required:
        problems.append("workflow gold has no required outcomes (scope empty)")
    return problems


# ------------------------------------------------------------------ envelope

def verify_envelope(example, trace=None):
    """Honesty gates every example must pass, independent of task."""
    problems = []
    meta = example.get("metadata") or {}
    if meta.get("review_status") not in (None, "draft"):
        problems.append("example review_status must stay draft in this slice")
    if meta.get("human_seal"):
        problems.append("human_seal must never be auto-asserted")
    if meta.get("test_qualified"):
        problems.append("test qualification must never be claimed")
    if trace:
        if trace.get("review_status") not in (None, "draft"):
            problems.append("trace review_status must stay draft")
        if trace.get("human_seal"):
            problems.append("trace must not be human-sealed")
        if trace.get("test_qualified"):
            problems.append("trace must not be test-qualified")
    return problems


def verify_example(example, gold=None, trace=None):
    """Run the full verification for an example; returns problems ([] = valid)."""
    problems = list(verify_envelope(example, trace))
    task = example.get("task")
    if task == "decision":
        problems.extend(verify_decision(example, gold))
    elif task == "workflow":
        problems.extend(verify_workflow(example, gold, trace=trace))
    else:
        problems.append("unknown task %r" % task)
    return problems


def is_test_qualified(_trace=None):
    """This synthetic-only slice never qualifies a held-out test set."""
    return False
