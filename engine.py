"""Mail Triage engine: IMAP through the email-oauth2-proxy, rule matching,
LLM escalation, and the background worker."""
from collections import Counter

import email
import email.header
import email.parser
import email.utils
import html
import imaplib
import json
import quopri
import re
import threading
import time

import requests

import config
import store


# ---------------------------------------------------------------- helpers

def _decode_header(value):
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                out.append(text.decode(enc or "utf-8", "replace"))
            else:
                out.append(text)
        return "".join(out)
    except Exception:
        return str(value)


def _clean_snippet(raw):
    """Turn up to a few KB of a raw message body into readable-ish text."""
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    # quoted-printable leftovers (common on O365 text parts) without part headers
    if len(re.findall(r"=[0-9A-Fa-f]{2}", text)) > 20:
        try:
            text = quopri.decodestring(text.encode("latin-1", "replace")).decode("utf-8", "replace")
        except Exception:
            pass
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1500]


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def render_template_text(template_body, msg):
    fields = _SafeDict(
        sender=msg.get("from_addr", ""),
        subject=msg.get("subject", ""),
        date=msg.get("date", ""),
        my_name=store.get_setting("my_name", "Sean"),
    )
    try:
        return template_body.format_map(fields)
    except Exception:
        return template_body


def build_draft_message(msg, body_text, user):
    from email.message import EmailMessage
    m = EmailMessage()
    m["To"] = msg.get("from_addr", "")
    subj = msg.get("subject") or ""
    m["Subject"] = subj if subj.lower().startswith("re:") else "Re: " + subj
    mid = (msg.get("msgid") or "").strip()
    if mid and not mid.startswith("<"):
        mid = "<%s>" % mid
    if mid:
        m["In-Reply-To"] = mid
        m["References"] = mid
    if user:
        m["From"] = user
    m.set_content(body_text or "")
    return m.as_bytes()


# ---------------------------------------------------------------- IMAP

class MailClient:
    """Thin IMAP wrapper. Connects to the proxy with a PLAIN connection (the proxy
    performs OAuth 2.0 and secures the far side)."""

    def __init__(self):
        self.M = None
        self._folders = None
        self.selected = None

    def connect(self):
        if config.IMAP_TLS:
            self.M = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=30)
        else:
            self.M = imaplib.IMAP4(config.IMAP_HOST, config.IMAP_PORT, timeout=30)
        self.selected = None
        typ, dat = self.M.login(config.IMAP_USER, config.IMAP_PASSWORD)
        if typ != "OK":
            raise RuntimeError("IMAP login failed: %s %s" % (typ, dat))
        return self

    def close(self):
        if self.M is not None:
            try:
                self.M.logout()
            except Exception:
                pass
            self.M = None

    # ---- folders

    def folders(self, refresh=False):
        if self._folders is None or refresh:
            typ, dat = self.M.list()
            if typ != "OK":
                raise RuntimeError("LIST failed: %s %s" % (typ, dat))
            out = {}
            for line in dat or []:
                if not line:
                    continue
                s = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
                mobj = re.match(r'\((?P<f>[^)]*)\)\s+"(?P<d>[^"]*)"\s+(?P<n>.+)$', s)
                if mobj:
                    out[mobj.group("n").strip().strip('"')] = mobj.group("f")
            self._folders = out
        return self._folders

    def find_special_use(self, flag):
        for name, flags in self.folders().items():
            if flag.lower() in (flags or "").lower():
                return name
        return None

    def ensure_folder(self, folder):
        if folder in self.folders():
            return
        typ, dat = self.M.create('"%s"' % folder)
        if typ != "OK":
            raise RuntimeError("CREATE %s failed: %s %s" % (folder, typ, dat))
        self.folders(refresh=True)

    # ---- mailboxes / messages

    def select(self, folder):
        typ, dat = self.M.select('"%s"' % folder)
        if typ != "OK":
            raise RuntimeError("SELECT %s failed: %s %s" % (folder, typ, dat))
        self.selected = folder
        uv = 0
        resp = self.M.response("UIDVALIDITY")
        if resp and resp[1] and resp[1][0]:
            try:
                uv = int(resp[1][0])
            except (TypeError, ValueError):
                uv = 0
        return uv

    def ensure_selected(self, folder):
        """Select `folder` unless it is already the selected mailbox."""
        if self.selected != folder:
            self.select(folder)

    def search(self, *criteria):
        typ, dat = self.M.uid("SEARCH", *criteria)
        if typ != "OK":
            raise RuntimeError("SEARCH failed: %s %s" % (typ, dat))
        out = []
        for chunk in dat or []:
            if not chunk:
                continue
            out += [int(x) for x in chunk.split()]
        return sorted(set(out))

    def _fetch_literal(self, uid, spec):
        typ, dat = self.M.uid("FETCH", str(uid), spec)
        if typ != "OK":
            raise RuntimeError("FETCH %s failed: %s %s" % (spec, typ, dat))
        for item in dat or []:
            if isinstance(item, tuple) and len(item) >= 2 and item[1]:
                return item[1]
        return b""

    def fetch_meta(self, uid):
        hdr_raw = self._fetch_literal(
            uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])")
        try:
            snippet_raw = self._fetch_literal(uid, "(BODY.PEEK[TEXT]<0.4000>)")
        except RuntimeError:
            snippet_raw = self._fetch_literal(uid, "(BODY.PEEK[TEXT])")[:4000]
        try:
            hdr = email.parser.BytesHeaderParser().parsebytes(hdr_raw) if hdr_raw else {}
        except Exception:
            hdr = {}
        from_disp, from_addr = email.utils.parseaddr(_decode_header(hdr.get("From", "")))
        _, to_addr = email.utils.parseaddr(_decode_header(hdr.get("To", "")))
        return {
            "msgid": _decode_header(hdr.get("Message-ID", "")).strip("<>"),
            "from_addr": from_addr or from_disp,
            "from_display": from_disp,
            "to_addr": to_addr,
            "subject": re.sub(r"\s+", " ", _decode_header(hdr.get("Subject", ""))).strip(),
            "date": _decode_header(hdr.get("Date", "")),
            "snippet": _clean_snippet(snippet_raw),
        }

    def fetch_body_text(self, uid, limit=6000):
        try:
            raw = self._fetch_literal(uid, "(BODY.PEEK[TEXT]<0.%d>)" % limit)
        except RuntimeError:
            raw = self._fetch_literal(uid, "(BODY.PEEK[TEXT])")[:limit]
        return _clean_snippet(raw)

    def set_flags(self, uid, op, flags):
        typ, dat = self.M.uid("STORE", str(uid), op, flags)
        if typ != "OK":
            raise RuntimeError("STORE failed: %s %s" % (typ, dat))

    def move(self, uid, folder):
        typ, dat = self.M.uid("MOVE", str(uid), '"%s"' % folder)
        if typ == "OK":
            return
        typ, dat = self.M.uid("COPY", str(uid), '"%s"' % folder)
        if typ != "OK":
            raise RuntimeError("COPY %s failed: %s %s" % (folder, typ, dat))
        self.M.uid("STORE", str(uid), "+FLAGS", r"(\Deleted)")
        self.M.expunge()

    def append_draft(self, folder, raw):
        typ, dat = self.M.append('"%s"' % folder, r"(\Draft)",
                                 imaplib.Time2Internaldate(time.time()), raw)
        if typ != "OK":
            raise RuntimeError("APPEND to %s failed: %s %s" % (folder, typ, dat))


# ---------------------------------------------------------------- rules

def rule_matches(rule, fields):
    try:
        conds = json.loads(rule.get("conditions") or "[]")
    except (TypeError, ValueError):
        return False
    if not conds:
        return False
    results = []
    for c in conds:
        field = (c.get("field") or "subject").lower()
        op = (c.get("op") or "contains").lower()
        val = c.get("value") or ""
        hay_raw = fields.get(field) or ""
        hay = hay_raw.lower()
        if op == "contains":
            ok = bool(val) and val.lower() in hay
        elif op == "equals":
            ok = hay.strip() == val.strip().lower()
        elif op == "regex":
            try:
                ok = re.search(val, hay_raw, re.I) is not None
            except re.error:
                ok = False
        else:
            ok = False
        results.append(ok)
    if len(results) == 1:
        return results[0]
    return all(results) if (rule.get("match_mode") or "all") == "all" else any(results)


def match_first(rules, fields):
    for rule in rules:
        if rule_matches(rule, fields):
            return rule
    return None


# ---------------------------------------------------------------- LLM

class LLMClient:
    def __init__(self):
        self.base = config.LLM_BASE_URL
        self.key = config.LLM_API_KEY
        self.model = config.LLM_MODEL
        self.fallback = None
        if config.LLM_FALLBACK_BASE_URL:
            self.fallback = (config.LLM_FALLBACK_BASE_URL,
                             config.LLM_FALLBACK_API_KEY,
                             config.LLM_FALLBACK_MODEL or config.LLM_MODEL)

    def _chat(self, system, user, json_mode=True, history=None):
        convo = list(history or []) + [{"role": "user", "content": user}]
        return self._chat_convo(system, convo, json_mode=json_mode)

    def _chat_convo(self, system, convo, json_mode=True):
        try:
            return self._chat_once(self.base, self.key, self.model, system, convo, json_mode)
        except Exception as primary_exc:
            if not self.fallback:
                raise
            fb_base, fb_key, fb_model = self.fallback
            try:
                out = self._chat_once(fb_base, fb_key, fb_model, system, convo, json_mode)
            except Exception:
                raise primary_exc
            store.log_event("info", "LLM: primary '%s' failed (%s) - served by fallback '%s'"
                            % (self.model, type(primary_exc).__name__, fb_model))
            return out

    def _chat_once(self, base, key, model, system, convo, json_mode=True):
        if not key:
            raise RuntimeError("LLM_API_KEY is not configured for %s" % base)
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}] + convo,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        r = None
        for attempt in (1, 2):
            r = requests.post(base + "/chat/completions", json=payload,
                              headers={"Authorization": "Bearer " + key},
                              timeout=config.LLM_TIMEOUT)
            if r.status_code == 400 and json_mode and attempt == 1:
                payload.pop("response_format", None)  # provider does not support it
                continue
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        if r is not None:
            r.raise_for_status()
        raise RuntimeError("LLM request failed")

    def classify(self, msg, categories, my_name="Sean"):
        cats = ", ".join(categories) if categories else "Action, Notification, Newsletter, Receipt, Personal, Promo"
        system = ("You triage incoming email for %s. Reply with a single JSON object and nothing else. "
                  "Shape: {\"category\": one of [%s], \"needs_reply\": true|false, "
                  "\"confidence\": 0.0-1.0, \"summary\": \"at most 12 words\"}" % (my_name or "the user", cats))
        user = ("From: %s\nTo: %s\nSubject: %s\nDate: %s\n\n%s"
                % (msg.get("from_addr", ""), msg.get("to_addr", ""), msg.get("subject", ""),
                   msg.get("date", ""), (msg.get("snippet") or "")[:1500]))
        content = self._chat(system, user, json_mode=True)
        m = re.search(r"\{.*\}", content or "", re.S)
        if not m:
            raise RuntimeError("LLM returned no JSON: %r" % (content or "")[:200])
        result = json.loads(m.group(0))
        if not isinstance(result, dict) or not result.get("category"):
            raise RuntimeError("LLM JSON missing category: %r" % result)
        return result

    def draft_reply(self, msg, body_text, template, settings):
        my_name = settings.get("my_name", "Sean")
        system = ("You write email replies as %s (%s). Be concise, warm and professional. "
                  "Output ONLY the plain-text reply body (no subject line, no headers, no quotes)."
                  % (my_name, config.IMAP_USER))
        guidance = ""
        if template:
            subject_hint = template.get("subject") or ""
            body_hint = render_template_text(template.get("body") or "", msg)
            guidance = ("Use this reply template as guidance for structure and tone:\n---\n%s\n%s\n---\n"
                        % (subject_hint, body_hint))
        user = ("%sOriginal message:\nFrom: %s\nSubject: %s\nDate: %s\n\n%s"
                % (guidance, msg.get("from_addr", ""), msg.get("subject", ""),
                   msg.get("date", ""), (body_text or msg.get("snippet") or "")[:6000]))
        return (self._chat(system, user, json_mode=False) or "").strip()


# ---------------------------------------------------------------- worker

class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="triage-worker")
        self.force = threading.Event()
        self.stop_flag = threading.Event()
        self.state = {"last_cycle": 0, "last_ok": 0, "last_error": None, "running": False,
                      "last_summary": ""}

    def trigger(self):
        self.force.set()

    def run(self):
        store.init_db()
        self.stop_flag.wait(2)  # let the UI come up first
        while not self.stop_flag.is_set():
            settings = store.all_settings()
            interval = max(15, int(settings.get("poll_interval", 90) or 90))
            due = time.time() - self.state["last_cycle"] >= interval
            if self.force.is_set() or due:
                self.force.clear()
                self.run_cycle()
                continue
            self.stop_flag.wait(1)

    def run_cycle(self):
        self.state["running"] = True
        started = time.time()
        try:
            summary = process_mailbox()
            self.state["last_ok"] = int(time.time())
            self.state["last_error"] = None
            self.state["last_summary"] = summary
            if summary:
                store.log_event("info", "check complete in %.1fs - %s" % (time.time() - started, summary))
            else:
                store.log_event("debug", "check complete in %.1fs (nothing new)" % (time.time() - started))
        except Exception as exc:
            self.state["last_error"] = repr(exc)
            store.log_event("error", "check failed: %r" % exc)
        finally:
            self.state["last_cycle"] = time.time()
            self.state["running"] = False


def _fields_for(meta):
    return {"from": meta.get("from_addr", ""), "to": meta.get("to_addr", ""),
            "subject": meta.get("subject", ""), "body": meta.get("snippet", "")}


def _process_folder(mc, folder, settings, rules):
    uv = mc.select(folder)
    last_uid, last_uv = store.last_uid(folder)
    if last_uid is not None and last_uv != uv:
        store.reset_folder_index(folder)
        store.log_event("info", "%s: UIDVALIDITY changed — re-indexing folder" % folder)
        last_uid = None
    if last_uid is None:
        since = time.strftime("%d-%b-%Y",
                              time.gmtime(time.time() - int(settings.get("lookback_hours", 48)) * 3600))
        uids = mc.search("SINCE", since)
    else:
        uids = [u for u in mc.search("UID", "%d:*" % (last_uid + 1)) if u > last_uid]
    scanned = moved = 0
    for uid in uids[:120]:
        try:
            meta = mc.fetch_meta(uid)
        except Exception as exc:
            store.log_event("error", "fetch failed for %s uid=%s: %r" % (folder, uid, exc))
            continue
        store.insert_message(folder, uid, uv, meta)  # no-op if already known
        row = store.get_message_by_uid(folder, uid, uv)
        if row is None:
            continue
        scanned += 1
        rule = match_first(rules, _fields_for(meta))
        if rule:
            try:
                actions = json.loads(rule.get("actions") or "{}")
            except (TypeError, ValueError):
                actions = {}
            taken = []
            apply = bool(settings.get("rules_apply", True))
            status = "matched-dry"
            if apply:
                try:
                    if actions.get("move_to"):
                        mc.ensure_folder(actions["move_to"])
                        mc.move(uid, actions["move_to"])
                        taken.append("move:" + actions["move_to"])
                    if actions.get("mark_read"):
                        mc.set_flags(uid, "+FLAGS", r"(\Seen)")
                        taken.append("read")
                    if actions.get("flag"):
                        mc.set_flags(uid, "+FLAGS", r"(\Flagged)")
                        taken.append("flag")
                    status = "matched"
                    if taken:
                        moved += 1
                except Exception as exc:
                    status = "error"
                    store.log_event("error", "rule '%s' action failed for uid=%s: %r"
                                    % (rule.get("name"), uid, exc))
            store.update_message(row["id"], status=status, rule_id=rule["id"],
                                 action_taken=",".join(taken))
            store.log_event("info", "rule '%s' → %s | %s (%s)"
                            % (rule.get("name") or rule["id"], ", ".join(taken) or "suggest",
                               (meta.get("subject") or "")[:60], meta.get("from_addr")))
        else:
            store.update_message(row["id"], status="queued")
    if uids:
        store.log_event("debug", "%s: scanned %d new message(s)" % (folder, scanned))
    return scanned, moved


def _process_llm_queue(mc, settings, batch):
    llm = LLMClient()
    categories = settings.get("categories") or []
    done = 0
    for msg in store.queued_messages(batch):
        try:
            res = llm.classify(msg, categories, settings.get("my_name", ""))
            store.add_llm_log(msg["id"], True)
        except Exception as exc:
            store.add_llm_log(msg["id"], False, repr(exc))
            fails = store.llm_fail_count(msg["id"])
            if fails >= 3:
                store.update_message(msg["id"], status="error")
                store.log_event("error", "LLM failed %d times for '%s' - parked "
                                "(fix the LLM endpoint, then use Retry parked)"
                                % (fails, (msg.get("subject") or "")[:50]))
            else:
                store.log_event("error", "LLM attempt %d failed for '%s' (will retry): %s"
                                % (fails, (msg.get("subject") or "")[:50], exc))
            continue
        category = str(res.get("category", ""))
        try:
            conf = float(res.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        folder = (settings.get("category_folders") or {}).get(category, "")
        fields = {
            "llm_category": category,
            "llm_confidence": conf,
            "llm_summary": str(res.get("summary", ""))[:200],
            "llm_needs_reply": 1 if res.get("needs_reply") else 0,
            "llm_suggested_folder": folder,
        }
        if settings.get("llm_apply") and folder:
            try:
                mc.ensure_selected(msg["folder"])
                mc.ensure_folder(folder)
                mc.move(msg["uid"], folder)
                fields["status"] = "llm-moved"
                fields["action_taken"] = "move:" + folder
            except Exception as exc:
                fields["status"] = "classified"
                store.log_event("error", "LLM move to %s failed: %r" % (folder, exc))
        else:
            fields["status"] = "classified"
        store.update_message(msg["id"], **fields)
        store.log_event("info", "LLM: '%s' → %s (%.0f%%) %s"
                        % ((msg.get("subject") or "")[:50], category, conf * 100,
                           ("moved to %s" % folder) if fields.get("action_taken") else "(suggestion only)"))
        done += 1
    return done


def process_mailbox():
    """One full pass: scan watched folders, apply rules, run the LLM queue."""
    settings = store.all_settings()
    rules = [r for r in store.list_rules() if r.get("enabled")]
    mc = MailClient().connect()
    scanned = moved = classified = 0
    try:
        for folder in settings.get("watch_folders") or ["INBOX"]:
            s, m = _process_folder(mc, folder, settings, rules)
            scanned += s
            moved += m
        if settings.get("llm_suggest"):
            budget = int(settings.get("max_llm_per_hour", 40)) - store.llm_count_last_hour()
            batch = max(0, min(int(settings.get("llm_batch_per_cycle", 5)), budget))
            if batch:
                classified = _process_llm_queue(mc, settings, batch)
    finally:
        mc.close()
    parts = []
    if scanned:
        parts.append("%d new" % scanned)
    if moved:
        parts.append("%d sorted" % moved)
    if classified:
        parts.append("%d classified" % classified)
    return ", ".join(parts)


def connectivity_check():
    """Read-only check used by the dashboard / --check mode."""
    mc = MailClient().connect()
    try:
        folders = mc.folders()
        uv = mc.select("INBOX")
        unseen = len(mc.search("UNSEEN"))
        return {"ok": True, "user": config.IMAP_USER, "folders": len(folders),
                "inbox_uidvalidity": uv, "unseen": unseen}
    finally:
        mc.close()


def generate_draft(msg_id, template_id=None):
    msg = store.get_message(msg_id)
    if not msg:
        raise RuntimeError("message %s not found" % msg_id)
    template = store.get_template(template_id) if template_id else None
    mc = MailClient().connect()
    body_text = ""
    try:
        if msg.get("folder"):
            try:
                mc.select(msg["folder"])
                body_text = mc.fetch_body_text(msg["uid"])
            except Exception as exc:
                store.log_event("debug", "draft: body fetch failed (%r) — using snippet" % exc)
    finally:
        mc.close()
    return LLMClient().draft_reply(msg, body_text, template, store.all_settings())


def save_draft(msg_id, body_text):
    msg = store.get_message(msg_id)
    if not msg:
        raise RuntimeError("message %s not found" % msg_id)
    raw = build_draft_message(msg, body_text, config.IMAP_USER)
    mc = MailClient().connect()
    try:
        folder = store.get_setting("drafts_folder") or mc.find_special_use("\\Drafts") or "Drafts"
        mc.append_draft(folder, raw)
    finally:
        mc.close()
    store.log_event("info", "draft saved for '%s'" % (msg.get("subject") or "")[:60])
    return folder

# ---------------------------------------------------------------- assistant

ASSISTANT_INSTRUCTIONS = """You are the rule architect for "Mail Triage", a local email-sorting app.
The user describes how they want their mail sorted, in plain language. You turn that into filter
rules the app executes, and explain briefly. Most mail should end up handled by simple rules; the
LLM classifier only sees what no rule matched.

Reply with ONE JSON object and nothing else:
{"reply": "short plain-text message to the user",
 "proposed_rules": [
   {"name": "short rule name",
    "match_mode": "all" or "any",
    "conditions": [{"field": "from|to|subject|body", "op": "contains|equals|regex", "value": "..."}],
    "actions": {"move_to": "Folder name", "mark_read": true, "flag": true},
    "rationale": "one line"}]}

Rules about rules:
- 1-4 conditions each; prefer "contains" on a distinctive substring (an address fragment like
  "@linkedin.com" or a subject word). Use "regex" only if the user asks for it.
- Every proposed rule needs at least one condition AND at least one action (move_to / mark_read / flag).
- move_to creates the folder if missing; use short folder names ("Newsletters", "Receipts", "Work").
- Max 3 proposed rules per turn; use [] when you are just answering or asking a clarifying question.
- Rules are added by the user with one click and act on mail scanned after that; they are evaluated
  top to bottom, first match wins.
- Keep "reply" under 80 words, friendly, no fluff. Never promise capabilities the app does not have
  (no deleting, no sending replies)."""


def _safe_json(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _rules_to_text(rules):
    if not rules:
        return "(none yet)"
    lines = []
    for i, r in enumerate(rules, 1):
        conds = _safe_json(r.get("conditions"), [])
        acts = _safe_json(r.get("actions"), {})
        joiner = " AND " if (r.get("match_mode") or "all") == "all" else " OR "
        cs = joiner.join('%s %s "%s"' % (c.get("field"), c.get("op"), c.get("value")) for c in conds)
        parts = []
        if acts.get("move_to"):
            parts.append("move to %s" % acts["move_to"])
        if acts.get("mark_read"):
            parts.append("mark read")
        if acts.get("flag"):
            parts.append("flag")
        state = "" if r.get("enabled") else " [disabled]"
        lines.append("%d. %s: %s => %s%s"
                     % (i, r.get("name") or "rule", cs, ", ".join(parts) or "noop", state))
    return "\n".join(lines)


ALLOWED_FIELDS = ("from", "to", "subject", "body")
ALLOWED_OPS = ("contains", "equals", "regex")


def normalize_rule(proposal):
    """Validate an LLM-proposed rule; returns a clean dict or None."""
    if not isinstance(proposal, dict):
        return None
    name = str(proposal.get("name") or "").strip()[:80] or "Assistant rule"
    mode = "any" if str(proposal.get("match_mode") or "").lower() == "any" else "all"
    conditions = []
    for c in proposal.get("conditions") or []:
        if not isinstance(c, dict):
            continue
        field = str(c.get("field") or "").lower()
        op = str(c.get("op") or "contains").lower()
        value = str(c.get("value") or "").strip()
        if field not in ALLOWED_FIELDS or op not in ALLOWED_OPS or not value:
            continue
        if op == "regex":
            try:
                re.compile(value)
            except re.error:
                continue
        conditions.append({"field": field, "op": op, "value": value[:300]})
    actions = {}
    a = proposal.get("actions") or {}
    if isinstance(a, dict):
        if isinstance(a.get("move_to"), str) and a["move_to"].strip():
            actions["move_to"] = a["move_to"].strip()[:120]
        if a.get("mark_read"):
            actions["mark_read"] = True
        if a.get("flag"):
            actions["flag"] = True
    if not conditions or not actions:
        return None
    return {"name": name, "match_mode": mode, "conditions": conditions, "actions": actions,
            "rationale": str(proposal.get("rationale") or "").strip()[:300]}


def _parse_assistant_json(content):
    m = re.search(r"\{.*\}", content or "", re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except (TypeError, ValueError):
            pass
    return {"reply": (content or "").strip()[:1500], "proposed_rules": []}


def assistant_respond(user_text):
    """One turn of the rule-crafting chat. Returns (reply, normalized_proposals)."""
    user_text = (user_text or "").strip()
    if not user_text:
        raise RuntimeError("empty message")
    settings = store.all_settings()
    store.add_assistant_message("user", user_text[:4000])
    msgs = store.messages(limit=200)
    senders = Counter(m["from_addr"] for m in msgs if m.get("from_addr")).most_common(12)
    recent = ["%s | %s" % ((m.get("from_addr") or "")[:45], (m.get("subject") or "")[:70])
              for m in store.messages(limit=12)]
    context = (
        "CURRENT STATE\n"
        "Rules (top to bottom, first match wins):\n%s\n\n"
        "LLM classifier categories: %s\n"
        "Checked folders: %s | check interval: %ss\n"
        "Recently seen senders (count): %s\n"
        "Recent messages (sender | subject):\n%s"
        % (_rules_to_text(store.list_rules()),
           ", ".join(settings.get("categories") or []),
           ", ".join(settings.get("watch_folders") or ["INBOX"]),
           settings.get("poll_interval", 90),
           ", ".join("%s (%d)" % (s, c) for s, c in senders) or "(none)",
           "\n".join(recent) or "(none)")
    )
    convo = [{"role": r["role"], "content": r["content"]}
             for r in store.assistant_messages(limit=24)]
    content = LLMClient()._chat_convo(ASSISTANT_INSTRUCTIONS + "\n\n" + context, convo,
                                      json_mode=True)
    parsed = _parse_assistant_json(content)
    reply = str(parsed.get("reply") or "").strip()[:2000] or "(no reply)"
    proposals = []
    for item in (parsed.get("proposed_rules") or [])[:3]:
        norm = normalize_rule(item)
        if norm:
            proposals.append(norm)
    store.add_assistant_message("assistant", reply, proposals=json.dumps(proposals))
    return reply, proposals

