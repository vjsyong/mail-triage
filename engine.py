"""Mail Triage engine: IMAP through the email-oauth2-proxy, rule matching,
LLM escalation, and the background worker."""
from collections import Counter

import calendar
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
            # A failed SELECT leaves imaplib's state tracking stale (it marks the
            # connection unselected before issuing the command, so a later
            # SEARCH dies with "command SEARCH illegal in state AUTH"). Forget
            # our cached selection so the next use re-issues SELECT.
            self.selected = None
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

    # ---- streaming (used by the assistant agent) ----

    def chat_stream(self, system, messages, tools=None, thinking=True):
        """Stream one chat turn against the primary endpoint, yielding event dicts:
        reasoning_delta / content_delta / tool_calls / turn_done. When the primary
        fails before producing any output, the fallback endpoint serves instead."""
        attempts = [(self.base, self.key, self.model, "primary '%s'" % self.model)]
        if self.fallback:
            attempts.append((*self.fallback, "fallback '%s'" % self.fallback[2]))
        for i, (base, key, model, label) in enumerate(attempts):
            produced = False
            try:
                for ev in self._stream_once(base, key, model, system, messages, tools, thinking):
                    produced = True
                    yield ev
                return
            except Exception as exc:
                if produced or i == len(attempts) - 1:
                    raise
                store.log_event("info", "LLM stream: %s failed (%s) - serving from %s"
                                % (label, type(exc).__name__, attempts[i + 1][3]))
        raise RuntimeError("no LLM endpoint available")

    def _stream_once(self, base, key, model, system, messages, tools, thinking):
        if not key:
            raise RuntimeError("LLM_API_KEY is not configured for %s" % base)
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 2500,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": system}] + messages,
        }
        if tools:
            payload["tools"] = tools
        if thinking:
            # vLLM extension: lets this chat template emit the thinking channel,
            # which the reasoning parser surfaces as delta.reasoning.
            payload["chat_template_kwargs"] = {"enable_thinking": True}
        r = requests.post(base + "/chat/completions", json=payload,
                          headers={"Authorization": "Bearer " + key},
                          timeout=config.LLM_TIMEOUT, stream=True)
        try:
            if r.status_code != 200:
                raise RuntimeError("LLM HTTP %s from %s: %s"
                                   % (r.status_code, base, (r.text or "")[:300]))
            tool_state = {}
            finish = None
            usage = None
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                ch = choices[0]
                delta = ch.get("delta") or {}
                text = delta.get("reasoning") or delta.get("reasoning_content")
                if text:
                    yield {"type": "reasoning_delta", "text": text}
                if delta.get("content"):
                    yield {"type": "content_delta", "text": delta["content"]}
                for tc in delta.get("tool_calls") or []:
                    try:
                        idx = int(tc.get("index") or 0)
                    except (TypeError, ValueError):
                        idx = 0
                    st = tool_state.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        st["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        st["name"] += fn["name"]
                    if fn.get("arguments"):
                        st["arguments"] += fn["arguments"]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
            if tool_state:
                calls = []
                for idx in sorted(tool_state):
                    st = tool_state[idx]
                    calls.append({"id": st["id"] or ("call_%d" % idx), "name": st["name"],
                                  "arguments": st["arguments"]})
                yield {"type": "tool_calls", "calls": calls}
            yield {"type": "turn_done", "finish_reason": finish, "usage": usage}
        finally:
            r.close()

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
#
# The assistant is a streaming, tool-calling agent. It talks to the same
# OpenAI-compatible endpoint as the classifier, but with:
#   * stream=True  -> the UI shows tokens + the model's thinking live (SSE)
#   * tools=[...]  -> it can search the mailbox (local index + live IMAP over
#                     the full history), read/move/flag messages, create
#                     folders, and propose rules for one-click approval.
# The transcript (reasoning + tool steps) is persisted per message so the
# chat page can render it again later.

ASSISTANT_SYSTEM = """You are the mail operations assistant for "Mail Triage", a local app that sorts the mailbox of %(user)s. You inspect the mailbox and act on it through tools, and you design the filter rules the app executes.

How to work
- Ground every answer with tools instead of guessing. search_messages reads the app's local index (what the background scanner has seen); search_mail runs a live IMAP search over the full mailbox, any folder, including history much older than the app. For any question about the user's mail, search first.
- You may act directly on what the user asks for: create_folder, move_message, flag_message. Moving never deletes mail. For ongoing sorting, propose a rule with propose_rule instead (the user approves proposals with one click).
- You cannot send mail, reply to mail, or delete mail; never claim that you did.
- Keep searches bounded: small limits, use since/before for windows. Summarize results; never dump raw rows.
- At most %(max_calls)d tool calls per step. Stop as soon as you can answer or act.

Today is %(today)s (Hong Kong time). Reply in the user's language, as plain text (no markdown tables), concise and friendly. Text you write is shown to the user directly; tool calls happen through the tool interface."""


def _fn(name, description, properties=None, required=()):
    params = {"type": "object", "properties": properties or {}}
    if required:
        params["required"] = list(required)
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": params}}


ASSISTANT_TOOLS = [
    _fn("mailbox_overview",
        "Snapshot of mailbox state: folders (with counts), indexed message counts by status, categories, current rules. Call this first when you need orientation.",
        {}),
    _fn("search_messages",
        "Search the app's LOCAL INDEX of scanned messages (all folders it has seen, newest first). Instant; covers the window the app has processed. For older mail or other folders use search_mail.",
        {"query": {"type": "string", "description": "free text; matches sender, subject and snippet"},
         "sender": {"type": "string", "description": "sender address fragment"},
         "subject": {"type": "string", "description": "subject fragment"},
         "folder": {"type": "string", "description": "exact folder name"},
         "status": {"type": "string", "description": "sorted / classified / queued / error …"},
         "since": {"type": "string", "description": "YYYY-MM-DD; only messages seen on/after this date"},
         "until": {"type": "string", "description": "YYYY-MM-DD; only messages seen on/before this date"},
         "limit": {"type": "integer", "description": "max rows (default 20, max 100)"},
         "offset": {"type": "integer", "description": "skip this many rows (paging)"}}),
    _fn("search_mail",
        "Live IMAP search over the real mailbox (full history, any folder). Criteria are ANDed. Returns the newest matches with folder + uid; use those with read_message / move_message / flag_message.",
        {"folder": {"type": "string", "description": "mailbox folder (default INBOX); see list_folders"},
         "from_contains": {"type": "string", "description": "substring of the sender address"},
         "subject_contains": {"type": "string", "description": "substring of the subject"},
         "body_contains": {"type": "string", "description": "substring of the message body"},
         "since": {"type": "string", "description": "YYYY-MM-DD; messages on/after this date"},
         "before": {"type": "string", "description": "YYYY-MM-DD; messages strictly before this date"},
         "unseen_only": {"type": "boolean", "description": "only unread messages"},
         "limit": {"type": "integer", "description": "max rows, default 20, max 50"}}),
    _fn("read_message",
        "Read one message: headers plus the full text body. Identify it with message_id (from search_messages) OR folder + uid (from search_mail).",
        {"message_id": {"type": "integer"},
         "folder": {"type": "string"},
         "uid": {"type": "integer"}}),
    _fn("move_message",
        "Move one message to a folder (created if missing). Identify it with message_id OR folder + uid. Moves never delete mail.",
        {"target_folder": {"type": "string", "description": "destination folder name"},
         "message_id": {"type": "integer"},
         "folder": {"type": "string"},
         "uid": {"type": "integer"}},
        ("target_folder",)),
    _fn("flag_message",
        "Set or clear flags on one message (\\Seen = read, \\Flagged = starred). Identify it with message_id OR folder + uid.",
        {"message_id": {"type": "integer"},
         "folder": {"type": "string"},
         "uid": {"type": "integer"},
         "seen": {"type": "boolean", "description": "true = mark read, false = mark unread"},
         "flagged": {"type": "boolean", "description": "true = star, false = unstar"}}),
    _fn("create_folder",
        "Create a folder if it does not exist.",
        {"name": {"type": "string"}}, ("name",)),
    _fn("list_folders",
        "List the mailbox folders with total and unseen message counts.",
        {}),
    _fn("propose_rule",
        "Propose a filter rule for the user to approve with one click. Approved rules sort matching mail automatically (top to bottom, first match wins).",
        {"name": {"type": "string", "description": "short rule name"},
         "match_mode": {"type": "string", "enum": ["all", "any"]},
         "conditions": {"type": "array", "description": "1-4 conditions", "items": {
             "type": "object",
             "properties": {
                 "field": {"type": "string", "enum": ["from", "to", "subject", "body"]},
                 "op": {"type": "string", "enum": ["contains", "equals", "regex"]},
                 "value": {"type": "string"}}}},
         "actions": {"type": "object", "description": "what the rule does",
                     "properties": {"move_to": {"type": "string"},
                                    "mark_read": {"type": "boolean"},
                                    "flag": {"type": "boolean"}}},
         "rationale": {"type": "string", "description": "one line for the user"}},
        ("name", "conditions", "actions")),
]


def _safe_json(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def _json_args(raw):
    """Best-effort parse of streamed tool-call arguments (string) -> dict."""
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except (TypeError, ValueError):
        pass
    try:
        import ast
        val = ast.literal_eval(raw)
        return val if isinstance(val, dict) else {"value": val}
    except Exception:
        return {}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return None


def _imap_date(value):
    """Accept YYYY-MM-DD (preferred) or a few common forms; return DD-Mon-YYYY."""
    v = (value or "").strip()
    for f in ("%Y-%m-%d", "%Y/%m/%d", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            return time.strftime("%d-%b-%Y", time.strptime(v, f))
        except ValueError:
            continue
    return v


def _epoch_from_date(value, end=False):
    """Parse a date string to a UTC-midnight epoch (end of day if end=True)."""
    v = (value or "").strip()
    for f in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            base = calendar.timegm(time.strptime(v, f))
            return base + 86399 if end else base
        except ValueError:
            continue
    return None


def _looks_like_tools_unsupported(exc):
    s = str(exc).lower()
    return "400" in s and ("tool" in s or "function" in s)


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


def _validate_rule(proposal):
    """Validate a proposed rule. Returns (normalized | None, [errors])."""
    errors = []
    if not isinstance(proposal, dict):
        return None, ["proposal must be an object"]
    name = str(proposal.get("name") or "").strip()[:80] or "Assistant rule"
    mode = "any" if str(proposal.get("match_mode") or "").lower() == "any" else "all"
    conditions = []
    for c in proposal.get("conditions") or []:
        if not isinstance(c, dict):
            errors.append("each condition must be an object")
            continue
        field = str(c.get("field") or "").lower()
        op = str(c.get("op") or "contains").lower()
        value = str(c.get("value") or "").strip()
        if field not in ALLOWED_FIELDS:
            errors.append("bad field %r (use %s)" % (field, "/".join(ALLOWED_FIELDS)))
            continue
        if op not in ALLOWED_OPS:
            errors.append("bad op %r (use %s)" % (op, "/".join(ALLOWED_OPS)))
            continue
        if not value:
            errors.append("empty value for %s %s" % (field, op))
            continue
        if op == "regex":
            try:
                re.compile(value)
            except re.error as exc:
                errors.append("bad regex %r: %s" % (value, exc))
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
    if not conditions:
        errors.append("at least one valid condition is required")
    if not actions:
        errors.append("at least one action (move_to / mark_read / flag) is required")
    if errors:
        return None, errors
    return {"name": name, "match_mode": mode, "conditions": conditions, "actions": actions,
            "rationale": str(proposal.get("rationale") or "").strip()[:300]}, []


def normalize_rule(proposal):
    """Validate an LLM-proposed rule; returns a clean dict or None."""
    return _validate_rule(proposal)[0]


def _assistant_context():
    settings = store.all_settings()
    with store.db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return (
        "CURRENT STATE\n"
        "Rules (top to bottom, first match wins):\n%s\n\n"
        "LLM classifier categories: %s\n"
        "Category → folder map: %s\n"
        "Watched folders: %s | check interval: %ss\n"
        "Indexed messages: %d | assistant actions: %s\n"
        % (_rules_to_text(store.list_rules()),
           ", ".join(settings.get("categories") or []),
           ", ".join("%s=%s" % (k, v) for k, v in (settings.get("category_folders") or {}).items()) or "(none)",
           ", ".join(settings.get("watch_folders") or ["INBOX"]),
           settings.get("poll_interval", 90), total,
           "live" if settings.get("assistant_actions_apply", True) else "dry-run")
    )


class AssistantAgent:
    """Streaming tool-calling harness for the assistant chat.

    One instance per user turn. Yields UI events (reasoning / content /
    tool_start / tool_end / proposals / done / error) and persists the
    transcript to the assistant_messages table when it finishes.
    """

    MAX_STEPS = 8             # tool-calling rounds, then one forced wrap-up turn
    MAX_CALLS_PER_TURN = 4    # tool calls executed per model turn
    RESULT_CHARS = 4500       # max JSON chars of a tool result fed back to the model
    TRANSCRIPT_BUDGET = 30000  # cumulative tool-result chars before hard truncation

    def __init__(self):
        self.mc = None
        self.proposals = []
        self.tools_log = []
        self.actions_apply = bool(store.get_setting("assistant_actions_apply", True))
        self._budget = self.TRANSCRIPT_BUDGET
        self.steps_used = 0

    # ---- plumbing

    def _mail(self):
        if self.mc is None:
            self.mc = MailClient().connect()
        return self.mc

    def close(self):
        if self.mc is not None:
            try:
                self.mc.close()
            finally:
                self.mc = None

    # ---- tool dispatch

    def call_tool(self, name, args):
        fn = getattr(self, "_tool_" + str(name or ""), None)
        if fn is None:
            return {"ok": False, "summary": "unknown tool %r" % name,
                    "result": {"error": "unknown tool",
                               "available": [t["function"]["name"] for t in ASSISTANT_TOOLS]}}
        try:
            out = fn(args if isinstance(args, dict) else {})
        except Exception as exc:
            return {"ok": False, "summary": "tool %s failed: %r" % (name, exc),
                    "result": {"error": repr(exc)}}
        out.setdefault("ok", True)
        out.setdefault("summary", str(name))
        out.setdefault("result", {})
        return out

    # ---- tool implementations

    def _folders_with_counts(self):
        mc = self._mail()
        out = []
        for name in sorted(mc.folders()):
            entry = {"name": name}
            try:
                typ, dat = mc.M.status('"%s"' % name, "(MESSAGES UNSEEN)")
                if typ == "OK" and dat and dat[0]:
                    s = dat[0].decode("utf-8", "replace")
                    mm = re.search(r"MESSAGES\s+(\d+)", s)
                    uu = re.search(r"UNSEEN\s+(\d+)", s)
                    if mm:
                        entry["messages"] = int(mm.group(1))
                    if uu:
                        entry["unseen"] = int(uu.group(1))
            except Exception:
                pass
            out.append(entry)
        return out

    def _tool_mailbox_overview(self, a):
        settings = store.all_settings()
        with store.db() as conn:
            counts = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM messages GROUP BY status")}
        folders = self._folders_with_counts()
        rules = store.list_rules()
        data = {
            "indexed_messages": sum(counts.values()),
            "by_status": counts,
            "folders": folders,
            "rules": [{"name": r["name"], "enabled": bool(r["enabled"])} for r in rules],
            "categories": settings.get("categories"),
            "category_folders": settings.get("category_folders"),
            "watched_folders": settings.get("watch_folders"),
            "assistant_actions_live": self.actions_apply,
        }
        return {"ok": True,
                "summary": "%d indexed messages · %d folders · %d rules"
                           % (data["indexed_messages"], len(folders), len(rules)),
                "result": data}

    def _tool_search_messages(self, a):
        try:
            limit = max(1, min(int(a.get("limit") or 20), 100))
            offset = max(0, int(a.get("offset") or 0))
        except (TypeError, ValueError):
            limit, offset = 20, 0
        clauses, params = [], []
        q = (a.get("query") or "").strip()
        if q:
            clauses.append("(from_addr LIKE ? OR subject LIKE ? OR snippet LIKE ?)")
            params += ["%" + q + "%"] * 3
        for key, col in (("sender", "from_addr"), ("subject", "subject")):
            v = (a.get(key) or "").strip()
            if v:
                clauses.append("%s LIKE ?" % col)
                params.append("%" + v + "%")
        v = (a.get("folder") or "").strip()
        if v:
            clauses.append("folder = ?")
            params.append(v)
        v = (a.get("status") or "").strip()
        if v:
            clauses.append("status = ?")
            params.append(v)
        for key, op, end in (("since", ">=", False), ("until", "<=", True)):
            v = (a.get(key) or "").strip()
            if v:
                epoch = _epoch_from_date(v, end=end)
                if epoch is not None:
                    clauses.append("processed_at %s ?" % op)
                    params.append(epoch)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with store.db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM messages" + where, params).fetchone()[0]
            rows = [dict(r) for r in conn.execute(
                "SELECT id, folder, uid, from_addr, subject, date, status, action_taken, "
                "llm_category, processed_at, snippet FROM messages" + where +
                " ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, offset])]
        messages = [{"id": r["id"], "folder": r["folder"], "uid": r["uid"], "from": r["from_addr"],
                     "subject": r["subject"], "date": r["date"], "status": r["status"],
                     "action": r["action_taken"], "category": r["llm_category"],
                     "seen_at": time.strftime("%Y-%m-%d %H:%M",
                                              time.gmtime((r["processed_at"] or 0) + 8 * 3600)),
                     "snippet": _truncate(r["snippet"], 200)} for r in rows]
        return {"ok": True, "summary": "%d of %d indexed messages" % (len(messages), total),
                "result": {"total_matched": total, "returned": len(messages), "offset": offset,
                           "note": "Local index only (what the scanner has seen). Use search_mail for the full mailbox history.",
                           "messages": messages}}

    def _tool_search_mail(self, a):
        folder = (a.get("folder") or "INBOX").strip() or "INBOX"
        try:
            limit = max(1, min(int(a.get("limit") or 20), 50))
        except (TypeError, ValueError):
            limit = 20
        crit = []
        for key, imap_key in (("from_contains", "FROM"), ("subject_contains", "SUBJECT"),
                              ("body_contains", "BODY")):
            v = (a.get(key) or "").strip()
            if v:
                crit += [imap_key, '"%s"' % v.replace('"', " ")]
        for key, imap_key in (("since", "SINCE"), ("before", "BEFORE")):
            v = (a.get(key) or "").strip()
            if v:
                crit += [imap_key, _imap_date(v)]
        if _as_bool(a.get("unseen_only")):
            crit.append("UNSEEN")
        mc = self._mail()
        try:
            mc.ensure_selected(folder)
        except Exception as exc:
            return {"ok": False, "summary": "cannot open folder %r" % folder,
                    "result": {"error": repr(exc), "hint": "call list_folders for valid names"}}
        uids = mc.search(*(crit or ["ALL"]))
        total = len(uids)
        newest = uids[-limit:][::-1]
        messages = []
        for uid in newest:
            try:
                meta = mc.fetch_meta(uid)
            except Exception as exc:
                messages.append({"uid": uid, "error": repr(exc)})
                continue
            messages.append({"uid": uid, "folder": folder, "from": meta.get("from_addr"),
                             "subject": meta.get("subject"), "date": meta.get("date"),
                             "msgid": meta.get("msgid"),
                             "snippet": _truncate(meta.get("snippet"), 200)})
        return {"ok": True, "summary": "%d of %d in %s" % (len(messages), total, folder),
                "result": {"folder": folder, "total_matched": total, "returned": len(messages),
                           "note": "Newest first. Use folder + uid with read_message / move_message / flag_message.",
                           "messages": messages}}

    def _resolve_message(self, a):
        """→ (row|None, folder, uid, error|None). Accepts message_id or
        folder+uid; relocates by Message-ID when the uid is stale (moved mail)."""
        mid = a.get("message_id")
        row = None
        if mid not in (None, "", 0, "0", "null"):
            try:
                row = store.get_message(int(mid))
            except (TypeError, ValueError):
                row = None
            if row is None:
                return None, None, None, "no indexed message with id %r (search_messages lists ids)" % mid
            folder, uid = row["folder"], row["uid"]
        else:
            folder = (a.get("folder") or "").strip()
            uid = a.get("uid")
            if not folder or uid in (None, "", 0, "0"):
                return None, None, None, "identify the message by message_id, or by folder AND uid"
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                return None, None, None, "uid must be an integer"
        mc = self._mail()
        try:
            mc.ensure_selected(folder)
            present = bool(mc.search("UID", str(uid)))
        except Exception as exc:
            return row, folder, uid, "cannot open folder %r (%r)" % (folder, exc)
        if present:
            if row is None:
                row = store.find_message_by_uid(folder, uid)
            return row, folder, uid, None
        reloc = self._relocate(mc, row)
        if reloc:
            if row is None:
                row = store.find_message_by_uid(reloc[0], reloc[1])
            return row, reloc[0], reloc[1], None
        return row, folder, uid, ("message uid %s is not in %r — it may have been moved; "
                                  "find it again with search_mail" % (uid, folder))

    def _relocate(self, mc, row):
        msgid = (row or {}).get("msgid") or ""
        if not msgid:
            return None
        needle = '"<%s>"' % msgid.strip().strip("<>")
        for folder in sorted(mc.folders()):
            if row and folder == row.get("folder"):
                continue
            try:
                mc.ensure_selected(folder)
                hits = mc.search("HEADER", "Message-ID", needle)
            except Exception:
                continue
            if hits:
                store.log_event("debug", "assistant: relocated message %s to '%s' uid %s"
                                % (row["id"], folder, hits[-1]))
                return folder, hits[-1]
        return None

    def _tool_read_message(self, a):
        row, folder, uid, err = self._resolve_message(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        mc = self._mail()
        try:
            meta = mc.fetch_meta(uid)
            body = mc.fetch_body_text(uid, 10000)
        except Exception as exc:
            return {"ok": False, "summary": "could not read message: %r" % exc,
                    "result": {"error": repr(exc)}}
        if not body and row:
            body = row.get("snippet") or ""
        data = {"folder": folder, "uid": uid,
                "from": meta.get("from_addr"), "to": meta.get("to_addr"),
                "subject": meta.get("subject"), "date": meta.get("date"),
                "msgid": meta.get("msgid"), "body": _truncate(body, 8000),
                "indexed_id": row["id"] if row else None}
        return {"ok": True,
                "summary": "read %r in %s" % (_truncate(meta.get("subject") or "", 60), folder),
                "result": data}

    def _tool_move_message(self, a):
        target = (a.get("target_folder") or "").strip()
        if not target:
            return {"ok": False, "summary": "target_folder is required",
                    "result": {"error": "target_folder is required"}}
        row, folder, uid, err = self._resolve_message(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        if not self.actions_apply:
            store.log_event("info", "assistant (dry-run): would move %s uid %s → %s"
                            % (folder, uid, target))
            return {"ok": True, "dry_run": True,
                    "summary": "dry-run: would move uid %s from %s to %s" % (uid, folder, target),
                    "result": {"dry_run": True, "would_move": {"folder": folder, "uid": uid,
                                                               "to": target}}}
        mc = self._mail()
        try:
            mc.ensure_folder(target)
            mc.ensure_selected(folder)
            mc.move(uid, target)
        except Exception as exc:
            return {"ok": False, "summary": "move failed: %r" % exc, "result": {"error": repr(exc)}}
        if row:
            store.update_message(row["id"], status="assistant-moved", action_taken="move:" + target)
        store.log_event("info", "assistant moved %s uid %s ('%s') → %s"
                        % (folder, uid, _truncate((row or {}).get("subject") or "", 50), target))
        return {"ok": True, "summary": "moved to %s" % target,
                "result": {"moved": {"folder": folder, "uid": uid, "to": target}}}

    def _tool_flag_message(self, a):
        seen = _as_bool(a.get("seen"))
        flagged = _as_bool(a.get("flagged"))
        if seen is None and flagged is None:
            return {"ok": False, "summary": "set seen and/or flagged",
                    "result": {"error": "nothing to change"}}
        row, folder, uid, err = self._resolve_message(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        ops = []
        if seen is not None:
            ops.append(("+FLAGS" if seen else "-FLAGS", r"(\Seen)"))
        if flagged is not None:
            ops.append(("+FLAGS" if flagged else "-FLAGS", r"(\Flagged)"))
        if not self.actions_apply:
            return {"ok": True, "dry_run": True,
                    "summary": "dry-run: would update flags on uid %s in %s" % (uid, folder),
                    "result": {"dry_run": True}}
        mc = self._mail()
        try:
            mc.ensure_selected(folder)
            for op, fl in ops:
                mc.set_flags(uid, op, fl)
        except Exception as exc:
            return {"ok": False, "summary": "flag update failed: %r" % exc,
                    "result": {"error": repr(exc)}}
        store.log_event("info", "assistant set flags on %s uid %s (%s)" % (folder, uid, ops))
        return {"ok": True, "summary": "flags updated",
                "result": {"folder": folder, "uid": uid, "changes": [str(o) for o in ops]}}

    def _tool_create_folder(self, a):
        name = (a.get("name") or "").strip()
        if not name:
            return {"ok": False, "summary": "name is required", "result": {"error": "name required"}}
        mc = self._mail()
        existed = name in mc.folders()
        if not existed:
            mc.ensure_folder(name)
            store.log_event("info", "assistant created folder '%s'" % name)
        return {"ok": True,
                "summary": ("folder existed: " if existed else "folder created: ") + name,
                "result": {"folder": name, "created": not existed}}

    def _tool_list_folders(self, a):
        folders = self._folders_with_counts()
        return {"ok": True, "summary": "%d folders" % len(folders),
                "result": {"folders": folders}}

    def _tool_propose_rule(self, a):
        norm, errors = _validate_rule(a)
        if not norm:
            return {"ok": False, "summary": "rule invalid: " + "; ".join(errors[:3]),
                    "result": {"errors": errors,
                               "hint": "Every rule needs 1-4 conditions (field from/to/subject/body, "
                                       "op contains/equals/regex) and at least one action "
                                       "(move_to / mark_read / flag). Fix and propose again."}}
        self.proposals.append(norm)
        return {"ok": True, "summary": "rule proposed: %s" % norm["name"],
                "result": {"status": "queued for the user's one-click approval",
                           "proposal_index": len(self.proposals) - 1, "rule": norm}}

    # ---- the main loop

    def stream(self, user_text):
        """One assistant turn, as a generator of UI events."""
        user_text = (user_text or "").strip()
        if not user_text:
            yield {"type": "error", "message": "empty message"}
            return
        store.add_assistant_message("user", user_text[:4000])
        today = time.strftime("%Y-%m-%d (%a)", time.gmtime(time.time() + 8 * 3600))
        system = (ASSISTANT_SYSTEM % {"user": config.IMAP_USER, "today": today,
                                      "max_calls": self.MAX_CALLS_PER_TURN}
                  + "\n\n" + _assistant_context())
        convo = [{"role": m["role"], "content": m["content"]}
                 for m in store.assistant_messages(limit=24)]
        llm = LLMClient()
        reply_parts = []
        reasoning_all = []
        usage = None
        steps = 0
        tools_mode = True
        error = None
        try:
            while True:
                steps += 1
                use_tools = ASSISTANT_TOOLS if (tools_mode and steps <= self.MAX_STEPS) else None
                calls = []
                turn_reasoning = []
                turn_content = []
                try:
                    for ev in llm.chat_stream(system, convo, tools=use_tools, thinking=True):
                        if ev["type"] == "reasoning_delta":
                            turn_reasoning.append(ev["text"])
                            yield {"type": "reasoning", "text": ev["text"]}
                        elif ev["type"] == "content_delta":
                            turn_content.append(ev["text"])
                            yield {"type": "content", "text": ev["text"]}
                        elif ev["type"] == "tool_calls":
                            calls = ev["calls"]
                        elif ev["type"] == "turn_done":
                            usage = ev.get("usage") or usage
                except Exception as exc:
                    if use_tools and _looks_like_tools_unsupported(exc):
                        store.log_event("info", "assistant: the model rejected tools (%s) — "
                                        "continuing without them" % exc)
                        tools_mode = False
                        continue
                    raise
                reasoning_all.append("".join(turn_reasoning))
                if not calls or use_tools is None:
                    reply_parts = turn_content
                    break
                convo.append({"role": "assistant", "content": "".join(turn_content) or None,
                              "reasoning": "".join(turn_reasoning) or None,
                              "tool_calls": [{"id": c["id"], "type": "function",
                                              "function": {"name": c["name"], "arguments": c["arguments"]}}
                                             for c in calls]})
                for i, c in enumerate(calls):
                    if i >= self.MAX_CALLS_PER_TURN:
                        res = {"ok": False, "summary": "skipped: too many tool calls in one step",
                               "result": {"error": "per-step tool call limit reached; "
                                                   "ask again if it is still needed"}}
                    else:
                        args = _json_args(c["arguments"])
                        yield {"type": "tool_start", "id": c["id"], "name": c["name"], "args": args}
                        t0 = time.time()
                        res = self.call_tool(c["name"], args)
                        res["elapsed"] = round(time.time() - t0, 2)
                        self.tools_log.append({"name": c["name"],
                                               "args": _truncate(json.dumps(args, ensure_ascii=False), 300),
                                               "ok": bool(res.get("ok")),
                                               "summary": _truncate(res.get("summary") or "", 300),
                                               "dry_run": bool(res.get("dry_run")),
                                               "elapsed": res["elapsed"]})
                        yield {"type": "tool_end", "id": c["id"], "name": c["name"],
                               "ok": bool(res.get("ok")),
                               "summary": _truncate(res.get("summary") or "", 400),
                               "dry_run": bool(res.get("dry_run")), "elapsed": res["elapsed"]}
                    payload = json.dumps(res.get("result", {}), ensure_ascii=False)
                    cap = min(self.RESULT_CHARS, max(800, self._budget))
                    payload = _truncate(payload, cap)
                    self._budget -= len(payload)
                    convo.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"],
                                  "content": payload})
        except Exception as exc:
            error = exc
        finally:
            self.close()
        if error is not None:
            store.log_event("error", "assistant failed: %r" % error)
            yield {"type": "error", "message": repr(error)}
            return
        reply = "".join(reply_parts).strip()
        if not reply:
            reply = ("Proposed %d rule(s) — add them below, or ask for changes." % len(self.proposals)
                     if self.proposals else "(no reply)")
        meta = {"reasoning": _truncate("\n".join(r for r in reasoning_all if r), 20000),
                "tools": self.tools_log, "steps": steps, "usage": usage,
                "actions_live": self.actions_apply}
        msg_id = store.add_assistant_message("assistant", reply[:4000],
                                             proposals=json.dumps(self.proposals),
                                             meta=json.dumps(meta, ensure_ascii=False))
        if self.proposals:
            yield {"type": "proposals", "proposals": self.proposals}
        yield {"type": "done", "message_id": msg_id, "reply": reply, "steps": steps}


def assistant_respond(user_text):
    """Run one assistant turn to completion (no streaming). Returns (reply, proposals)."""
    agent = AssistantAgent()
    reply_parts, proposals, err = [], [], None
    for ev in agent.stream(user_text):
        if ev["type"] == "content":
            reply_parts.append(ev["text"])
        elif ev["type"] == "proposals":
            proposals = ev["proposals"]
        elif ev["type"] == "error":
            err = ev["message"]
    if err:
        raise RuntimeError(err)
    return "".join(reply_parts).strip() or "(no reply)", proposals
