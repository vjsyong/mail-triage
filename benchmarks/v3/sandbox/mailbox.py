"""Deterministic per-case mailbox state machine for benchmark v3 (WP3).

Design goals (spec §6/§12, plan WP3):

* **Deep fresh per case.**  :class:`Mailbox` deep-copies its fixture, so no two
  cases, attempts or model turns can leak state into one another.
* **Permissions are separate from writes.**  Every capability resolves to
  ``off`` / ``ask`` / ``auto``; a call is *attempted* (logged) before it is
  allowed to touch state.  ``delete`` and ``send`` are always denied -- this
  sandbox can never delete or send mail.
* **Transactionality.**  A handler validates and mutates in one place; any
  failure restores the pre-call mutable state so a failed operation leaves no
  partial mutation.
* **Derived folder counts.**  Counts are recomputed from the message list, so a
  repeated move stays a safe no-op and can never corrupt totals.
* **Read-after-write.**  Reads see the current message list, not a cache.

The mailbox fixture interface (handoff to the WP2 data worker; also documented
in ``benchmarks/v3/README.md``)::

    {
      "folders": ["INBOX", ...],                 # optional; derived if absent
      "messages": [
        {"message_id": "m1", "folder": "INBOX", "from_addr": "...",
         "to_addr": "...", "subject": "...", "date": "...",
         "snippet": "...", "body": "...", "seen": false, "flagged": false,
         "uid": 1}
      ],
      "rules": [
        {"rule_id": "r1", "name": "...", "enabled": true,
         "conditions": [{"field": "from_addr", "op": "contains", "value": "..."}],
         "actions": [{"type": "move", "folder": "Receipts"}]}
      ],
      "permissions": {...}                       # optional policy card or levels
    }

IDs (``message_id`` / ``rule_id`` / folder names) are normalised to non-empty
strings, so integer and string ids are addressable interchangeably.
"""
from __future__ import annotations

import copy

# ---------------------------------------------------------------- capabilities

READ = "read"
FLAG = "flag"              # read *update* (seen/flagged) -- safe metadata
FOLDER = "folder"          # create a folder -- safe metadata
DRAFT = "draft"            # save a draft (never sends)
MOVE = "move"
RULE_CREATE = "rule_create"
DELETE = "delete"          # dangerous -- always denied
SEND = "send"              # dangerous -- always denied

CAPABILITIES = (READ, FLAG, FOLDER, DRAFT, MOVE, RULE_CREATE, DELETE, SEND)
DANGEROUS = (DELETE, SEND)
CAPABILITY_LEVELS = ("off", "ask", "auto")

# Safe capabilities default to auto; mutating mailbox capabilities default off.
_SAFE_DEFAULT = {READ: "auto", FLAG: "auto", FOLDER: "auto", DRAFT: "auto"}


class SandboxError(Exception):
    """Base sandbox error."""


class UnknownToolError(SandboxError):
    """Raised when a tool name is not part of the sandbox surface."""


class ToolArgumentError(SandboxError):
    """Raised when a tool call has invalid or missing arguments."""


# ------------------------------------------------------------- id normalisation

def _norm_id(value, fallback=""):
    if value is None:
        return fallback
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or fallback


# ----------------------------------------------------------------- permissions

def _coerce_level(value):
    if isinstance(value, bool):
        return "auto" if value else "off"
    text = str(value or "").strip().lower()
    if text in ("auto", "allow", "on", "true", "yes"):
        return "auto"
    if text in ("ask", "approval", "require_approval", "pending", "prompt"):
        return "ask"
    if text in ("off", "deny", "false", "no", "none", "disabled"):
        return "off"
    return None


def normalize_permissions(source):
    """Return ``{capability: "off"|"ask"|"auto"}`` from levels or a policy card.

    Accepts either explicit levels (``{"move": "auto", "rule_create": "ask"}``)
    or the policy card's boolean block (``allow_move`` / ``allow_rule_create`` /
    ``require_approval``).  ``delete`` and ``send`` are forced ``off``: the
    sandbox can never be configured to delete or send real mail.
    """
    out = {c: _SAFE_DEFAULT.get(c, "off") for c in CAPABILITIES}
    if not isinstance(source, dict):
        source = {}
    # Explicit capability levels win when present.
    for cap in CAPABILITIES:
        if cap in source:
            level = _coerce_level(source[cap])
            if level is not None:
                out[cap] = level
    block = source.get("permissions") if isinstance(source.get("permissions"), dict) \
        else source
    if any(k in block for k in ("allow_move", "allow_rule_create",
                                "allow_send", "require_approval")):
        require = bool(block.get("require_approval"))
        out[MOVE] = ("ask" if require else "auto") if block.get("allow_move") else "off"
        out[RULE_CREATE] = (("ask" if require else "auto")
                            if block.get("allow_rule_create") else "off")
        out[SEND] = "off"
        out[DELETE] = "off"
    # Dangerous capabilities can never be enabled by configuration.
    out[DELETE] = "off"
    out[SEND] = "off"
    return out


# --------------------------------------------------------------------- mailbox

class Mailbox(object):
    """A deterministic, fresh, single-case mailbox."""

    def __init__(self, fixture=None, permissions=None, case_id=""):
        fixture = copy.deepcopy(fixture or {})
        self.case_id = str(case_id or "")
        perms_source = fixture.get("permissions") if permissions is None else permissions
        self.permissions = normalize_permissions(perms_source)
        self._seq = 0
        self.events = []
        self._moves = []
        self._drafts = []
        self._proposals = []
        self._simulations = []
        self._recv = 0

        self._messages = []
        for i, raw in enumerate(fixture.get("messages") or []):
            if not isinstance(raw, dict):
                continue
            mid = _norm_id(raw.get("message_id") or raw.get("id"), "m%d" % (i + 1))
            uid = raw.get("uid")
            if uid is None or uid == "":
                uid = i + 1
            self._messages.append({
                "message_id": mid,
                "uid": uid,
                "folder": str(raw.get("folder") or "INBOX"),
                "from_addr": str(raw.get("from_addr") or raw.get("from") or ""),
                "to_addr": str(raw.get("to_addr") or raw.get("to") or ""),
                "subject": str(raw.get("subject") or ""),
                "date": str(raw.get("date") or ""),
                "snippet": str(raw.get("snippet") or (raw.get("body") or "")[:200]),
                "body": str(raw.get("body") or raw.get("snippet") or ""),
                "seen": bool(raw.get("seen", False)),
                "flagged": bool(raw.get("flagged", False)),
            })

        self._folders = set(fixture.get("folders") or [])
        self._folders.update(m["folder"] for m in self._messages)
        self._folders.update(("INBOX", "Drafts", "Archive"))

        self._rules = []
        for i, raw in enumerate(fixture.get("rules") or []):
            if not isinstance(raw, dict):
                continue
            self._rules.append({
                "rule_id": _norm_id(raw.get("rule_id") or raw.get("id"), "r%d" % (i + 1)),
                "name": str(raw.get("name") or ""),
                "enabled": bool(raw.get("enabled", True)),
                "conditions": copy.deepcopy(raw.get("conditions") or []),
                "actions": copy.deepcopy(raw.get("actions") or []),
            })

    # ------------------------------------------------------------- state views

    def folder_counts(self):
        """Counts derived from the live message list (never stored stale)."""
        counts = {name: {"total": 0, "unseen": 0} for name in sorted(self._folders)}
        for m in self._messages:
            row = counts.setdefault(m["folder"], {"total": 0, "unseen": 0})
            row["total"] += 1
            if not m["seen"]:
                row["unseen"] += 1
        return counts

    def snapshot(self):
        return {
            "case_id": self.case_id,
            "folders": self.folder_counts(),
            "messages": sorted(
                ({"message_id": m["message_id"], "folder": m["folder"],
                  "seen": bool(m["seen"]), "flagged": bool(m["flagged"])}
                 for m in self._messages),
                key=lambda r: r["message_id"]),
            "rules": [{"rule_id": r["rule_id"], "name": r["name"],
                       "enabled": r["enabled"]} for r in self._rules],
            "moves": copy.deepcopy(self._moves),
            "drafts": copy.deepcopy(self._drafts),
            "proposed_rules": copy.deepcopy(self._proposals),
            "simulations": copy.deepcopy(self._simulations),
        }

    def final_state(self):
        """Scoreable final state (snapshot + the event log)."""
        state = self.snapshot()
        state["events"] = copy.deepcopy(self.events)
        return state

    @property
    def messages(self):
        return self._messages

    @property
    def folders(self):
        return sorted(self._folders)

    @property
    def rules(self):
        return self._rules

    # ---------------------------------------------------------------- lookup

    def _find(self, args):
        mid = args.get("message_id")
        if mid not in (None, ""):
            want = _norm_id(mid)
            for m in self._messages:
                if m["message_id"] == want:
                    return m, None
            return None, "no message with message_id %r" % mid
        folder = str(args.get("folder") or "").strip()
        uid = args.get("uid")
        if not folder or uid in (None, ""):
            return None, "identify the message by message_id, or by folder AND uid"
        for m in self._messages:
            if m["folder"] == folder and str(m["uid"]) == str(uid):
                return m, None
        return None, "no message with uid %s in folder %r" % (uid, folder)

    # ------------------------------------------------------------ tool handlers
    # Handlers return (model_result, mutated, state_changes).  They validate
    # everything before mutating so a failure cannot leave partial state.

    def _t_list_folders(self, _args):
        folders = [{"name": k, "total": v["total"], "unseen": v["unseen"]}
                   for k, v in self.folder_counts().items()]
        return ({"ok": True, "status": "ok", "summary": "%d folders" % len(folders),
                 "result": {"folders": folders}}, False, [])

    def _t_search_messages(self, args):
        query = str(args.get("query") or "").strip().lower()
        sender = str(args.get("sender") or "").strip().lower()
        subject = str(args.get("subject") or "").strip().lower()
        folder = str(args.get("folder") or "").strip()
        try:
            limit = max(1, min(int(args.get("limit") or 20), 100))
        except (TypeError, ValueError):
            limit = 20
        rows = []
        for m in self._messages:
            if query and query not in (m["subject"] + " " + m["body"] + " " + m["from_addr"]).lower():
                continue
            if sender and sender not in m["from_addr"].lower():
                continue
            if subject and subject not in m["subject"].lower():
                continue
            if folder and m["folder"] != folder:
                continue
            rows.append(m)
        out = [{"message_id": m["message_id"], "folder": m["folder"],
                "from": m["from_addr"], "subject": m["subject"], "date": m["date"],
                "snippet": m["snippet"][:120]} for m in rows[:limit]]
        return ({"ok": True, "status": "ok",
                 "summary": "%d of %d messages" % (len(out), len(rows)),
                 "result": {"total_matched": len(rows), "returned": len(out),
                            "messages": out}}, False, [])

    def _t_read_message(self, args):
        m, err = self._find(args)
        if err:
            raise ToolArgumentError(err)
        data = {"message_id": m["message_id"], "uid": m["uid"], "folder": m["folder"],
                "from_addr": m["from_addr"], "to_addr": m["to_addr"],
                "subject": m["subject"], "date": m["date"], "seen": m["seen"],
                "flagged": m["flagged"], "body": m["body"][:4000]}
        return ({"ok": True, "status": "ok",
                 "summary": "read %r in %s" % (m["subject"][:60], m["folder"]),
                 "result": data}, False, [])

    def _t_list_rules(self, _args):
        rules = [{"rule_id": r["rule_id"], "name": r["name"], "enabled": r["enabled"]}
                 for r in self._rules]
        return ({"ok": True, "status": "ok", "summary": "%d rule(s)" % len(rules),
                 "result": {"rules": rules}}, False, [])

    def _t_move_message(self, args):
        target = str(args.get("target_folder") or "").strip()
        if not target:
            raise ToolArgumentError("target_folder is required")
        m, err = self._find(args)
        if err:
            raise ToolArgumentError(err)
        src = m["folder"]
        if src == target:
            return ({"ok": True, "status": "noop",
                     "summary": "already in %s" % target,
                     "result": {"message_id": m["message_id"], "from": src,
                                "to": target, "noop": True}}, False, [])
        m["folder"] = target
        self._folders.add(target)
        change = {"message_id": m["message_id"], "from": src, "to": target}
        self._moves.append(change)
        return ({"ok": True, "status": "ok", "summary": "moved to %s" % target,
                 "result": {"message_id": m["message_id"], "from": src, "to": target}},
                True, [{"op": "move", **change}])

    def _t_flag_message(self, args):
        seen = args.get("seen")
        flagged = args.get("flagged")
        if seen is None and flagged is None:
            raise ToolArgumentError("set seen and/or flagged")
        m, err = self._find(args)
        if err:
            raise ToolArgumentError(err)
        changes = []
        if seen is not None:
            m["seen"] = bool(seen)
            changes.append({"op": "seen", "message_id": m["message_id"], "value": m["seen"]})
        if flagged is not None:
            m["flagged"] = bool(flagged)
            changes.append({"op": "flagged", "message_id": m["message_id"],
                            "value": m["flagged"]})
        return ({"ok": True, "status": "ok", "summary": "flags updated",
                 "result": {"message_id": m["message_id"], "changes": changes}},
                bool(changes), changes)

    def _t_create_folder(self, args):
        name = str(args.get("name") or "").strip()
        if not name:
            raise ToolArgumentError("name is required")
        existed = name in self._folders
        self._folders.add(name)
        changes = [] if existed else [{"op": "create_folder", "folder": name}]
        return ({"ok": True, "status": "noop" if existed else "ok",
                 "summary": ("folder existed: " if existed else "folder created: ") + name,
                 "result": {"folder": name, "created": not existed}}, not existed, changes)

    def _t_draft_reply(self, args):
        m = None
        if args.get("message_id") not in (None, ""):
            m, err = self._find(args)
            if err:
                raise ToolArgumentError(err)
        self._recv += 1
        draft = {"draft_id": "draft%d" % self._recv,
                 "message_id": m["message_id"] if m else None,
                 "instructions": str(args.get("instructions") or "")[:500]}
        self._drafts.append(draft)
        return ({"ok": True, "status": "ok", "summary": "draft saved to Drafts",
                 "result": {"draft_id": draft["draft_id"],
                            "note": "waiting in the Drafts folder (never sent)"}},
                True, [{"op": "draft", "draft_id": draft["draft_id"]}])

    def _t_propose_rule(self, args):
        name = str(args.get("name") or "").strip()
        if not name:
            raise ToolArgumentError("name is required")
        conditions = args.get("conditions")
        if conditions is not None and not isinstance(conditions, list):
            raise ToolArgumentError("conditions must be a list")
        actions = args.get("actions")
        if actions is not None and not isinstance(actions, list):
            raise ToolArgumentError("actions must be a list")
        proposal = {"proposal_id": "prop%d" % (len(self._proposals) + 1),
                    "name": name, "conditions": copy.deepcopy(conditions or []),
                    "actions": copy.deepcopy(actions or []),
                    "rationale": str(args.get("rationale") or "")[:500]}
        self._proposals.append(proposal)
        return ({"ok": True, "status": "ok", "summary": "proposed rule %r" % name,
                 "result": {"proposal": proposal,
                            "note": "proposal only; waiting for user approval"}},
                True, [{"op": "propose_rule", "proposal_id": proposal["proposal_id"]}])

    def _t_simulate_rule(self, args):
        conditions = args.get("conditions") or []
        if not isinstance(conditions, list):
            raise ToolArgumentError("conditions must be a list")
        matches = []
        for m in self._messages:
            ok = True
            for c in conditions:
                if not isinstance(c, dict):
                    ok = False
                    break
                field = str(c.get("field") or "")
                value = str(c.get("value") or "").lower()
                op = str(c.get("op") or "contains")
                hay = str(m.get(field, m.get({"from": "from_addr"}.get(field, field), ""))).lower()
                if op == "equals":
                    ok = hay == value
                elif op == "regex":
                    import re
                    try:
                        ok = re.search(value, hay) is not None
                    except re.error:
                        ok = False
                else:
                    ok = value in hay
                if not ok:
                    break
            if ok:
                matches.append(m["message_id"])
        sim = {"proposal_id": "sim%d" % (len(self._simulations) + 1),
               "matched": matches}
        self._simulations.append(sim)
        return ({"ok": True, "status": "ok",
                 "summary": "rule would match %d message(s)" % len(matches),
                 "result": {"matched": matches, "dry_run": True}},
                False, [])

    def _t_delete_message(self, _args):
        # Defence in depth: the permission layer already denies this.
        raise SandboxError("delete_message is not available in the sandbox")

    def _t_send_message(self, _args):
        raise SandboxError("send_message is not available in the sandbox")

    # capability + handler table
    _TOOLS = {
        "list_folders": (READ, _t_list_folders),
        "search_messages": (READ, _t_search_messages),
        "read_message": (READ, _t_read_message),
        "list_rules": (READ, _t_list_rules),
        "flag_message": (FLAG, _t_flag_message),
        "create_folder": (FOLDER, _t_create_folder),
        "draft_reply": (DRAFT, _t_draft_reply),
        "move_message": (MOVE, _t_move_message),
        "propose_rule": (RULE_CREATE, _t_propose_rule),
        "simulate_rule": (READ, _t_simulate_rule),
        "delete_message": (DELETE, _t_delete_message),
        "send_message": (SEND, _t_send_message),
    }

    @classmethod
    def tool_names(cls):
        return sorted(cls._TOOLS)

    # -------------------------------------------------------------- execution

    def _decide(self, capability, approve):
        if capability in DANGEROUS:
            return "deny", "off"
        level = self.permissions.get(capability, "off")
        if level == "auto":
            return "allow", level
        if level == "ask":
            return ("allow" if approve else "pending"), level
        return "deny", level

    def _log(self, tool, args, status, permission, executed, mutated,
             state_changes=None, summary="", error=None):
        self._seq += 1
        event = {
            "seq": self._seq,
            "case_id": self.case_id,
            "tool": tool,
            "args": copy.deepcopy(args) if isinstance(args, dict) else {},
            "status": status,
            "permission": permission,
            "executed": bool(executed),
            "mutated": bool(mutated),
            "state_changes": state_changes or [],
            "summary": summary,
            "error": error,
        }
        self.events.append(event)
        return event

    def _snapshot_mutables(self):
        return (copy.deepcopy(self._messages), set(self._folders),
                copy.deepcopy(self._rules), list(self._moves),
                [dict(d) for d in self._drafts], copy.deepcopy(self._proposals),
                copy.deepcopy(self._simulations), self._recv)

    def _restore_mutables(self, snap):
        (self._messages, self._folders, self._rules, self._moves,
         self._drafts, self._proposals, self._simulations, self._recv) = snap

    def execute(self, name, args, status="executed", approve=False):
        """Attempt one tool call and return the model-facing result.

        ``status="skipped"`` (budget exhaustion) records a skipped attempt and
        performs no work.  ``approve=True`` resolves a capability whose level is
        ``ask``; without it an ``ask`` call stays pending and does not write.
        """
        args = args if isinstance(args, dict) else {}
        if status != "executed":
            return self._skip(name, args, status)

        entry = self._TOOLS.get(name)
        if entry is None:
            result = {"ok": False, "status": "error",
                      "summary": "unknown tool %r" % name,
                      "result": {"error": "unknown_tool", "available": self.tool_names()}}
            self._log(name, args, "error", {"capability": None, "decision": "error",
                                            "level": None}, False, False,
                      summary=result["summary"], error="unknown_tool")
            return result

        capability, handler = entry
        decision, level = self._decide(capability, approve)
        permission = {"capability": capability, "decision": decision, "level": level}

        if decision == "deny":
            result = {"ok": False, "status": "denied",
                      "summary": "permission denied: %s is %s" % (capability, level),
                      "result": {"error": "permission_denied",
                                 "capability": capability, "level": level}}
            self._log(name, args, "denied", permission, False, False,
                      summary=result["summary"], error="permission_denied")
            return result
        if decision == "pending":
            result = {"ok": False, "status": "pending",
                      "summary": "approval required for %s" % capability,
                      "result": {"error": "approval_required",
                                 "capability": capability, "level": level}}
            self._log(name, args, "pending", permission, False, False,
                      summary=result["summary"], error="approval_required")
            return result

        snap = self._snapshot_mutables()
        try:
            model, mutated, changes = handler(self, args)
        except ToolArgumentError as exc:
            self._restore_mutables(snap)
            result = {"ok": False, "status": "error", "summary": str(exc),
                      "result": {"error": "invalid_arguments", "detail": str(exc)}}
            self._log(name, args, "error", permission, False, False,
                      summary=result["summary"], error="invalid_arguments")
            return result
        except Exception as exc:  # noqa: BLE001 - transactional restore
            self._restore_mutables(snap)
            result = {"ok": False, "status": "error",
                      "summary": "tool failed: %r" % exc,
                      "result": {"error": "tool_failure", "detail": repr(exc)}}
            self._log(name, args, "error", permission, False, False,
                      summary=result["summary"], error="tool_failure")
            return result

        event_status = model.get("status", "ok")
        self._log(name, args, event_status, permission, True, bool(mutated),
                  state_changes=changes, summary=model.get("summary", ""))
        return model

    def _skip(self, name, args, status):
        result = {"ok": False, "status": status,
                  "summary": "skipped: %s" % status,
                  "result": {"error": "skipped", "reason": status}}
        self._log(name, args, "skipped",
                  {"capability": self._TOOLS.get(name, (None,))[0],
                   "decision": "skip", "level": None},
                  False, False, summary=result["summary"], error="skipped")
        return result
