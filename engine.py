"""Mail Triage engine: IMAP through the email-oauth2-proxy, rule matching,
LLM escalation, and the background worker."""
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import calendar
import base64
import email
import email.header
import email.parser
import email.policy
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
import heuristics
import proxy
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


_B64_RUN = re.compile(r"[A-Za-z0-9+/=]{16,}(?:[ \t]+[A-Za-z0-9+/=]{16,})*")


def _b64_decode_run(run_text):
    """Decode one base64 run (whitespace-separated tokens joined up); None when it
    does not decode to mostly-printable text."""
    chunk = "".join(run_text.split())
    if len(chunk) < 40:
        return None
    chunk += "=" * (-len(chunk) % 4)
    try:
        dec = base64.b64decode(chunk).decode("utf-8", "replace")
    except Exception:
        return None
    printable = sum(1 for c in dec if c.isprintable() or c in "\r\n\t")
    if dec.strip() and printable >= len(dec) * 0.8:
        return dec
    return None


def _decode_b64_blocks(text):
    """Decode base64 runs wherever they appear: canonical MIME lines (buffered
    across lines so short tail lines still join), runs that a whitespace collapse
    joined with spaces, or runs sitting after part headers. Runs that do not
    decode cleanly are kept unchanged."""
    out, buf = [], []

    def flush():
        if not buf:
            return
        dec = _b64_decode_run(" ".join(buf))
        if dec is not None:
            out.append(dec)
        else:
            out.extend(buf)
        buf.clear()

    for ln in text.split("\n"):
        s = ln.strip()
        if len(s) >= 16 and re.fullmatch(r"[A-Za-z0-9+/=]{16,}", s):
            buf.append(s)
        else:
            flush()
            out.append(_B64_RUN.sub(
                lambda m: _b64_decode_run(m.group(0)) or m.group(0), ln))
    flush()
    return "\n".join(out)


_BOUNDARY_TOKEN = re.compile(r"(?<!\S)-{2,}[=_A-Za-z0-9][\w=._-]{2,}")


def _strip_inline_mime_scaffold(text):
    """Remove MIME scaffolding left inline in salvaged text (boundary markers,
    Content-Type / Content-Transfer-Encoding fragments). Salvage paths only."""
    text = _BOUNDARY_TOKEN.sub(" ", text)
    text = re.sub(r"(?i)\bContent-Type\s*:\s*[^;\s]+(?:;\s*charset\s*=\s*\"?[\w.-]+\"?)?", " ", text)
    text = re.sub(r"(?i)\bContent-Transfer-Encoding\s*:\s*[\w-]+", " ", text)
    text = re.sub(r"(?i)\bcharset\s*=\s*\"?[\w.-]+\"?", " ", text)
    return text


def _clean_snippet(raw, limit=1500):
    """Turn up to a few KB of a raw message body into readable-ish text."""
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    junk_in = looks_like_mime_junk(text)
    # quoted-printable leftovers (common on O365 text parts) without part headers
    if len(re.findall(r"=[0-9A-Fa-f]{2}", text)) > 20:
        try:
            text = quopri.decodestring(text.encode("latin-1", "replace")).decode("utf-8", "replace")
        except Exception:
            pass
    text = _decode_b64_blocks(text)
    if junk_in:
        text = _strip_inline_mime_scaffold(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def readable_body(raw, limit=6000):
    """Readable body text KEEPING paragraph breaks (message viewer + salvage)."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    junk_in = looks_like_mime_junk(text)
    if len(re.findall(r"=[0-9A-Fa-f]{2}", text)) > 20:
        try:
            text = quopri.decodestring(text.encode("latin-1", "replace")).decode("utf-8", "replace")
        except Exception:
            pass
    text = _decode_b64_blocks(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    if junk_in:
        text = _strip_inline_mime_scaffold(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def looks_like_mime_junk(s):
    """True when stored text is raw MIME rather than a decoded body."""
    if not s:
        return True
    head = s[:800]
    if re.search(r"Content-(Type|Transfer-Encoding)\s*:", head):
        return True
    if re.match(r"\s*-{2,}[=_A-Za-z0-9]", s):
        return True
    if re.match(r"(Received|Return-Path|Delivered-To|Authentication-Results|DKIM-Signature)\s*:", head, re.I):
        return True
    if re.search(r"\S{80,}", s) and re.fullmatch(r"[\sA-Za-z0-9+/=]+", s or ""):
        return True
    return False


def looks_readable(s, probe=4000):
    """True when text is mostly printable - guards against binary salvage."""
    if not s:
        return False
    head = s[:probe]
    bad = sum(1 for c in head if not (c.isprintable() or c in "\r\n\t"))
    return bad <= max(3, len(head) * 0.10)


def _salvage_if_junk(s):
    return readable_body(s, limit=600) if looks_like_mime_junk(s) else (s or "")


def html_to_text(html_text):
    """Readable text from an HTML part (BeautifulSoup when available)."""
    if not html_text:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        text = soup.get_text("\n")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def parse_full_message(raw, limit=20000):
    """Parse a full RFC822 message into {"meta": ..., "text": ...} in one pass.

    Prefers text/plain parts; falls back to HTML -> text; skips attachments.
    """
    if not raw:
        return {"meta": {}, "text": ""}
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return {"meta": {}, "text": _clean_snippet(raw)}

    def hdr(name):
        v = msg.get(name)
        return str(v).strip() if v is not None else ""

    from_disp, from_addr = email.utils.parseaddr(hdr("From"))
    _, to_addr = email.utils.parseaddr(hdr("To"))
    meta = {
        "msgid": hdr("Message-ID").strip("<>"),
        "from_addr": from_addr or from_disp,
        "from_display": from_disp,
        "to_addr": to_addr,
        "subject": re.sub(r"\s+", " ", _decode_header(hdr("Subject"))).strip(),
        "date": hdr("Date"),
    }
    plain, html_parts = [], []
    try:
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition") or "").lower():
                continue
            if ctype in ("text/plain", "text/html"):
                try:
                    t = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        t = payload.decode("utf-8", "replace")
                    else:
                        t = str(payload or "")
                (plain if ctype == "text/plain" else html_parts).append(t or "")
    except Exception:
        pass
    text = "\n".join(plain).strip()
    if not text and html_parts:
        text = html_to_text("\n".join(html_parts))
    if not text:
        text = _clean_snippet(raw)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[:limit]
    return {"meta": meta, "text": text}


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

_FOLDER_LOCK = threading.Lock()  # serialises folder CREATE across worker threads


def imap_config():
    """Effective IMAP connection for the app.

    Embedded mode (default): mail is read through the in-app email-oauth2-proxy on
    127.0.0.1, with the account record from the Accounts page supplying the user, the
    local listener port and the local password. Anything else falls back to the
    external settings / container env (IMAP_HOST etc.), so pointing the app at a
    proxy (or server) elsewhere still works."""
    mode = (store.get_setting("proxy_mode") or "embedded").lower()
    user = (store.get_setting("imap_user") or config.IMAP_USER or "").strip()
    if mode == "embedded":
        acct = proxy.get_account(user or None)
        if acct is None:
            # single-mailbox app: if the configured user matches no account, use the first one
            acct = proxy.get_account(None)
        if acct and acct.get("password"):
            return {"host": "127.0.0.1", "port": int(acct.get("imap_local_port") or 1993),
                    "user": acct["email"], "password": acct["password"], "tls": False,
                    "mode": "embedded"}
    host = (store.get_setting("imap_host") or config.IMAP_HOST or "").strip()
    raw_port = store.get_setting("imap_port") or config.IMAP_PORT
    try:
        port = int(raw_port or 143)
    except (TypeError, ValueError):
        port = 143
    password = store.get_setting("imap_password") or config.IMAP_PASSWORD
    tls_setting = store.get_setting("imap_tls")
    if tls_setting in (None, ""):
        tls = bool(config.IMAP_TLS)
    else:
        tls = str(tls_setting).lower() in ("1", "true", "on", "yes")
    return {"host": host, "port": port, "user": user, "password": password, "tls": tls,
            "mode": "external"}


class MailClient:
    """Thin IMAP wrapper. Connects to the proxy with a PLAIN connection (the proxy
    performs OAuth 2.0 and secures the far side)."""

    def __init__(self):
        self.M = None
        self._folders = None
        self.selected = None

    def connect(self):
        cfg = imap_config()
        if cfg["tls"]:
            self.M = imaplib.IMAP4_SSL(cfg["host"], cfg["port"], timeout=30)
        else:
            self.M = imaplib.IMAP4(cfg["host"], cfg["port"], timeout=30)
        self.selected = None
        typ, dat = self.M.login(cfg["user"], cfg["password"])
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
        with _FOLDER_LOCK:  # CREATE races between classify worker threads
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
        # Full fetch + proper MIME walk (multipart mail decodes to real text;
        # the old BODY[TEXT] prefix stored raw part headers + base64 as "snippet").
        try:
            raw = self._fetch_literal(uid, "(BODY.PEEK[])")
            parsed = parse_full_message(raw, limit=4000)
            meta = parsed.get("meta") or {}
            if meta:
                meta = dict(meta)
                meta["snippet"] = parsed.get("text", "")
                return meta
        except RuntimeError:
            pass
        # fallback: headers + body prefix
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
            raw = self._fetch_literal(uid, "(BODY.PEEK[])")
        except RuntimeError:
            raw = b""
        if raw:
            text = parse_full_message(raw, limit=limit).get("text") or ""
            if text and not looks_like_mime_junk(text):
                return text
            return readable_body(raw, limit=limit)
        try:
            raw = self._fetch_literal(uid, "(BODY.PEEK[TEXT]<0.%d>)" % limit)
        except RuntimeError:
            raw = self._fetch_literal(uid, "(BODY.PEEK[TEXT])")[:limit]
        return readable_body(raw, limit=limit)

    def fetch_full(self, uid, limit=20000):
        """Full message in one round trip: header meta + cleaned text body."""
        raw = self._fetch_literal(uid, "(BODY.PEEK[])")
        return parse_full_message(raw, limit=limit)

    def set_flags(self, uid, op, flags):
        typ, dat = self.M.uid("STORE", str(uid), op, flags)
        if typ != "OK":
            raise RuntimeError("STORE failed: %s %s" % (typ, dat))

    @staticmethod
    def _copyuid_new(dat):
        """New UID from the server's COPYUID response (UIDPLUS), when present."""
        for item in dat or []:
            if not item:
                continue
            s = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
            m = re.search(r"\[COPYUID\s+\d+\s+\S+\s+(\d+)\]", s)
            if m:
                return int(m.group(1))
        return None

    def move(self, uid, folder):
        """Move a message and return its UID in the destination folder when the
        server reports one (UIDPLUS COPYUID), else None."""
        typ, dat = self.M.uid("MOVE", str(uid), '"%s"' % folder)
        if typ == "OK":
            return self._copyuid_new(dat)
        typ, dat = self.M.uid("COPY", str(uid), '"%s"' % folder)
        if typ != "OK":
            raise RuntimeError("COPY %s failed: %s %s" % (folder, typ, dat))
        new_uid = self._copyuid_new(dat)
        self.M.uid("STORE", str(uid), "+FLAGS", r"(\Deleted)")
        self.M.expunge()
        return new_uid

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
            if val and len(val) <= 3 and re.fullmatch(r"[A-Za-z0-9]+", val):
                # short tokens match whole words: "PO" won't match "support"
                ok = re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(val),
                               hay_raw, re.I) is not None
            else:
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


def is_guard_rule(rule):
    """A guard rule keeps matching mail in place and stops further rules."""
    try:
        acts = json.loads(rule.get("actions") or "{}")
    except (TypeError, ValueError):
        acts = {}
    return (not acts) or bool(acts.get("keep"))


def match_first(rules, fields):
    for rule in rules:
        if rule_matches(rule, fields):
            return rule
    return None


# ---------------------------------------------------------------- LLM

def llm_config():
    """Effective LLM endpoint settings: values set in the UI (SQLite) win, blank
    fields fall back to the container env (LLM_BASE_URL etc.). Returns
    {base, key, model, thinking, timeout, fallback: {base, key, model} | None}."""
    def pick(name, env):
        v = store.get_setting(name)
        return v if v not in (None, "") else env

    cfg = {
        "base": (pick("llm_base_url", config.LLM_BASE_URL) or "").rstrip("/"),
        "key": pick("llm_api_key", config.LLM_API_KEY) or "",
        "model": pick("llm_model", config.LLM_MODEL) or "",
        "thinking": store.get_setting("llm_thinking") or "auto",
        "timeout": int(store.get_setting("llm_timeout") or config.LLM_TIMEOUT),
        "fallback": None,
    }
    fb_base = pick("llm_fallback_base_url", config.LLM_FALLBACK_BASE_URL)
    if fb_base:
        cfg["fallback"] = {
            "base": fb_base.rstrip("/"),
            "key": pick("llm_fallback_api_key", config.LLM_FALLBACK_API_KEY) or "",
            "model": pick("llm_fallback_model", config.LLM_FALLBACK_MODEL) or cfg["model"],
        }
    return cfg


class LLMClient:
    def __init__(self):
        c = llm_config()
        self.base = c["base"]
        self.key = c["key"]
        self.model = c["model"]
        self.thinking = c["thinking"]
        self.timeout = c["timeout"]
        self.fallback = None
        if c["fallback"]:
            fb = c["fallback"]
            self.fallback = (fb["base"], fb["key"], fb["model"])

    def _chat(self, system, user, json_mode=True, history=None, max_tokens=None,
              full=False, thinking=False):
        convo = list(history or []) + [{"role": "user", "content": user}]
        return self._chat_convo(system, convo, json_mode=json_mode, max_tokens=max_tokens,
                                full=full, thinking=thinking)

    def _chat_convo(self, system, convo, json_mode=True, max_tokens=None, full=False,
                    thinking=False):
        try:
            return self._chat_once(self.base, self.key, self.model, system, convo,
                                   json_mode, max_tokens, full, thinking)
        except Exception as primary_exc:
            if not self.fallback:
                raise
            fb_base, fb_key, fb_model = self.fallback
            try:
                out = self._chat_once(fb_base, fb_key, fb_model, system, convo,
                                      json_mode, max_tokens, full, thinking)
            except Exception:
                raise primary_exc
            store.log_event("info", "LLM: primary '%s' failed (%s) - served by fallback '%s'"
                            % (self.model, type(primary_exc).__name__, fb_model))
            return out

    def _chat_once(self, base, key, model, system, convo, json_mode=True,
                   max_tokens=None, full=False, thinking=False, timeout=None):
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}] + convo,
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        # Providers differ on the optional fields: response_format (JSON mode) and
        # chat_template_kwargs (the vLLM thinking extension). Send them, and strip
        # one at a time on a 4xx so any OpenAI-compatible endpoint works.
        optional = []
        if thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": True}
            optional.append("chat_template_kwargs")
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
            optional.append("response_format")
        headers = {"Authorization": "Bearer " + key} if key else {}
        r = None
        while True:
            r = requests.post(base + "/chat/completions", json=payload, headers=headers,
                              timeout=timeout or self.timeout)
            if r.status_code in (400, 404, 422) and optional:
                payload.pop(optional.pop(0), None)
                continue
            break
        if r.status_code in (401, 403):
            raise RuntimeError("LLM HTTP %s from %s - check the API key "
                               "(Settings -> LLM endpoint)" % (r.status_code, base))
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]
        message = choice["message"]
        if full:
            message = dict(message)
            message["_finish"] = choice.get("finish_reason")
            return message
        return message["content"]

    # ---- streaming (used by the assistant agent) ----

    def chat_stream(self, system, messages, tools=None, thinking=True):
        """Stream one chat turn against the primary endpoint, yielding event dicts:
        reasoning_delta / content_delta / tool_calls / turn_done. When the primary
        fails before producing any output, the fallback endpoint serves instead."""
        send_thinking = bool(thinking) and self.thinking != "off"
        attempts = [(self.base, self.key, self.model, "primary '%s'" % self.model)]
        if self.fallback:
            attempts.append((*self.fallback, "fallback '%s'" % self.fallback[2]))
        for i, (base, key, model, label) in enumerate(attempts):
            produced = False
            try:
                for ev in self._stream_once(base, key, model, system, messages, tools,
                                            send_thinking):
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
            # which the reasoning parser surfaces as delta.reasoning. Dropped
            # automatically below if the endpoint rejects unknown fields.
            payload["chat_template_kwargs"] = {"enable_thinking": True}
        headers = {"Authorization": "Bearer " + key} if key else {}
        r = None
        while True:
            r = requests.post(base + "/chat/completions", json=payload,
                              headers=headers, timeout=self.timeout, stream=True)
            if r.status_code in (400, 404, 422) and "chat_template_kwargs" in payload:
                payload.pop("chat_template_kwargs", None)
                r.close()
                continue
            break
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
                  "\"confidence\": 0.0-1.0, \"summary\": \"one short sentence saying what the email is\", "
                  "\"reason\": \"why that category, max 15 words\"}" % (my_name or "the user", cats))
        body_text = msg.get("snippet") or ""
        if looks_like_mime_junk(body_text):
            body_text = readable_body(body_text, limit=1500)
        user = ("From: %s\nTo: %s\nSubject: %s\nDate: %s\n\n%s"
                % (msg.get("from_addr", ""), msg.get("to_addr", ""), msg.get("subject", ""),
                   msg.get("date", ""), body_text[:1500]))
        # Thinking is ON for classification (user's call): the reasoning streams in
        # `message.reasoning` (a separate channel from content, so JSON mode still
        # holds). max_tokens must cover reasoning + content: 1500 truncated long
        # thinking runs (empty content), so 4096 with a single 8192 retry when the
        # finish reason says "length".
        message = self._chat(system, user, json_mode=True, max_tokens=4096,
                             full=True, thinking=(self.thinking != "off"))
        content = (message.get("content") or "") if isinstance(message, dict) else (message or "")
        m = re.search(r"\{.*\}", content, re.S)
        if (not m and isinstance(message, dict)
                and message.get("_finish") == "length"):
            message = self._chat(system, user, json_mode=True, max_tokens=8192,
                                 full=True, thinking=(self.thinking != "off"))
            content = (message.get("content") or "") if isinstance(message, dict) else (message or "")
            m = re.search(r"\{.*\}", content, re.S)
        thinking = ""
        if isinstance(message, dict):
            thinking = message.get("reasoning") or message.get("reasoning_content") or ""
        if not m:
            raise RuntimeError("LLM returned no JSON: %r" % (content or "")[:200])
        result = json.loads(m.group(0))
        if not isinstance(result, dict) or not result.get("category"):
            raise RuntimeError("LLM JSON missing category: %r" % result)
        if thinking:
            result["_thinking"] = str(thinking)[:6000]
        return result

    def draft_reply(self, msg, body_text, template, settings):
        my_name = settings.get("my_name", "Sean")
        system = ("You write email replies as %s (%s). Be concise, warm and professional. "
                  "Output ONLY the plain-text reply body (no subject line, no headers, no quotes)."
                  % (my_name, imap_config()["user"]))
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
        try:
            # embedded proxy may still be booting; wait for its listener so the first
            # cycle does not race it after a container restart
            if (store.get_setting("proxy_mode") or "embedded").lower() == "embedded":
                proxy.wait_ready(20)
        except Exception:
            pass
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
        try:
            if store.get_setting("heuristic_autorefine", True):
                for _hid, hname, delta in heuristics.auto_refine():
                    store.log_event("info", "heuristic %r retrained (+%d new label(s))" % (hname, delta))
        except Exception as exc:
            store.log_event("error", "heuristic auto-refine failed: %r" % exc)


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
            mv_fields = {}
            apply = bool(settings.get("rules_apply", True))
            status = "matched-dry"
            if apply:
                try:
                    if actions.get("move_to"):
                        mc.ensure_folder(actions["move_to"])
                        new_uid = mc.move(uid, actions["move_to"])
                        taken.append("move:" + actions["move_to"])
                        mv_fields = {"folder": actions["move_to"]}
                        if new_uid:
                            mv_fields["uid"] = new_uid
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
                                 action_taken=",".join(taken), **mv_fields)
            store.log_event("info", "rule '%s' → %s | %s (%s)"
                            % (rule.get("name") or rule["id"],
                               ", ".join(taken) or ("kept (guard)" if is_guard_rule(rule) else "suggest"),
                               (meta.get("subject") or "")[:60], meta.get("from_addr")))
        else:
            store.update_message(row["id"], status="queued")
    if uids:
        store.log_event("debug", "%s: scanned %d new message(s)" % (folder, scanned))
    return scanned, moved


def _header_index(folder):
    """Message-ID -> uid map for one folder (single ranged header fetch)."""
    mc = MailClient().connect()
    try:
        mc.select(folder)
        typ, dat = mc.M.uid("FETCH", "1:*", "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        out = {}
        for item in dat or []:
            if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
                continue
            pre = item[0].decode("utf-8", "replace") if isinstance(item[0], bytes) else str(item[0])
            mu = re.search(r"UID (\d+)", pre)
            mm = re.search(rb"Message-ID:\s*(<[^>]{4,400}>)", item[1], re.I | re.S)
            if mu and mm:
                mid = mm.group(1).decode("utf-8", "replace").strip().strip("<>")
                if mid:
                    out.setdefault(mid, int(mu.group(1)))
        return out
    finally:
        try:
            mc.close()
        except Exception:
            pass


def _run_parallel(items, fn, workers):
    """Split items into up to `workers` chunks; fn(chunk) runs on one thread each."""
    n = max(1, min(workers, len(items) or 1))
    chunks = [[] for _ in range(n)]
    for i, item in enumerate(items):
        chunks[i % n].append(item)
    threads = [threading.Thread(target=fn, args=(c,), daemon=True) for c in chunks if c]
    for th in threads:
        th.start()
    for th in threads:
        th.join()


def rescue_stale_snippets(rows, workers=6):
    """Rows whose stored folder/uid went stale: build a Message-ID -> location
    index (one ranged header fetch per folder, parallel) and refetch the bodies
    from wherever the messages actually live. Returns {"fixed", "not_found"}."""
    pend = {}
    for r in rows:
        mid = (r.get("msgid") or "").strip().strip("<>").strip()
        if mid:
            pend.setdefault(mid, []).append(r)
    if not pend:
        return {"fixed": 0, "not_found": len(rows)}
    mc = MailClient().connect()
    try:
        folders = list(mc.folders())
    finally:
        try:
            mc.close()
        except Exception:
            pass
    index, lock = {}, threading.Lock()

    def index_chunk(chunk):
        for folder in chunk:
            try:
                found = _header_index(folder)
            except Exception:
                continue
            with lock:
                for mid, uid in found.items():
                    if mid not in index:
                        index[mid] = (folder, uid)

    _run_parallel(folders, index_chunk, workers)

    jobs, not_found = {}, 0
    for mid, rs in pend.items():
        loc = index.get(mid)
        if not loc:
            not_found += len(rs)
            continue
        jobs.setdefault(loc, []).extend(rs)
    groups = sorted(jobs.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    fixed, flock = [0], threading.Lock()

    def fetch_chunk(chunk):
        cur_folder, conn = None, None
        for (folder, uid), rs in chunk:
            if folder != cur_folder:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn, cur_folder = None, folder
                try:
                    conn = MailClient().connect()
                    conn.select(folder)
                except Exception:
                    conn = None
                    continue
            try:
                text = conn.fetch_body_text(uid, limit=6000)
            except Exception:
                text = ""
            if text and not looks_like_mime_junk(text) and looks_readable(text):
                for r in rs:
                    store.update_message(r["id"], snippet=text[:4000], folder=folder, uid=uid)
                    with flock:
                        fixed[0] += 1
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    _run_parallel(groups, fetch_chunk, workers)
    return {"fixed": fixed[0], "not_found": not_found}


def heal_snippets(workers=6, rescue=True):
    """One-shot maintenance: refetch + decode raw-MIME snippet rows (legacy data).
    Phase 1 refetches per stored folder/uid (SELECT once per folder per worker);
    phase 2 re-locates the failures by Message-ID across folders and refetches.
    Returns {"fixed", "skipped", "remaining", "not_found"}."""
    def junk_rows():
        return [r for r in store.messages(limit=999999)
                if looks_like_mime_junk(r.get("snippet") or "")]

    rows = junk_rows()
    if not rows:
        return {"fixed": 0, "skipped": 0, "remaining": 0, "not_found": 0}
    by_folder = {}
    for r in rows:
        by_folder.setdefault(r["folder"], []).append(r)
    fixed, skipped, stuck, lock = [0], [0], [], threading.Lock()

    def heal_chunk(chunk):
        for folder, items in chunk:
            try:
                mc = MailClient().connect()
            except Exception as exc:
                store.log_event("error", "heal: connect failed: %r" % exc)
                with lock:
                    skipped[0] += len(items)
                    stuck.extend(items)
                continue
            try:
                try:
                    mc.select(folder)
                except Exception:
                    with lock:
                        skipped[0] += len(items)
                        stuck.extend(items)
                    continue
                for r in items:
                    try:
                        text = mc.fetch_body_text(r["uid"], limit=6000)
                    except Exception:
                        text = ""
                    if text and not looks_like_mime_junk(text) and looks_readable(text):
                        store.update_message(r["id"], snippet=text[:4000])
                        with lock:
                            fixed[0] += 1
                    else:
                        with lock:
                            skipped[0] += 1
                            stuck.append(r)
            finally:
                try:
                    mc.close()
                except Exception:
                    pass

    groups = sorted(by_folder.items(), key=lambda kv: -len(kv[1]))
    _run_parallel(groups, heal_chunk, workers)

    res_fixed, res_nf = 0, 0
    if rescue and stuck:
        store.log_event("info", "heal: %d row(s) going through the Message-ID rescue" % len(stuck))
        r2 = rescue_stale_snippets(stuck, workers=workers)
        res_fixed, res_nf = r2["fixed"], r2["not_found"]
    remaining = len(junk_rows())
    store.log_event("info", "heal: %d repaired (%d refetch + %d rescue), %d not found, %d remaining"
                    % (fixed[0] + res_fixed, fixed[0], res_fixed, res_nf, remaining))
    return {"fixed": fixed[0] + res_fixed, "skipped": max(0, skipped[0] - res_fixed),
            "remaining": remaining, "not_found": res_nf}



def classify_and_store(msg, settings, mc=None):
    """Classify one message, persist the result, file it when llm_apply is on.

    Order: trained heuristic classifiers first (deterministic, no LLM call for a
    confident verdict); the LLM only sees what the heuristics abstain on.

    Returns the classification dict with '_moved_to' set when it was filed.
    Raises on LLM failure. Reused by the worker queue and the manual batch job.
    """
    hres = heuristics.classify(msg) if settings.get("heuristics_enabled", True) else None
    if hres:
        res = {"category": hres["category"], "confidence": hres["confidence"],
               "summary": "", "reason": hres["reason"], "needs_reply": False,
               "_heuristic_id": hres["heuristic_id"],
               "_heuristic_name": hres["heuristic_name"]}
    else:
        res = LLMClient().classify(msg, settings.get("categories") or [],
                                   settings.get("my_name", ""))
    category = str(res.get("category", ""))
    try:
        conf = float(res.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    folder = (settings.get("category_folders") or {}).get(category, "")
    already_filed = str(msg.get("action_taken") or "").startswith("move")
    fields = {
        "llm_category": category,
        "llm_confidence": conf,
        "llm_summary": str(res.get("summary", ""))[:200],
        "llm_reason": str(res.get("reason", ""))[:200],
        "llm_thinking": str(res.get("_thinking") or "")[:6000],
        "llm_needs_reply": 1 if res.get("needs_reply") else 0,
        "llm_suggested_folder": folder,
        "classified_by": ("heuristic:%s %s" % (hres["heuristic_id"], hres["heuristic_name"])) if hres else "llm",
        "status": "classified",
    }
    res["_moved_to"] = ""
    guard = None
    if settings.get("llm_apply") and folder and not already_filed:
        # guard rules protect mail from ALL filing, including this category map
        g_fields = {"from": msg.get("from_addr", ""), "to": msg.get("to_addr", ""),
                    "subject": msg.get("subject", ""), "body": msg.get("snippet", "")}
        for r in store.list_rules(enabled_only=True):
            if is_guard_rule(r) and rule_matches(r, g_fields):
                guard = r.get("name") or ("rule %s" % r.get("id"))
                break
    if guard:
        store.log_event("info", "LLM: kept '%s' in place (guard rule '%s')"
                        % ((msg.get("subject") or "")[:50], guard))
    if settings.get("llm_apply") and folder and not already_filed and not guard:
        own = mc is None
        try:
            if own:
                mc = MailClient().connect()
            mc.ensure_selected(msg["folder"])
            mc.ensure_folder(folder)
            new_uid = mc.move(msg["uid"], folder)
            fields["status"] = "llm-moved"
            fields["action_taken"] = "move:" + folder
            fields["folder"] = folder
            if new_uid:
                fields["uid"] = new_uid
            res["_moved_to"] = folder
        except Exception as exc:
            store.log_event("error", "LLM move to %s failed: %r" % (folder, exc))
        finally:
            if own and mc is not None:
                try:
                    mc.close()
                except Exception:
                    pass
    store.update_message(msg["id"], **fields)
    return res


def _process_llm_queue(mc, settings, batch):
    done = 0
    for msg in store.queued_messages(batch):
        try:
            res = classify_and_store(msg, settings, mc=mc)
            if not res.get("_heuristic_id"):
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
        store.log_event("info", "LLM: '%s' → %s (%.0f%%) %s"
                        % ((msg.get("subject") or "")[:50], res.get("category"),
                           (float(res.get("confidence") or 0)) * 100,
                           ("moved to %s" % res["_moved_to"]) if res.get("_moved_to")
                           else "(suggestion only)"))
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
        return {"ok": True, "user": imap_config()["user"], "folders": len(folders),
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
    raw = build_draft_message(msg, body_text, imap_config()["user"])
    mc = MailClient().connect()
    try:
        folder = store.get_setting("drafts_folder") or mc.find_special_use("\\Drafts") or "Drafts"
        mc.append_draft(folder, raw)
    finally:
        mc.close()
    store.log_event("info", "draft saved for '%s'" % (msg.get("subject") or "")[:60])
    return folder


# ---------------------------------------------------------------- manual classification job

class ClassifyJob(threading.Thread):
    """Manual batch classification ("Classify selected" / "Classify all unclassified").

    Runs on demand (explicit user action, so no hourly cap), newest mail first,
    with progress in `state` for the UI. Failures follow the worker's park rules
    (3 strikes -> status 'error')."""

    def __init__(self):
        super().__init__(daemon=True, name="triage-classifier")
        self.lock = threading.Lock()
        self.force = threading.Event()
        self.stop_flag = threading.Event()
        self.queue = []
        self._skip = set()
        self._tls = threading.local()   # per-pool-thread IMAP connection
        self._conns = []
        self.state = {"running": False, "done": 0, "failed": 0, "total": 0,
                      "current": "", "last_error": None, "started": 0}

    def trigger(self, ids=None):
        with self.lock:
            self.queue = [int(i) for i in (ids or [])]
        self.stop_flag.clear()
        self.force.set()

    def request_stop(self):
        self.stop_flag.set()

    def run(self):
        store.init_db()
        while True:
            self.force.wait(1)
            if not self.force.is_set():
                continue
            self.force.clear()
            try:
                self._run_job()
            except Exception as exc:  # keep the thread alive no matter what
                self.state["last_error"] = repr(exc)
                self.state["running"] = False
                store.log_event("error", "classify job crashed: %r" % exc)

    def _mail(self):
        """One IMAP connection per pool thread (imaplib is not thread-safe)."""
        mc = getattr(self._tls, "mc", None)
        if mc is None:
            mc = MailClient().connect()
            self._tls.mc = mc
            with self.lock:
                self._conns.append(mc)
        return mc

    def _classify_one(self, msg, settings):
        res = classify_and_store(msg, settings, mc=self._mail())
        if not res.get("_heuristic_id"):
            store.add_llm_log(msg["id"], True)
        return res

    def _run_job(self):
        with self.lock:
            ids = list(self.queue)
            self.queue = []
        self._skip = set()
        settings = store.all_settings()
        concurrency = max(1, min(16, int(settings.get("classify_concurrency") or 8)))
        done = failed = 0
        self.state.update({"running": True, "done": 0, "failed": 0, "concurrency": concurrency,
                           "started": int(time.time()), "last_error": None, "current": ""})
        self.state["total"] = len(ids) if ids else store.unclassified_count()
        try:
            pending = None
            if ids:
                pending = [m for m in (store.get_message(i) for i in ids) if m]
            exhausted = False

            def next_msg():
                if pending is not None:
                    return pending.pop(0) if pending else None
                return store.unclassified_next(skip=self._skip)

            with ThreadPoolExecutor(max_workers=concurrency,
                                    thread_name_prefix="classify") as pool:
                inflight = {}
                while inflight or not exhausted:
                    while (not exhausted and not self.stop_flag.is_set()
                           and len(inflight) < concurrency):
                        msg = next_msg()
                        if msg is None:
                            exhausted = True
                            break
                        self._skip.add(msg["id"])  # claimed: in flight counts as skip
                        self.state["current"] = "%s%s" % (
                            (msg.get("subject") or "")[:56],
                            (" (+%d more)" % len(inflight)) if inflight else "")
                        inflight[pool.submit(self._classify_one, msg, settings)] = msg
                    if not inflight:
                        break
                    done_set, _ = wait(list(inflight), timeout=0.5,
                                       return_when=FIRST_COMPLETED)
                    for fut in done_set:
                        msg = inflight.pop(fut)
                        try:
                            res = fut.result()
                            done += 1
                            store.log_event("info", "classify: '%s' → %s%s"
                                            % ((msg.get("subject") or "")[:50], res.get("category"),
                                               (" (moved to %s)" % res["_moved_to"]) if res.get("_moved_to") else ""))
                        except Exception as exc:
                            failed += 1
                            store.add_llm_log(msg["id"], False, repr(exc))
                            if store.llm_fail_count(msg["id"]) >= 3:
                                store.update_message(msg["id"], status="error")
                                store.log_event("error", "classify: '%s' parked after repeated failures"
                                                % (msg.get("subject") or "")[:50])
                            else:
                                store.log_event("error", "classify: '%s' failed (retry later): %r"
                                                % ((msg.get("subject") or "")[:50], exc))
                    self.state["done"] = done
                    self.state["failed"] = failed
            if self.stop_flag.is_set():
                store.log_event("info", "classify: stopped after %d message(s)" % done)
            else:
                store.log_event("info", "classify: finished - %d classified, %d failed"
                                % (done, failed))
        finally:
            with self.lock:
                conns, self._conns = self._conns, []
            for mc in conns:
                try:
                    mc.close()
                except Exception:
                    pass
            self.state["running"] = False
            self.state["current"] = ""


# ---------------------------------------------------------------- learn rules from tags

LEARN_SYSTEM = """You are the rule architect for "Mail Triage". The user has manually tagged a set of emails with their own labels. Infer filter rules that would sort matching mail the same way, without duplicating rules that already exist.

Reply with ONE JSON object and nothing else:
{"reply": "one short sentence for the user",
 "proposed_rules": [
   {"name": "short rule name",
    "match_mode": "all" or "any",
    "conditions": [{"field": "from|to|subject|body", "op": "contains|equals|regex", "value": "..."}],
    "actions": {"move_to": "Folder name", "mark_read": true, "flag": true},
    "placement": "top" or "bottom",
    "rationale": "one line"}]}

Rules about rules:
- 1-3 conditions each; prefer distinctive substrings (an address fragment, a subject keyword).
- Every rule needs at least one condition; a rule with NO actions is a GUARD rule: matching mail stays where it is and nothing else can move it. Use guards (with "placement": "top") to protect mail the user wants kept, e.g. "never move X".
- Learn generalisable patterns from the tags (senders, domains, subject words): do not hardcode single message ids.
- Max 5 proposed rules; [] when the examples are too inconsistent.
- Prefer "move_to" a folder whose name matches the tag or the closest existing folder."""


def propose_rules_from_tags():
    """Derive proposed rules from the user's manual tags. Returns (reply, [rules])."""
    tagged = store.tagged_examples(80)
    if not tagged:
        raise RuntimeError("no tagged messages yet - tag some mail on the Messages page first")
    lines = []
    for i, t in enumerate(tagged, 1):
        lines.append("%d. tag=%s | from=%s | subject=%s"
                     % (i, t["user_tag"], (t["from_addr"] or "")[:70],
                        (t["subject"] or "")[:90]))
    context = ("EXISTING RULES (do not duplicate):\n%s\n\n"
               "CATEGORIES: %s\n\n"
               "TAGGED EXAMPLES (the user's manual labels):\n%s"
               % (_rules_to_text(store.list_rules()),
                  ", ".join(store.get_setting("categories") or []),
                  "\n".join(lines)))
    content = LLMClient()._chat(LEARN_SYSTEM + "\n\n" + context,
                                "Propose rules matching my tagging.", json_mode=True)
    obj = {}
    m = re.search(r"\{.*\}", content or "", re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except (TypeError, ValueError):
            obj = {}
    reply = str(obj.get("reply") or "").strip()[:400]
    rules = []
    existing = store.list_rules()
    for item in (obj.get("proposed_rules") or [])[:5]:
        norm = normalize_rule(item)
        if norm:
            sim, reasons = rule_similarity(norm, existing)
            if sim:
                norm["similar_rule"] = _rule_brief(sim)
                norm["similar_reasons"] = reasons
            rules.append(norm)
    return reply, rules

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
- Ground every answer with tools instead of guessing. semantic_search finds mail by MEANING across every indexed folder and years of history (paraphrases welcome) — use it first for content questions ("what did the landlord want", "the trip itinerary email"). search_messages reads the app's local index; search_mail runs a live IMAP search for exact tokens or folders outside the index. For any question about the user's mail, search first.
- Reference specific messages in your answers as [msg:ID] (the message_id from tool results); the UI turns those into links. Use read_message for the full text of anything you quote.
- You may act directly on what the user asks for: create_folder, move_message, flag_message. Moving never deletes mail. For ongoing sorting, propose a rule with propose_rule instead (the user approves proposals with one click).
- You cannot send mail, reply to mail, or delete mail; never claim that you did.
- Keep searches bounded: small limits, use since/before for windows. Summarize results; never dump raw rows.
- When you read a message, never paste long verbatim quotes into the answer: give the gist in your own words, keep only short key phrases (prices, dates, rules), and cite the message as [msg:ID].
- The user can also tag mail by hand on the Messages page. If they ask you to learn rules from their tags, call list_tagged first and base propose_rule calls on the tag-to-pattern evidence.
- To PROTECT mail from being moved (any "never move X" / "keep X in the inbox" request): propose a rule with conditions only and NO actions - that is a guard rule. Guards must sit at the top, so set placement="top". A guard also stops LLM category filing for matching mail.
- BEFORE proposing a rule, call list_rules (or mailbox_overview) and check what already exists. If a similar rule exists (same sender/domain/subject), propose an UPDATE instead of a near-duplicate: pass updates_rule_id with the rule as it should look afterwards (name/conditions/actions). If you propose something that overlaps an existing rule without updates_rule_id, the app flags it to the user, so handle it yourself first.
- Heuristic classifiers (train_classifier / list_classifiers / manage_classifier / evaluate_classifier): deterministic trained models that run BEFORE the LLM in triage. Suggest them when the user wants less LLM dependence, when a category has regular labelled mail (tags), or when classification feels inconsistent. decision_list suits sender/keyword patterns, naive_bayes fuzzier ones; retrain via retrain_id as labels grow; evaluate before claiming quality. After a tagging session, suggest training one when a category has around 8+ tagged examples.
- Only tell the user a rule was proposed once propose_rule has returned ok:true in this turn; never claim a proposal you did not actually make.
- Condition values of 3 characters or fewer (letters/digits) match whole words: a value "PO" will not match "support" or "report".
- At most %(max_calls)d tool calls per step. Stop as soon as you can answer or act.

Today is %(today)s (Hong Kong time). Reply in the user's language, as plain text (no markdown tables), concise and friendly.

Output discipline: everything you write outside the tool interface is shown to the user as your answer. Write only the final answer — never narrate your process, never restate tool output or think out loud ("The user wants…", "I will provide…", "Let me count…", "The tool shows…"). The user already sees every tool call as a card; report conclusions, not your reading of the payload. Verify numbers and lists in the thinking channel, then answer once, cleanly."""


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
    _fn("semantic_search",
        "Semantic search over the ENTIRE indexed mail archive (all folders, years of history). It understands meaning and paraphrases: 'the tax refund email' can find messages that never use those words. Use it FIRST for content questions; results carry message_id for read_message and are cited in answers as [msg:ID].",
        {"query": {"type": "string", "description": "natural-language description of what to find"},
         "limit": {"type": "integer", "description": "max results (default 8, max 20)"},
         "folder": {"type": "string", "description": "restrict to one folder (optional)"},
         "since": {"type": "string", "description": "YYYY-MM-DD; only messages from this date on (optional)"}}),
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
    _fn("list_tagged",
        "List the messages the user has manually tagged with their own labels (Messages page -> select rows -> Tag). Use when they ask to learn from their manual tagging; then turn the patterns into propose_rule calls.",
        {"limit": {"type": "integer", "description": "max rows (default 40, max 100)"}}),
    _fn("propose_rule",
        "Propose a filter rule for the user to approve with one click. Approved rules sort matching mail automatically (top to bottom, first match wins). A rule with NO actions is a GUARD: matching mail stays put and nothing else (later rules, LLM filing) can move it.",
        {"name": {"type": "string", "description": "short rule name"},
         "match_mode": {"type": "string", "enum": ["all", "any"]},
         "conditions": {"type": "array", "description": "1-4 conditions", "items": {
             "type": "object",
             "properties": {
                 "field": {"type": "string", "enum": ["from", "to", "subject", "body"]},
                 "op": {"type": "string", "enum": ["contains", "equals", "regex"]},
                 "value": {"type": "string"}}}},
         "actions": {"type": "object", "description": "what the rule does; OMIT (or {\"keep\": true}) for a guard rule that keeps matching mail in place and stops further rules",
                     "properties": {"move_to": {"type": "string"},
                                    "mark_read": {"type": "boolean"},
                                    "flag": {"type": "boolean"},
                                    "keep": {"type": "boolean"}}},
         "placement": {"type": "string", "enum": ["top", "bottom"],
                       "description": "where the rule lands in the list; use \"top\" for guard/protection rules so they catch mail before other rules act (default bottom)"},
         "updates_rule_id": {"type": "integer",
                             "description": "id of an EXISTING rule this proposal changes (from list_rules). Set it when the user asks to change/adjust a rule instead of creating a near-duplicate; the proposal then replaces that rule's name/conditions/actions"},
         "rationale": {"type": "string", "description": "one line for the user"}},
        ("name", "conditions")),
    _fn("list_rules",
        "List the user's current filter rules in order: id, name, enabled, conditions, actions. Call this BEFORE proposing a rule so you can update an existing rule rather than stacking a near-duplicate.",
        {}),
    _fn("train_classifier",
        "Train (or retrain) a deterministic heuristic classifier for a category, from the user's labels. Heuristics run BEFORE the LLM in the triage pipeline: a confident verdict is applied without any LLM call, so trained categories stop depending on the non-deterministic model and cannot be steered by text inside emails (prompt injection). Prefer this when a category has regular labelled mail, when the user wants less LLM dependence, or after a tagging session.",
        {"name": {"type": "string", "description": "short name for the classifier"},
         "kind": {"type": "string", "enum": ["decision_list", "naive_bayes"],
                  "description": "decision_list = interpretable learned conditions (sender/keyword patterns); naive_bayes = fuzzier token patterns"},
         "category": {"type": "string", "description": "category this classifier outputs (match the app categories)"},
         "source": {"type": "string", "enum": ["tags", "classified"],
                    "description": "labels from the user's manual tags (best) or existing classified mail (weak). Default tags"},
         "min_confidence": {"type": "number", "description": "minimum probability to accept a verdict without the LLM (default 0.8)"},
         "params": {"type": "object",
                    "description": "decision_list: {min_precision, min_support}; naive_bayes: {max_vocab, min_df}",
                    "properties": {}},
         "retrain_id": {"type": "integer", "description": "existing classifier id to retrain with the latest labels"},
         "enable": {"type": "boolean", "description": "enable after training (default true)"}},
        ("kind", "category")),
    _fn("list_classifiers",
        "List the heuristic classifiers: id, name, kind, category, enabled, samples, label source, and what they match on.",
        {}),
    _fn("manage_classifier",
        "Enable, disable or delete a heuristic classifier.",
        {"id": {"type": "integer", "description": "classifier id"},
         "action": {"type": "string", "enum": ["enable", "disable", "delete"]}},
        ("id", "action")),
    _fn("evaluate_classifier",
        "Evaluate a heuristic classifier against the current labelled examples: accuracy plus example mistakes. Check before telling the user a classifier is good.",
        {"id": {"type": "integer", "description": "classifier id"},
         "limit": {"type": "integer", "description": "max examples to check (default 500)"}},
        ("id",)),
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
        if not parts:
            parts.append("KEEP IN PLACE (guard — stops further rules)")
        state = "" if r.get("enabled") else " [disabled]"
        lines.append("%d. %s: %s => %s%s"
                     % (i, r.get("name") or "rule", cs, ", ".join(parts), state))
    return "\n".join(lines)


def _cond_norm(c):
    return ((c.get("field") or "subject").lower(),
            (c.get("op") or "contains").lower(),
            re.sub(r"\s+", " ", (c.get("value") or "").strip().lower()))


def rule_similarity(new_rule, rules):
    """Find the existing rule most overlapping a proposed one.

    Requires at least one condition pair matching exactly (same field+op+value)
    or two looser overlaps; returns (rule_dict, reasons) or (None, [])."""
    n_conds = new_rule.get("conditions") or []
    best, best_score, best_reasons = None, 0, []
    for r in rules:
        try:
            r_conds = json.loads(r.get("conditions") or "[]")
        except (TypeError, ValueError):
            continue
        score, reasons = 0, []
        for a in n_conds:
            af, ao, av = _cond_norm(a)
            if not av:
                continue
            for b in r_conds:
                bf, bo, bv = _cond_norm(b)
                if (af, ao) != (bf, bo) or not bv:
                    continue
                if av == bv:
                    score += 2
                    reasons.append('%s %s "%s"' % (af, ao, a.get("value")))
                elif av in bv or bv in av:
                    if min(len(av), len(bv)) >= 4:
                        score += 1
                        reasons.append('%s %s "%s" overlaps "%s"' % (af, ao, a.get("value"), b.get("value")))
        if score > best_score:
            best, best_score, best_reasons = r, score, reasons
    if best and best_score >= 1:
        return best, best_reasons[:3]
    return None, []


def _rule_brief(r):
    """UI/prompt-friendly snapshot of a rule."""
    return {"id": r.get("id"), "name": r.get("name") or ("rule %s" % r.get("id")),
            "enabled": bool(r.get("enabled")), "match_mode": r.get("match_mode") or "all",
            "conditions": _safe_json(r.get("conditions"), []),
            "actions": _safe_json(r.get("actions"), {})}


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
        if a.get("keep"):
            actions["keep"] = True
    if not conditions:
        errors.append("at least one valid condition is required")
    # No actions = a GUARD rule: matching mail is kept in place and no further
    # rule (or LLM filing) touches it. This is how "never move X" is expressed.
    if errors:
        return None, errors
    out = {"name": name, "match_mode": mode, "conditions": conditions, "actions": actions,
           "rationale": str(proposal.get("rationale") or "").strip()[:300]}
    placement = str(proposal.get("placement") or "").lower()
    if placement in ("top", "bottom"):
        out["placement"] = placement
    try:
        upd_id = proposal.get("updates_rule_id")
        if upd_id not in (None, "", 0):
            out["updates_rule_id"] = int(upd_id)
    except (TypeError, ValueError):
        pass
    return out, []


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
            "rules": [{"id": r["id"], "name": r["name"], "enabled": bool(r["enabled"])} for r in rules],
            "rules_text": _rules_to_text(rules),
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
                     "snippet": _truncate(_salvage_if_junk(r["snippet"]), 200)} for r in rows]
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

    def _tool_semantic_search(self, a):
        import rag
        q = (a.get("query") or "").strip()
        if not q:
            return {"ok": False, "summary": "query is required",
                    "result": {"error": "query is required"}}
        try:
            limit = max(1, min(int(a.get("limit") or 8), 20))
        except (TypeError, ValueError):
            limit = 8
        res = rag.search(q, k=limit,
                         folder=(a.get("folder") or "").strip() or None,
                         since=(a.get("since") or "").strip() or None)
        if not res.get("ok"):
            return {"ok": False, "summary": res.get("error") or "search failed",
                    "result": {"error": res.get("error")}}
        note = (res.get("meta") or {}).get("note") or ""
        items = [{"message_id": r["message_id"], "folder": r["folder"], "from": r["from_addr"],
                  "subject": r["subject"], "date": r["date"], "excerpt": r["excerpt"]}
                 for r in res["results"]]
        summary = "%d semantic match(es): %s" % (
            len(items), "; ".join((i["subject"] or "")[:40] for i in items[:3]))
        if note:
            summary += " [degraded: %s]" % note
        return {"ok": True, "summary": summary,
                "result": {"query": q, "count": len(items),
                           "note": ("Cite as [msg:ID]; use read_message with message_id for full text."
                                    + (" Partial channels: %s" % note if note else "")),
                           "results": items}}

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
            new_uid = mc.move(uid, target)
        except Exception as exc:
            return {"ok": False, "summary": "move failed: %r" % exc, "result": {"error": repr(exc)}}
        if row:
            mv = {"status": "assistant-moved", "action_taken": "move:" + target, "folder": target}
            if new_uid:
                mv["uid"] = new_uid
            store.update_message(row["id"], **mv)
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

    def _tool_list_tagged(self, a):
        try:
            limit = max(1, min(int(a.get("limit") or 40), 100))
        except (TypeError, ValueError):
            limit = 40
        rows = store.tagged_examples(limit)
        items = [{"message_id": r["id"], "from": r["from_addr"], "subject": r["subject"],
                  "date": r["date"], "tag": r["user_tag"],
                  "snippet": _truncate(_salvage_if_junk(r.get("snippet")), 120)} for r in rows]
        return {"ok": True, "summary": "%d tagged example(s)" % len(items),
                "result": {"count": len(items),
                           "note": "These are the user's manual labels; propose rules that reproduce them.",
                           "tagged": items}}

    def _tool_propose_rule(self, a):
        norm, errors = _validate_rule(a)
        if not norm:
            return {"ok": False, "summary": "rule invalid: " + "; ".join(errors[:3]),
                    "result": {"errors": errors,
                               "hint": "Every rule needs 1-4 conditions (field from/to/subject/body, "
                                       "op contains/equals/regex). Actions are optional: a rule with "
                                       "no actions is a GUARD that keeps matching mail in place. "
                                       "Fix and propose again."}}
        target_id = a.get("updates_rule_id")
        if target_id not in (None, "", 0):
            try:
                target_id = int(target_id)
            except (TypeError, ValueError):
                target_id = None
        similar = None
        if target_id:
            target = store.get_rule(target_id)
            if target is None:
                return {"ok": False, "summary": "no rule #%s to update" % target_id,
                        "result": {"errors": ["updates_rule_id %s does not exist" % target_id],
                                   "hint": "Call list_rules to see the current rule ids."}}
            norm["updates_rule_id"] = target_id
            norm["updates_rule"] = _rule_brief(target)
        else:
            similar, reasons = rule_similarity(norm, store.list_rules())
            if similar:
                norm["similar_rule"] = _rule_brief(similar)
                norm["similar_reasons"] = reasons
        # a re-proposal of the same name replaces the earlier one (never stacks)
        self.proposals = [p for p in self.proposals
                          if (p.get("name") or "").lower() != (norm.get("name") or "").lower()]
        self.proposals.append(norm)
        result = {"status": "queued for the user's one-click approval",
                  "proposal_index": len(self.proposals) - 1, "rule": norm}
        if norm.get("updates_rule"):
            summary = ("rule proposed as an UPDATE of #%s '%s'"
                       % (target_id, norm["updates_rule"]["name"]))
        else:
            summary = "rule proposed: %s" % norm["name"]
            if similar:
                summary += " (similar to existing rule #%s)" % similar.get("id")
                result["hint"] = ("An existing rule #%s '%s' already matches similar conditions. If this "
                                  "proposal is meant to CHANGE that rule, call propose_rule again with "
                                  "updates_rule_id=%s and the rule as it should look afterwards; otherwise "
                                  "keep it as a separate rule."
                                  % (similar.get("id"), similar.get("name") or "", similar.get("id")))
        return {"ok": True, "summary": summary, "result": result}

    def _tool_list_rules(self, a):
        rules = store.list_rules()
        items = []
        for r in rules:
            b = _rule_brief(r)
            b["position"] = r.get("position")
            items.append(b)
        return {"ok": True,
                "summary": "%d rule(s) (top to bottom, first match wins)" % len(items),
                "result": {"rules": items, "text": _rules_to_text(rules)}}

    def _tool_train_classifier(self, a):
        kind = str(a.get("kind") or "").strip()
        category = str(a.get("category") or "").strip()
        if kind not in heuristics.kind_names():
            return {"ok": False, "summary": "unknown kind %r" % kind,
                    "result": {"error": "kind must be one of: %s" % ", ".join(heuristics.kind_names())}}
        if not category:
            return {"ok": False, "summary": "category is required",
                    "result": {"error": "category is required"}}
        source = a.get("source") if a.get("source") in ("tags", "classified") else "tags"
        params = a.get("params") if isinstance(a.get("params"), dict) else {}
        try:
            min_conf = float(a.get("min_confidence") or 0.8)
        except (TypeError, ValueError):
            min_conf = 0.8
        enable = bool(a.get("enable", True))
        row = None
        retrain_id = a.get("retrain_id")
        if retrain_id:
            row = store.get_heuristic(int(retrain_id)) if str(retrain_id).isdigit() else None
            if not row:
                return {"ok": False, "summary": "no classifier #%s" % retrain_id,
                        "result": {"error": "classifier id not found"}}
        try:
            model, stats = heuristics.train_heuristic(
                kind, category, source=source, params=params, min_confidence=min_conf,
                created_by="assistant",
                exclude=heuristics.heuristic_excluded(row) if row else None)
        except Exception as exc:
            return {"ok": False, "summary": str(exc)[:200], "result": {"error": str(exc)}}
        if row:
            store.update_heuristic(row["id"], kind=kind, category=category,
                                   model=json.dumps(model), stats=json.dumps(stats),
                                   min_confidence=min_conf, enabled=1 if enable else 0,
                                   name=(a.get("name") or row["name"] or ""))
            hid = row["id"]
            verb = "retrained"
        else:
            hid = store.add_heuristic(a.get("name") or ("%s classifier" % category), kind, category,
                                      model=json.dumps(model), stats=json.dumps(stats),
                                      min_confidence=min_conf, enabled=enable, created_by="assistant")
            verb = "trained"
        view = heuristics.view(store.get_heuristic(hid) or {})
        weak = " (labels auto-tagged by the LLM - review the dataset to prune failures)" \
            if stats.get("weak_labels") else ""
        store.log_event("info", "classifier #%d '%s' %s (by assistant, %d sample(s), %d negative)"
                        % (hid, view.get("name") or "", "retrained" if row else "trained",
                           stats.get("trained_label_count") or 0, stats.get("negatives") or 0))
        return {"ok": True,
                "summary": "classifier #%d %r %s on %d example(s) (%d negative)%s - %s"
                           % (hid, view.get("name"), verb, stats.get("trained_label_count") or 0,
                              stats.get("negatives") or 0, weak,
                              (view.get("description") or "")[:110]),
                "result": {"classifier": view,
                           "note": "runs before the LLM on new mail; the user can review and prune "
                                   "samples on the classifier's dataset page (/classifiers/<id>/dataset)"}}

    def _tool_list_classifiers(self, a):
        rows = [heuristics.view(h) for h in store.list_heuristics()]
        return {"ok": True, "summary": "%d classifier(s)" % len(rows),
                "result": {"classifiers": rows,
                           "kinds_available": heuristics.kind_names()}}

    def _tool_manage_classifier(self, a):
        try:
            hid = int(a.get("id") or 0)
        except (TypeError, ValueError):
            hid = 0
        action = str(a.get("action") or "").strip()
        row = store.get_heuristic(hid) if hid else None
        if not row:
            return {"ok": False, "summary": "no classifier #%s" % a.get("id"),
                    "result": {"error": "classifier id not found"}}
        if action == "delete":
            store.delete_heuristic(hid)
            store.log_event("info", "classifier #%d '%s' deleted (by assistant)" % (hid, row.get("name") or ""))
        elif action == "disable":
            store.update_heuristic(hid, enabled=0)
        elif action == "enable":
            store.update_heuristic(hid, enabled=1)
        else:
            return {"ok": False, "summary": "unknown action %r" % action,
                    "result": {"error": "action must be enable / disable / delete"}}
        return {"ok": True, "summary": "classifier #%d %r %sd" % (hid, row.get("name"), action.rstrip("e")),
                "result": {"id": hid, "action": action}}

    def _tool_evaluate_classifier(self, a):
        try:
            hid = int(a.get("id") or 0)
        except (TypeError, ValueError):
            hid = 0
        row = store.get_heuristic(hid) if hid else None
        if not row:
            return {"ok": False, "summary": "no classifier #%s" % a.get("id"),
                    "result": {"error": "classifier id not found"}}
        try:
            limit = int(a.get("limit") or 500)
        except (TypeError, ValueError):
            limit = 500
        out = heuristics.evaluate_heuristic(row, limit=limit)
        acc = out.get("accuracy")
        return {"ok": True,
                "summary": "classifier #%d %r: %s accuracy on %d example(s)"
                           % (hid, row.get("name"), ("%.0f%%" % (acc * 100)) if acc is not None else "n/a",
                              out.get("total") or 0),
                "result": out}

    # ---- the main loop

    def stream(self, user_text):
        """One assistant turn, as a generator of UI events."""
        user_text = (user_text or "").strip()
        if not user_text:
            yield {"type": "error", "message": "empty message"}
            return
        store.add_assistant_message("user", user_text[:4000])
        today = time.strftime("%Y-%m-%d (%a)", time.gmtime(time.time() + 8 * 3600))
        system = (ASSISTANT_SYSTEM % {"user": imap_config()["user"], "today": today,
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
                if turn_content:
                    yield {"type": "content_break"}
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
        reasoning_text = "\n".join(r for r in reasoning_all if r)
        thought_summary = self._summarize_thoughts(reasoning_text) if reasoning_text else ""
        meta = {"reasoning": _truncate(reasoning_text, 20000),
                "reasoning_summary": thought_summary,
                "tools": self.tools_log, "steps": steps, "usage": usage,
                "actions_live": self.actions_apply}
        msg_id = store.add_assistant_message("assistant", reply[:4000],
                                             proposals=json.dumps(self.proposals),
                                             meta=json.dumps(meta, ensure_ascii=False))
        if self.proposals:
            yield {"type": "proposals", "proposals": self.proposals}
        yield {"type": "done", "message_id": msg_id, "reply": reply, "steps": steps}
        if thought_summary:
            # after done so the final answer never waits on the summary call
            yield {"type": "thought_summary", "text": thought_summary}

    def _summarize_thoughts(self, reasoning_text):
        """One short sentence describing what the reasoning was about (best-effort)."""
        text = (reasoning_text or "").strip()
        if not text:
            return ""
        try:
            out = LLMClient()._chat(
                "You condense an AI assistant's private reasoning into ONE short sentence "
                "(max 12 words) describing what it was working on, written for the user - "
                "e.g. \"Checked the rule list and the Promo dataset\". Plain text only, "
                "no quotes, no preamble, no trailing period.",
                text[:6000], json_mode=False, max_tokens=60)
            out = re.sub(r"\s+", " ", (out or "").strip().strip('"')).strip(" .")
            if 3 <= len(out) <= 200:
                return out[:140]
        except Exception:
            pass
        # fallback: first sentence of the thinking
        first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        return re.sub(r"\s+", " ", first).strip().strip(" .")[:140]


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
