#!/usr/bin/env python3
"""End-to-end test for Mail Triage with mock IMAP + mock LLM servers.

Nothing here touches real mail: a fake IMAP server stands in for the
email-oauth2-proxy, and a fake OpenAI-compatible endpoint stands in for the LLM.
Covers: rule matching, rule actions (move), dry-run mode, LLM classification,
LLM auto-filing, reply drafting, draft saving (APPEND), UIDVALIDITY re-indexing,
route smoke tests.

Usage:  .venv/bin/python tests/mock_e2e.py
"""
import email.utils
import hashlib
import json
import math
import os
import re
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

FAKE_DRAFT = "Hi, thanks for the note - I will reply properly shortly. - Sean"

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("  \u2713 %s" % name)
    else:
        failed += 1
        print("  \u2717 FAIL: %s" % name)


def section(name):
    print("\n== %s ==" % name)


# ---------------------------------------------------------------- mock state

class MockState:
    def __init__(self):
        self.lock = threading.RLock()
        self.folders = {}
        self.appended = []
        self._next_uid = 1

    def ensure(self, name, flags=""):
        with self.lock:
            if name not in self.folders:
                self.folders[name] = {"flags": flags, "uids": [], "msgs": {}, "uidvalidity": 1}
            return self.folders[name]

    def get(self, name):
        with self.lock:
            return self.folders.get(name)

    def add(self, folder, raw, flags=None):
        with self.lock:
            f = self.ensure(folder)
            uid = self._next_uid
            self._next_uid += 1
            f["uids"].append(uid)
            f["msgs"][uid] = {"raw": raw, "flags": set(flags or [])}
            return uid

    def move(self, src, uid, dst):
        with self.lock:
            s = self.ensure(src)
            d = self.ensure(dst)
            if uid in s["msgs"]:
                msg = s["msgs"].pop(uid)
                s["uids"].remove(uid)
                d["uids"].append(uid)
                d["msgs"][uid] = msg
                return uid
        return None

    def copy(self, src, uid, dst):
        with self.lock:
            s = self.ensure(src)
            d = self.ensure(dst)
            if uid in s["msgs"]:
                new_uid = self._next_uid
                self._next_uid += 1
                d["uids"].append(new_uid)
                d["msgs"][new_uid] = {"raw": s["msgs"][uid]["raw"],
                                      "flags": set(s["msgs"][uid]["flags"])}
                return new_uid
        return None


# ---------------------------------------------------------------- mock IMAP

class IMAPHandler(socketserver.StreamRequestHandler):
    def send(self, line):
        if isinstance(line, str):
            line = line.encode()
        self.wfile.write(line + b"\r\n")
        self.wfile.flush()

    def raw_send(self, data):
        self.wfile.write(data)
        self.wfile.flush()

    def handle(self):
        self.cur = "INBOX"
        self.send("* OK [CAPABILITY IMAP4rev1 UIDPLUS MOVE] Mock IMAP ready")
        while True:
            line = self.rfile.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if not text:
                continue
            parts = text.split(" ", 2)
            tag = parts[0]
            if len(parts) < 2:
                continue
            cmd = parts[1].upper()
            rest = parts[2].strip() if len(parts) > 2 else ""
            if cmd == "CAPABILITY":
                self.send("* CAPABILITY IMAP4rev1 UIDPLUS MOVE")
                self.send("%s OK CAPABILITY completed" % tag)
            elif cmd in ("LOGIN", "NOOP", "CHECK"):
                self.send("%s OK %s completed" % (tag, cmd))
            elif cmd == "LOGOUT":
                self.send("* BYE closing")
                self.send("%s OK LOGOUT completed" % tag)
                break
            elif cmd == "CLOSE":
                self.send("%s OK CLOSE completed" % tag)
            elif cmd == "LIST":
                with self.server.state.lock:
                    lines = ['* LIST (%s) "/" "%s"' % (f["flags"] or "\\HasNoChildren", name)
                             for name, f in self.server.state.folders.items()]
                for l in lines:
                    self.send(l)
                self.send("%s OK LIST completed" % tag)
            elif cmd == "CREATE":
                self.server.state.ensure(rest.strip().strip('"'))
                self.send("%s OK CREATE completed" % tag)
            elif cmd == "SELECT":
                self.do_select(tag, rest.strip().strip('"'))
            elif cmd == "STATUS":
                m = re.match(r'"?([^"]+)"?\s+\(([^)]*)\)', rest)
                name = m.group(1) if m else rest.strip().strip('"')
                items = (m.group(2).upper() if m else "MESSAGES")
                f = self.server.state.get(name)
                if f is None:
                    self.send("%s NO no such mailbox" % tag)
                else:
                    parts = []
                    if "MESSAGES" in items:
                        parts.append("MESSAGES %d" % len(f["uids"]))
                    if "UNSEEN" in items:
                        parts.append("UNSEEN %d" % sum(
                            1 for u in f["uids"] if "\\Seen" not in f["msgs"][u]["flags"]))
                    self.send('* STATUS "%s" (%s)' % (name, " ".join(parts)))
                    self.send("%s OK STATUS completed" % tag)
            elif cmd == "EXPUNGE":
                self.do_expunge(tag)
            elif cmd == "APPEND":
                self.do_append(tag, rest)
            elif cmd == "UID":
                self.do_uid(tag, rest)
            else:
                self.send("%s BAD unknown command %s" % (tag, cmd))

    def do_select(self, tag, name):
        f = self.server.state.get(name)
        if f is None:
            self.send("%s NO no such mailbox" % tag)
            return
        self.cur = name
        uidnext = (max(f["uids"]) + 1) if f["uids"] else 1
        self.send("* %d EXISTS" % len(f["uids"]))
        self.send("* 0 RECENT")
        self.send("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
        self.send("* OK [UIDVALIDITY %d] ." % f["uidvalidity"])
        self.send("* OK [UIDNEXT %d] ." % uidnext)
        self.send("* OK [PERMANENTFLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft \\*)] .")
        self.send("%s OK [READ-WRITE] SELECT completed" % tag)

    def do_expunge(self, tag):
        f = self.server.state.get(self.cur)
        removed = 0
        if f:
            with self.server.state.lock:
                for uid in list(f["uids"]):
                    if "\\Deleted" in f["msgs"][uid]["flags"]:
                        f["uids"].remove(uid)
                        f["msgs"].pop(uid)
                        removed += 1
        if removed:
            self.send("* %d EXPUNGE" % removed)
        self.send("%s OK EXPUNGE completed" % tag)

    def do_append(self, tag, rest):
        m = re.search(r"\{(\d+)\}\s*$", rest)
        if not m:
            self.send("%s BAD APPEND expecting literal" % tag)
            return
        n = int(m.group(1))
        fm = re.match(r'"?([^"\s]+)"?', rest)
        folder = fm.group(1) if fm else "INBOX"
        flags = []
        if "\\Draft" in rest:
            flags.append("\\Draft")
        self.send("+ Ready for literal data")
        data = self.rfile.read(n)
        self.connection.settimeout(1.0)
        try:
            self.rfile.readline()  # trailing CRLF, if any
        except (socket.timeout, TimeoutError, OSError):
            pass
        finally:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
        uid = self.server.state.add(folder, data, flags)
        self.server.state.appended.append({"folder": folder, "raw": data, "uid": uid})
        self.send("%s OK APPEND completed" % tag)

    def do_uid(self, tag, rest):
        st = self.server.state
        tokens = rest.split(" ", 2)
        sub = tokens[0].upper()
        arg1 = tokens[1] if len(tokens) > 1 else ""
        arg2 = tokens[2] if len(tokens) > 2 else ""
        if sub == "SEARCH":
            tokens = re.findall(r'"[^"]*"|\S+', rest)[1:]
            uids = self.search_uids(tokens)
            self.send("* SEARCH %s" % " ".join(str(u) for u in uids))
            self.send("%s OK UID SEARCH completed" % tag)
        elif sub == "FETCH":
            if ":" in arg1 or "*" in arg1:
                self.do_fetch_range(tag, arg2)
            else:
                self.do_fetch(tag, int(arg1), arg2)
        elif sub == "STORE":
            f = st.get(self.cur)
            uid = int(arg1)
            if f and uid in f["msgs"]:
                with st.lock:
                    if arg2.upper().startswith("+FLAGS"):
                        for fl in re.findall(r"\\\w+", arg2):
                            f["msgs"][uid]["flags"].add(fl)
                seq = f["uids"].index(uid) + 1
                self.send("* %d FETCH (UID %d FLAGS (%s))"
                          % (seq, uid, " ".join(sorted(f["msgs"][uid]["flags"]))))
            self.send("%s OK UID STORE completed" % tag)
        elif sub == "MOVE":
            dst = st.move(self.cur, int(arg1), arg2.strip().strip('"'))
            if dst:
                self.send("* OK [COPYUID 1 %s %s] moved" % (arg1, dst))
            self.send("%s OK UID MOVE completed" % tag)
        elif sub == "COPY":
            dst = st.copy(self.cur, int(arg1), arg2.strip().strip('"'))
            if dst:
                self.send("* OK [COPYUID 1 %s %s] copied" % (arg1, dst))
            self.send("%s OK UID COPY completed" % tag)
        else:
            self.send("%s BAD unknown UID command %s" % (tag, sub))

    # ---- SEARCH support ---------------------------------------------------

    @staticmethod
    def header_val(header_text, name):
        for line in header_text.split("\r\n"):
            if line.lower().startswith(name.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return ""

    def raw_parts(self, f, uid):
        raw = f["msgs"][uid]["raw"].decode("utf-8", "replace")
        sep = raw.find("\r\n\r\n")
        return (raw[:sep if sep >= 0 else len(raw)],
                raw[sep + 4:] if sep >= 0 else "")

    def msg_value(self, f, uid, kind, header_name=None):
        header, body = self.raw_parts(f, uid)
        if kind == "FROM":
            return self.header_val(header, "From")
        if kind == "TO":
            return self.header_val(header, "To")
        if kind == "SUBJECT":
            return self.header_val(header, "Subject")
        if kind == "DATE":
            return self.header_val(header, "Date")
        if kind == "HEADER":
            return self.header_val(header, header_name or "")
        return body  # BODY / TEXT

    def msg_ts(self, f, uid):
        d = self.msg_value(f, uid, "DATE")
        try:
            return email.utils.parsedate_to_datetime(d).timestamp()
        except Exception:
            return 0.0

    def search_uids(self, tokens):
        f = self.server.state.get(self.cur)
        if not f:
            return []
        uids = list(f["uids"])
        i = 0
        while i < len(tokens):
            c = tokens[i].upper()
            val = tokens[i + 1].strip('"') if i + 1 < len(tokens) else ""
            if c == "ALL":
                i += 1
            elif c == "UNSEEN":
                uids = [u for u in uids if "\\Seen" not in f["msgs"][u]["flags"]]
                i += 1
            elif c in ("SEEN", "FLAGGED", "UNFLAGGED"):
                if c == "FLAGGED":
                    uids = [u for u in uids if "\\Flagged" in f["msgs"][u]["flags"]]
                elif c == "UNFLAGGED":
                    uids = [u for u in uids if "\\Flagged" not in f["msgs"][u]["flags"]]
                else:
                    uids = [u for u in uids if "\\Seen" in f["msgs"][u]["flags"]]
                i += 1
            elif c == "UID":
                if ":" in val:
                    a, b = val.split(":", 1)
                    lo = int(a)
                    hi = max(f["uids"]) if b == "*" else int(b)
                    uids = [u for u in uids if lo <= u <= hi]
                elif val.isdigit():
                    uids = [u for u in uids if u == int(val)]
                i += 2
            elif c in ("FROM", "TO", "SUBJECT", "BODY", "TEXT"):
                uids = [u for u in uids
                        if val.lower() in (self.msg_value(f, u, c) or "").lower()]
                i += 2
            elif c == "HEADER":
                name = val
                needle = tokens[i + 2].strip('"') if i + 2 < len(tokens) else ""
                uids = [u for u in uids
                        if needle.lower() in (self.msg_value(f, u, "HEADER", name) or "").lower()]
                i += 3
            elif c in ("SINCE", "BEFORE"):
                cut = time.mktime(time.strptime(val, "%d-%b-%Y"))
                if c == "SINCE":
                    uids = [u for u in uids if self.msg_ts(f, u) >= cut]
                else:
                    uids = [u for u in uids if self.msg_ts(f, u) < cut]
                i += 2
            else:
                i += 1
        return uids

    def do_fetch_range(self, tag, spec):
        f = self.server.state.get(self.cur)
        if not f:
            self.send("%s OK UID FETCH completed" % tag)
            return
        su = spec.upper()
        for uid in list(f["uids"]):
            msg = f["msgs"].get(uid)
            if not msg:
                continue
            seq = f["uids"].index(uid) + 1
            raw = msg["raw"]
            sep = raw.find(b"\r\n\r\n")
            header = raw[:sep + 4] if sep >= 0 else raw
            body = raw[sep + 4:] if sep >= 0 else b""
            if "HEADER.FIELDS" in su:
                data, label = header, "BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)]"
            elif "TEXT" in su:
                data, label = body, "BODY[TEXT]"
            else:
                data, label = raw, "RFC822"
            self.raw_send(b"* %d FETCH (UID %d %s {%d}\r\n" % (seq, uid, label.encode(), len(data))
                          + data + b")\r\n")
        self.send("%s OK UID FETCH completed" % tag)

    @staticmethod
    def _section_part(raw, section):
        from email import message_from_bytes
        msg = message_from_bytes(raw)

        def walk(part, sec):
            if part.is_multipart():
                for i, sub in enumerate(part.get_payload() or [], 1):
                    found = walk(sub, ("%s.%d" % (sec, i)) if sec else str(i))
                    if found is not None:
                        return found
                return None
            return part if (sec or "1") == section else None

        return walk(msg, "")

    def section_bytes(self, raw, section):
        # like a real IMAP server: return the section AS STORED (CTE applied)
        part = self._section_part(raw, section)
        if part is None:
            return b""
        pl = part.get_payload()
        if isinstance(pl, str):
            return pl.encode("utf-8", "replace")
        return pl if isinstance(pl, bytes) else b""

    def section_headers(self, raw, section):
        part = self._section_part(raw, section)
        if part is None:
            return b""
        lines = ["%s: %s" % (k, v) for k, v in part.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def do_fetch(self, tag, uid, spec):
        f = self.server.state.get(self.cur)
        msg = f["msgs"].get(uid) if f else None
        if not msg:
            self.send("%s NO uid not present" % tag)
            return
        seq = f["uids"].index(uid) + 1
        raw = msg["raw"]
        secs = re.findall(r"BODY\.PEEK\[(\d(?:\.\d+)*)(\.MIME)?\]", spec, re.I)
        if secs:
            out = b"* %d FETCH (UID %d " % (seq, uid)
            for i, (snum, mime) in enumerate(secs):
                data = self.section_headers(raw, snum) if mime else self.section_bytes(raw, snum)
                label = "BODY[%s%s]" % (snum, ".MIME" if mime else "")
                out += b"%s {%d}\r\n" % (label.encode(), len(data)) + data
                out += b" " if i < len(secs) - 1 else b")\r\n"
            self.raw_send(out)
            self.send("%s OK UID FETCH completed" % tag)
            return
        sep = raw.find(b"\r\n\r\n")
        header = raw[:sep + 4] if sep >= 0 else raw
        body = raw[sep + 4:] if sep >= 0 else b""
        su = spec.upper()
        if "HEADER.FIELDS" in su:
            data = header
            label = "BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)]"
        elif "TEXT" in su:
            pm = re.search(r"<0\.(\d+)>", spec)
            data = body[:int(pm.group(1))] if pm else body
            label = "BODY[TEXT]"
        else:
            data = raw
            label = "RFC822"
        self.raw_send(b"* %d FETCH (UID %d %s {%d}\r\n" % (seq, uid, label.encode(), len(data))
                      + data + b")\r\n")
        self.send("%s OK UID FETCH completed" % tag)


# ---------------------------------------------------------------- mock LLM

class LLMHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        messages = payload.get("messages") or []
        system = messages[0]["content"] if messages and isinstance(messages[0].get("content"), str) else ""
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                user = m["content"]
                break
        self.server.calls.append({"system": system, "user": user, "payload": payload,
                                  "auth": self.headers.get("Authorization")})
        if getattr(self.server, "reject_ctk", False) and "chat_template_kwargs" in payload:
            body = b'{"error": {"message": "unrecognized field chat_template_kwargs"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if payload.get("stream"):
            self.stream_reply(payload, system, user)
            return
        is_classify = False
        reasoning = ""
        if "write email replies" in system:
            content = FAKE_DRAFT
        elif "manually tagged" in system:
            content = json.dumps({
                "reply": "Learned rules from your tags.",
                "proposed_rules": [
                    {"name": "Tagged receipts", "match_mode": "any",
                     "conditions": [{"field": "subject", "op": "contains", "value": "invoice"}],
                     "actions": {"move_to": "Receipts"}, "rationale": "from your tags"},
                    {"name": "Broken", "match_mode": "all", "conditions": [], "actions": {}},
                ]})
        elif "connectivity test" in system:
            content = "ok"
        elif "condense an ai assistant" in system.lower():
            content = "Checked the budget mail and moved it"
        else:
            is_classify = True
            t = user.lower()
            if "permfail" in t:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if "newsletter" in t:
                cat, conf = "Newsletter", 0.95
            elif "invoice" in t or "receipt" in t:
                cat, conf = "Receipt", 0.9
            elif "lunch" in t:
                cat, conf = "Personal", 0.8
            else:
                cat, conf = "Action", 0.85
            reasoning = "mock thinking about " + cat
            content = json.dumps({
                "category": cat,
                "needs_reply": ("lunch" in t or "budget" in t),
                "confidence": conf,
                "summary": "mock: " + cat,
                "reason": "because it says " + cat,
            })
        if is_classify:
            with self.server.llm_lock:
                self.server.inflight += 1
                if self.server.inflight > self.server.max_inflight:
                    self.server.max_inflight = self.server.inflight
            time.sleep(0.05)  # widen the window so parallel workers overlap
        message = {"content": content}
        if reasoning:
            message["reasoning"] = reasoning
        body = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if is_classify:
            with self.server.llm_lock:
                self.server.inflight -= 1

    # ---- streaming (assistant agent) --------------------------------------

    def sse_write(self, data):
        self.wfile.write(data)
        self.wfile.flush()

    def sse(self, obj):
        self.sse_write(("data: %s\n\n" % json.dumps(obj)).encode())

    def sse_done(self):
        self.sse_write(b"data: [DONE]\n\n")

    def sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def delta(self, **kw):
        self.sse({"choices": [{"index": 0, "delta": kw}]})

    def tool_delta(self, idx, name, args, cid):
        self.sse({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": idx, "id": cid, "type": "function",
             "function": {"name": name, "arguments": ""}}]}}]})
        args_str = json.dumps(args)
        for i in range(0, len(args_str), 10):  # fragment the arguments like vLLM does
            self.sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": idx, "function": {"arguments": args_str[i:i + 10]}}]}}]})

    def stream_reply(self, payload, system, user):
        if "mail operations assistant" in system:
            return self.stream_agent(payload, user)
        # generic streamed reply — exercises the streaming-fallback path
        self.sse_start()
        self.delta(role="assistant")
        for word in ("streaming ", "from ", "the fallback ", "works."):
            self.delta(content=word)
        self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self.sse({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 5,
                                           "total_tokens": 6}})
        self.sse_done()

    def stream_agent(self, payload, user):
        if "streamfail" in (user or "").lower():
            body = b'{"error": "mock stream failure"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        messages = payload["messages"]
        step = sum(1 for m in messages
                   if m.get("role") == "assistant" and m.get("tool_calls"))
        if "semantic search" in (user or "").lower():
            self.sse_start()
            self.delta(role="assistant")
            if step == 0:
                self.delta(reasoning="I will look this up with semantic_search.")
                self.tool_delta(0, "semantic_search", {"query": "vendor invoice"}, "call_%d_0" % step)
                self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            elif step == 1:
                self.delta(reasoning="The invoice is message 3; I will cite it.")
                self.delta(content="Found it: [msg:3] - the invoice from the vendor.")
                self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            else:
                self.delta(content="Done.")
                self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.sse({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 5,
                                               "total_tokens": 10}})
            self.sse_done()
            return
        self.sse_start()
        self.delta(role="assistant")
        if step == 0:
            self.delta(reasoning="The user wants budget mail handled. ")
            self.delta(reasoning="I will search the mailbox first.")
            self.tool_delta(0, "search_mail", {"subject_contains": "budget"}, "call_%d_0" % step)
            self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        elif step == 1:
            self.delta(reasoning="Found matches. I will file the lunch mail and propose a rule.")
            self.delta(content="Working on it. ")
            self.tool_delta(0, "move_message",
                            {"folder": "INBOX", "uid": 4, "target_folder": "Personal"},
                            "call_%d_0" % step)
            self.tool_delta(1, "propose_rule",
                            {"name": "Budget mail to Budget", "match_mode": "any",
                             "conditions": [{"field": "subject", "op": "contains", "value": "budget"}],
                             "actions": {"move_to": "Budget"}, "rationale": "test rule"},
                            "call_%d_1" % step)
            self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            self.delta(content="Done. I moved the lunch mail to Personal and proposed a rule "
                               "for budget mail — add it below if it looks right.")
            self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self.sse({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                                           "total_tokens": 30}})
        self.sse_done()

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- mock TEI (embed + rerank)

SEM_GROUPS = [
    ["invoice", "payment", "receipt", "budget", "refund", "paid", "money"],
    ["lunch", "dinner", "coffee", "food", "restaurant"],
    ["newsletter", "digest", "promo", "deals", "sale", "discount"],
    ["student", "attendance", "class", "course", "grade", "university"],
]


def semantic_vec(text, dim=8):
    """Deterministic 'semantic' embedding: keyword-group axes + hash noise."""
    t = (text or "").lower()
    v = [0.0] * dim
    for i, words in enumerate(SEM_GROUPS):
        for w in words:
            if w in t:
                v[i] += 1.0
    h = hashlib.sha256(t.encode()).digest()
    for j in range(len(SEM_GROUPS), dim):
        v[j] = 0.02 * (h[j] / 255.0)
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def kw_score(q, t):
    qw = set(re.findall(r"[a-z0-9]+", (q or "").lower()))
    tw = set(re.findall(r"[a-z0-9]+", (t or "").lower()))
    return round(len(qw & tw) / (len(qw) or 1), 4)


class TEIHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        self.server.calls.append({"path": self.path, "payload": payload})
        if self.path.startswith("/embeddings"):
            # OpenAI-compatible embeddings shape ({"input": [...], "model": ...})
            inputs = payload.get("input") or []
            body = json.dumps({
                "object": "list", "model": payload.get("model") or "mock-embed",
                "data": [{"object": "embedding", "index": i, "embedding": semantic_vec(t)}
                         for i, t in enumerate(inputs)],
            }).encode()
        elif self.path.startswith("/embed"):
            inputs = payload.get("inputs") or []
            body = json.dumps([semantic_vec(t) for t in inputs]).encode()
        elif self.path.startswith("/rerank"):
            q = payload.get("query") or ""
            if "documents" in payload:  # Cohere/Jina-style request shape
                texts = payload.get("documents") or []
                results = [{"index": i, "relevance_score": kw_score(q, t)}
                           for i, t in enumerate(texts)]
                results.sort(key=lambda x: -x["relevance_score"])
                body = json.dumps({"results": results}).encode()
            else:
                texts = payload.get("texts") or []
                out = [{"index": i, "score": kw_score(q, t)} for i, t in enumerate(texts)]
                out.sort(key=lambda x: -x["score"])
                body = json.dumps(out).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_response(200 if self.path.startswith("/health") else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- helpers

def add_msg(state, frm, subj, body, msgid, folder="INBOX", date="Wed, 30 Sep 2026 10:00:00 +0800"):
    raw = ("From: %s\r\nTo: seanyong@ust.hk\r\nSubject: %s\r\n"
           "Date: %s\r\nMessage-ID: <%s>\r\n"
           "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n%s"
           % (frm, subj, date, msgid, body)).encode()
    return state.add(folder, raw)


def main():
    state = MockState()
    state.ensure("INBOX", "\\HasNoChildren")
    state.ensure("Drafts", "\\HasNoChildren \\Drafts")
    state.ensure("Sent", "\\HasNoChildren \\Sent")
    add_msg(state, "boss@work.com", "Budget review Q4", "Please review the budget before Friday.", "b1@x")
    add_msg(state, "newsletter@deals.com", "Weekly newsletter: top deals", "This week's deals are inside.", "n1@x")
    add_msg(state, "billing@vendor.com", "Invoice INV-1234 receipt", "Your invoice is attached.", "i1@x")
    add_msg(state, "friend@life.com", "Lunch tomorrow?", "Want to grab lunch tomorrow near campus?", "f1@x")

    imap_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), IMAPHandler)
    imap_server.state = state
    imap_server.daemon_threads = True
    imap_port = imap_server.server_address[1]
    threading.Thread(target=imap_server.serve_forever, daemon=True).start()

    llm_server = ThreadingHTTPServer(("127.0.0.1", 0), LLMHandler)
    llm_server.llm_lock = threading.Lock()
    llm_server.inflight = 0
    llm_server.max_inflight = 0
    llm_server.calls = []
    llm_server.reject_ctk = False
    llm_server.daemon_threads = True
    llm_port = llm_server.server_address[1]
    threading.Thread(target=llm_server.serve_forever, daemon=True).start()

    tei_server = ThreadingHTTPServer(("127.0.0.1", 0), TEIHandler)
    tei_server.calls = []
    tei_server.daemon_threads = True
    tei_port = tei_server.server_address[1]
    threading.Thread(target=tei_server.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="mail-triage-test-")
    os.environ.update({
        "DATA_DIR": tmp,
        "IMAP_HOST": "127.0.0.1",
        "IMAP_PORT": str(imap_port),
        "IMAP_USER": "seanyong@ust.hk",
        "IMAP_PASSWORD": "test-password",
        "IMAP_TLS": "0",
        "LLM_BASE_URL": "http://127.0.0.1:%d/v1" % llm_port,
        "LLM_API_KEY": "test-key",
        "LLM_MODEL": "mock-model",
        "EMBED_BASE_URL": "http://127.0.0.1:%d" % tei_port,
        "RERANK_BASE_URL": "http://127.0.0.1:%d" % tei_port,
    })
    sys.path.insert(0, PROJECT)
    import config
    import store
    import engine
    import heuristics as heuristics_mod
    import rag

    store.init_db()
    store.set_setting("max_llm_per_hour", 200)
    store.set_setting("llm_batch_per_cycle", 10)
    check("env: config points at mock IMAP",
          config.IMAP_HOST == "127.0.0.1" and config.IMAP_PORT == imap_port)
    check("env: config points at mock LLM",
          config.LLM_BASE_URL.endswith(str(llm_port) + "/v1"))

    section("T0 rule matcher units")
    check("contains", engine.rule_matches(
        {"conditions": json.dumps([{"field": "from", "op": "contains", "value": "BOSS@"}]),
         "match_mode": "all"}, {"from": "boss@work.com"}))
    check("regex", engine.rule_matches(
        {"conditions": json.dumps([{"field": "subject", "op": "regex", "value": "^Inv\\w+"}]),
         "match_mode": "all"}, {"subject": "Invoice x"}))
    check("equals miss", not engine.rule_matches(
        {"conditions": json.dumps([{"field": "from", "op": "equals", "value": "a@b.com"}]),
         "match_mode": "all"}, {"from": "c@d.com"}))
    check("any-mode", engine.rule_matches(
        {"conditions": json.dumps([{"field": "from", "op": "contains", "value": "nope"},
                                   {"field": "subject", "op": "contains", "value": "Budget"}]),
         "match_mode": "any"}, {"from": "a@b", "subject": "Budget review"}))

    section("T1 first pass: rule sort + LLM classification (suggest only)")
    store.set_setting("rules_apply", True)
    store.set_setting("llm_suggest", True)
    store.set_setting("llm_apply", False)
    store.add_rule("Work from boss", "all",
                   [{"field": "from", "op": "contains", "value": "boss@"}],
                   {"move_to": "Work"})
    summary = engine.process_mailbox()
    print("  cycle summary: %r" % summary)
    inbox = state.get("INBOX")
    check("boss mail moved out of INBOX", 1 not in inbox["uids"])
    check("Work folder created and has the message",
          state.get("Work") is not None and 1 in state.get("Work")["uids"])
    rows = {r["subject"]: r for r in store.messages(limit=50)}
    check("boss row status=matched", rows["Budget review Q4"]["status"] == "matched")
    check("boss row action=move:Work", rows["Budget review Q4"]["action_taken"] == "move:Work")
    check("newsletter classified", rows["Weekly newsletter: top deals"]["llm_category"] == "Newsletter")
    check("invoice classified", rows["Invoice INV-1234 receipt"]["llm_category"] == "Receipt")
    check("lunch classified", rows["Lunch tomorrow?"]["llm_category"] == "Personal")
    check("lunch needs_reply=1", rows["Lunch tomorrow?"]["llm_needs_reply"] == 1)
    check("newsletter needs_reply=0", rows["Weekly newsletter: top deals"]["llm_needs_reply"] == 0)
    check("3 LLM classifications", len(llm_server.calls) == 3)
    check("nothing filed by LLM yet (suggest only)",
          len(state.get("Newsletters", )["uids"] if state.get("Newsletters") else []) == 0)

    section("T2 LLM auto-filing ON")
    store.set_setting("llm_apply", True)
    add_msg(state, "newsletter@deals.com", "Weekly newsletter: even more deals", "More deals inside.", "n2@x")
    engine.process_mailbox()
    check("uid 5 moved out of INBOX", 5 not in state.get("INBOX")["uids"])
    check("uid 5 filed in Newsletters",
          state.get("Newsletters") is not None and 5 in state.get("Newsletters")["uids"])
    row5 = [r for r in store.messages(limit=60) if r["uid"] == 5][0]
    check("row status=llm-moved", row5["status"] == "llm-moved")
    check("row action=move:Newsletters", row5["action_taken"] == "move:Newsletters")

    section("T3 dry-run mode")
    store.set_setting("rules_apply", False)
    add_msg(state, "boss@work.com", "Second budget note", "Another budget item.", "b2@x")
    engine.process_mailbox()
    check("uid 6 still in INBOX (dry-run)", 6 in state.get("INBOX")["uids"])
    row6 = [r for r in store.messages(limit=60) if r["uid"] == 6][0]
    check("row status=matched-dry", row6["status"] == "matched-dry")

    section("T4 reply drafting + save to Drafts")
    store.add_template("Ack", "Re: {subject}", "Hi {sender},\n\nThanks for your note.\n\n{my_name}")
    msg4 = [r for r in store.messages(limit=60) if r["uid"] == 4][0]
    tid = store.list_templates()[0]["id"]
    draft = engine.generate_draft(msg4["id"], tid)
    check("draft text came back", draft == FAKE_DRAFT)
    check("template included in prompt",
          any("Use this reply template as guidance" in c["user"] for c in llm_server.calls))
    folder = engine.save_draft(msg4["id"], draft)
    check("saved into Drafts folder", folder == "Drafts" and len(state.appended) == 1)
    if state.appended:
        raw = state.appended[0]["raw"]
        check("draft To header", b"To: friend@life.com" in raw)
        check("draft subject header", b"Subject: Re: Lunch tomorrow?" in raw)
        check("draft In-Reply-To", b"In-Reply-To: <f1@x>" in raw)

    section("T5 connectivity check")
    conn = engine.connectivity_check()
    check("connectivity ok", conn["ok"] and conn["user"] == "seanyong@ust.hk")
    check("unseen counted", conn["unseen"] >= 3)

    section("T6 idempotency")
    before = len(store.messages(limit=500))
    engine.process_mailbox()
    after = len(store.messages(limit=500))
    check("no duplicates on re-run", before == after)

    section("T7 UIDVALIDITY change triggers re-index")
    state.get("INBOX")["uidvalidity"] = 42
    engine.process_mailbox()
    rows_in = [r for r in store.messages(limit=500) if r["folder"] == "INBOX"]
    uids_rows = sorted(set(r["uid"] for r in rows_in))
    check("INBOX re-indexed without duplicate rows", len(uids_rows) == len(rows_in))
    check("INBOX rows cover the messages still in INBOX", set(uids_rows) == {4, 6})
    check("re-processing re-filed newsletter+receipt, kept lunch+boss",
          sorted(state.get("INBOX")["uids"]) == [4, 6])

    section("T8 web UI smoke (Flask test client)")
    import app as app_mod
    client = app_mod.app.test_client()
    for path in ("/", "/assistant", "/rules", "/classifiers", "/templates", "/messages",
                 "/accounts", "/accounts/new", "/settings", "/log", "/proxy/log", "/healthz"):
        r = client.get(path)
        check("GET %s -> 200" % path, r.status_code == 200)
    latest = store.messages(limit=1)[0]
    r = client.get("/messages/%d" % latest["id"])
    check("message detail renders", r.status_code == 200)
    r = client.post("/check")
    check("check-now triggers", r.status_code == 302)
    r = client.post("/settings/test-llm")
    check("settings test-llm triggers", r.status_code == 302)
    check("connectivity-test prompt reached the LLM",
          any("connectivity test" in c["system"] for c in llm_server.calls))

    section("T9a assistant tools (direct executor tests)")
    agent = engine.AssistantAgent()
    r = agent.call_tool("list_folders", {})
    check("list_folders lists folders with counts",
          r["ok"] and any(f["name"] == "Drafts" for f in r["result"]["folders"])
          and all("messages" in f for f in r["result"]["folders"]))
    r = agent.call_tool("mailbox_overview", {})
    check("mailbox_overview ok", r["ok"] and r["result"]["indexed_messages"] >= 4)
    r = agent.call_tool("search_messages", {"query": "lunch"})
    check("search_messages finds indexed mail",
          r["ok"] and any("Lunch" in (m["subject"] or "") for m in r["result"]["messages"]))
    r = agent.call_tool("search_mail", {"subject_contains": "budget"})
    check("search_mail searches IMAP history",
          r["ok"] and any("budget" in (m.get("subject") or "").lower()
                          for m in r["result"]["messages"]))
    r = agent.call_tool("search_mail", {"subject_contains": "budget", "since": "2026-10-01"})
    check("search_mail since filter excludes older mail", r["ok"] and r["result"]["returned"] == 0)
    r = agent.call_tool("search_mail", {"folder": "INBOX", "unseen_only": True})
    check("search_mail unseen filter", r["ok"] and r["result"]["total_matched"] >= 1)
    r = agent.call_tool("search_mail", {"folder": "NoSuchBox"})
    check("search_mail on missing folder errors cleanly", (not r["ok"]) and "error" in r["result"])
    r = agent.call_tool("read_message", {"folder": "INBOX", "uid": 6})
    check("read_message returns the full body",
          r["ok"] and "Another budget item" in r["result"]["body"])
    r = agent.call_tool("create_folder", {"name": "AgentTests"})
    check("create_folder creates",
          r["ok"] and r["result"]["created"] is True and state.get("AgentTests") is not None)
    r = agent.call_tool("create_folder", {"name": "AgentTests"})
    check("create_folder idempotent", r["ok"] and r["result"]["created"] is False)
    r = agent.call_tool("flag_message", {"folder": "INBOX", "uid": 6, "flagged": True})
    check("flag_message stars the message",
          r["ok"] and "\\Flagged" in state.get("INBOX")["msgs"][6]["flags"])
    r = agent.call_tool("propose_rule", {"name": "bad", "conditions": [], "actions": {}})
    check("invalid rule rejected with errors", (not r["ok"]) and r["result"]["errors"])
    r = agent.call_tool("propose_rule", {"name": "Budget rule",
        "conditions": [{"field": "subject", "op": "contains", "value": "budget"}],
        "actions": {"move_to": "Budget"}})
    check("valid rule queued as proposal", r["ok"] and len(agent.proposals) == 1)
    r = agent.call_tool("nonsense_tool", {})
    check("unknown tool rejected", not r["ok"])
    store.set_setting("assistant_actions_apply", False)
    dry = engine.AssistantAgent()
    r = dry.call_tool("move_message", {"folder": "INBOX", "uid": 6, "target_folder": "Budgets"})
    check("dry-run move does not touch mail",
          r["ok"] and r.get("dry_run") and 6 in state.get("INBOX")["uids"]
          and state.get("Budgets") is None)
    dry.close()
    agent.close()
    store.set_setting("assistant_actions_apply", True)

    section("T9b assistant agent stream (SSE + tools + proposals)")
    rules_before = len(store.list_rules())
    r = client.post("/assistant/stream", data={"message": "Sort the budget mail please"})
    check("stream responds 200 + SSE", r.status_code == 200 and r.mimetype == "text/event-stream")
    body = r.data.decode()
    for ev in ("event: reasoning", "event: tool_start", "event: tool_end",
               "event: content", "event: proposals", "event: done"):
        check("SSE carries %s" % ev, ev in body)
    check("reasoning streamed before tool calls",
          body.index("event: reasoning") < body.index("event: tool_start"))
    check("multi-turn content separated (content_break)", "event: content_break" in body)
    r2 = client.get("/assistant")
    check("assistant page shows the collapsed thinking summary",
          b"Checked the budget mail and moved it" in r2.data
          and b'<details class="think" open' not in r2.data)
    import config as _cfg
    _saved_base = _cfg.LLM_BASE_URL
    _cfg.LLM_BASE_URL = "http://127.0.0.1:1/v1"
    try:
        _sum = engine.AssistantAgent()._summarize_thoughts(
            "The user wants me to check the rules. Then I should answer briefly.")
    finally:
        _cfg.LLM_BASE_URL = _saved_base
    check("summary falls back to the first sentence when the LLM is down",
          _sum == "The user wants me to check the rules")
    dm = re.search(r"event: done\ndata: (.*)", body)
    done_data = json.loads(dm.group(1)) if dm else {}
    check("done names the stored message", isinstance(done_data.get("message_id"), int))
    conv = store.assistant_messages(limit=6)
    row = conv[-1]
    check("assistant turn persisted",
          row["role"] == "assistant" and row["id"] == done_data.get("message_id"))
    props = json.loads(row["proposals"])
    check("rule proposal persisted", len(props) == 1 and props[0]["name"] == "Budget mail to Budget")
    meta = json.loads(row["meta"])
    check("meta has reasoning + 3 tool steps",
          "budget" in (meta.get("reasoning") or "").lower() and len(meta.get("tools") or []) == 3)
    check("thought summary generated and persisted",
          meta.get("reasoning_summary") == "Checked the budget mail and moved it")
    check("thought_summary event streamed after done",
          body.index("event: done") < body.index("event: thought_summary"))
    check("tool ran against real IMAP: model saw actual results",
          any("Second budget note" in json.dumps(c["payload"]) for c in llm_server.calls[-4:]))
    check("assistant moved the lunch mail",
          4 in (state.get("Personal") or {"uids": []})["uids"])
    row4 = [x for x in store.messages(limit=100) if x["uid"] == 4 and x["folder"] == "Personal"][0]
    check("move recorded on the row",
          row4["status"] == "assistant-moved" and row4["action_taken"] == "move:Personal")
    check("move logged to events", any("assistant moved" in e["message"]
                                       for e in store.recent_events(60)))

    section("T9c assistant page: transcript + one-click apply")
    r = client.get("/assistant")
    check("assistant page renders", r.status_code == 200)
    check("page shows the thinking transcript", b"thinking" in r.data)
    check("page shows the proposal", "Budget mail to Budget".encode() in r.data)
    r = client.post("/assistant/apply", data={"msg_id": row["id"], "idx": 0})
    check("one-click apply added the rule",
          len(store.list_rules()) == rules_before + 1
          and any(x["name"] == "Budget mail to Budget" for x in store.list_rules()))
    r = client.post("/assistant/clear")
    check("conversation cleared", len(store.assistant_messages(limit=10)) == 0)

    section("T9d assistant failure is visible, not silent")
    r = client.post("/assistant/stream", data={"message": "streamfail please"})
    check("error event streamed, no done", b"event: error" in r.data and b"event: done" not in r.data)
    check("failure logged", any("assistant failed" in e["message"]
                                for e in store.recent_events(80)))
    r = client.post("/assistant/send", data={"message": "hello again"})
    check("buffered no-JS fallback still works", r.status_code == 302)

    section("T9e streaming fallback to the secondary endpoint")
    c = engine.LLMClient()
    c.base = "http://127.0.0.1:1/v1"
    c.fallback = (config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_MODEL)
    evs = list(c.chat_stream("You are a plain test bot.", [{"role": "user", "content": "hi"}],
                             tools=None, thinking=True))
    txt = "".join(e.get("text", "") for e in evs if e["type"] == "content_delta")
    check("fallback streamed a reply", "fallback" in txt)

    section("T10 LLM fallback")
    c = engine.LLMClient()
    c.base = "http://127.0.0.1:1/v1"
    c.fallback = (config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_MODEL)
    out = c.classify({"from_addr": "x@y", "subject": "Weekly newsletter", "snippet": "deals",
                      "to_addr": "", "date": ""}, ["Newsletter"], "Sean")
    check("fallback served the classification", out.get("category") == "Newsletter")

    section("T11 LLM failure: retry twice, park, then retry button")
    for _ in range(25):
        if not store.queued_messages(999):
            break
        engine.process_mailbox()
    add_msg(state, "noise@list.com", "permfail item", "please classify me", "pf@x")
    engine.process_mailbox()
    row = [r for r in store.messages(limit=400) if r["subject"] == "permfail item"][0]
    check("first failure leaves it queued",
          row["status"] == "queued" and store.llm_fail_count(row["id"]) == 1)
    engine.process_mailbox()
    engine.process_mailbox()
    row = store.get_message(row["id"])
    check("parked as error after 3 failures",
          row["status"] == "error" and store.llm_fail_count(row["id"]) == 3)
    n = store.retry_parked_errors()
    row = store.get_message(row["id"])
    check("retry requeues and clears failures",
          n == 1 and row["status"] == "queued" and store.llm_fail_count(row["id"]) == 0)

    section("T12 RAG: indexer, chunking, folder exclusions")
    uid_probe = add_msg(state, "probe@x.com", "Half index probe", "probe body text", "hx@x")
    mc = engine.MailClient().connect()
    uv_inbox = mc.select("INBOX")
    saved_embed = config.EMBED_BASE_URL
    config.EMBED_BASE_URL = "http://127.0.0.1:1"
    probe_failed = False
    try:
        rag._index_one(mc, "INBOX", uid_probe, uv_inbox)
    except Exception:
        probe_failed = True
    config.EMBED_BASE_URL = saved_embed
    probe_row = store.get_message_by_uid("INBOX", uid_probe, uv_inbox)
    check("failed embed leaves no partial chunks",
          probe_failed and probe_row is not None
          and store.message_chunk_count(probe_row["id"]) == 0)
    nchunks = rag._index_one(mc, "INBOX", uid_probe, uv_inbox)
    with store.db(vec=True) as conn:
        vcount = conn.execute(
            "SELECT COUNT(*) FROM vec_chunks WHERE rowid IN "
            "(SELECT id FROM chunks WHERE message_id=?)", (probe_row["id"],)).fetchone()[0]
    check("retry after embed recovery indexes cleanly",
          nchunks >= 1 and store.message_chunk_count(probe_row["id"]) == nchunks)
    check("retry stored vectors too", vcount == nchunks)
    mc.close()
    long_body = "Attendance summary for HMAW1905E. " + "The student roster lists 40 names. " * 100
    uid_long = add_msg(state, "registry@ust.hk", "Attendance report long", long_body, "lr@x")
    add_msg(state, "spam@spam.com", "You won a prize", "claim your money now", "j1@x",
            folder="Junk Email")
    res = rag.index_pass(limit=200)
    check("indexer walks all folders", res["remaining"] == 0 and res["processed"] >= 8)
    total_chunks = store.chunk_count()
    check("chunks created for indexed mail", total_chunks >= 10)
    with store.db() as conn:
        dupes = conn.execute("SELECT msgid, COUNT(*) c FROM messages WHERE msgid != '' "
                             "GROUP BY msgid HAVING c > 1").fetchall()
    check("moved mail does not create duplicate message rows", len(dupes) == 0)
    row_long = [r for r in store.messages(limit=500)
                if r["uid"] == uid_long and r["folder"] == "INBOX"][0]
    with store.db() as conn:
        long_texts = [r["text"] for r in conn.execute(
            "SELECT text FROM chunks WHERE message_id=? ORDER BY seq", (row_long["id"],))]
    check("long message split into multiple chunks", len(long_texts) >= 2)
    check("every chunk carries the header prefix",
          bool(long_texts) and all(t.startswith("From: ") for t in long_texts))
    row_short = [r for r in store.messages(limit=500) if r["subject"] == "permfail item"][0]
    check("short message stays one chunk", store.message_chunk_count(row_short["id"]) == 1)
    check("junk folder excluded", store.index_state_get("Junk Email") is None)
    with store.db() as conn:
        junk_chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE folder='Junk Email'").fetchone()[0]
    check("no chunks for junk mail", junk_chunks == 0)
    drafts_rows = [r for r in store.messages(limit=500) if r["folder"] == "Drafts"]
    check("unscanned folders got backfilled by the indexer",
          len(drafts_rows) >= 1 and store.message_chunk_count(drafts_rows[0]["id"]) >= 1)
    with store.db(vec=True) as conn:
        nvec = conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
    check("vectors stored for every chunk", nvec == total_chunks)
    res2 = rag.index_pass(limit=5)
    check("second pass is a no-op", res2["processed"] == 0 and res2["remaining"] == 0)

    section("T13 RAG: hybrid search, rerank, filters")
    r = rag.search("payment", mode="vector")
    check("vector search finds the invoice for a paraphrase",
          r["ok"] and any("Invoice" in (x["subject"] or "") for x in r["results"]))
    r = rag.search("INV-1234", mode="fts")
    check("keyword search finds the exact token",
          r["ok"] and any("Invoice" in (x["subject"] or "") for x in r["results"]))
    r = rag.search("outstanding payment for the vendor")
    check("hybrid search returns semantic matches",
          r["ok"] and any("Invoice" in (x["subject"] or "") for x in r["results"]))
    r = rag.search("budget")
    check("rerank puts budget mail first",
          r["ok"] and "budget" in (r["results"][0]["subject"] or "").lower())
    check("rerank endpoint was called",
          any(c["path"].startswith("/rerank") for c in tei_server.calls))
    r = rag.search("budget", folder="AgentTests")
    check("folder filter excludes other folders", r["ok"] and len(r["results"]) == 0)
    r = rag.search("budget", folder="Work")
    check("folder filter keeps matching folder", r["ok"] and len(r["results"]) >= 1)
    r = rag.search("")
    check("empty query rejected", not r["ok"])
    agent = engine.AssistantAgent()
    r = agent.call_tool("semantic_search", {"query": "the invoice from the vendor"})
    check("assistant semantic_search tool returns matches",
          r["ok"] and any("Invoice" in (x["subject"] or "") for x in r["result"]["results"]))
    agent.close()

    section("T14 RAG: assistant streams a semantic-search turn")
    r = client.post("/assistant/stream",
                    data={"message": "do a semantic search for the vendor invoice"})
    body = r.data.decode()
    check("semantic_search tool ran in the stream",
          '"name": "semantic_search"' in body and '"ok": true' in body)
    check("stream completed with done", bool(re.search(r"event: done\ndata: ", body)))
    check("reply carries a message reference", "[msg:" in body)
    check("linkify renders message refs as links",
          'href="/messages/3"' in app_mod.linkify("see [msg:3]"))

    section("T15 UI: ordering, markdown, tags, non-mail rows")
    md = app_mod.md_to_html("**bold** and `code`\n- one\n- two")
    check("md renderer handles bold/lists/code",
          "<b>bold</b>" in md and "<code>code</code>" in md and "<li>one</li>" in md)
    check("md renderer linkifies msg refs",
          'href="/messages/9"' in app_mod.md_to_html("see [msg:9]"))
    check("md renderer escapes html",
          "&lt;script&gt;" in app_mod.md_to_html("<script>alert(1)</script>"))
    id_old = add_msg(state, "old@x.com", "Old message from 2024", "ancient history", "old1@x",
                     date="Mon, 15 Jan 2024 09:00:00 +0800")
    id_new = add_msg(state, "new@x.com", "Fresh message latest", "recent stuff", "new1@x",
                     date="Fri, 02 Oct 2026 09:00:00 +0800")
    rag.index_pass(limit=60)
    rows_dated = store.messages(limit=5)
    check("messages ordered by mail date", rows_dated[0]["subject"] == "Fresh message latest")
    row_old = [r for r in store.messages(limit=1000)
               if r["uid"] == id_old and r["folder"] == "INBOX"][0]
    check("date_ts parsed for new rows", row_old["date_ts"] > 0)
    store.insert_message("Tasks", 77, 1, {})
    with store.db() as conn:
        nm = conn.execute("SELECT * FROM messages WHERE folder='Tasks' AND uid=77").fetchone()
    check("headerless non-mail row hidden from lists",
          nm is not None and all(not (r["folder"] == "Tasks" and r["uid"] == 77)
                                 for r in store.messages(limit=2000, order="id")))
    r = client.post("/messages/tag", data={"ids": [row_old["id"]], "tag": "Archive me"})
    check("bulk tag redirects", r.status_code == 302)
    check("tag stored", store.get_message(row_old["id"])["user_tag"] == "Archive me")
    r = client.get("/messages?f=tagged")
    check("tagged filter shows the badge", b"Archive me" in r.data)
    r = client.post("/messages/untag", data={"ids": [row_old["id"]]})
    check("untag clears", store.get_message(row_old["id"])["user_tag"] == "")

    section("T16 classify: single + batch")
    c1 = add_msg(state, "cafe@x.com", "Lunch with the team", "grabbing lunch friday", "c1@x")
    c2 = add_msg(state, "billing2@vendor.com", "Invoice for September", "invoice attached", "c2@x")
    c3 = add_msg(state, "deals2@shop.com", "Weekly newsletter deals", "deals inside", "c3@x")
    rag.index_pass(limit=60)
    rowc2 = [r for r in store.messages(limit=2000)
             if r["uid"] == c2 and r["folder"] == "INBOX"][0]
    r = client.post("/messages/%d/classify" % rowc2["id"])
    check("single classify renders the result", r.status_code == 200 and b"classified this as" in r.data)
    rowc2b = store.get_message(rowc2["id"])
    check("single classify stored the category", rowc2b["llm_category"] == "Receipt")
    check("single classify filed when a mapping exists",
          rowc2b["status"] == "llm-moved" and rowc2b["action_taken"] == "move:Receipts")
    check("single classify stored the reason",
          "because it says Receipt" in (rowc2b["llm_reason"] or ""))
    check("classify result page shows the reason",
          b"why: because it says Receipt" in r.data)
    r = client.get("/messages/%d" % rowc2["id"])
    check("message page shows summary + reason",
          b"LLM summary: mock: Receipt" in r.data
          and b"why: because it says Receipt" in r.data)
    job = engine.ClassifyJob()
    job._run_job()
    check("batch classified the backlog", job.state["done"] >= 3)
    rowc1 = [r for r in store.messages(limit=2000) if r["uid"] == c1][0]
    rowc3 = [r for r in store.messages(limit=2000) if r["uid"] == c3][0]
    check("batch classified lunch as Personal",
          store.get_message(rowc1["id"])["llm_category"] == "Personal")
    check("batch classified newsletter as Newsletter",
          store.get_message(rowc3["id"])["llm_category"] == "Newsletter")
    pfrow = [r for r in store.messages(limit=2000) if r["subject"] == "permfail item"][0]
    check("batch failure does not park on first strike",
          store.get_message(pfrow["id"])["status"] == "queued"
          and store.llm_fail_count(pfrow["id"]) == 1)
    check("batch recorded the failure", job.state["failed"] >= 1)
    r = client.get("/classify/status")
    check("classify status endpoint", r.status_code == 200 and b"running" in r.data)
    r = client.post("/messages/classify-all")
    check("classify-all route triggers", r.status_code == 302)

    section("T17 learn rules from manual tags")
    store.tag_messages([rowc1["id"], rowc3["id"]], "Receipt")
    r = client.post("/learn-rules")
    check("learn-rules redirects", r.status_code == 302)
    props = store.list_rule_proposals()
    check("valid proposal stored from tags",
          len(props) == 1 and props[0]["rule_obj"]["name"] == "Tagged receipts")
    r = client.get("/messages")
    check("proposals render on the messages page", b"Tagged receipts" in r.data)
    rules_before = len(store.list_rules())
    r = client.post("/proposals/%d/apply" % props[0]["id"])
    check("proposal apply adds the rule",
          len(store.list_rules()) == rules_before + 1
          and any(x["name"] == "Tagged receipts" for x in store.list_rules()))
    check("applied proposal no longer pending", not store.list_rule_proposals())
    store.add_rule_proposal("tags", {"name": "Dismiss me", "match_mode": "all",
                                     "conditions": [{"field": "subject", "op": "contains", "value": "x"}],
                                     "actions": {"mark_read": True}})
    p2 = store.list_rule_proposals()[0]
    r = client.post("/proposals/%d/dismiss" % p2["id"])
    check("dismiss hides the proposal", not store.list_rule_proposals())
    agent = engine.AssistantAgent()
    r = agent.call_tool("list_tagged", {})
    check("assistant list_tagged tool works",
          r["ok"] and r["result"]["count"] >= 2
          and any(t["tag"] == "Receipt" for t in r["result"]["tagged"]))
    agent.close()

    section("T18 guard rules, top placement, short-token matching")
    po_rule = {"conditions": json.dumps([{"field": "subject", "op": "contains", "value": "PO"}]),
               "match_mode": "all"}
    check("short contains value does not fire inside words",
          engine.rule_matches(po_rule, {"subject": "support and reports"}) is False)
    check("short contains value fires as a whole word",
          engine.rule_matches(po_rule, {"subject": "PO D100305765 stationery"}) is True
          and engine.rule_matches(po_rule, {"subject": "please raise (PO), thanks"}) is True)
    long_rule = {"conditions": json.dumps([{"field": "subject", "op": "contains", "value": "portal"}]),
                 "match_mode": "all"}
    check("longer values still substring-match",
          engine.rule_matches(long_rule, {"subject": "supportportal update"}) is True)

    agent = engine.AssistantAgent()
    r = agent.call_tool("propose_rule", {
        "name": "Keep Jac in inbox", "match_mode": "any",
        "conditions": [{"field": "from", "op": "contains", "value": "jac.leung"}],
        "placement": "top", "rationale": "his mail always stays in the inbox"})
    check("guard rule accepted without actions",
          r["ok"] and r["result"]["rule"]["actions"] == {}
          and r["result"]["rule"].get("placement") == "top")
    agent.close()

    store.set_setting("rules_apply", True)
    store.set_setting("llm_apply", True)
    guard_id = store.add_rule("Keep Jac in inbox", "any",
                              [{"field": "from", "op": "contains", "value": "jac.leung"}],
                              {}, enabled=True, position="top")
    mover_id = store.add_rule("Jac to Notifications", "any",
                              [{"field": "from", "op": "contains", "value": "jac.leung"}],
                              {"move_to": "Notifications"}, enabled=True)
    check("top placement wins the ordering", store.list_rules()[0]["id"] == guard_id)
    jac_uid = add_msg(state, "jac.leung@ust.hk", "Absence arrangement for AISC1000B",
                      "here is the invoice arrangement", "jac1@x")
    engine.process_mailbox()
    rowj = [r for r in store.messages(limit=2000) if r["uid"] == jac_uid][0]
    rowj = store.get_message(rowj["id"])
    check("guard keeps matching mail in the inbox",
          rowj["status"] == "matched" and rowj["action_taken"] == ""
          and rowj["rule_id"] == guard_id and rowj["folder"] == "INBOX")
    notif = state.get("Notifications")
    check("guard stopped the later move rule",
          jac_uid in state.get("INBOX")["uids"]
          and (notif is None or jac_uid not in notif.get("uids", [])))
    r = client.post("/messages/%d/classify" % rowj["id"])
    rowj2 = store.get_message(rowj["id"])
    check("guard blocks LLM category filing",
          r.status_code == 200 and rowj2["llm_category"] == "Receipt"
          and rowj2["status"] == "classified"
          and not str(rowj2["action_taken"] or "").startswith("move"))
    r = client.get("/rules")
    check("rules page labels the guard", b"keep in place (guard)" in r.data)
    r = client.post("/rules/%d/move" % mover_id, data={"dir": "top"})
    check("move-to-top reorders rules",
          r.status_code == 302 and store.list_rules()[0]["id"] == mover_id)
    store.delete_rule(guard_id)
    store.delete_rule(mover_id)

    section("T19 batch classify: parallel workers + thinking")
    store.set_setting("classify_concurrency", 4)
    b_uids = [add_msg(state, "batch%d@x.com" % i, "Batch newsletter item %d" % i,
                      "weekly deals inside", "batch%d@x" % i) for i in range(6)]
    rag.index_pass(limit=200)
    rows_b = [r for r in store.messages(limit=2000) if r["uid"] in b_uids]
    llm_server.max_inflight = 0
    job = engine.ClassifyJob()
    job.trigger([r["id"] for r in rows_b])
    job._run_job()
    check("parallel batch classified every message",
          job.state["done"] == len(rows_b) and job.state["failed"] == 0)
    check("batch state reports the concurrency", job.state.get("concurrency") == 4)
    check("LLM saw overlapping classify requests", llm_server.max_inflight >= 2)
    rowb0 = store.get_message(rows_b[0]["id"])
    check("thinking stored on the row", "mock thinking about" in (rowb0["llm_thinking"] or ""))
    r = client.get("/messages/%d" % rows_b[0]["id"])
    check("message page shows classifier thinking",
          b"classifier thinking" in r.data and b"mock thinking about" in r.data)
    c19 = add_msg(state, "t19@x.com", "Invoice for T19", "invoice attached", "t19@x")
    rag.index_pass(limit=200)
    rowt = [r for r in store.messages(limit=2000) if r["uid"] == c19][0]
    r = client.post("/messages/%d/classify" % rowt["id"])
    rt = store.get_message(rowt["id"])
    check("single classify stores thinking",
          "mock thinking about Receipt" in (rt["llm_thinking"] or ""))

    section("T20 messages list: pagination + summary line")
    store.update_message(rows_b[0]["id"], llm_summary="Summary under the row test")
    r = client.get("/messages?per=500")
    check("summary line renders under the message row", b"Summary under the row test" in r.data)
    check("count_messages matches the unfiltered list",
          store.count_messages("all") == len(store.messages(limit=100000)))
    check("count_messages respects filters",
          store.count_messages("tagged") == len(store.messages(limit=1000, filt="tagged")))
    p1 = store.messages(limit=5, offset=0)
    p2 = store.messages(limit=5, offset=5)
    check("offset paginates without overlap",
          len(p1) == 5 and len(p2) == 5
          and not ({m["id"] for m in p1} & {m["id"] for m in p2}))
    r = client.get("/messages?per=10&page=2")
    check("page 2 renders with pager text", r.status_code == 200 and b"page 2 of" in r.data)
    r = client.get("/messages?per=10&page=1")
    check("pager shows total and an Older link",
          b"page 1 of" in r.data and b"Older" in r.data and b"per page" in r.data)

    section("T21 message viewer: decoded MIME bodies")
    import base64 as _b64
    payload = "Hi Sean, this is the decoded invoice text for September, please process it."
    enc = _b64.b64encode(payload.encode()).decode()
    enc_lines = "\r\n".join(enc[i:i+76] for i in range(0, len(enc), 76))
    raw_b64 = ("From: siyan@connect.ust.hk\r\nTo: seanyong@ust.hk\r\n"
               "Subject: Re: About PGTA of HMAW1905E\r\n"
               "Date: Tue, 15 Sep 2026 13:24:40 +0000\r\nMessage-ID: <b64msg@x>\r\n"
               "MIME-Version: 1.0\r\n"
               "Content-Type: multipart/mixed; boundary=\"XXB\"\r\n\r\n"
               "--XXB\r\nContent-Type: text/plain; charset=\"utf-8\"\r\n"
               "Content-Transfer-Encoding: base64\r\n\r\n"
               + enc_lines + "\r\n--XXB--\r\n").encode()
    b64uid = state.add("INBOX", raw_b64)
    engine.process_mailbox()
    rowb64 = [r for r in store.messages(limit=3000) if r["uid"] == b64uid][0]
    check("scan stores the decoded body as the snippet",
          "decoded invoice text for September" in (rowb64["snippet"] or "")
          and "Content-Transfer-Encoding" not in (rowb64["snippet"] or ""))
    r = client.get("/messages/%d" % rowb64["id"])
    check("message viewer shows the decoded body",
          b"decoded invoice text for September" in r.data
          and b"Content-Transfer-Encoding" not in r.data)
    if not (store.get_message(rowb64["id"])["llm_category"] or ""):
        job21 = engine.ClassifyJob()
        job21.trigger([rowb64["id"]])
        job21._run_job()
    hits = [c for c in llm_server.calls if "pgta" in c["user"].lower()]
    check("classifier received the decoded body, not base64",
          bool(hits) and "decoded invoice text" in hits[-1]["user"].lower()
          and enc[:40] not in hits[-1]["user"])
    check("base64 mail classified as Receipt",
          (store.get_message(rowb64["id"])["llm_category"] or "") == "Receipt")
    salv = engine.readable_body(("--XXB\r\nContent-Type: text/plain\r\n"
                                 "Content-Transfer-Encoding: base64\r\n\r\n" + enc_lines),
                                limit=4000)
    check("salvage decodes truncated MIME snippets", "decoded invoice text" in salv)
    check("junk detector flags raw MIME, passes clean text",
          engine.looks_like_mime_junk("--XXB\r\nContent-Type: text/plain") is True
          and engine.looks_like_mime_junk("Hi Sean, readable text.") is False)
    junk_text = ("--XXB\r\nContent-Type: text/plain; charset=utf-8\r\n"
                 "Content-Transfer-Encoding: base64\r\n\r\n" + enc_lines)
    jr = add_msg(state, "junktest@x.com", "Junk snippet repair", "plain body for repair test", "junkrepair@x")
    rag.index_pass(limit=300)
    rowjr = [r for r in store.messages(limit=3000) if r["uid"] == jr][0]
    store.update_message(rowjr["id"], snippet=junk_text)
    r = client.get("/messages/%d" % rowjr["id"])
    fixed = store.get_message(rowjr["id"])
    check("viewer repairs junk snippets from IMAP",
          r.status_code == 200 and b"Content-Transfer-Encoding" not in r.data
          and "plain body for repair test" in (fixed["snippet"] or ""))

    # move-tracking: app-initiated auto-filing records the destination on the row
    raw_mv = raw_b64.replace(b"b64msg@x", b"b64moved@x")
    mv_uid = state.add("INBOX", raw_mv)
    engine.process_mailbox()
    rowmv = [r for r in store.messages(limit=3000) if r["msgid"] == "b64moved@x"][0]
    check("auto-filing records the destination folder on the row",
          rowmv["folder"] == "Receipts" and rowmv["status"] == "llm-moved")
    # server-side move by another client leaves the row stale; the viewer repairs it
    store.update_message(rowmv["id"], snippet=junk_text)
    mvcur = [name for name, f in state.folders.items() if mv_uid in f["uids"]][0]
    state.move(mvcur, mv_uid, "AgentTests")
    r = client.get("/messages/%d" % rowmv["id"])
    fixedmv = store.get_message(rowmv["id"])
    check("viewer repairs + relocates a message moved on the server",
          r.status_code == 200 and b"decoded invoice text for September" in r.data
          and fixedmv["folder"] == "AgentTests"
          and "decoded invoice text" in (fixedmv["snippet"] or "")
          and b"Content-Transfer-Encoding" not in r.data)

    # bulk snippet heal (maintenance CLI: app.py --heal-snippets)
    heal_uid = add_msg(state, "heal@x.com", "Heal me", "healthy heal body text", "heal@x")
    engine.process_mailbox()
    healrow = [r for r in store.messages(limit=3000) if r["uid"] == heal_uid][0]
    store.update_message(healrow["id"], snippet=junk_text)
    hres = engine.heal_snippets(workers=2)
    healed = store.get_message(healrow["id"])
    check("bulk heal repairs legacy junk snippets",
          hres["fixed"] >= 1 and "healthy heal body text" in (healed["snippet"] or "")
          and hres["remaining"] == 0)

    # bulk heal phase 2: rescue a stale row via the Message-ID folder index
    mv2_uid = add_msg(state, "mover2@x.com", "Heal moved", "moved heal body text", "healmoved@x")
    engine.process_mailbox()
    mv2row = [r for r in store.messages(limit=3000) if r["uid"] == mv2_uid][0]
    store.update_message(mv2row["id"], snippet=junk_text)
    state.move("INBOX", mv2_uid, "AgentTests")
    hres2 = engine.heal_snippets(workers=2)
    mv2fixed = store.get_message(mv2row["id"])
    check("bulk heal rescue locates + repairs moved messages",
          hres2["fixed"] >= 1 and "moved heal body text" in (mv2fixed["snippet"] or "")
          and mv2fixed["folder"] == "AgentTests")

    # viewer: an undecodable legacy row shows the unavailable state, not garbage
    ghost_uid = add_msg(state, "ghost@x.com", "Ghost mail", "ghost body", "ghost@x")
    engine.process_mailbox()
    ghostrow = [r for r in store.messages(limit=3000) if r["uid"] == ghost_uid][0]
    store.update_message(ghostrow["id"], snippet="\x01\x02\x03" * 40,
                         msgid="gone-gone@x", folder="NoSuch", uid=999999)
    r = client.get("/messages/%d" % ghostrow["id"])
    check("undecodable row shows the unavailable state, not garbage",
          r.status_code == 200 and b"could not be decoded" in r.data)

    # ---- proper email renderer: html, inline cid images, remote blocking ----
    import base64 as _b642
    tiny_png = _b642.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAABlBMVEX///+/v7+jQ3Y5"
        "AAAADklEQVQI12P4AIX8EAgALgAD/aNpbtEAAAAASUVORK5CYII=")
    png_b64 = _b642.b64encode(tiny_png).decode()
    png_lines = "\r\n".join(png_b64[i:i+76] for i in range(0, len(png_b64), 76))
    html_part = ('<html><body><p>Hello <b>bold</b> and <i>italic</i>, '
                 '<a href="https://example.com/x">a link</a>.</p>'
                 '<table><tr><td>cell one</td><td>cell two</td></tr></table>'
                 '<img src="cid:img1@x" alt="inline">'
                 '<img src="https://tracker.example.com/pixel.gif" alt="remote">'
                 '<script>alert(1)</script><p onclick="hack()">safe text</p>'
                 '</body></html>')
    raw_html_mail = (
        "From: news@example.com\r\nTo: seanyong@ust.hk\r\n"
        "Subject: Formatted mail test\r\nDate: Wed, 30 Sep 2026 09:00:00 +0800\r\n"
        "Message-ID: <htmlmail@x>\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/related; boundary="REL"\r\n\r\n'
        "--REL\r\n"
        'Content-Type: multipart/alternative; boundary="ALT"\r\n\r\n'
        "--ALT\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "plain version text\r\n"
        "--ALT\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        + html_part + "\r\n"
        "--ALT--\r\n"
        "--REL\r\nContent-Type: image/png\r\nContent-ID: <img1@x>\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        + png_lines + "\r\n"
        "--REL--\r\n"
    ).encode()
    html_uid = state.add("INBOX", raw_html_mail)
    engine.process_mailbox()
    hrow = [r for r in store.messages(limit=3000) if r["uid"] == html_uid][0]
    check("scan extracted + sanitized the html body",
          (hrow["body_html"] or "").find("<b>bold</b>") >= 0
          and "alert(1)" not in (hrow["body_html"] or "")
          and "img1@x" in (hrow["body_cids"] or ""))
    page = client.get("/messages/%d" % hrow["id"]).data
    check("html mail renders formatted (bold + table kept)",
          b"<b>bold</b>" in page and b"cell one" in page)
    check("scripts, handlers stripped from the render",
          b"alert(1)" not in page and b"onclick" not in page)
    check("cid image rewritten to the inline part route",
          ('/messages/%d/part/2?v=2"' % hrow["id"]).encode() in page and b"cid:img1" not in page)
    check("remote image blocked by default with a load button",
          b"Load images" in page and b"/img?u=" not in page
          and b"tracker.example.com" not in page)
    r = client.get("/messages/%d?imgs=1" % hrow["id"])
    check("load-images rewrites remote srcs to the proxy",
          ('/messages/%d/img?u=https%%3A%%2F%%2Ftracker.example.com%%2Fpixel.gif'
           % hrow["id"]).encode() in r.data)
    r = client.get("/messages/%d/part/2" % hrow["id"])
    check("inline part route serves the image bytes",
          r.status_code == 200 and r.mimetype == "image/png" and r.data == tiny_png)
    r = client.get("/messages/%d?view=plain" % hrow["id"])
    check("plain-text view toggle serves the text alternative",
          b"plain version text" in r.data and b"View formatted" in r.data)
    check("remote fetch guard rejects private hosts and odd schemes",
          engine.image_url_ok("http://127.0.0.1/x") is False
          and engine.image_url_ok("http://192.168.0.10/x") is False
          and engine.image_url_ok("ftp://example.com/x") is False)

    # bulk html extraction (maintenance CLI: app.py --extract-html)
    store.update_message(hrow["id"], body_html="", body_cids="", body_html_at=0)
    eres = engine.extract_rendered(workers=2)
    hrow2 = store.get_message(hrow["id"])
    check("bulk extract fills the renderer cache",
          eres["updated"] >= 1 and "<b>bold</b>" in (hrow2["body_html"] or "")
          and (hrow2["body_html_at"] or 0) > 0)

    # collapsed base64 salvage (legacy snippets lost their line breaks)
    collapsed = ("------=_NextPart_9ZZ Content-Type: text/plain; charset=\"utf-8\" "
                 "Content-Transfer-Encoding: base64 " + enc_lines.replace("\r\n", " "))
    salv2 = engine.readable_body(collapsed, limit=4000)
    check("salvage handles space-collapsed base64 (legacy snippets)",
          "decoded invoice text for September" in salv2
          and "Content-Transfer-Encoding" not in salv2
          and enc[:20] not in salv2)
    check("b64 salvage leaves normal text alone",
          engine._decode_b64_blocks("Hello team, meeting at 3pm.") == "Hello team, meeting at 3pm.")
    check("junk detector flags raw headers, passes tracking-number text",
          engine.looks_like_mime_junk("Received: from mail.example.com by mx1; Wed") is True
          and engine.looks_like_mime_junk("Your order 1Z999AA10123456784 has shipped.") is False)

    section("T22 rule proposals consult existing rules (update vs add)")
    rules_all = store.list_rules()
    boss_rule = [r for r in rules_all if r["name"] == "Work from boss"][0]
    agent = engine.AssistantAgent()
    r = agent.call_tool("list_rules", {})
    check("list_rules returns rules with ids",
          r["ok"] and any(x["id"] == boss_rule["id"] for x in r["result"]["rules"]))
    r = agent.call_tool("propose_rule", {
        "name": "Boss mail to Budget", "match_mode": "all",
        "conditions": [{"field": "from", "op": "contains", "value": "boss@work.com"}],
        "actions": {"move_to": "Budget"}})
    check("overlapping proposal is flagged with the similar rule",
          r["ok"] and (r["result"]["rule"].get("similar_rule") or {}).get("id") == boss_rule["id"])
    check("similar hint points at updates_rule_id",
          "updates_rule_id" in (r["result"].get("hint") or ""))
    r = agent.call_tool("propose_rule", {
        "name": "Boss mail to Budget", "match_mode": "all",
        "conditions": [{"field": "from", "op": "contains", "value": "boss@work.com"}],
        "actions": {"move_to": "Budget"}, "updates_rule_id": boss_rule["id"]})
    check("update proposal accepted, replaces the earlier same-name one",
          r["ok"] and r["result"]["rule"].get("updates_rule_id") == boss_rule["id"]
          and len([p for p in agent.proposals if p.get("name") == "Boss mail to Budget"]) == 1)
    r = agent.call_tool("propose_rule", {
        "name": "Nope", "match_mode": "all",
        "conditions": [{"field": "from", "op": "contains", "value": "x@y.com"}],
        "actions": {"move_to": "Budget"}, "updates_rule_id": 99999})
    check("update of a missing rule is rejected", not r["ok"])
    upd_prop = [p for p in agent.proposals if p.get("name") == "Boss mail to Budget"][0]
    mid_upd = store.add_assistant_message("assistant", "Update the boss rule?",
                                          proposals=json.dumps([upd_prop]))
    r = client.get("/assistant")
    check("update card renders with badge and button",
          ("updates #%d" % boss_rule["id"]).encode() in r.data
          and ("Update rule #%d" % boss_rule["id"]).encode() in r.data)
    before_rules = len(store.list_rules())
    r = client.post("/assistant/apply", data={"msg_id": mid_upd, "idx": 0, "mode": "update",
                                              "rule_id": str(boss_rule["id"])})
    check("update apply redirects", r.status_code == 302)
    check("existing rule edited in place, nothing added",
          len(store.list_rules()) == before_rules
          and json.loads(store.get_rule(boss_rule["id"])["actions"]).get("move_to") == "Budget")
    agent.close()

    agent2 = engine.AssistantAgent()
    agent2.call_tool("propose_rule", {
        "name": "Boss to Budget (dup)", "match_mode": "all",
        "conditions": [{"field": "from", "op": "contains", "value": "boss@work.com"}],
        "actions": {"move_to": "Budget"}})
    sim_prop = agent2.proposals[0]
    store.add_assistant_message("assistant", "Maybe update?", proposals=json.dumps([sim_prop]))
    r = client.get("/assistant")
    check("similar-rule note renders with an update button",
          b"Similar rule exists" in r.data
          and ("Update rule #%d" % boss_rule["id"]).encode() in r.data)
    sim, _reasons = engine.rule_similarity(
        {"conditions": [{"field": "from", "op": "contains", "value": "boss@work.com"}]},
        store.list_rules())
    check("rule_similarity matches exact conditions", bool(sim) and sim["id"] == boss_rule["id"])
    agent2.close()

    section("T23 heuristic classifiers: registry, pipeline order, refine")
    ex = [("Promo", heuristics_mod.featurize({"from_addr": "deals@shop.example",
                                              "subject": "Weekly deal blast",
                                              "snippet": "promo code inside"})) for _ in range(3)]
    ex += [("Receipt", heuristics_mod.featurize({"from_addr": "billing@vendor.example",
                                                 "subject": "Your invoice receipt",
                                                 "snippet": "invoice attached"})) for _ in range(3)]
    m_nb, _s = heuristics_mod.train("naive_bayes", ex)
    out = heuristics_mod.predict("naive_bayes", m_nb,
                                 heuristics_mod.featurize({"from_addr": "deals@shop.example",
                                                           "subject": "deal blast", "snippet": ""}))
    check("naive_bayes predicts the right class", bool(out) and out[0] == "Promo")
    m_dl, _s = heuristics_mod.train("decision_list", ex, {"min_support": 2, "min_precision": 0.8})
    out = heuristics_mod.predict("decision_list", m_dl,
                                 heuristics_mod.featurize({"from_addr": "x@y.com",
                                                           "subject": "invoice receipt", "snippet": ""}))
    check("decision_list predicts via a learned condition",
          bool(out) and out[0] == "Receipt" and bool(out[2]))
    check("decision_list abstains on unknown tokens",
          heuristics_mod.predict("decision_list", m_dl,
                                 heuristics_mod.featurize({"from_addr": "zz@zz.zz",
                                                           "subject": "hello there", "snippet": ""})) is None)
    heuristics_mod.register_kind("always_x", lambda exs, p: ({"c": 1}, {}),
                                 lambda mo, f: ("X", 1.0, None), lambda mo, l=6: "always")
    check("new kinds plug into the registry",
          heuristics_mod.predict("always_x", {}, {})[0] == "X")

    promo_uids = [add_msg(state, "deals@promos.example", "Mega deal blast %d" % i,
                          "limited time promo", "hz%d@x" % i) for i in range(6)]
    rag.index_pass(limit=300)
    rows_h = [r for r in store.messages(limit=3000) if r["uid"] in promo_uids]
    store.tag_messages([r["id"] for r in rows_h], "Promo")
    model, stats = heuristics_mod.train_heuristic("decision_list", "Promo", source="tags",
                                                  params={"min_support": 2, "min_precision": 0.8})
    check("train_heuristic builds from tags",
          stats["trained_label_count"] >= 6 and bool(model.get("conditions")))
    hid = store.add_heuristic("Promo robot", "decision_list", "Promo",
                              model=json.dumps(model), stats=json.dumps(stats))
    check("classifier registered and enabled", bool(hid) and store.get_heuristic(hid)["enabled"] == 1)
    newu = add_msg(state, "deals@promos.example", "Another blowout deal", "promo inside", "hz-new@x")
    rag.index_pass(limit=300)
    row_new = [r for r in store.messages(limit=3000) if r["uid"] == newu][0]
    calls_before = len(llm_server.calls)
    res = engine.classify_and_store(store.get_message(row_new["id"]), store.all_settings())
    check("heuristic decided without any LLM call",
          res.get("_heuristic_id") == hid and len(llm_server.calls) == calls_before)
    row_after = store.get_message(row_new["id"])
    check("row records the heuristic verdict",
          row_after["llm_category"] == "Promo"
          and (row_after["classified_by"] or "").startswith("heuristic:%d" % hid))

    agent = engine.AssistantAgent()
    r = agent.call_tool("list_classifiers", {})
    check("assistant list_classifiers shows it",
          r["ok"] and any(c["id"] == hid for c in r["result"]["classifiers"]))
    r = agent.call_tool("evaluate_classifier", {"id": hid})
    check("assistant evaluate_classifier reports accuracy",
          r["ok"] and (r["result"]["accuracy"] or 0) >= 0.99)
    r = agent.call_tool("manage_classifier", {"id": hid, "action": "disable"})
    check("assistant manage_classifier disables", r["ok"] and store.get_heuristic(hid)["enabled"] == 0)
    r = agent.call_tool("train_classifier", {"kind": "decision_list", "category": "Promo",
                                             "source": "tags", "retrain_id": hid,
                                             "params": {"min_support": 2, "min_precision": 0.8}})
    check("assistant train_classifier retrains by id",
          r["ok"] and store.get_heuristic(hid)["enabled"] == 1)
    agent.close()

    r = client.get("/classifiers")
    check("classifiers page renders", r.status_code == 200 and b"Promo robot" in r.data)
    r = client.get("/messages/%d" % row_new["id"])
    check("message page shows the heuristic badge",
          ("heuristic:%d" % hid).encode() in r.data)

    more_uids = [add_msg(state, "deals@promos.example", "Mega deal blast extra %d" % i,
                         "promo again", "hz2-%d@x" % i) for i in range(5)]
    rag.index_pass(limit=400)
    rows_m = [r for r in store.messages(limit=4000) if r["uid"] in more_uids]
    store.tag_messages([r["id"] for r in rows_m], "Promo")
    refined = heuristics_mod.auto_refine()
    check("auto_refine retrained on new labels", any(h == hid for h, _n, _d in refined))
    stats_after = json.loads(store.get_heuristic(hid)["stats"])
    check("stats reflect the larger label set",
          (stats_after.get("trained_label_count") or 0) >= 11)
    check("training included negative examples", (stats_after.get("negatives") or 0) > 0)
    hx = store.add_heuristic("neg-only", "decision_list", "Promo",
                             model=json.dumps({"conditions": [
                                 {"token": "b:zzzqqq", "label": "__other__", "prob": 1.0,
                                  "precision": 1.0, "support": 9, "seen": 9}]}),
                             stats=json.dumps({"source": "test"}))
    verdict = heuristics_mod.classify({"from_addr": "n@x.com", "subject": "no match",
                                       "snippet": "zzzqqq boop"})
    check("negative-only match abstains instead of mislabeling", verdict is None)
    store.delete_heuristic(hx)

    section("T24 classifier datasets: review, remove, re-include")
    sample_id = rows_h[0]["id"]  # a tagged Promo sample from T23
    ds = heuristics_mod.dataset_for(store.get_heuristic(hid))
    check("dataset view lists positives untouched",
          ds["pos_total"] >= 11 and not any(s["excluded"] for s in ds["positives"]))
    check("dataset view includes negatives", ds["neg_total"] > 0)
    r = client.get("/classifiers/%d/dataset" % hid)
    check("dataset page renders", r.status_code == 200 and b"dataset review" in r.data)
    r = client.post("/classifiers/%d/dataset/remove" % hid, data={"msg_id": sample_id})
    check("remove persists the exclusion",
          sample_id in heuristics_mod.heuristic_excluded(store.get_heuristic(hid)))
    ds2 = heuristics_mod.dataset_for(store.get_heuristic(hid))
    check("removed sample stays visible as excluded",
          any(s["msg_id"] == sample_id and s["excluded"] for s in ds2["positives"]))
    n_expected = ds2["pos_total"] - ds2["pos_excluded"]
    _ex, n = heuristics_mod.build_examples("Promo", "tags", 500,
                                           exclude=heuristics_mod.heuristic_excluded(store.get_heuristic(hid)))
    check("training set drops the removed sample", n == n_expected)
    r = client.post("/classifiers/%d/retrain" % hid)
    stats_r = json.loads(store.get_heuristic(hid)["stats"])
    check("UI retrain respects the exclusions",
          stats_r.get("trained_label_count") == n_expected and stats_r.get("excluded") == 1)
    r = client.post("/classifiers/%d/dataset/reinclude" % hid, data={"msg_id": sample_id})
    check("re-include clears the exclusion",
          sample_id not in heuristics_mod.heuristic_excluded(store.get_heuristic(hid)))
    check("classifiers page links the dataset page",
          ("/classifiers/%d/dataset" % hid).encode() in client.get("/classifiers").data)

    section("T25 dataset relabel: dropdown reclassification + toast")
    pos_ids = [s["msg_id"] for s in heuristics_mod.dataset_for(store.get_heuristic(hid))["positives"]]
    rid = [i for i in pos_ids if i != sample_id][0]
    r = client.post("/classifiers/%d/dataset/relabel" % hid,
                    data={"msg_id": rid, "category": "Personal"})
    check("relabel redirects with an out-of-set toast",
          r.status_code == 302 and "toast=out" in (r.headers.get("Location") or ""))
    ds3 = heuristics_mod.dataset_for(store.get_heuristic(hid))
    check("relabelled sample moved to the negatives",
          any(s["msg_id"] == rid for s in ds3["negatives"])
          and not any(s["msg_id"] == rid for s in ds3["positives"]))
    r = client.get("/classifiers/%d/dataset?toast=out&subj=x&cat=Personal" % hid)
    check("toast markup renders on the page",
          b'class="toast"' in r.data and b"moved to" in r.data)
    r = client.post("/classifiers/%d/dataset/relabel" % hid,
                    data={"msg_id": rid, "category": "Promo"})
    check("relabel back redirects with an in-set toast",
          r.status_code == 302 and "toast=in" in (r.headers.get("Location") or ""))
    check("sample moved back up into the positives",
          any(s["msg_id"] == rid and s["tag"] == "Promo"
              for s in heuristics_mod.dataset_for(store.get_heuristic(hid))["positives"]))
    hcl = store.add_heuristic("NL weak", "decision_list", "Newsletter",
                              model=json.dumps({"conditions": []}),
                              stats=json.dumps({"source": "classified", "weak_labels": True}))
    dsn = heuristics_mod.dataset_for(store.get_heuristic(hcl))
    nid = dsn["positives"][0]["msg_id"] if dsn["positives"] else 0
    ok_relabel = False
    if nid:
        r = client.post("/classifiers/%d/dataset/relabel" % hcl,
                        data={"msg_id": nid, "category": "Personal"})
        rowx = store.get_message(nid)
        ok_relabel = (rowx["llm_category"] == "Personal" and rowx["classified_by"] == "user")
    check("classified-source relabel corrects the LLM label on the message", ok_relabel)
    store.delete_heuristic(hcl)

    section("T26 settings decouple endpoints from env (LLM + RAG)")
    llm_base_mock = "http://127.0.0.1:%d/v1" % llm_port
    tei_base = "http://127.0.0.1:%d" % tei_port
    # -- the LLM endpoint (base/model/key/timeout) is now a setting
    r = client.post("/settings", data={"section": "llm", "llm_base_url": llm_base_mock,
                                       "llm_model": "settings-model-x", "llm_api_key": "settings-key-1",
                                       "llm_timeout": "33"})
    check("LLM settings save redirects", r.status_code == 302)
    c = engine.LLMClient()
    check("LLMClient reads the settings endpoint",
          c.base == llm_base_mock and c.model == "settings-model-x" and c.timeout == 33)
    check("stored API key is used", c.key == "settings-key-1")
    out = c.classify({"from_addr": "x@y", "subject": "Weekly newsletter", "snippet": "deals",
                      "to_addr": "", "date": ""}, ["Newsletter"], "Sean")
    call = llm_server.calls[-1]
    check("classify uses the settings model + bearer from settings",
          out.get("category") == "Newsletter" and call["payload"].get("model") == "settings-model-x"
          and call.get("auth") == "Bearer settings-key-1")
    check("thinking extension sent in auto mode",
          "chat_template_kwargs" in call["payload"])
    # -- blank key input keeps the stored key; the clear checkbox removes it
    client.post("/settings", data={"section": "llm", "llm_base_url": llm_base_mock,
                                   "llm_model": "settings-model-x", "llm_api_key": "",
                                   "llm_timeout": "33"})
    check("blank key field keeps the stored key", engine.LLMClient().key == "settings-key-1")
    client.post("/settings", data={"section": "llm", "llm_base_url": llm_base_mock,
                                   "llm_model": "settings-model-x", "llm_api_key_clear": "1",
                                   "llm_timeout": "33"})
    check("clear checkbox falls back to the env key",
          engine.LLMClient().key == config.LLM_API_KEY)
    # -- thinking=off never sends the extension
    client.post("/settings", data={"section": "llm", "llm_base_url": llm_base_mock,
                                   "llm_model": "settings-model-x", "llm_thinking": "off"})
    engine.LLMClient().classify({"from_addr": "x@y", "subject": "Weekly newsletter",
                                 "snippet": "deals", "to_addr": "", "date": ""}, ["Newsletter"], "Sean")
    check("thinking=off suppresses chat_template_kwargs",
          "chat_template_kwargs" not in llm_server.calls[-1]["payload"])
    client.post("/settings", data={"section": "llm", "llm_base_url": llm_base_mock,
                                   "llm_model": "settings-model-x", "llm_thinking": "auto"})
    # -- auto mode: an endpoint that rejects the extension still succeeds (strip + retry)
    llm_server.reject_ctk = True
    n0 = len(llm_server.calls)
    out = engine.LLMClient().classify({"from_addr": "x@y", "subject": "Weekly newsletter",
                                       "snippet": "deals", "to_addr": "", "date": ""},
                                      ["Newsletter"], "Sean")
    attempts = llm_server.calls[n0:]
    llm_server.reject_ctk = False
    check("auto mode survives an endpoint that rejects the thinking extension",
          out.get("category") == "Newsletter" and len(attempts) == 2
          and attempts[0]["payload"].get("chat_template_kwargs")
          and "chat_template_kwargs" not in attempts[1]["payload"])
    # -- fallback endpoint is a setting too
    client.post("/settings", data={"section": "llm", "llm_fallback_base_url": llm_base_mock,
                                   "llm_fallback_model": "settings-fb"})
    check("fallback endpoint comes from settings",
          engine.LLMClient().fallback == (llm_base_mock, "", "settings-fb"))
    # -- blank fields fall back to the env
    client.post("/settings", data={"section": "llm", "llm_base_url": "", "llm_model": "",
                                   "llm_timeout": "0", "llm_fallback_base_url": "",
                                   "llm_fallback_model": ""})
    check("blank LLM settings fall back to the env endpoint",
          engine.LLMClient().base == config.LLM_BASE_URL
          and engine.LLMClient().model == config.LLM_MODEL)
    # -- RAG endpoints as settings, incl. OpenAI-style embeddings + Cohere-style rerank
    client.post("/settings", data={"section": "rag", "embed_base_url": tei_base,
                                   "embed_model": "mock-embed-1", "embed_protocol": "openai",
                                   "rerank_base_url": tei_base, "rerank_protocol": "cohere"})
    n0 = len(tei_server.calls)
    vec = rag.embed_one("invoice payment")
    emb_calls = tei_server.calls[n0:]
    check("openai-protocol embeddings hit /embeddings with model + input",
          len(vec) == 8 and len(emb_calls) == 1
          and emb_calls[0]["path"].startswith("/embeddings")
          and emb_calls[0]["payload"].get("model") == "mock-embed-1"
          and isinstance(emb_calls[0]["payload"].get("input"), list))
    check("query instruction prefix still applied on queries only",
          "Instruct:" in emb_calls[0]["payload"]["input"][0])
    rr = rag.rerank("invoice payment", ["invoice paid", "lunch tomorrow"])
    check("cohere-protocol rerank normalizes to index/score",
          bool(rr) and all("index" in d and "score" in d for d in rr)
          and rr[0]["score"] >= rr[-1]["score"])
    r = client.post("/settings/test-embed")
    check("Test embeddings button reaches the mock endpoint",
          r.status_code == 302 and any(c["path"].startswith("/embeddings")
                                       for c in tei_server.calls[-2:]))
    r = client.post("/settings/test-rerank")
    check("Test reranker button reaches the mock endpoint",
          r.status_code == 302 and any(c["path"].startswith("/rerank")
                                       for c in tei_server.calls[-2:]))
    r = client.post("/settings/test-llm", query_string={"which": "fallback"})
    check("Test fallback button redirects (no fallback configured)",
          r.status_code == 302)
    client.post("/settings", data={"section": "rag", "embed_protocol": "tei",
                                   "rerank_protocol": "tei"})
    check("protocols flip back to tei",
          rag.embed_config()["protocol"] == "tei" and rag.rerank_config()["protocol"] == "tei")
    # -- the embed-model change guard reads the settings value
    dim = store.meta_get("embed_dim")
    client.post("/settings", data={"section": "rag", "embed_model": "other-embed-9"})
    guard_error = ""
    try:
        rag._ensure_dim(dim)
    except Exception as exc:
        guard_error = str(exc)
    check("embedding model change guard fires from the settings value",
          "other-embed-9" in guard_error and "rebuild" in guard_error)
    client.post("/settings", data={"section": "rag", "embed_model": ""})
    # -- excluded folders, refresh cadence, display timezone are settings now
    client.post("/settings", data={"section": "rag", "rag_exclude_folders": "junk, custom-skip"})
    kept = rag.default_folders(["INBOX", "Junk Email", "custom-skip folder", "Work"])
    check("RAG excluded folders come from settings", kept == ["INBOX", "Work"])
    client.post("/settings", data={"section": "rag",
                                   "rag_exclude_folders": ("junk, deleted, trash, sync issues, "
                                                           "calendar, contacts, journal, "
                                                           "conversation history, outbox, rss feeds")})
    client.post("/settings", data={"section": "behavior", "display_tz_offset": "0"})
    check("display timezone offset is a setting", app_mod.fmt_ts(3600) == "01-01 01:00")
    client.post("/settings", data={"section": "behavior", "display_tz_offset": "8"})
    check("timezone back to +8", app_mod.fmt_ts(3600) == "01-01 09:00")
    r = client.get("/settings")
    check("settings page carries the new endpoint cards",
          b"LLM endpoint" in r.data and b"RAG / semantic search" in r.data)

    section("T27 embedded proxy: account store, config generation, connection resolution")
    import proxy as proxy_mod
    store.set_setting("proxy_tailnet_host", "node.example.ts.net")
    rec, ferr = proxy_mod.account_from_form({
        "provider": "outlook", "email": "acct@example.com", "password": "local-pw-1",
        "client_id": "cid-1", "client_secret": "csec-1", "redirect_mode": "loopback"})
    check("account form builds the preset record",
          bool(rec) and not ferr and rec["imap_local_port"] == 1993
          and rec["redirect_port"] == proxy_mod.REDIRECT_POOL_START)
    proxy_mod.upsert_account(rec)
    check("account stored",
          [a["email"] for a in proxy_mod.list_accounts()] == ["acct@example.com"])
    txt = proxy_mod.config_text()
    check("config: preset listener section on loopback",
          "[IMAP-1993]" in txt and "server_address = outlook.office365.com" in txt
          and "local_address = 127.0.0.1" in txt)
    check("config: account section with loopback redirect + secret",
          "[acct@example.com]" in txt
          and "redirect_uri = https://localhost:41810" in txt
          and "redirect_listen_address = http://127.0.0.1:41810" in txt
          and "client_secret = csec-1" in txt)
    store.set_setting("proxy_mode", "embedded")
    ic = engine.imap_config()
    check("embedded mode resolves the account for the app",
          ic["host"] == "127.0.0.1" and ic["port"] == 1993 and ic["user"] == "acct@example.com"
          and ic["password"] == "local-pw-1" and not ic["tls"])
    store.set_setting("proxy_mode", "external")
    ic = engine.imap_config()
    check("external mode falls back to the env",
          ic["host"] == config.IMAP_HOST and ic["port"] == config.IMAP_PORT
          and ic["user"] == config.IMAP_USER)
    store.set_setting("proxy_mode", "embedded")
    check("token status starts unauthenticated",
          proxy_mod.token_status("acct@example.com")["authorized"] is False)
    r = client.get("/accounts")
    check("accounts page renders the account + status",
          r.status_code == 200 and b"acct@example.com" in r.data
          and b"not authorised" in r.data)
    r = client.post("/api/proxy/auth/nobody@example.com")
    check("auth start rejects unknown accounts", r.status_code == 404)
    ok, rerr = proxy_mod.remove_account("acct@example.com")
    check("removing the last account is a clean outcome", ok and not rerr)
    check("account removed", proxy_mod.list_accounts() == [])

    print("\n%s\n%d passed, %d failed (workspace: %s)\n"
          % ("ALL PASS" if failed == 0 else "FAILURES PRESENT", passed, failed, tmp))
    try:
        imap_server.shutdown()
        llm_server.shutdown()
        tei_server.shutdown()
    except Exception:
        pass
    if failed == 0:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
