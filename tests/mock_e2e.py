#!/usr/bin/env python3
"""End-to-end test for Mail Triage with mock IMAP + mock LLM servers.

Nothing here touches real mail: a fake IMAP server stands in for the
email-oauth2-proxy, and a fake OpenAI-compatible endpoint stands in for the LLM.
Covers: rule matching, rule actions (move), dry-run mode, LLM classification,
LLM auto-filing, reply drafting, draft saving (APPEND), UIDVALIDITY re-indexing,
route smoke tests.

Usage:  .venv/bin/python tests/mock_e2e.py
"""
import json
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
            uids = self.search_uids(rest.split()[1:])
            self.send("* SEARCH %s" % " ".join(str(u) for u in uids))
            self.send("%s OK UID SEARCH completed" % tag)
        elif sub == "FETCH":
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
            st.move(self.cur, int(arg1), arg2.strip().strip('"'))
            self.send("* OK [COPYUID 1 %s 1] moved" % arg1)
            self.send("%s OK UID MOVE completed" % tag)
        elif sub == "COPY":
            st.copy(self.cur, int(arg1), arg2.strip().strip('"'))
            self.send("%s OK UID COPY completed" % tag)
        else:
            self.send("%s BAD unknown UID command %s" % (tag, sub))

    def search_uids(self, crit):
        f = self.server.state.get(self.cur)
        if not f:
            return []
        uids = list(f["uids"])
        crit = [c.upper() for c in crit]
        if "UNSEEN" in crit:
            uids = [u for u in uids if "\\Seen" not in f["msgs"][u]["flags"]]
        for i, c in enumerate(crit):
            if c == "UID" and i + 1 < len(crit):
                rng = crit[i + 1]
                if ":" in rng:
                    a, b = rng.split(":", 1)
                    lo = int(a)
                    hi = max(f["uids"]) if b == "*" else int(b)
                    uids = [u for u in uids if lo <= u <= hi]
        return uids

    def do_fetch(self, tag, uid, spec):
        f = self.server.state.get(self.cur)
        msg = f["msgs"].get(uid) if f else None
        if not msg:
            self.send("%s NO uid not present" % tag)
            return
        seq = f["uids"].index(uid) + 1
        raw = msg["raw"]
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
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        self.server.calls.append({"system": system, "user": user})
        if "write email replies" in system:
            content = FAKE_DRAFT
        elif "rule architect" in system:
            content = json.dumps({
                "reply": "Here is a newsletter rule you can add.",
                "proposed_rules": [
                    {"name": "File newsletters", "match_mode": "any",
                     "conditions": [{"field": "subject", "op": "contains", "value": "newsletter"},
                                    {"field": "from", "op": "contains", "value": "newsletter@"}],
                     "actions": {"move_to": "Newsletters"}, "rationale": "bulk mail"},
                    {"name": "Broken rule", "match_mode": "all", "conditions": [], "actions": {}},
                ]})
        elif "connectivity test" in system:
            content = "ok"
        else:
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
            content = json.dumps({
                "category": cat,
                "needs_reply": ("lunch" in t or "budget" in t),
                "confidence": conf,
                "summary": "mock: " + cat,
            })
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- helpers

def add_msg(state, frm, subj, body, msgid, folder="INBOX"):
    raw = ("From: %s\r\nTo: seanyong@ust.hk\r\nSubject: %s\r\n"
           "Date: Wed, 30 Sep 2026 10:00:00 +0800\r\nMessage-ID: <%s>\r\n"
           "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n%s"
           % (frm, subj, msgid, body)).encode()
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
    llm_server.calls = []
    llm_server.daemon_threads = True
    llm_port = llm_server.server_address[1]
    threading.Thread(target=llm_server.serve_forever, daemon=True).start()

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
    })
    sys.path.insert(0, PROJECT)
    import config
    import store
    import engine

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
    check("re-indexed rows cover original messages", set(uids_rows) == {2, 3, 4, 6})
    check("re-processing re-filed newsletter+receipt, kept lunch+boss",
          sorted(state.get("INBOX")["uids"]) == [4, 6])

    section("T8 web UI smoke (Flask test client)")
    import app as app_mod
    client = app_mod.app.test_client()
    for path in ("/", "/assistant", "/rules", "/templates", "/messages", "/settings", "/log", "/healthz"):
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

    section("T9 assistant: chat -> proposal -> add rule")
    rules_before = len(store.list_rules())
    r = client.post("/assistant/send", data={"message": "Sort newsletters out of my inbox"})
    check("assistant send redirects", r.status_code == 302)
    convo = store.assistant_messages(limit=10)
    check("two turns stored", len(convo) == 2 and convo[0]["role"] == "user"
          and convo[1]["role"] == "assistant")
    props = json.loads(convo[-1]["proposals"])
    check("valid proposal kept, broken one dropped",
          len(props) == 1 and props[0]["name"] == "File newsletters")
    r = client.get("/assistant")
    check("assistant page renders with proposal", r.status_code == 200 and b"File newsletters" in r.data)
    r = client.post("/assistant/apply", data={"msg_id": convo[-1]["id"], "idx": 0})
    rules_now = store.list_rules()
    check("rule added from chat", len(rules_now) == rules_before + 1)
    new_rule = [x for x in rules_now if x["name"] == "File newsletters"][0]
    conds = json.loads(new_rule["conditions"])
    check("rule fields round-tripped", new_rule["enabled"] == 1 and conds[0]["field"] == "subject")
    r = client.post("/assistant/clear")
    check("conversation cleared", len(store.assistant_messages(limit=10)) == 0)

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

    print("\n%s\n%d passed, %d failed (workspace: %s)\n"
          % ("ALL PASS" if failed == 0 else "FAILURES PRESENT", passed, failed, tmp))
    try:
        imap_server.shutdown()
        llm_server.shutdown()
    except Exception:
        pass
    if failed == 0:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
