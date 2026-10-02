#!/usr/bin/env python3
"""Deterministic tool simulator for the assistant benchmark.

Mirrors the app's tool result SHAPES (see engine.py _tool_*) but answers from
the synthetic corpus. No side effects on any real mailbox; actions are recorded
for scoring. One instance per case execution.

Usage:
    sim = ToolSim(corpus_dir)
    res = sim.call("search_messages", {"query": "alice"})   # -> dict
"""
import json
import os
import re
import time

STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "from", "about",
        "what", "did", "say", "any", "is", "are", "was", "me", "my", "i", "it", "that",
        "this", "with", "at", "by", "email", "emails", "mail", "message", "messages"}


def _tok(s):
    return [w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if w and w not in STOP and len(w) > 1]


class ToolSim:
    def __init__(self, corpus_dir):
        self.dir = corpus_dir
        self.messages = []
        with open(os.path.join(corpus_dir, "messages.jsonl")) as f:
            for line in f:
                m = json.loads(line)
                m["uid"] = m["id"]
                m["status"] = "classified"
                m["seen"] = m["id"] not in (160, 190, 212, 293)
                m["snippet"] = m["body"][:200]
                self.messages.append(m)
        self.by_id = {m["id"]: m for m in self.messages}
        self.folders = {}
        for m in self.messages:
            fd = self.folders.setdefault(m["folder"], {"total": 0, "unseen": 0})
            fd["total"] += 1
            if not m["seen"]:
                fd["unseen"] += 1
        self.folders.setdefault("Drafts", {"total": 3, "unseen": 0})
        self.folders.setdefault("Archive", {"total": 0, "unseen": 0})
        self.calls = []          # [{"name","args","ok","summary"}]
        self.proposals = []      # propose_rule / propose_flow accepted payloads
        self.tagged = [          # synthetic tag set (for list_tagged cases)
            {"id": 160, "subject": "[rigel-ci] Build #4821 failed on main",
             "from": "notifications@rigel-ci.example", "user_tag": "CI"},
            {"id": 161, "subject": "[rigel-ci] Build #4822 passed on main",
             "from": "notifications@rigel-ci.example", "user_tag": "CI"},
            {"id": 162, "subject": "[rigel-ci] Nightly benchmarks: 3 regressions detected",
             "from": "notifications@rigel-ci.example", "user_tag": "CI"},
        ]
        # synthetic world rules (mirrors the live app's shape: id + name + enabled)
        self.rules = [
            {"id": 1, "name": "Newsletter Filter", "enabled": True,
             "conds": [{"field": "subject", "op": "contains", "value": "Weekly"}]},
            {"id": 2, "name": "Promo Filter", "enabled": True,
             "conds": [{"field": "from", "op": "contains", "value": "techbazaar"}]},
            {"id": 3, "name": "Protect Alice", "enabled": True, "guard": True,
             "conds": [{"field": "from", "op": "contains", "value": "alice.chan"}]},
            {"id": 4, "name": "CI Notifications", "enabled": True,
             "conds": [{"field": "from", "op": "contains", "value": "rigel-ci"}]},
            {"id": 5, "name": "PO/DPO Filter", "enabled": True,
             "conds": [{"field": "subject", "op": "contains", "value": "PO-"}]},
            {"id": 6, "name": "PO/DPO Filter (duplicate)", "enabled": False,
             "conds": [{"field": "subject", "op": "contains", "value": "PO/DPO"}]},
        ]
        self.flows = [
            {"id": 15, "name": "Meridian paperwork flow", "enabled": True,
             "steps": ["move to Meridian", "draft ack (fixed)"]},
        ]
        self._recv = 0

    # ---------------- helpers ----------------
    def _find(self, args):
        """Resolve message_id or folder+uid -> message dict or (None, err)."""
        mid = args.get("message_id")
        if mid not in (None, "", 0, "0", "null"):
            try:
                m = self.by_id.get(int(mid))
            except (TypeError, ValueError):
                m = None
            if m is None:
                return None, "no indexed message with id %r (search_messages lists ids)" % mid
            return m, None
        folder = (args.get("folder") or "").strip()
        uid = args.get("uid")
        if not folder or uid in (None, "", 0, "0"):
            return None, "identify the message by message_id, or by folder AND uid"
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return None, "uid must be an integer"
        for m in self.messages:
            if m["folder"] == folder and m["uid"] == uid:
                return m, None
        return None, "no message with uid %s in folder %r" % (uid, folder)

    def _date_key(self, d):
        # "Mon, 14 Sep 2026 09:12:00 +0800" -> sortable int
        try:
            t = time.strptime(d.split(" +")[0].replace(",", ""), "%a %d %b %Y %H:%M:%S")
            return int(time.mktime(t))
        except Exception:
            return 0

    def _iso_from(self, d):
        try:
            t = time.strptime(d.split(" +")[0].replace(",", ""), "%a %d %b %Y %H:%M:%S")
            return time.strftime("%Y-%m-%d", t)
        except Exception:
            return ""

    def _row(self, m, snippet_key="snippet"):
        return {"id": m["id"], "folder": m["folder"], "uid": m["uid"],
                "from": m["from"].split("<")[-1].strip("<>"),
                "subject": m["subject"], "date": m["date"], "status": m["status"],
                "category": m["category"], "seen_at": self._iso_from(m["date"]),
                snippet_key: ("[" + m["folder"] + "] " + m["subject"])[:120]}

    def log(self, name, args, res):
        self.calls.append({"name": name, "args": args, "ok": bool(res.get("ok")),
                           "summary": (res.get("summary") or "")[:300]})

    # ---------------- tools ----------------
    def t_search_messages(self, a):
        q = (a.get("query") or "").strip().lower()
        sender = (a.get("sender") or "").strip().lower()
        subject = (a.get("subject") or "").strip().lower()
        folder = (a.get("folder") or "").strip()
        since = (a.get("since") or "").strip()
        until = (a.get("until") or "").strip()
        try:
            limit = max(1, min(int(a.get("limit") or 20), 100))
        except (TypeError, ValueError):
            limit = 20
        rows = []
        for m in self.messages:
            blob = (m["from"] + " " + m["subject"] + " " + m["body"]).lower()
            if q and q not in blob:
                continue
            if sender and sender not in m["from"].lower():
                continue
            if subject and subject not in m["subject"].lower():
                continue
            if folder and m["folder"] != folder:
                continue
            iso = self._iso_from(m["date"])
            if since and iso < since:
                continue
            if until and iso > until:
                continue
            rows.append(m)
        rows.sort(key=lambda m: -self._date_key(m["date"]))
        out = [self._row(m) for m in rows[:limit]]
        return {"ok": True, "summary": "%d of %d indexed messages" % (len(out), len(rows)),
                "result": {"total_matched": len(rows), "returned": len(out), "offset": 0,
                           "note": "Local index only (what the scanner has seen). Use search_mail for the full mailbox history.",
                           "messages": out}}

    def t_search_mail(self, a):
        folder = (a.get("folder") or "INBOX").strip() or "INBOX"
        if folder not in self.folders:
            return {"ok": False, "summary": "cannot open folder %r" % folder,
                    "result": {"error": "cannot open folder %r" % folder,
                               "hint": "call list_folders for valid names"}}
        fc = (a.get("from_contains") or "").strip().lower()
        sc = (a.get("subject_contains") or "").strip().lower()
        bc = (a.get("body_contains") or "").strip().lower()
        since = (a.get("since") or "").strip()
        before = (a.get("before") or "").strip()
        unseen = bool(a.get("unseen_only"))
        try:
            limit = max(1, min(int(a.get("limit") or 20), 50))
        except (TypeError, ValueError):
            limit = 20
        rows = []
        for m in self.messages:
            if m["folder"] != folder:
                continue
            if fc and fc not in m["from"].lower():
                continue
            if sc and sc not in m["subject"].lower():
                continue
            if bc and bc not in m["body"].lower():
                continue
            iso = self._iso_from(m["date"])
            if since and iso < since:
                continue
            if before and iso >= before:
                continue
            if unseen and m["seen"]:
                continue
            rows.append(m)
        rows.sort(key=lambda m: -self._date_key(m["date"]))
        out = [self._row(m, "snippet") for m in rows[:limit]]
        return {"ok": True, "summary": "%d of %d in %s" % (len(out), len(rows), folder),
                "result": {"folder": folder, "total_matched": len(rows), "returned": len(out),
                           "note": "Newest first. Use folder + uid with read_message / move_message / flag_message.",
                           "messages": out}}

    def t_semantic_search(self, a):
        q = (a.get("query") or "").strip()
        if not q:
            return {"ok": False, "summary": "query is required", "result": {"error": "query is required"}}
        try:
            limit = max(1, min(int(a.get("limit") or 8), 20))
        except (TypeError, ValueError):
            limit = 8
        folder = (a.get("folder") or "").strip()
        since = (a.get("since") or "").strip()
        toks = _tok(q)
        scored = []
        for m in self.messages:
            if folder and m["folder"] != folder:
                continue
            if since and self._iso_from(m["date"]) < since:
                continue
            tag_blob = " ".join(m["semantic_tags"]).lower()
            body = m["body"].lower()
            score = 0.0
            for t in toks:
                if t in tag_blob:
                    score += 1.0
                if t in body:
                    score += 0.5
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda x: (-x[0], -self._date_key(x[1]["date"])))
        items = [{"message_id": m["id"], "folder": m["folder"],
                  "from": m["from"].split("<")[-1].strip("<>"), "subject": m["subject"],
                  "date": m["date"], "excerpt": ("[" + m["folder"] + "] " + m["subject"] + " — " + m["body"][:160])}
                 for _s, m in scored[:limit]]
        summary = "%d semantic match(es): %s" % (
            len(items), "; ".join((i["subject"] or "")[:40] for i in items[:3]))
        return {"ok": True, "summary": summary,
                "result": {"query": q, "count": len(items),
                           "note": "Cite as [msg:ID]; use read_message with message_id for full text.",
                           "results": items}}

    def t_read_message(self, a):
        m, err = self._find(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        data = self._row(m, "snippet")
        data.update({"to_addr": m["to"], "body": m["body"][:4000], "msgid": "<sim-%d@bench>" % m["id"]})
        return {"ok": True, "summary": "read %r in %s" % (m["subject"][:60], m["folder"]),
                "result": data}

    def t_mailbox_overview(self, _a):
        data = {"indexed_messages": len(self.messages),
                "folders": [{"name": k, "total": v["total"], "unseen": v["unseen"]}
                            for k, v in sorted(self.folders.items())],
                "by_status": {"classified": len(self.messages)},
                "categories": ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"],
                "rules_text": self.rules_text()}
        return {"ok": True, "summary": "%d indexed messages · %d folders · %d rules"
                                         % (len(self.messages), len(self.folders), len(self.rules)),
                "result": data}

    def t_list_folders(self, _a):
        folders = [{"name": k, "total": v["total"], "unseen": v["unseen"]}
                   for k, v in sorted(self.folders.items())]
        return {"ok": True, "summary": "%d folders" % len(folders), "result": {"folders": folders}}

    def t_list_rules(self, _a):
        items = [{"id": r["id"], "name": r["name"], "enabled": r["enabled"]} for r in self.rules]
        return {"ok": True, "summary": "%d rule(s) (top to bottom, first match wins)" % len(items),
                "result": {"rules": items, "text": self.rules_text()}}

    def t_list_flows(self, _a):
        items = [{"id": f["id"], "name": f["name"], "enabled": f["enabled"]} for f in self.flows]
        return {"ok": True, "summary": "%d flow(s)" % len(items),
                "result": {"flows": items, "text": self.flows_text()}}

    def t_list_tagged(self, a):
        try:
            limit = max(1, min(int(a.get("limit") or 40), 100))
        except (TypeError, ValueError):
            limit = 40
        items = self.tagged[:limit]
        return {"ok": True, "summary": "%d tagged example(s)" % len(items),
                "result": {"count": len(items), "tagged": items}}

    def t_move_message(self, a):
        target = (a.get("target_folder") or "").strip()
        if not target:
            return {"ok": False, "summary": "target_folder is required",
                    "result": {"error": "target_folder is required"}}
        m, err = self._find(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        src = m["folder"]
        if target not in self.folders:
            self.folders[target] = {"total": 0, "unseen": 0}
        self.folders[src]["total"] -= 1
        self.folders[target]["total"] += 1
        m["folder"] = target
        return {"ok": True, "summary": "moved to %s" % target,
                "result": {"moved": {"folder": src, "uid": m["uid"], "to": target,
                                      "message_id": m["id"]}}}

    def t_flag_message(self, a):
        seen = a.get("seen")
        flagged = a.get("flagged")
        if seen is None and flagged is None:
            return {"ok": False, "summary": "set seen and/or flagged",
                    "result": {"error": "nothing to change"}}
        m, err = self._find(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        ops = []
        if seen is not None:
            m["seen"] = bool(seen)
            ops.append("seen=%s" % bool(seen))
        if flagged is not None:
            m["flagged"] = bool(flagged)
            ops.append("flagged=%s" % bool(flagged))
        return {"ok": True, "summary": "flags updated",
                "result": {"folder": m["folder"], "uid": m["uid"], "changes": ops,
                           "message_id": m["id"]}}

    def t_create_folder(self, a):
        name = (a.get("name") or "").strip()
        if not name:
            return {"ok": False, "summary": "name is required", "result": {"error": "name required"}}
        existed = name in self.folders
        self.folders.setdefault(name, {"total": 0, "unseen": 0})
        return {"ok": True, "summary": ("folder existed: " if existed else "folder created: ") + name,
                "result": {"folder": name, "created": not existed}}

    def t_propose_rule(self, a):
        errors = []
        if not (a.get("name") or "").strip():
            errors.append("name is required")
        conds = a.get("conditions") or []
        if not isinstance(conds, list) or (not conds and a.get("actions")):
            errors.append("conditions required unless it is a guard rule")
        for c in conds:
            if not isinstance(c, dict) or (c.get("field") or "") not in ("from", "to", "subject", "body") \
                    or (c.get("op") or "contains") not in ("contains", "equals", "regex") \
                    or not str(c.get("value") or "").strip():
                errors.append("bad condition %r" % (c,))
        if errors:
            return {"ok": False, "summary": "rule invalid: " + "; ".join(errors[:3]),
                    "result": {"errors": errors}}
        self.proposals.append({"kind": "rule", **a})
        return {"ok": True, "summary": "proposed rule %r (%d condition(s)) — add it from the card"
                                       % (a.get("name"), len(conds)),
                "result": {"proposal": {k: a.get(k) for k in
                                        ("name", "match_mode", "conditions", "actions", "placement", "rationale")},
                           "note": "waiting for the user's one-click approval"}}

    def t_propose_flow(self, a):
        if not (a.get("name") or "").strip() or not (a.get("steps") or []):
            return {"ok": False, "summary": "flow invalid: name and steps are required",
                    "result": {"errors": ["name and steps required"]}}
        self.proposals.append({"kind": "flow", **a})
        return {"ok": True, "summary": "proposed flow %r (%d step(s)) — add it from the card"
                                       % (a.get("name"), len(a.get("steps") or [])),
                "result": {"proposal": a, "note": "waiting for the user's one-click approval"}}

    def t_delete_rule(self, a):
        try:
            rid = int(a.get("rule_id") or a.get("id") or 0)
        except (TypeError, ValueError):
            rid = 0
        row = next((r for r in self.rules if r["id"] == rid), None)
        if not row:
            return {"ok": False, "summary": "no rule #%s" % a.get("id"),
                    "result": {"error": "rule not found"}}
        self.rules = [r for r in self.rules if r["id"] != rid]
        return {"ok": True, "summary": "deleted rule #%d %r" % (rid, row["name"]),
                "result": {"id": rid, "deleted": True}}

    def t_set_rule_enabled(self, a):
        try:
            rid = int(a.get("rule_id") or a.get("id") or 0)
        except (TypeError, ValueError):
            rid = 0
        row = next((r for r in self.rules if r["id"] == rid), None)
        if not row:
            return {"ok": False, "summary": "no rule #%s" % a.get("id"),
                    "result": {"error": "rule not found"}}
        row["enabled"] = bool(a.get("enabled"))
        return {"ok": True, "summary": "rule #%d %s" % (rid, "enabled" if row["enabled"] else "paused"),
                "result": {"id": rid, "enabled": row["enabled"]}}

    def t_classify_message(self, a):
        m, err = self._find(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        return {"ok": True, "summary": "classified as %s" % m["category"],
                "result": {"message_id": m["id"], "category": m["category"],
                           "needs_reply": m["needs_reply"],
                           "summary": m["body"][:90], "reason": "designed label"}}

    def t_tag_message(self, a):
        if not (a.get("tag") or "").strip():
            return {"ok": False, "summary": "tag is required", "result": {"error": "tag required"}}
        m, err = self._find(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        return {"ok": True, "summary": "tagged %r with %r" % (m["subject"][:40], a.get("tag")),
                "result": {"message_id": m["id"], "tag": a.get("tag")}}

    def t_draft_reply(self, a):
        m, err = self._find(a)
        if err and "message_id" in a:
            return {"ok": False, "summary": err, "result": {"error": err}}
        self._recv += 1
        return {"ok": True, "summary": "draft saved to Drafts (for the user to review)",
                "result": {"draft_id": 9000 + self._recv,
                           "instructions": (a.get("instructions") or "")[:200],
                           "note": "the draft is waiting in the Drafts folder"}}

    def t_delete_message(self, a):
        return {"ok": False, "permission_denied": "delete",
                "summary": "delete_message is disabled (Agent permissions: delete = off)",
                "result": {"error": "permission_denied", "capability": "delete", "level": "off",
                           "note": "This capability is switched off in Settings - AI settings - Agent permissions. Tell the user; do not retry."}}

    def t_send_message(self, a):
        return {"ok": False, "permission_denied": "send",
                "summary": "send_message is disabled (Agent permissions: send = off)",
                "result": {"error": "permission_denied", "capability": "send", "level": "off",
                           "note": "This capability is switched off in Settings - AI settings - Agent permissions. Tell the user; do not retry."}}

    # ---------------- dispatch ----------------
    def call(self, name, args):
        fn = getattr(self, "t_" + str(name or ""), None)
        if fn is None:
            res = {"ok": False, "summary": "unknown tool %r" % name,
                   "result": {"error": "unknown tool",
                              "available": [n for n in sorted(self._tools())]}}
        else:
            try:
                res = fn(args if isinstance(args, dict) else {})
            except Exception as exc:
                res = {"ok": False, "summary": "tool failed: %r" % exc,
                       "result": {"error": repr(exc)}}
        self.log(name, args, res)
        return res

    def _tools(self):
        return [n[2:] for n in dir(self) if n.startswith("t_")]

    # ---------------- text forms (match engine._rules_to_text style) ----------------
    def rules_text(self):
        return "\n".join(
            "%d. %s [%s]: %s" % (r["id"], r["name"], "enabled" if r["enabled"] else "disabled",
                                  ("keep in place (guard)" if r.get("guard") else
                                   ";".join("%s %s %r" % (c["field"], c["op"], c["value"]) for c in r["conds"])))
            for r in self.rules)

    def flows_text(self):
        return "\n".join("%d. %s [%s]: %s" % (f["id"], f["name"],
                                              "enabled" if f["enabled"] else "disabled",
                                              " → ".join(f["steps"])) for f in self.flows)


if __name__ == "__main__":
    sim = ToolSim(os.path.join(os.path.dirname(__file__), "..", "corpus"))
    print(sim.call("search_messages", {"query": "singapore"})["summary"])
    print(sim.call("semantic_search", {"query": "hotel changed"})["summary"])
    print(sim.call("move_message", {"message_id": 132, "target_folder": "Receipts"})["summary"])
    print(sim.call("unknown_thing", {})["summary"])
    print(sim.rules_text()[:200])
