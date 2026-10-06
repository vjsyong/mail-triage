"""Verification for the mail SFT slice (AC5/M1).

* ``verify_decision`` parses the **actual supervised assistant JSON** (never a
  duplicated ``decision`` field), validates enums/types/needs_reply and gold
  observability, and rejects factual contradictions or fabrications (numbers or
  significant words absent from the source and the taxonomy).
* ``verify_dialogue`` compares the real final mailbox state and events against the
  authored ``expected_state`` / required / forbidden outcomes / assertions,
  including positive **and** negative rule-match scope, and rejects a claimed
  completion with no matching event (an "approval" word elsewhere does not
  excuse it).
* ``verify_envelope`` enforces the honesty gates (draft only, no seal, no test
  qualification).
"""
from __future__ import annotations

import json
import re

from .reply_gold import visible_reply_obligation
from .schema import gold_answer

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is",
         "are", "was", "were", "this", "that", "with", "your", "you", "it",
         "its", "as", "at", "by", "be", "has", "have", "not", "no", "from",
         "into", "matches", "match", "category", "message", "email", "mail"}


def _significant(text):
    words = re.findall(r"[a-z0-9@._-]{4,}", (text or "").lower())
    return {w for w in words if w not in _STOP}


def _numbers(text):
    return set(re.findall(r"\d[\d.,]*", text or ""))


def _supervised_assistant_obj(example):
    """Parse the last supervised assistant JSON object (the real target)."""
    for msg in reversed(example.get("messages") or []):
        if msg.get("role") != "assistant" or not msg.get("supervised"):
            continue
        content = msg.get("content") or ""
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            continue
        try:
            return json.loads(m.group(0))
        except ValueError:
            continue
    return None


def _source_text(example):
    parts = []
    for msg in example.get("messages") or []:
        if msg.get("role") in ("system", "user", "tool"):
            parts.append(str(msg.get("content") or ""))
    return "\n".join(parts)


# ------------------------------------------------------------------ decision

def verify_decision(example, source, taxonomy):
    """Problems with a decision example ([] when valid)."""
    problems = []
    obj = _supervised_assistant_obj(example)
    if obj is None:
        return ["supervised assistant JSON missing or unparseable"]
    if "confidence" in obj:
        problems.append("confidence must not be a synthetic gold target")
    names = [c.get("name") for c in taxonomy["categories"]]
    if obj.get("category") not in names:
        problems.append("category %r is not an authored taxonomy name"
                        % obj.get("category"))
    if not isinstance(obj.get("needs_reply"), bool):
        problems.append("needs_reply must be a boolean")
    for field in ("summary", "reason"):
        if not isinstance(obj.get(field), str) or not obj.get(field).strip():
            problems.append("%s must be a non-empty string" % field)
    intent = (source or {}).get("intent") or {}
    if obj.get("category") != intent.get("category"):
        problems.append("category %r != authored intent %r"
                        % (obj.get("category"), intent.get("category")))
    if obj.get("needs_reply") != intent.get("needs_reply"):
        problems.append("needs_reply %r != authored intent %r"
                        % (obj.get("needs_reply"), intent.get("needs_reply")))
    # the label must be justified by the visible email, not only the hidden intent
    email = (source or {}).get("email") or example.get("source_email") or {}
    vis = visible_reply_obligation("%s\n%s" % (email.get("subject", ""),
                                               email.get("body", "")))
    if vis is not None and obj.get("needs_reply") != vis:
        problems.append("needs_reply %r contradicts the visible email obligation %r"
                        % (obj.get("needs_reply"), vis))

    src = _source_text(example)
    src_tokens = _significant(src)
    defs = " ".join("%s %s" % (c.get("name"), c.get("definition"))
                    for c in taxonomy["categories"])
    allowed_tokens = src_tokens | _significant(defs)
    summary = obj.get("summary") or ""
    reason = obj.get("reason") or ""
    if _significant(summary) and not (_significant(summary) & src_tokens):
        problems.append("summary shares no significant word with the source")
    for text, label in ((summary, "summary"), (reason, "reason")):
        missing_nums = _numbers(text) - _numbers(src)
        if missing_nums:
            problems.append("%s contains numbers absent from the source: %s"
                            % (label, ", ".join(sorted(missing_nums))))
        ungrounded = _significant(text) - allowed_tokens
        if ungrounded:
            problems.append("%s has ungrounded words: %s"
                            % (label, ", ".join(sorted(ungrounded)[:5])))
    low = (summary + " " + reason).lower()
    if intent.get("needs_reply") is True and re.search(
            r"no action|does not need|no reply needed", low):
        problems.append("reason contradicts the authored reply intent")
    return problems


# ------------------------------------------------------------------ dialogue

def _events(trace):
    return [e for turn in (trace.get("turns") or []) for e in turn.get("events") or []]


def _moves(trace):
    moves = []
    for ev in _events(trace):
        for ch in ev.get("state_changes") or []:
            if ch.get("op") == "move":
                moves.append(ch)
    return moves


def _rule_matches(conditions, message, match_mode="all"):
    """Whether a proposed rule's conditions match a synthetic message."""
    checks = []
    for c in conditions or []:
        field = (c.get("field") or "").lower()
        op = (c.get("op") or "contains").lower()
        value = str(c.get("value") or "")
        hay = str({
            "from": message.get("from_addr", ""),
            "to": message.get("to_addr", ""),
            "subject": message.get("subject", ""),
            "body": message.get("body", ""),
        }.get(field, "")).lower()
        v = value.lower()
        if op == "equals":
            checks.append(hay == v)
        elif op == "regex":
            try:
                checks.append(re.search(value, hay) is not None)
            except re.error:
                checks.append(False)
        else:
            checks.append(v in hay)
    if not checks:
        return False
    return all(checks) if match_mode != "any" else any(checks)


def _messages_by_id(trace):
    out = {}
    for m in (trace.get("state_final") or {}).get("messages") or []:
        out[m["id"]] = m
    return out


def _outcome_hit(outcome, trace, final, proposals):
    kind = outcome.get("kind")
    if kind == "moved":
        return any(mv.get("message_id") == outcome.get("message_id")
                   and (outcome.get("to") is None or mv.get("to") == outcome["to"])
                   for mv in _moves(trace))
    if kind == "no_mutation":
        return (len(_moves(trace)) == 0
                and not any(ev.get("mutated") and ev.get("tool") not in ("propose_rule",)
                            for ev in _events(trace)))
    if kind == "rule_proposed":
        return bool(proposals)
    if kind == "rule_applied":
        return (final.get("rule_count", 0) > 0)
    if kind == "send" or kind == "delete":
        return any(ev.get("tool") in ("send_message", "delete_message")
                   and ev.get("executed") for ev in _events(trace))
    return None


def verify_dialogue(example, source, gold, taxonomy=None):
    """Problems with a bounded tool dialogue ([] when valid)."""
    problems = []
    trace = example.get("trace") or {}
    final = trace.get("state_final") or {}
    proposals = final.get("proposals") or []
    events = _events(trace)
    gold = gold or {}

    # proposals are never applied; send/delete never execute.
    for ev in events:
        if ev.get("tool") == "propose_rule" and ev.get("ok") and final.get(
                "rule_count", 0) > 0:
            problems.append("a proposed rule was applied")
        if ev.get("tool") in ("send_message", "delete_message") and ev.get("ok"):
            problems.append("%s executed; send/delete must never run" % ev["tool"])

    expected = gold.get("expected_state") or {}
    exp_folders = expected.get("folders") or {}
    for folder, ids in exp_folders.items():
        got = sorted(final.get("folders", {}).get(folder, []))
        if got != sorted(ids):
            problems.append("expected_state folder %r=%r but final=%r"
                            % (folder, sorted(ids), got))
    if "rule_count" in expected and final.get("rule_count") != expected["rule_count"]:
        problems.append("expected rule_count %r but final=%r"
                        % (expected["rule_count"], final.get("rule_count")))

    by_id = _messages_by_id(trace)
    for outcome in gold.get("required_outcomes") or []:
        hit = _outcome_hit(outcome, trace, final, proposals)
        if hit is None:
            problems.append("unsupported required outcome %r" % outcome)
        elif not hit:
            problems.append("required outcome not satisfied: %r" % outcome)
    for outcome in gold.get("forbidden_outcomes") or []:
        hit = _outcome_hit(outcome, trace, final, proposals)
        if hit:
            problems.append("forbidden outcome occurred: %r" % outcome)

    for a in gold.get("assertions") or []:
        kind = a.get("kind")
        if kind == "folder_contains":
            if a.get("message_id") not in final.get("folders", {}).get(
                    a.get("folder"), []):
                problems.append("assertion folder_contains failed: %r" % a)
        elif kind == "folder_excludes":
            if a.get("message_id") in final.get("folders", {}).get(a.get("folder"), []):
                problems.append("assertion folder_excludes failed: %r" % a)
        elif kind == "rule_proposed":
            if not proposals:
                problems.append("assertion rule_proposed failed (no proposal)")
        elif kind == "no_mutation":
            if _moves(trace) or any(ev.get("mutated")
                                    and ev.get("tool") != "propose_rule"
                                    for ev in events):
                problems.append("assertion no_mutation failed")
        elif kind == "no_send":
            if any(ev.get("tool") == "send_message" and ev.get("executed")
                   for ev in events):
                problems.append("assertion no_send failed")

    # positive + negative rule-match scope
    scope = gold.get("rule_scope") or {}
    if scope:
        norm = None
        for ev in events:
            if ev.get("tool") == "propose_rule" and ev.get("ok"):
                for ch in ev.get("state_changes") or []:
                    if ch.get("op") == "propose_rule":
                        idx = ch.get("proposal_index")
                        if isinstance(idx, int) and 0 <= idx < len(proposals):
                            norm = proposals[idx]
        if norm is None:
            problems.append("rule_scope asserted but no proposal was queued")
        else:
            must = scope.get("proposal_must_match")
            if must is not None:
                msg = by_id.get(must)
                if msg is None or not _rule_matches(
                        norm.get("conditions"), _message_for_match(example, must),
                        norm.get("match_mode", "all")):
                    problems.append("proposed rule does not match required message %r"
                                    % must)
            for bad in scope.get("proposal_must_not_match") or []:
                msg = by_id.get(bad)
                if msg and _rule_matches(norm.get("conditions"),
                                         _message_for_match(example, bad),
                                         norm.get("match_mode", "all")):
                    problems.append("proposed rule also matches forbidden message %r"
                                    % bad)

    # no fabricated completion: a completion claim needs a real event.  Approval
    # or "pending" wording elsewhere does NOT excuse a claimed completed action.
    assistant_text = " ".join(str(m.get("content") or "").lower()
                              for m in example.get("messages") or []
                              if m.get("role") == "assistant")
    moved = any(True for _ in _moves(trace))
    claimed_move = re.search(r"(?<!not )(?<!never )\bmoved\b", assistant_text)
    if claimed_move and not moved:
        problems.append("assistant claims a move completed but no move event")
    if re.search(r"(?<!not )\bsent\b|\bdeleted\b", assistant_text):
        if not any(ev.get("tool") in ("send_message", "delete_message")
                   and ev.get("executed") for ev in events):
            problems.append("assistant claims an off capability completed")
    return problems


def _message_for_match(example, message_id):
    """The mailbox message with this id (from the recorded mailbox)."""
    for m in example.get("mailbox") or []:
        if m.get("id") == message_id or m.get("message_id") == message_id:
            return {"from_addr": m.get("from_addr", ""),
                    "to_addr": m.get("to_addr", ""),
                    "subject": m.get("subject", ""),
                    "body": m.get("body", "")}
    return {}


# ------------------------------------------------------------------ envelope

def verify_envelope(example, trace=None):
    problems = []
    meta = example.get("metadata") or {}
    if meta.get("review_status") not in (None, "draft"):
        problems.append("example review_status must stay draft in this slice")
    if meta.get("human_seal"):
        problems.append("human_seal must never be auto-asserted")
    if meta.get("test_qualified"):
        problems.append("test qualification must never be claimed")
    trace = trace or example.get("trace")
    if trace:
        if trace.get("review_status") not in (None, "draft"):
            problems.append("trace review_status must stay draft")
        if trace.get("human_seal"):
            problems.append("trace must not be human-sealed")
        if trace.get("test_qualified"):
            problems.append("trace must not be test-qualified")
    return problems


def verify_example(example, *, source=None, gold=None, taxonomy=None):
    """Route to the task verifier; returns problems ([] = valid)."""
    problems = list(verify_envelope(example))
    if example.get("task") == "decision":
        if taxonomy is None:
            problems.append("missing_context:taxonomy")
        elif source is None:
            problems.append("missing_context:source")
        else:
            problems.extend(verify_decision(example, source, taxonomy))
    elif example.get("task") == "workflow":
        if gold is None:
            problems.append("missing_context:gold")
        else:
            problems.extend(verify_dialogue(example, source, gold, taxonomy))
    else:
        problems.append("unknown task %r" % example.get("task"))
    return problems


def is_test_qualified(_trace=None):
    return False
