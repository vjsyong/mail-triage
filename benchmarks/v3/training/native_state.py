"""Synthetic mailbox exposing the **production** tool result shapes (AC3).

The bounded assistant surface is production's, so arguments use production types
(integer ``message_id``/``uid``, condition ``field`` enum ``from/to/subject/body``
and ``op`` enum, ``actions`` as an object) and every tool returns the exact
``result`` sub-payload production returns.  Untrusted message fields are wrapped
with the production ``_wrap_untrusted`` delimiter and neutralisation.

``propose_rule`` uses production ``_validate_rule`` and **queues** a proposal; it
never applies a rule.  Schema type/enum violations are rejected before any state
change (see ``tools_native.execute_tool_call``).
"""
from __future__ import annotations

import copy

from . import engine_contract as EC

MOVE = "move"
RULE_CREATE = "rule_create"
CAPABILITIES = (MOVE, RULE_CREATE)
LEVELS = ("off", "ask", "auto")
_TOOL_CAPABILITY = {"move_message": MOVE, "propose_rule": RULE_CREATE}


def _coerce_level(value, default):
    if isinstance(value, bool):
        return "auto" if value else "off"
    text = str(value or "").strip().lower()
    if text in ("auto", "allow", "on", "true", "yes"):
        return "auto"
    if text in ("ask", "approval", "require_approval", "pending", "prompt"):
        return "ask"
    if text in ("off", "deny", "false", "no", "none", "disabled"):
        return "off"
    return default


def _norm_int(value, fallback=None):
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return fallback


class NativeMailbox(object):
    """Deterministic synthetic mailbox with production-shaped results."""

    def __init__(self, messages=None, rules=None, case_id="", permissions=None):
        self.case_id = str(case_id or "")
        self.permissions = {MOVE: "auto", RULE_CREATE: "auto"}
        for cap in CAPABILITIES:
            if isinstance(permissions, dict) and cap in permissions:
                self.permissions[cap] = _coerce_level(permissions[cap], "auto")
        self.messages = []
        for i, raw in enumerate(messages or []):
            mid = _norm_int(raw.get("id", raw.get("message_id")), i + 1)
            uid = _norm_int(raw.get("uid"), i + 1)
            self.messages.append({
                "id": mid,
                "uid": uid,
                "folder": str(raw.get("folder") or "INBOX"),
                "from_addr": str(raw.get("from_addr") or raw.get("from") or ""),
                "to_addr": str(raw.get("to_addr") or raw.get("to") or ""),
                "subject": str(raw.get("subject") or ""),
                "date": str(raw.get("date") or ""),
                "body": str(raw.get("body") or ""),
                "snippet": str(raw.get("snippet") or (raw.get("body") or "")[:200]),
                "seen": bool(raw.get("seen", False)),
                "flagged": bool(raw.get("flagged", False)),
                "status": str(raw.get("status") or "sorted"),
            })
        self.rules = [copy.deepcopy(r) for r in (rules or [])]
        self.proposals = []
        self.events = []
        self._seq = 0

    # ------------------------------------------------------------- lookups
    def _find(self, args):
        mid = _norm_int(args.get("message_id"))
        if mid is not None:
            for m in self.messages:
                if m["id"] == mid:
                    return m, None
            return None, "no message with id %r" % args.get("message_id")
        folder = str(args.get("folder") or "").strip()
        uid = _norm_int(args.get("uid"))
        if not folder or uid is None:
            return None, "identify the message by message_id, or by folder AND uid"
        for m in self.messages:
            if m["folder"] == folder and m["uid"] == uid:
                return m, None
        return None, "no message with uid %s in folder %r" % (uid, folder)

    def folders(self):
        return sorted({m["folder"] for m in self.messages} | {"INBOX", "Drafts",
                                                             "Archive"})

    def _folder_counts(self):
        out = {name: {"total": 0, "unseen": 0} for name in self.folders()}
        for m in self.messages:
            row = out.setdefault(m["folder"], {"total": 0, "unseen": 0})
            row["total"] += 1
            if not m["seen"]:
                row["unseen"] += 1
        return out

    # ------------------------------------------------------------- tool calls
    def call_tool(self, name, args, host_approve=False):
        handler = getattr(self, "_tool_%s" % name, None)
        if handler is None:
            return {"ok": False, "summary": "unknown tool %r" % name,
                    "result": {"error": "unknown_tool", "available":
                               list(EC.BOUNDED_SURFACE)}}
        cap = _TOOL_CAPABILITY.get(name)
        if cap is not None:
            level = self.permissions.get(cap, "auto")
            if level == "off":
                res = {"ok": False, "summary": "permission denied: %s is off" % cap,
                       "result": {"error": "permission_denied", "capability": cap,
                                  "level": level}}
                self._log(name, args, res)
                return res
            if level == "ask" and not host_approve:
                res = {"ok": False, "summary": "approval required for %s" % cap,
                       "result": {"error": "approval_required", "capability": cap,
                                  "level": level}}
                self._log(name, args, res)
                return res
        return handler(args or {})

    def _log(self, tool, args, res, state_changes=None, mutated=False):
        self._seq += 1
        self.events.append({
            "seq": self._seq, "case_id": self.case_id, "tool": tool,
            "args": copy.deepcopy(args), "status": "ok" if res.get("ok") else "error",
            "ok": bool(res.get("ok")), "mutated": bool(mutated),
            "state_changes": copy.deepcopy(state_changes or []),
            "summary": res.get("summary", "")})

    def _tool_search_messages(self, a):
        try:
            limit = max(1, min(int(a.get("limit") or 20), 100))
            offset = max(0, int(a.get("offset") or 0))
        except (TypeError, ValueError):
            limit, offset = 20, 0
        q = str(a.get("query") or "").strip().lower()
        rows = []
        for m in self.messages:
            if q and q not in (m["from_addr"] + " " + m["subject"] + " "
                               + m["snippet"] + " " + m["body"]).lower():
                continue
            for key, col in (("sender", "from_addr"), ("subject", "subject")):
                v = str(a.get(key) or "").strip().lower()
                if v and v not in m[col].lower():
                    break
            else:
                if a.get("folder") and m["folder"] != a["folder"]:
                    continue
                if a.get("status") and m["status"] != a["status"]:
                    continue
                rows.append(m)
        total = len(rows)
        page = rows[offset:offset + limit]
        messages = [{
            "id": m["id"], "folder": m["folder"], "uid": m["uid"],
            "from": EC.wrap_untrusted(m["from_addr"]),
            "subject": EC.wrap_untrusted(m["subject"]),
            "date": EC.wrap_untrusted(m["date"]), "status": m["status"],
            "action": None, "category": None, "seen_at": None,
            "snippet": EC.wrap_untrusted(EC.truncate(m["snippet"], 200)),
        } for m in page]
        res = {"ok": True,
               "summary": "%d of %d indexed messages" % (len(messages), total),
               "result": {"total_matched": total, "returned": len(messages),
                          "offset": offset,
                          "note": ("Local index only (what the scanner has seen). "
                                   "Use search_mail for the full mailbox history."),
                          "messages": messages}}
        self._log("search_messages", a, res)
        return res

    def _tool_read_message(self, a):
        m, err = self._find(a)
        if err:
            res = {"ok": False, "summary": err, "result": {"error": err}}
            self._log("read_message", a, res)
            return res
        res = {"ok": True,
               "summary": "read %r in %s" % (EC.truncate(m["subject"], 60),
                                             m["folder"]),
               "result": {"folder": m["folder"], "uid": m["uid"],
                          "from": EC.wrap_untrusted(m["from_addr"]),
                          "to": EC.wrap_untrusted(m["to_addr"]),
                          "subject": EC.wrap_untrusted(m["subject"]),
                          "date": EC.wrap_untrusted(m["date"]), "msgid": None,
                          "body": EC.wrap_untrusted(EC.truncate(m["body"], 8000)),
                          "indexed_id": m["id"]}}
        self._log("read_message", a, res)
        return res

    def _tool_move_message(self, a):
        target = str(a.get("target_folder") or "").strip()
        if not target:
            res = {"ok": False, "summary": "target_folder is required",
                   "result": {"error": "target_folder is required"}}
            self._log("move_message", a, res)
            return res
        m, err = self._find(a)
        if err:
            res = {"ok": False, "summary": err, "result": {"error": err}}
            self._log("move_message", a, res)
            return res
        src = m["folder"]
        if src == target:
            res = {"ok": True, "summary": "already in %s" % target,
                   "result": {"moved": {"folder": src, "uid": m["uid"],
                                        "to": target, "noop": True}}}
            self._log("move_message", a, res)
            return res
        m["folder"] = target
        res = {"ok": True, "summary": "moved to %s" % target,
               "result": {"moved": {"folder": src, "uid": m["uid"], "to": target}}}
        self._log("move_message", a, res,
                  state_changes=[{"op": "move", "message_id": m["id"],
                                  "from": src, "to": target}], mutated=True)
        return res

    def _tool_list_rules(self, a):
        items = []
        for r in self.rules:
            b = EC.rule_brief(r)
            b["position"] = r.get("position")
            items.append(b)
        res = {"ok": True,
               "summary": "%d rule(s) (top to bottom, first match wins)" % len(items),
               "result": {"rules": items, "text": EC.rules_to_text(self.rules)}}
        self._log("list_rules", a, res)
        return res

    def _tool_propose_rule(self, a):
        norm, errors = EC.validate_rule(a)
        if not norm:
            res = {"ok": False,
                   "summary": "rule invalid: " + "; ".join(errors[:3]),
                   "result": {"errors": errors,
                              "hint": ("Every rule needs 1-4 conditions (field "
                                       "from/to/subject/body, op contains/equals/"
                                       "regex). Actions are optional: a rule with no "
                                       "actions is a GUARD that keeps matching mail in "
                                       "place. Fix and propose again.")}}
            self._log("propose_rule", a, res)
            return res
        self.proposals = [p for p in self.proposals
                          if (p.get("name") or "").lower()
                          != (norm.get("name") or "").lower()]
        self.proposals.append(norm)
        res = {"ok": True, "summary": "rule proposed: %s" % norm["name"],
               "result": {"status": "queued for the user's one-click approval",
                          "proposal_index": len(self.proposals) - 1, "rule": norm}}
        self._log("propose_rule", a, res,
                  state_changes=[{"op": "propose_rule",
                                  "proposal_index": len(self.proposals) - 1}],
                  mutated=True)
        return res

    # ------------------------------------------------------------- state views
    def snapshot(self):
        return {
            "case_id": self.case_id,
            "folders": {f: [m["id"] for m in self.messages if m["folder"] == f]
                        for f in self.folders()},
            "messages": [{"id": m["id"], "folder": m["folder"], "uid": m["uid"]}
                         for m in self.messages],
            "proposal_count": len(self.proposals),
        }

    def final_state(self):
        folders = {}
        for m in self.messages:
            folders.setdefault(m["folder"], []).append(m["id"])
        for f in self.folders():
            folders.setdefault(f, [])
        for f in folders:
            folders[f] = sorted(folders[f])
        moves = []
        for ev in self.events:
            for ch in ev.get("state_changes") or []:
                if ch.get("op") == "move":
                    moves.append({"message_id": ch["message_id"],
                                  "from": ch["from"], "to": ch["to"]})
        return {
            "case_id": self.case_id,
            "folders": folders,
            "messages": [{"id": m["id"], "folder": m["folder"], "uid": m["uid"],
                          "seen": m["seen"], "flagged": m["flagged"]}
                         for m in self.messages],
            "moves": moves,
            "rules": [EC.rule_brief(r) for r in self.rules],
            "rule_count": len(self.rules),
            "proposals": copy.deepcopy(self.proposals),
            "proposal_count": len(self.proposals),
            "events": copy.deepcopy(self.events),
        }
