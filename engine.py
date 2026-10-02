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
import math
import nh3
import quopri
import re
import threading
import time

import requests

import config
import heuristics
import learning
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


_EMAIL_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "cite", "code",
    "dd", "div", "dl", "dt", "em", "figcaption", "figure", "font", "h1", "h2",
    "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "mark", "ol",
    "p", "pre", "q", "s", "small", "span", "strike", "strong", "sub", "sup",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul", "var", "wbr",
}
_EMAIL_ATTRS = {
    "a": {"href", "title", "name"},
    "img": {"src", "alt", "title", "width", "height", "border"},
    "font": {"color", "face", "size"},
    "table": {"border", "cellpadding", "cellspacing", "width", "height", "align",
              "bgcolor", "dir", "role"},
    "td": {"colspan", "rowspan", "width", "height", "align", "valign", "bgcolor", "dir"},
    "th": {"colspan", "rowspan", "width", "height", "align", "valign", "bgcolor", "dir"},
    "tr": {"align", "valign", "bgcolor", "dir"},
    "p": {"align", "dir"},
    "*": {"style", "class", "title", "dir", "lang", "id", "width", "height", "align", "valign"},
}


def sanitize_email_html(html):
    """Sanitize untrusted email HTML for the viewer (nh3/ammonia). Keeps layout,
    formatting, tables and cid:/http(s) image refs; strips scripts, stylesheets,
    forms, frames and event handlers."""
    if not html:
        return ""
    try:
        return nh3.clean(
            html,
            tags=_EMAIL_TAGS,
            attributes=_EMAIL_ATTRS,
            url_schemes={"http", "https", "mailto", "tel", "cid"},
            clean_content_tags={"script", "style", "iframe", "object", "embed",
                                "form", "head", "title", "meta", "link", "base"})
    except Exception:
        return ""


def _walk_sections(part, section=""):
    """Yield (imap_section, leaf_part) in depth-first order; sections follow the
    IMAP part-numbering scheme ('1', '2', '2.1', ...)."""
    if part.is_multipart():
        for i, sub in enumerate(part.get_payload() or [], 1):
            sub_sec = ("%s.%d" % (section, i)) if section else str(i)
            for item in _walk_sections(sub, sub_sec):
                yield item
    else:
        yield (section or "1"), part


def _sniff_image_type(data):
    """Magic-number fallback when a part carries no usable Content-Type."""
    if not data:
        return ""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    head = data[:512].lstrip()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        return "image/svg+xml"
    return ""


def image_url_ok(url):
    """http(s) only, and every resolved address must be public (SSRF guard)."""
    try:
        from urllib.parse import urlparse
        import socket
        import ipaddress
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = socket.getaddrinfo(p.hostname, port)
        if not infos:
            return False
        for info in infos:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        return True
    except Exception:
        return False


def parse_full_message(raw, limit=20000):
    """Parse a full RFC822 message into {"meta": ..., "text": ...} in one pass.

    Prefers text/plain parts; falls back to HTML -> text; skips attachments.
    """
    if not raw:
        return {"meta": {}, "text": "", "html": "", "cids": {}}
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return {"meta": {}, "text": _clean_snippet(raw), "html": "", "cids": {}}

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
    plain, html_parts, cids = [], [], {}
    try:
        for sec, part in _walk_sections(msg):
            ctype = part.get_content_type()
            cid = str(part.get("Content-ID") or "").strip().strip("<>")
            if cid and ctype.startswith("image/"):
                try:
                    cids[cid] = {"section": sec, "type": ctype,
                                 "name": _decode_header(str(part.get_filename() or ""))}
                except Exception:
                    pass
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
    html = max(html_parts, key=len).strip() if html_parts else ""
    if not text and html_parts:
        text = html_to_text("\n".join(html_parts))
    if not text:
        text = _clean_snippet(raw)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[:limit]
    if len(html) > 400000:
        html = html[:400000]
    return {"meta": meta, "text": text, "html": html, "cids": cids}


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def render_template_text(template_body, msg):
    fields = _SafeDict(
        sender=msg.get("from_addr", ""),
        subject=msg.get("subject", ""),
        date=msg.get("date", ""),
        my_name=store.get_setting("my_name", ""),
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
                meta["body_html_at"] = int(time.time())
                if parsed.get("html"):
                    meta["body_html"] = sanitize_email_html(parsed["html"])
                    meta["body_cids"] = json.dumps(parsed.get("cids") or {})
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

    def fetch_body_payload(self, uid, limit=6000):
        """Viewer payload in one round trip: text + sanitized html + cid map."""
        raw = self._fetch_literal(uid, "(BODY.PEEK[])")
        parsed = parse_full_message(raw, limit=limit)
        return {"text": parsed.get("text") or "",
                "html": sanitize_email_html(parsed.get("html") or ""),
                "cids": parsed.get("cids") or {}}

    def fetch_section(self, uid, section):
        """(content_type, bytes) for one MIME part ('2', '2.1', ...) - fetched as
        two small literals: the part's MIME headers plus its content."""
        typ, dat = self.M.uid("FETCH", str(uid),
                              "(BODY.PEEK[%s.MIME] BODY.PEEK[%s])" % (section, section))
        if typ != "OK":
            raise RuntimeError("FETCH %s failed: %s %s" % (section, typ, dat))
        chunks = [item[1] for item in (dat or [])
                  if isinstance(item, tuple) and len(item) >= 2 and item[1]]
        if not chunks:
            raise RuntimeError("no data for section %s" % section)
        mime_raw, data = (chunks[0], chunks[-1]) if len(chunks) >= 2 else (b"", chunks[-1])
        ct, cte = "", ""
        if mime_raw:
            try:
                hdr = email.parser.BytesHeaderParser().parsebytes(mime_raw)
                ct = str(hdr.get("Content-Type") or "")
                cte = str(hdr.get("Content-Transfer-Encoding") or "").strip().lower()
            except Exception:
                ct, cte = "", ""
        # BODY[section] returns the part AS STORED - undo the transfer encoding
        if cte == "base64":
            try:
                data = base64.b64decode(re.sub(rb"\s+", b"", data))
            except Exception:
                pass
        elif cte == "quoted-printable":
            try:
                data = quopri.decodestring(data)
            except Exception:
                pass
        if not ct or ct.lower().startswith("application/octet-stream"):
            ct = _sniff_image_type(data) or ct or "application/octet-stream"
        return ct.split(";")[0].strip(), data

    def fetch_full(self, uid, limit=20000):
        """Full message in one round trip: header meta + cleaned text body."""
        raw = self._fetch_literal(uid, "(BODY.PEEK[])")
        return parse_full_message(raw, limit=limit)

    def set_flags(self, uid, op, flags):
        typ, dat = self.M.uid("STORE", str(uid), op, flags)
        if typ != "OK":
            raise RuntimeError("STORE failed: %s %s" % (typ, dat))

    def _copyuid_new(self, dat=None):
        """New UID from the server's UIDPLUS [COPYUID ...] response, when present.

        imaplib does NOT put a tagged ``OK [COPYUID u s d]`` code into the
        command's dat (that returns [None]); it parks the bracketed code in the
        untagged-response store, where ``M.response`` reads AND clears it. Check
        both places so every server/imaplib shape is covered."""
        items = []
        try:
            resp = self.M.response("COPYUID")
            if resp and resp[1]:
                items.extend(resp[1])
        except Exception:
            pass
        items.extend(dat or [])
        for item in items:
            if not item:
                continue
            s = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
            m = re.search(r"\[COPYUID\s+\d+\s+\S+\s+(\d+)\]", s)
            if m:
                return int(m.group(1))
            # the untagged store keeps the bare code content: "<uidvalidity> <src> <dst>"
            m = re.match(r"\s*(\d+)\s+(\S+)\s+(\S+)\s*$", s)
            if m:
                dst = m.group(3).split(":")[0]
                if dst.isdigit():
                    return int(dst)
        return None

    def _locate_uid(self, folder, msgid):
        """Destination UID by Message-ID lookup (fallback for servers that do
        not report COPYUID)."""
        needle = '"<%s>"' % (msgid or "").strip().strip("<>")
        if not msgid or needle == '"<>"':
            return None
        try:
            self.ensure_selected(folder)
            hits = self.search("HEADER", "Message-ID", needle)
        except Exception:
            return None
        return hits[-1] if hits else None

    def move(self, uid, folder, msgid=None):
        """Move a message and return its UID in the destination folder (UIDPLUS
        COPYUID; falls back to a Message-ID lookup in the destination when the
        server does not report one and msgid is given), else None."""
        try:
            self.M.response("COPYUID")  # drop any stale value from earlier commands
        except Exception:
            pass
        typ, dat = self.M.uid("MOVE", str(uid), '"%s"' % folder)
        if typ == "OK":
            new_uid = self._copyuid_new(dat)
            if new_uid is None and msgid:
                new_uid = self._locate_uid(folder, msgid)
            return new_uid
        typ, dat = self.M.uid("COPY", str(uid), '"%s"' % folder)
        if typ != "OK":
            raise RuntimeError("COPY %s failed: %s %s" % (folder, typ, dat))
        new_uid = self._copyuid_new(dat)
        self.M.uid("STORE", str(uid), "+FLAGS", r"(\Deleted)")
        self.M.expunge()
        if new_uid is None and msgid:
            new_uid = self._locate_uid(folder, msgid)
        return new_uid

    def append_message(self, folder, raw, flags=r"(\Draft)"):
        typ, dat = self.M.append('"%s"' % folder, flags,
                                 imaplib.Time2Internaldate(time.time()), raw)
        if typ != "OK":
            raise RuntimeError("APPEND to %s failed: %s %s" % (folder, typ, dat))

    def append_draft(self, folder, raw):
        self.append_message(folder, raw, r"(\Draft)")


# ---------------------------------------------------------------- rules

def _cond_field(c, fields):
    """One deterministic {field, op, value} condition against the mail fields."""
    field = (c.get("field") or "subject").lower()
    op = (c.get("op") or "contains").lower()
    val = c.get("value") or ""
    hay_raw = fields.get(field) or ""
    hay = hay_raw.lower()
    if op == "contains":
        if val and len(val) <= 3 and re.fullmatch(r"[A-Za-z0-9]+", val):
            # short tokens match whole words: "PO" won't match "support"
            return re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(val),
                             hay_raw, re.I) is not None
        return bool(val) and val.lower() in hay
    if op == "equals":
        return hay.strip() == val.strip().lower()
    if op == "regex":
        try:
            return re.search(val, hay_raw, re.I) is not None
        except re.error:
            return False
    if op == "plugin":
        pid = str(c.get("plugin") or "").strip()
        if not pid:
            return False
        try:
            import plugins as _plugins
            import plugin_rt
            row = _plugins.get(pid)
            if not row or not row.get("enabled"):
                return False
            if pid not in (store.get_setting("plugin_matchers", []) or []):
                return False
            out = plugin_rt.matcher(pid, {
                "fields": {str(k): ("" if v is None else str(v)) for k, v in (fields or {}).items()},
                "text": " ".join("" if v is None else str(v)
                                 for v in (fields or {}).values())[:4000]})
            return bool(out)
        except Exception:
            return False
    return False


def rule_matches(rule, fields):
    try:
        conds = json.loads(rule.get("conditions") or "[]")
    except (TypeError, ValueError):
        return False
    if not conds:
        return False
    results = [_cond_field(c, fields) for c in conds]
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


# ---------------------------------------------------------------- flows

# ---- fuzzy flow conditions (AI category / about-topic) ----
# Mature flow builders (n8n text classifier, Shortwave AI filters) route on the
# MEANING of text, not just exact fields: a model classifies, then the flow runs.
# We do the same in two layers:
#   kind "category" - matches the app's classifier verdict (heuristic or LLM),
#                     evaluated right after classification (no extra LLM call).
#   kind "topic"    - cosine(message, description) >= threshold via the embed
#                     endpoint, evaluated at scan time; deterministic conditions
#                     are checked first so obvious mail skips the AI cost.
_TOPIC_QUERY_CACHE = {}
_TOPIC_MIN_DEFAULT = 0.45
# Qwen3-Embedding-style models score retrieval queries better when the query is
# wrapped in an instruction; calibrated on a real mailbox where relevant matches
# land ~0.45-0.55 and noise p90 sits ~0.36-0.41, so 0.45 is the useful default.
_TOPIC_INSTR = ("Instruct: Given a description of the kind of email the user wants "
                "to find, retrieve matching inbox messages\nQuery: ")
_TOPIC_FAIL_TS = [0.0]


def _needs_verdict(flow):
    """True when a flow has an AI-category condition (checked after classification)."""
    try:
        conds = json.loads(flow.get("conditions") or "[]")
    except (TypeError, ValueError):
        return False
    return any((c.get("kind") or "field").lower() == "category" for c in conds)


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    return (num / (da * db)) if da and db else 0.0


def _topic_vector(text, kind):
    """Cached embedding for a topic description (query) / message text (document)."""
    key = (kind, text)
    hit = _TOPIC_QUERY_CACHE.get(key)
    if hit is not None:
        return hit
    import rag  # local import: rag pulls store/config; keep module load order stable
    vec = rag.embed_one(text, kind=kind)
    if len(_TOPIC_QUERY_CACHE) > 300:
        _TOPIC_QUERY_CACHE.clear()
    _TOPIC_QUERY_CACHE[key] = vec
    return vec


def _topic_matches(c, ctx):
    """'is about' condition: cosine(message text, description) >= threshold."""
    desc = (c.get("value") or "").strip()
    if not desc or not ctx or not (ctx.get("text") or "").strip():
        return False
    try:
        need = float(c.get("threshold") or _TOPIC_MIN_DEFAULT)
    except (TypeError, ValueError):
        need = _TOPIC_MIN_DEFAULT
    text = ctx["text"][:2000]
    try:
        if ctx.get("_doc_vec") is None:
            ctx["_doc_vec"] = _topic_vector(text, "document")
        score = _cosine(_topic_vector(_TOPIC_INSTR + desc, "query"), ctx["_doc_vec"])
    except Exception as exc:
        ctx["_topic_failed"] = True
        now = time.time()
        if now - _TOPIC_FAIL_TS[0] > 300:
            _TOPIC_FAIL_TS[0] = now
            store.log_event("error", "flow topic condition: embedding failed: %r "
                            "(check the Search index endpoint settings)" % exc)
        return False
    ctx["last_topic_score"] = score
    ctx["last_topic_desc"] = desc
    return score >= need


def _cond_matches(c, fields, ctx):
    """One condition of any kind. Fuzzy kinds need ctx (verdict / text)."""
    kind = (c.get("kind") or "field").lower()
    if kind == "category":
        v = (ctx or {}).get("verdict")
        if not v:
            return False
        want = str(c.get("value") or "").strip().lower()
        got = str(v.get("category") or "").strip().lower()
        if want and want != got:
            return False
        try:
            need = float(c.get("min_confidence") or 0)
        except (TypeError, ValueError):
            need = 0.0
        return float(v.get("confidence") or 0) >= need
    if kind == "topic":
        return _topic_matches(c, ctx or {})
    return _cond_field(c, fields)


def flow_matches(flow, fields, ctx=None):
    """WHEN conditions for a flow, including the fuzzy kinds.

    Deterministic conditions evaluate first (free); a failing deterministic
    condition short-circuits in "all" mode before any embedding work."""
    try:
        conds = json.loads(flow.get("conditions") or "[]")
    except (TypeError, ValueError):
        return False
    if not conds:
        return False
    mode = (flow.get("match_mode") or "all").lower()
    cheap = [c for c in conds if (c.get("kind") or "field").lower() == "field"]
    fuzzy = [c for c in conds if (c.get("kind") or "field").lower() != "field"]
    if mode != "any":
        if not all(_cond_field(c, fields) for c in cheap):
            return False
        return all(_cond_matches(c, fields, ctx) for c in fuzzy)
    if any(_cond_field(c, fields) for c in cheap):
        return True
    return any(_cond_matches(c, fields, ctx) for c in fuzzy)


def match_first_flows(flows, fields, ctx=None):
    for flow in flows:
        if flow_matches(flow, fields, ctx):
            return flow
    return None


def _apply_flow(mc, flow, row, settings, live):
    """Execute one flow's steps in order. Returns (taken, fields). Move steps
    update the DB row immediately so later steps (and drafts) address the
    message's new location."""
    try:
        steps = json.loads(flow.get("actions") or "[]")
    except (TypeError, ValueError):
        steps = []
    taken, fields = [], {}
    cur_folder, cur_uid = row["folder"], row["uid"]
    for st in steps:
        t = (st.get("type") or "").lower()
        if t == "move" and (st.get("folder") or "").strip():
            dest = st["folder"].strip()
            if live:
                try:
                    store.record_move(row, dest, "flow", from_folder=cur_folder)
                    mc.ensure_folder(dest)
                    mc.ensure_selected(cur_folder)
                    new_uid = mc.move(cur_uid, dest, msgid=row.get("msgid"))
                    cur_folder = dest
                    if new_uid:
                        cur_uid = new_uid
                    fields["folder"], fields["uid"] = cur_folder, cur_uid
                    store.update_message(row["id"], folder=cur_folder, uid=cur_uid)
                    taken.append("move:" + dest)
                    _emit_plugin_event("mail.filed", {
                        "id": row.get("id"), "msgid": row.get("msgid") or "",
                        "from_folder": row.get("folder") or "", "to_folder": dest,
                        "from": row.get("from_addr") or "",
                        "subject": row.get("subject") or "",
                        "by": "flow \"%s\"" % (flow.get("name") or "")})
                except Exception as exc:
                    taken.append("move:%s FAILED" % dest)
                    store.log_event("error", "flow '%s': move to %s failed: %r"
                                    % (flow.get("name"), dest, exc))
            else:
                taken.append("move:" + dest)
        elif t == "mark_read":
            if live:
                try:
                    mc.ensure_selected(cur_folder)
                    mc.set_flags(cur_uid, "+FLAGS", r"(\Seen)")
                except Exception:
                    pass
            taken.append("read")
        elif t == "flag":
            if live:
                try:
                    mc.ensure_selected(cur_folder)
                    mc.set_flags(cur_uid, "+FLAGS", r"(\Flagged)")
                except Exception:
                    pass
            taken.append("flag")
        elif t == "tag":
            tg = (st.get("tag") or "").strip()[:40]
            if live and tg:
                fields["user_tag"] = tg
            taken.append("tag:" + tg)
        elif t == "draft":
            if live:
                try:
                    row_now = store.get_message(row["id"]) or dict(row)
                    tpl_id = st.get("template_id") or None
                    try:
                        tpl_id = int(tpl_id) if tpl_id else None
                    except (TypeError, ValueError):
                        tpl_id = None
                    mode = (st.get("mode") or "template").lower()
                    body = ""
                    if mode == "plugin" and st.get("plugin"):
                        try:
                            import plugin_rt
                            pd = plugin_rt.draft(str(st["plugin"]), {
                                "subject": row_now.get("subject") or "",
                                "from": row_now.get("from_addr") or "",
                                "snippet": (row_now.get("snippet") or "")[:2000],
                                "instructions": st.get("instructions") or ""})
                            body = (pd or {}).get("text") or ""
                        except Exception:
                            body = ""
                    elif mode == "llm":
                        body = generate_draft(row_now["id"], tpl_id, st.get("instructions") or "")
                    elif mode == "fixed":
                        body = render_template_text(st.get("body") or "", row_now)
                    else:
                        tpl = store.get_template(tpl_id) if tpl_id else None
                        if tpl:
                            body = render_template_text(tpl.get("body") or "", row_now)
                    if body:
                        saved_to = save_draft(row_now["id"], body)
                        taken.append("draft\u2192" + saved_to)
                    else:
                        taken.append("draft skipped (no template)")
                except Exception as exc:
                    taken.append("draft FAILED")
                    store.log_event("error", "flow '%s': draft failed: %r" % (flow.get("name"), exc))
            else:
                taken.append("draft")
    return taken, fields


def _emit_plugin_event(kind, payload):
    """Fire-and-forget kernel event for integration plugins (never blocks)."""
    try:
        import plugin_rt
        plugin_rt.emit_event(kind, payload)
    except Exception:
        pass


def _process_flow(mc, flow, row, meta, settings, why=""):
    """Run one matching flow, once per message (flow_runs guards re-scans)."""
    live = bool(settings.get("flows_apply", True))
    key = (row.get("msgid") or "").strip() or ("id:%s" % row.get("id"))
    if store.flow_already_ran(flow["id"], key):
        store.log_event("debug", "flow '%s' already ran for '%s'"
                        % (flow.get("name"), (meta.get("subject") or "")[:50]))
        return
    taken, fields = _apply_flow(mc, flow, row, settings, live)
    fields["status"] = "flow" if live else "flow-dry"
    fields["action_taken"] = "flow:%s" % (flow.get("name") or flow.get("id"))
    store.update_message(row["id"], **fields)
    if live:
        store.record_flow_run(flow["id"], key, row["id"])
    store.log_event("info", "flow '%s'%s \u2192 %s | %s%s"
                    % (flow.get("name"), "" if live else " (dry-run)",
                       ", ".join(taken) or "no steps", (meta.get("subject") or "")[:60],
                       (" [%s]" % why) if why else ""))
    store.log_msg_event(row["id"], "flow", "flow \u201c%s\u201d%s \u2192 %s%s"
                        % (flow.get("name") or flow.get("id"), "" if live else " (dry-run)",
                           ", ".join(taken) or "no steps", (" [%s]" % why) if why else ""))


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

    def list_models(self, base=None, key=None):
        """Model ids from an OpenAI-compatible /models endpoint (Settings dropdown).
        Raises on any connection/HTTP error so the UI can fall back to a text box."""
        base = (base or self.base or "").rstrip("/")
        if not base:
            raise RuntimeError("no LLM endpoint configured")
        if key is None:
            key = self.key or ""
        headers = {"Authorization": "Bearer " + key} if key else {}
        r = requests.get(base + "/models", headers=headers,
                         timeout=min(self.timeout or 15, 15))
        r.raise_for_status()
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        out = []
        for it in items or []:
            mid = it.get("id") if isinstance(it, dict) else it
            if mid:
                out.append(str(mid))
        return sorted(set(out))

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
            # mild anti-repetition: small local models can fall into verbatim CoT loops
            "repetition_penalty": 1.05,
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
            if r.status_code in (400, 404, 422):
                # strict OpenAI-compatible servers reject our extensions one by one
                if "chat_template_kwargs" in payload:
                    payload.pop("chat_template_kwargs", None)
                    r.close()
                    continue
                if "repetition_penalty" in payload:
                    payload.pop("repetition_penalty", None)
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

    def classify(self, msg, categories, my_name=""):
        cats = ", ".join(categories) if categories else "Action, Notification, Newsletter, Receipt, Personal, Promo"
        system = ("You triage incoming email for %s. Reply with a single JSON object and nothing else. "
                  "Shape: {\"category\": one of [%s], \"needs_reply\": true|false, "
                  "\"confidence\": 0.0-1.0, \"summary\": \"one short sentence saying what the email is\", "
                  "\"reason\": \"why that category, max 15 words\"}" % (my_name or "the account owner", cats))
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

    def draft_reply(self, msg, body_text, template, settings, instructions=""):
        my_name = settings.get("my_name", "")
        system = ("You write email replies as %s (%s). Be concise, warm and professional. "
                  "Output ONLY the plain-text reply body (no subject line, no headers, no quotes)."
                  % (my_name or "the account owner", imap_config()["user"]))
        guidance = ""
        if template:
            subject_hint = template.get("subject") or ""
            body_hint = render_template_text(template.get("body") or "", msg)
            guidance = ("Use this reply template as guidance for structure and tone:\n---\n%s\n%s\n---\n"
                        % (subject_hint, body_hint))
        if instructions:
            guidance += ("Follow these instructions for the reply (they win over the "
                         "template's wording where they conflict):\n%s\n" % str(instructions)[:1000])
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
        try:
            # one-time (idempotent) audit backfill for messages that predate the
            # msg_events feature; cheap to re-run, inserts only missing kinds
            n = sweep_msg_events()
            if n:
                store.log_event("info", "audit backfill: %d event(s) reconstructed from stored state" % n)
        except Exception as exc:
            store.log_event("warn", "audit backfill failed: %r" % exc)
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


def _process_folder(mc, folder, settings, rules, flows=None):
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
    kept_ids = store.kept_ids()
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
        # category conditions need the classifier verdict; those flows wait for
        # the classify phase. Everything else (incl. topic) evaluates at scan.
        scan_flows = [f for f in (flows or []) if not _needs_verdict(f)]
        f_ctx = {"text": "%s\n%s" % (meta.get("subject") or "", meta.get("snippet") or "")}
        flow = None if rule else match_first_flows(scan_flows, _fields_for(meta), f_ctx)
        kept = ((meta.get("msgid") or row.get("msgid") or "").strip().strip("<>")
                in kept_ids)
        if rule and kept:
            store.update_message(row["id"], status="kept", rule_id=rule["id"],
                                 action_taken="kept (undo)")
            store.log_event("info", "rule '%s' skipped — '%s' is kept (undo)"
                            % (rule.get("name") or rule["id"],
                               (meta.get("subject") or "")[:50]))
            store.log_msg_event(row["id"], "rule", "rule \u201c%s\u201d skipped - mail is kept (undo)"
                                % (rule.get("name") or rule["id"]))
        elif rule:
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
                        store.record_move(row, actions["move_to"], "rule")
                        mc.ensure_folder(actions["move_to"])
                        new_uid = mc.move(uid, actions["move_to"], msgid=row.get("msgid"))
                        taken.append("move:" + actions["move_to"])
                        _emit_plugin_event("mail.filed", {
                            "id": row.get("id"), "msgid": row.get("msgid") or "",
                            "from_folder": row.get("folder") or "",
                            "to_folder": actions["move_to"],
                            "from": row.get("from_addr") or "",
                            "subject": row.get("subject") or "",
                            "by": "rule \"%s\"" % (rule.get("name") or "")})
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
            store.log_msg_event(row["id"], "rule", "rule \u201c%s\u201d \u2192 %s"
                                % (rule.get("name") or rule["id"],
                                   ", ".join(taken) or ("kept (guard)" if is_guard_rule(rule) else "suggest")))
        elif flow and not kept:
            why = ("topic match %.2f" % f_ctx["last_topic_score"]) if "last_topic_score" in f_ctx else ""
            _process_flow(mc, flow, row, meta, settings, why=why)
        else:
            store.update_message(row["id"], status="queued")
    if uids:
        store.log_event("debug", "%s: scanned %d new message(s)" % (folder, scanned))
    return scanned, moved


def undo_filing(log_id):
    """Move a previously filed message back to its origin folder, and keep it
    from being re-filed by rules/flows/LLM (undo guard by Message-ID)."""
    entry = store.get_undo(log_id)
    if not entry:
        return False, "Unknown undo entry."
    if entry.get("undone_ts"):
        return False, "That filing was already undone."
    msg = store.get_message(entry["msg_id"])
    if not msg:
        return False, "The message row no longer exists."
    if (msg.get("folder") or "") != (entry.get("to_folder") or ""):
        return False, "This message has moved since — undoing would clobber newer state."
    if not entry.get("from_folder"):
        return False, "I do not know where this came from."
    mc = MailClient().connect()
    try:
        mc.ensure_selected(msg.get("folder") or "")
        mc.ensure_folder(entry["from_folder"])
        new_uid = mc.move(msg["uid"], entry["from_folder"], msgid=msg.get("msgid"))
    except Exception as exc:
        return False, "Move back failed: %r" % exc
    finally:
        try:
            mc.close()
        except Exception:
            pass
    fields = {"folder": entry["from_folder"], "status": entry["prev_status"] or "classified",
              "action_taken": entry["prev_action_taken"] or ""}
    if new_uid:
        fields["uid"] = new_uid
    store.update_message(msg["id"], **fields)
    store.keep_message(msg.get("msgid"))
    store.mark_undone(log_id, uid=new_uid or 0)
    store.log_event("info", "undo: '%s' moved back to %s (kept - automation will not re-file it)"
                    % ((msg.get("subject") or "")[:50], entry["from_folder"]))
    store.log_msg_event(msg["id"], "undo", "moved back to \u201c%s\u201d - automation will not re-file it"
                        % entry["from_folder"])
    learning.observe(msg["id"], "undo", entry["to_folder"], source="ui")
    return True, "Moved back to %s — automation will not re-file it." % entry["from_folder"]


def sweep_msg_events(msg_id=None):
    """Backfill audit trails from stored state for messages that predate the
    msg_events feature (or miss some event kinds). Reconstructs from what the
    message row and flow_runs already know - never invents beyond that; entries
    are marked reconstructed. Idempotent: only inserts kinds that are absent.
    Returns the number of events inserted."""
    inserted = []
    with store.db() as conn:
        kinds = {}
        for r in conn.execute("SELECT msg_id, kind FROM msg_events"):
            kinds.setdefault(r["msg_id"], set()).add(r["kind"])
        if msg_id:
            rows = conn.execute("SELECT * FROM messages WHERE id=?", (int(msg_id),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM messages").fetchall()
        flow_ts = {}
        for r in conn.execute("SELECT message_id, MAX(ran_at) AS t FROM flow_runs"
                              " WHERE message_id > 0 GROUP BY message_id"):
            flow_ts[r["message_id"]] = r["t"]
    rules = {r["id"]: (r.get("name") or ("rule %s" % r["id"])) for r in store.list_rules()}
    for m in rows:
        have = kinds.get(m["id"], set())
        base = int(m["processed_at"] or m["date_ts"] or time.time())
        at = (m["action_taken"] or "")
        status = (m["status"] or "")
        if m["rule_id"] and "rule" not in have:
            nm = rules.get(m["rule_id"]) or ("rule %s" % m["rule_id"])
            if at.startswith("move"):
                res = at
            elif status == "kept" or at.startswith("kept"):
                res = "kept (undo)"
            elif at:
                res = at
            else:
                res = "suggest (dry-run)" if status.startswith("matched") else "matched"
            inserted.append((m["id"], "rule",
                             "rule \u201c%s\u201d \u2192 %s \u00b7 reconstructed" % (nm, res), base + 1))
        if at.startswith("flow:") and "flow" not in have:
            nm = at.split(":", 1)[1]
            inserted.append((m["id"], "flow",
                             "flow \u201c%s\u201d ran \u00b7 %s \u00b7 reconstructed"
                             % (nm, time.strftime("%Y-%m-%d %H:%M", time.localtime(base))),
                             int(flow_ts.get(m["id"]) or (base + 2))))
        if m["llm_category"] and "classify" not in have:
            try:
                conf = round(float(m["llm_confidence"]), 3) if m["llm_confidence"] is not None else 0
            except (TypeError, ValueError):
                conf = 0
            inserted.append((m["id"], "classify", json.dumps({
                "category": m["llm_category"], "confidence": conf,
                "by": m["classified_by"] or "llm", "reason": m["llm_reason"] or "",
                "summary": m["llm_summary"] or "", "needs_reply": bool(m["llm_needs_reply"]),
                "thinking": m["llm_thinking"] or "", "_backfilled": True}, ensure_ascii=False),
                base + 3))
        if at.startswith("move:") and "move" not in have:
            dest = at.split(":", 1)[1]
            src = ("rule" if m["rule_id"] else
                   ("flow" if status.startswith("flow") else
                    ("assistant" if status == "assistant-moved" else "auto-file")))
            inserted.append((m["id"], "move",
                             "filed to \u201c%s\u201d \u00b7 %s \u00b7 reconstructed" % (dest, src),
                             base + 4))
        if (m["user_tag"] or "").strip() and "tag" not in have:
            inserted.append((m["id"], "tag", "tagged \u201c%s\u201d" % m["user_tag"].strip(), base + 5))
        if (m["snoozed_until"] or 0) > 0 and "snooze" not in have:
            inserted.append((m["id"], "snooze",
                             "snoozed until %s" % time.strftime("%Y-%m-%d %H:%M",
                                                                time.localtime(m["snoozed_until"])),
                             base + 6))
    # cleanup: an earlier sweep pass labeled assistant moves as auto-file
    with store.db() as conn:
        conn.execute("UPDATE msg_events SET detail = replace(detail,"
                     " '\u00b7 auto-file \u00b7 reconstructed', '\u00b7 assistant \u00b7 reconstructed')"
                     " WHERE kind = 'move' AND detail LIKE '%auto-file%'"
                     " AND msg_id IN (SELECT id FROM messages WHERE status = 'assistant-moved')")
        conn.commit()
    return store.log_msg_events_bulk(inserted)


def _sim_flow_steps(flow, settings):
    """Step descriptions for a flow in dry mode (no IMAP, no drafts, no writes)."""
    fake = {"id": 0, "folder": "INBOX", "uid": 0, "msgid": "", "subject": ""}
    try:
        taken, _fields = _apply_flow(None, flow, fake, settings, live=False)
        return taken or ["no steps configured"]
    except Exception as exc:
        return ["(could not simulate: %r)" % exc]


EXAMPLE_DRAFT_SYSTEM = """You write ONE short, realistic example email used to test an \
email-automation rule or flow in a simulator. Reply with STRICT JSON only:
{"from": "sender@example.com", "subject": "...", "body": "..."}
Rules for the example:
- It MUST satisfy every condition of the rule/flow below (sender, subject keywords, etc).
- Prefer the real sender addresses named in the conditions; otherwise use example.com.
- Keep it 2-4 sentences, realistic, like real mail a person receives.
- Do not include any explanation outside the JSON."""


def _fallback_draft(kind, row):
    """Deterministic example built from the conditions, when the model is off."""
    try:
        conds = json.loads(row.get("conditions") or "[]")
    except (TypeError, ValueError):
        conds = []
    from_addr, subject, body = "", "", []
    for c in conds:
        fld = (c.get("field") or "").lower()
        val = str(c.get("value") or "").strip()
        if not val:
            continue
        if fld == "from":
            from_addr = val if ("@" in val and " " not in val) else (
                "%s@example.com" % re.sub(r"[^a-z0-9]+", ".", val.lower()).strip("."))
        elif fld == "subject":
            subject = ("Example: " + val)[:180]
        elif fld in ("body", "text"):
            body.append(val)
    if not subject:
        subject = "Example email for %s" % (row.get("name") or "the test")
    if not from_addr:
        from_addr = "someone@example.com"
    body_text = ((" ".join(body) + " ") if body else "") + (
        "This is a sample email written to test the \u201c%s\u201d %s. Edit anything and run the simulation."
        % (row.get("name"), kind))
    return {"from_addr": from_addr, "subject": subject, "body": body_text}


def example_draft_for(kind, gid):
    """One example email that exercises the given flow/rule: model-written when
    possible, deterministic fallback from the conditions. Returns
    {draft: {from_addr,subject,body,by}, name, kind} or None."""
    kind = (kind or "").lower()
    if kind == "flow":
        row = store.get_flow(gid)
        text = _flows_to_text([row]) if row else ""
    elif kind == "rule":
        row = store.get_rule(gid)
        text = _rules_to_text([row]) if row else ""
    else:
        return None
    if not row:
        return None
    name = row.get("name") or ("#%d" % gid)
    draft = None
    try:
        content = LLMClient()._chat(EXAMPLE_DRAFT_SYSTEM + "\n\n" + text,
                                    "Write the example email now.", json_mode=True)
        m = re.search(r"\{.*\}", content or "", re.S)
        if m:
            obj = json.loads(m.group(0))
            fr = str(obj.get("from") or "").strip()[:120]
            su = str(obj.get("subject") or "").strip()[:200]
            bo = str(obj.get("body") or "").strip()[:2000]
            if su or bo:
                draft = {"from_addr": fr, "subject": su, "body": bo, "by": "llm"}
    except Exception as exc:
        store.log_event("debug", "example draft generation failed (%r); using conditions" % exc)
        draft = None
    if not draft:
        draft = _fallback_draft(kind, row)
        draft["by"] = "rules"
    return {"draft": draft, "name": name, "kind": kind}


def _sim_draft_preview(sim_msg, step, settings, use_llm):
    """Dry-run of a flow's draft step: the text that would land in Drafts.
    fixed / template render locally; the llm mode only runs when the user ticked
    \u201cAsk the classifier\u201d, so the default simulation stays instant."""
    mode = (step.get("mode") or "template").lower()
    tpl_id = step.get("template_id")
    try:
        tpl_id = int(tpl_id) if tpl_id else None
    except (TypeError, ValueError):
        tpl_id = None
    tpl = store.get_template(tpl_id) if tpl_id else None
    subj = sim_msg.get("subject") or ""
    head = {"mode": mode,
            "to": sim_msg.get("from_addr") or "",
            "subject": subj if subj.lower().startswith("re:") else ("Re: " + subj)}
    if mode == "plugin":
        pid = str(step.get("plugin") or "")
        if not use_llm:
            note = "plugin '%s'" % pid
            if step.get("instructions"):
                note += " - " + str(step["instructions"])
            return {"mode": mode, "needs_llm": True, "instructions": note[:400]}
        body = ""
        try:
            import plugin_rt
            pd = plugin_rt.draft(pid, {
                "subject": sim_msg.get("subject") or "",
                "from": sim_msg.get("from_addr") or "",
                "snippet": (sim_msg.get("body") or "")[:2000],
                "instructions": step.get("instructions") or ""})
            body = (pd or {}).get("text") or ""
        except Exception:
            body = ""
        if not body:
            return {"mode": mode, "error": "plugin '%s' produced no draft" % pid}
        head.update({"by": "plugin", "body": body.strip()[:4000]})
        return head
    if mode == "llm":
        if not use_llm:
            return {"mode": mode, "needs_llm": True,
                    "instructions": (step.get("instructions") or "")[:400]}
        body = LLMClient().draft_reply(sim_msg, sim_msg.get("body") or "", tpl, settings,
                                       instructions=step.get("instructions") or "")
        head.update({"by": "model", "body": (body or "").strip()[:4000]})
        return head
    if mode == "fixed":
        head.update({"by": "fixed", "body": render_template_text(step.get("body") or "", sim_msg)})
        return head
    if not tpl:
        return {"mode": mode, "error": "this step has no template set"}
    head.update({"by": "template", "body": render_template_text(tpl.get("body") or "", sim_msg)})
    return head


def simulate_email(from_addr, subject, body, to_addr="", use_llm=False):
    """Dry-run of the triage pipeline over a drafted email: which guard/rule
    matches, which flows would fire, and (optionally) what the classifier thinks.
    Nothing is changed - no mail, no rows, no events."""
    settings = store.all_settings()
    fields = {"from": from_addr or "", "to": to_addr or "",
              "subject": subject or "", "body": body or ""}
    sim_msg = {"from_addr": from_addr or "", "to": to_addr or "",
               "subject": subject or "", "snippet": (body or "")[:500],
               "body": body or "", "msgid": "", "id": 0, "date": "", "folder": "", "uid": 0}
    out = {"guard": None, "rule": None, "rule_actions": [], "flow": None, "flow_taken": [],
           "verdict": None, "suggested_folder": "", "would": [], "notes": [],
           "rules_apply": bool(settings.get("rules_apply", True)),
           "flows_apply": bool(settings.get("flows_apply", True)),
           "llm_apply": bool(settings.get("llm_apply")), "use_llm": bool(use_llm)}
    rule = match_first(store.list_rules(enabled_only=True), fields)
    if rule and is_guard_rule(rule):
        out["guard"] = rule.get("name") or ("rule %s" % rule.get("id"))
        rule = None
    out["rule"] = rule
    if rule:
        try:
            acts = json.loads(rule.get("actions") or "{}")
        except (TypeError, ValueError):
            acts = {}
        desc = []
        if acts.get("move_to"):
            desc.append("move to \u201c%s\u201d" % acts["move_to"])
        if acts.get("tag"):
            desc.append("tag \u201c%s\u201d" % acts["tag"])
        if acts.get("mark_read"):
            desc.append("mark as read")
        if acts.get("flag"):
            desc.append("flag it")
        out["rule_actions"] = desc or ["keep in place (no actions)"]
    flows = store.list_flows(enabled_only=True)
    f_ctx = {"text": "%s\n%s" % (subject or "", body or "")}
    if out["guard"]:
        out["would"] = ["Guard rule \u201c%s\u201d matches - all automated filing is blocked; the mail stays put." % out["guard"]]
    elif rule:
        out["would"] = ["Rule \u201c%s\u201d matches first (rules run in list order)." % (rule.get("name") or rule.get("id"))]
        out["would"] += ["Action: %s" % d for d in out["rule_actions"]]
        if not out["rules_apply"]:
            out["notes"].append("Rules are in dry-run mode - a live run would only suggest, nothing would move.")
    else:
        try:
            fl = match_first_flows([f for f in flows if not _needs_verdict(f)], fields, f_ctx)
        except Exception as exc:
            fl = None
            out["notes"].append("Flow conditions could not be evaluated: %r" % exc)
        if fl:
            out["flow"] = fl
            out["flow_taken"] = _sim_flow_steps(fl, settings)
            out["would"] = ["Flow \u201c%s\u201d matches." % (fl.get("name") or fl.get("id"))]
            out["would"] += ["Step: %s" % s for s in out["flow_taken"]]
            if not out["flows_apply"]:
                out["notes"].append("Flows are in dry-run mode - a live run would only record what they would do.")
        elif not out["use_llm"]:
            out["would"] = ["No rule or deterministic flow matches."]
            out["notes"].append("Tick \u201cAsk the classifier\u201d to also test AI category / topic flows.")
    if out["use_llm"]:
        res, hres = classify_verdict(sim_msg, settings)
        try:
            conf = float(res.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        out["verdict"] = {"category": res.get("category") or "", "confidence": conf,
                          "reason": res.get("reason") or "", "summary": res.get("summary") or "",
                          "needs_reply": bool(res.get("needs_reply")),
                          "thinking": str(res.get("_thinking") or ""),
                          "by": ("heuristic: %s" % hres.get("heuristic_name")) if hres else "LLM"}
        folder = (settings.get("category_folders") or {}).get(out["verdict"]["category"], "")
        out["suggested_folder"] = folder
        line = "Classifier: %s" % (out["verdict"]["category"] or "(no category)")
        if out["verdict"]["confidence"]:
            line += " (%d%%)" % round(out["verdict"]["confidence"] * 100)
        out["would"].append(line + " \u00b7 by %s" % out["verdict"]["by"])
        if out["verdict"]["needs_reply"]:
            out["would"].append("Would be flagged \u201cneeds reply\u201d.")
        if not out["flow"] and not out["guard"]:
            try:
                vf = match_first_flows(
                    [f for f in flows if _needs_verdict(f)], fields,
                    {"verdict": {"category": out["verdict"]["category"],
                                 "confidence": out["verdict"]["confidence"], "source": "sim"},
                     "text": f_ctx["text"]})
            except Exception as exc:
                vf = None
                out["notes"].append("AI flow conditions could not be evaluated: %r" % exc)
            if vf:
                out["flow"] = vf
                out["flow_taken"] = _sim_flow_steps(vf, settings)
                out["would"].append("Flow \u201c%s\u201d matches on the classification." % (vf.get("name") or vf.get("id")))
                out["would"] += ["Step: %s" % s for s in out["flow_taken"]]
            elif folder and not rule:
                if out["llm_apply"]:
                    out["would"].append("Auto-filing would move it to \u201c%s\u201d." % folder)
                else:
                    out["would"].append("Suggested folder \u201c%s\u201d (auto-filing is off - suggestion only)." % folder)
    _fl = out.get("flow")
    if _fl:
        try:
            _steps = _fl.get("steps") or _fl.get("actions")
            if isinstance(_steps, str):
                _steps = json.loads(_steps or "[]")
        except (TypeError, ValueError):
            _steps = []
        for _st in (_steps or []):
            if isinstance(_st, dict) and _st.get("type") == "draft":
                try:
                    out["draft_preview"] = _sim_draft_preview(sim_msg, _st, settings,
                                                              bool(out.get("use_llm")))
                except Exception as exc:
                    out["draft_preview"] = {"error": repr(exc)}
                break
    return out


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
                got = conn.fetch_body_payload(uid, limit=6000)
            except Exception:
                got = None
            txt = (got or {}).get("text") or ""
            if txt and not looks_like_mime_junk(txt) and looks_readable(txt):
                fields = {"snippet": txt[:4000], "folder": folder, "uid": uid,
                          "body_html_at": int(time.time())}
                if got.get("html"):
                    fields["body_html"] = got["html"][:400000]
                    fields["body_cids"] = json.dumps(got.get("cids") or {})
                for r in rs:
                    store.update_message(r["id"], **fields)
                    with flock:
                        fixed[0] += 1
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    _run_parallel(groups, fetch_chunk, workers)
    return {"fixed": fixed[0], "not_found": not_found}


def extract_rendered(workers=6):
    """One-shot: extract + sanitize the HTML body for every row that has not been
    through it yet, so the viewer renders without a per-message IMAP fetch.
    Returns {"updated", "remaining"}."""
    rows = [r for r in store.messages(limit=999999) if not (r.get("body_html_at") or 0)]
    if not rows:
        return {"updated": 0, "remaining": 0}
    by_folder = {}
    for r in rows:
        by_folder.setdefault(r["folder"], []).append(r)
    updated, lock = [0], threading.Lock()

    def chunk_fn(chunk):
        for folder, items in chunk:
            try:
                mc = MailClient().connect()
                mc.select(folder)
            except Exception:
                continue
            try:
                for r in items:
                    try:
                        got = mc.fetch_body_payload(r["uid"], limit=6000)
                    except Exception:
                        continue
                    txt = got.get("text") or ""
                    if not txt or not looks_readable(txt):
                        continue
                    fields = {"body_html_at": int(time.time())}
                    if got.get("html"):
                        fields["body_html"] = got["html"][:400000]
                        fields["body_cids"] = json.dumps(got.get("cids") or {})
                    if looks_like_mime_junk(r.get("snippet") or ""):
                        fields["snippet"] = txt[:4000]
                    store.update_message(r["id"], **fields)
                    with lock:
                        updated[0] += 1
            finally:
                try:
                    mc.close()
                except Exception:
                    pass

    groups = sorted(by_folder.items(), key=lambda kv: -len(kv[1]))
    _run_parallel(groups, chunk_fn, workers)

    relook = [r for r in store.messages(limit=999999) if not (r.get("body_html_at") or 0)]
    if relook:
        store.log_event("info", "render-extract: %d row(s) going through the Message-ID rescue"
                        % len(relook))
        rescue_stale_snippets(relook, workers=workers)
    remaining = sum(1 for r in store.messages(limit=999999)
                    if not (r.get("body_html_at") or 0))
    store.log_event("info", "render-extract: %d row(s) done, %d remaining"
                    % (updated[0], remaining))
    return {"updated": updated[0], "remaining": remaining}


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


def heal_locations(workers=6):
    """One-shot maintenance: reconcile every stored (folder, uid) with the real
    mailbox. Builds a Message-ID -> [(folder, uid)] index (one ranged header
    fetch per folder, parallel), then re-points the rows whose message still
    exists somewhere; orphans are reported, never touched. Rows whose message
    sits in the SAME folder under a different uid (move renumbering - the
    COPYUID recording gap) are the common case this repairs. Returns a summary
    dict with counts + small samples."""
    mc = MailClient().connect()
    try:
        folders = [f for f in sorted(mc.folders()) if f]
    finally:
        try:
            mc.close()
        except Exception:
            pass
    locs, lock = {}, threading.Lock()

    def index_chunk(chunk):
        for folder in chunk:
            try:
                found = _header_index(folder)
            except Exception as exc:
                store.log_event("warn", "heal-locations: %s: %r" % (folder, exc))
                continue
            with lock:
                for mid, uid in found.items():
                    key = mid.strip().strip("<>").lower()
                    if key:
                        locs.setdefault(key, []).append((folder, uid))

    _run_parallel(folders, index_chunk, workers)

    with store.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, folder, uid, msgid, subject FROM messages").fetchall()]
    updated, not_found, no_id = [], [], 0
    for r in rows:
        mid = (r["msgid"] or "").strip().strip("<>").lower()
        if not mid:
            no_id += 1
            continue
        got = locs.get(mid)
        if not got:
            not_found.append(r["id"])
            continue
        if (r["folder"], r["uid"]) in got:
            continue
        same = [g for g in got if g[0] == r["folder"]]
        pick = same[0] if same else sorted(got)[0]
        store.update_message(r["id"], folder=pick[0], uid=pick[1])
        updated.append({"id": r["id"], "old": "%s/%s" % (r["folder"], r["uid"]),
                        "new": "%s/%s" % pick,
                        "subject": (r.get("subject") or "")[:60]})
    store.log_event("info", "heal-locations: %d row(s) re-pointed, %d not found, %d folders"
                    % (len(updated), len(not_found), len(folders)))
    return {"folders": len(folders), "indexed": sum(len(v) for v in locs.values()),
            "updated": len(updated), "not_found": len(not_found), "no_msgid": no_id,
            "updated_sample": updated[:12], "not_found_sample": not_found[:12]}



def classify_verdict(msg, settings):
    """Heuristic classifiers first (deterministic, no LLM call for a confident
    verdict); the LLM only sees what the heuristics abstain on. Returns (res, hres)."""
    hres = heuristics.classify(msg) if settings.get("heuristics_enabled", True) else None
    if hres:
        res = {"category": hres["category"], "confidence": hres["confidence"],
               "summary": "", "reason": hres["reason"], "needs_reply": False,
               "_heuristic_id": hres["heuristic_id"],
               "_heuristic_name": hres["heuristic_name"]}
    else:
        res = LLMClient().classify(msg, settings.get("categories") or [],
                                   settings.get("my_name", ""))
    return res, hres


def _needs_reply_effective(msg_id, llm_value):
    """User corrections outrank the model: a cleared needs_reply stays cleared
    through every re-classification."""
    try:
        u = store.user_needs_reply(msg_id)
    except Exception:
        u = None
    return u if u is not None else (1 if llm_value else 0)


def classify_and_store(msg, settings, mc=None):
    """Classify one message, persist the result, file it when llm_apply is on.

    Order: trained heuristic classifiers first (deterministic, no LLM call for a
    confident verdict); the LLM only sees what the heuristics abstain on.

    Returns the classification dict with '_moved_to' set when it was filed.
    Raises on LLM failure. Reused by the worker queue and the manual batch job.
    """
    res, hres = classify_verdict(msg, settings)
    category = str(res.get("category", ""))
    try:
        conf = float(res.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    folder = (settings.get("category_folders") or {}).get(category, "")
    already_filed = str(msg.get("action_taken") or "").startswith("move")
    kept = store.is_kept(msg.get("msgid"))
    fields = {
        "llm_category": category,
        "llm_confidence": conf,
        "llm_summary": str(res.get("summary", ""))[:200],
        "llm_reason": str(res.get("reason", ""))[:200],
        "llm_thinking": str(res.get("_thinking") or "")[:6000],
        "llm_needs_reply": _needs_reply_effective(msg.get("id"), res.get("needs_reply")),
        "llm_suggested_folder": folder,
        "classified_by": (("plugin:%s" % hres["plugin"]) if hres.get("plugin")
                          else ("heuristic:%s %s" % (hres["heuristic_id"], hres["heuristic_name"]))) if hres else "llm",
        "status": "classified",
    }
    store.log_msg_event(msg.get("id"), "classify", json.dumps({
        "category": category, "confidence": round(conf, 3), "by": fields["classified_by"],
        "reason": fields["llm_reason"], "summary": fields["llm_summary"],
        "needs_reply": bool(res.get("needs_reply")), "thinking": fields["llm_thinking"]},
        ensure_ascii=False))
    res["_moved_to"] = ""
    res["_flow"] = ""
    verdict = {"category": category, "confidence": conf,
               "source": ("heuristic" if hres else "llm")}
    classify_flows = [f for f in store.list_flows(enabled_only=True) if _needs_verdict(f)]
    filing_wanted = bool(settings.get("llm_apply") and folder)
    guard = None
    if (classify_flows or filing_wanted) and not already_filed:
        # guard rules protect mail from ALL automated filing (flows + category map)
        g_fields = {"from": msg.get("from_addr", ""), "to": msg.get("to_addr", ""),
                    "subject": msg.get("subject", ""), "body": msg.get("snippet", "")}
        for r in store.list_rules(enabled_only=True):
            if is_guard_rule(r) and rule_matches(r, g_fields):
                guard = r.get("name") or ("rule %s" % r.get("id"))
                store.log_event("info", "kept '%s' in place (guard rule '%s')"
                                % ((msg.get("subject") or "")[:50], guard))
                store.log_msg_event(msg.get("id"), "guard",
                                    "guard rule \u201c%s\u201d keeps it in place - automated filing skipped" % guard)
                break
    if classify_flows and not already_filed and not guard and not kept:
        # fuzzy flows fire once the verdict is in; the first matching flow wins
        m_fields = {"from": msg.get("from_addr", ""), "to": msg.get("to_addr", ""),
                    "subject": msg.get("subject", ""), "body": msg.get("snippet", "")}
        f_ctx = {"verdict": verdict,
                 "text": "%s\n%s" % (msg.get("subject") or "", msg.get("snippet") or "")}
        for fl in classify_flows:
            if not flow_matches(fl, m_fields, f_ctx):
                continue
            try:
                own = mc is None
                if own:
                    mc = MailClient().connect()
                try:
                    _process_flow(mc, fl, msg, {"subject": msg.get("subject")}, settings,
                                  why="AI category %s %d%%" % (category, round(conf * 100)))
                finally:
                    if own and mc is not None:
                        try:
                            mc.close()
                        except Exception:
                            pass
                live = bool(settings.get("flows_apply", True))
                res["_flow"] = fl.get("name") or ("flow %s" % fl.get("id"))
                fields["status"] = "flow" if live else "flow-dry"
                fields["action_taken"] = "flow:%s" % (fl.get("name") or fl.get("id"))
            except Exception as exc:
                store.log_event("error", "flow '%s' failed after classification: %r"
                                % (fl.get("name"), exc))
            break
    if filing_wanted and not already_filed and not guard and not kept and not res.get("_flow"):
        own = mc is None
        try:
            if own:
                mc = MailClient().connect()
            mc.ensure_selected(msg["folder"])
            mc.ensure_folder(folder)
            store.record_move(msg, folder, "auto-file")
            new_uid = mc.move(msg["uid"], folder, msgid=msg.get("msgid"))
            fields["status"] = "llm-moved"
            fields["action_taken"] = "move:" + folder
            fields["folder"] = folder
            if new_uid:
                fields["uid"] = new_uid
            res["_moved_to"] = folder
            store.log_msg_event(msg.get("id"), "file",
                                "auto-filed to \u201c%s\u201d (LLM suggested)" % folder)
        except Exception as exc:
            store.log_event("error", "LLM move to %s failed: %r" % (folder, exc))
        finally:
            if own and mc is not None:
                try:
                    mc.close()
                except Exception:
                    pass
    store.update_message(msg["id"], **fields)
    learning.observe_classification(msg, fields, res, hres, settings)
    _emit_plugin_event("mail.classified", {
        "id": msg.get("id"), "category": category, "confidence": round(conf, 3),
        "needs_reply": bool(res.get("needs_reply")),
        "from": msg.get("from_addr") or "", "subject": msg.get("subject") or ""})
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
        note = "(suggestion only)"
        if res.get("_flow"):
            note = "flow '%s'" % res["_flow"]
        elif res.get("_moved_to"):
            note = "moved to %s" % res["_moved_to"]
        store.log_event("info", "LLM: '%s' → %s (%.0f%%) %s"
                        % ((msg.get("subject") or "")[:50], res.get("category"),
                           (float(res.get("confidence") or 0)) * 100, note))
        done += 1
    return done


def process_mailbox():
    """One full pass: scan watched folders, apply rules, run the LLM queue."""
    settings = store.all_settings()
    rules = [r for r in store.list_rules() if r.get("enabled")]
    flows = [f for f in store.list_flows() if f.get("enabled")]
    mc = MailClient().connect()
    scanned = moved = classified = 0
    try:
        for folder in settings.get("watch_folders") or ["INBOX"]:
            s, m = _process_folder(mc, folder, settings, rules, flows)
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


def detect_hardware():
    """Best-effort look at what this machine can serve (GPU names + VRAM).

    Runs inside the app container, so it only sees GPUs the container was
    given. Host-side detection for deploy planning is in docs/getting-started.md."""
    info = {"gpus": [], "tier": "none", "tier_label": "no GPU visible", "advice": ""}
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            for line in (out.stdout or "").strip().splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 2:
                    try:
                        info["gpus"].append({"name": parts[0], "vram_mb": int(float(parts[1])),
                                             "gb": round(int(float(parts[1])) / 1024)})
                    except ValueError:
                        pass
    except Exception:
        pass
    vmax = max((g["vram_mb"] for g in info["gpus"]), default=0)
    if vmax >= 20000:
        info.update(tier="large", tier_label="%dGB GPU" % round(vmax / 1024),
                    advice="A local model server fits: see gemma/ for a validated "
                           "single-24GB-card vLLM setup, or serve any model that fits.")
    elif vmax >= 9000:
        info.update(tier="medium", tier_label="%dGB GPU" % round(vmax / 1024),
                    advice="A quantized 7-14B instruct model fits locally (vLLM or "
                           "ollama); a hosted API is an equally good option.")
    elif vmax > 0:
        info.update(tier="small", tier_label="%dGB GPU" % round(vmax / 1024),
                    advice="A small (3-8B, quantized) model via ollama or llama.cpp "
                           "fits; quality is modest - a hosted API is the stronger option.")
    else:
        info.update(tier="none", tier_label="no GPU visible",
                    advice="No GPU visible to the app. Rules + search work as-is; "
                           "use a hosted OpenAI-compatible API, or run a small CPU "
                           "model via ollama or llama.cpp.")
    return info


def doctor():
    """Setup report: hardware, mailbox config, LLM endpoint. Read-only, safe anytime.

    Deliberately does not connect to IMAP (that is what --check is for); it does
    probe the LLM endpoint when one is configured, with the client's timeout."""
    rep = {"hardware": detect_hardware()}
    try:
        imap = imap_config()
        rep["mailbox"] = {"host": imap.get("host"), "port": imap.get("port"),
                          "user": imap.get("user") or "",
                          "configured": bool(imap.get("user"))}
    except Exception as exc:
        rep["mailbox"] = {"configured": False, "error": repr(exc)[:120]}
    try:
        cl = LLMClient()
        base = cl.base or ""
        rep["llm"] = {"base_url": base, "model": cl.model or "", "configured": bool(base)}
        if base:
            t0 = time.time()
            try:
                out = cl._chat_once(base, cl.key, cl.model,
                                    "You are a connectivity test. Reply with the single word ok.",
                                    [{"role": "user", "content": "Reply with the single word ok."}],
                                    json_mode=False)
                rep["llm"].update(reachable=True, latency_ms=int((time.time() - t0) * 1000),
                                  reply=(out or "").strip()[:40])
            except Exception as exc:
                rep["llm"].update(reachable=False, error=repr(exc)[:160])
    except Exception as exc:
        rep["llm"] = {"configured": False, "error": repr(exc)[:120]}
    return rep


def generate_draft(msg_id, template_id=None, instructions=""):
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
    return LLMClient().draft_reply(msg, body_text, template, store.all_settings(),
                                   instructions=str(instructions or ""))


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
    "conditions": [{"field": "from|to|subject|body", "op": "contains|equals|regex|plugin", "value": "...", "plugin": "matcher plugin id when op=plugin"}],
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
               "EXISTING FLOWS (multi-step; do not duplicate):\n%s\n\n"
               "CATEGORIES: %s\n\n"
               "TAGGED EXAMPLES (the user's manual labels):\n%s"
               % (_rules_to_text(store.list_rules()),
                  _flows_to_text(store.list_flows()),
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
- The CURRENT PAGE block (appended below when the app knows what the user is looking at) describes what is on their screen right now, section by section, with the controls by name. Questions like "what is this page", "how do I use this", "what does X mean here" refer to THAT page: explain its sections, what the numbers mean, and the concrete next click. Only when the user is asking about the assistant itself (or no page is known) fall back to describing your own capabilities.
- Ground every answer with tools instead of guessing. semantic_search finds mail by MEANING across every indexed folder and years of history (paraphrases welcome) — use it first for content questions ("what did the landlord want", "the trip itinerary email"). search_messages reads the app's local index; search_mail runs a live IMAP search for exact tokens or folders outside the index. For any question about the user's mail, search first.
- Reference specific messages in your answers as [msg:ID] (the message_id from tool results); the UI turns those into links. Use read_message for the full text of anything you quote.
- Capability permissions are enforced by the app. Current grants: %(permissions)s. Honour them: tools under "may do directly" you may call; tools under "requires the user's approval" you may still call - the app turns them into a pending approval card the user clicks (say it is waiting; never claim it happened); tools under "disabled" are refused - tell the user the capability is off and where to enable it (Settings, AI settings, Agent permissions).
- For ongoing sorting, propose a rule with propose_rule (the user approves with one click). Moving never deletes mail; only a delete capability moves mail to Trash, only send sends mail.
- Keep searches bounded: small limits, use since/before for windows. Summarize results; never dump raw rows.
- When you read a message, never paste long verbatim quotes into the answer: give the gist in your own words, keep only short key phrases (prices, dates, rules), and cite the message as [msg:ID].
- The user can also tag mail by hand on the Messages page. If they ask you to learn rules from their tags, call list_tagged first and base propose_rule calls on the tag-to-pattern evidence.
- To PROTECT mail from being moved (any "never move X" / "keep X in the inbox" request): propose a rule with conditions only and NO actions - that is a guard rule. Guards must sit at the top, so set placement="top". A guard also stops LLM category filing for matching mail.
- BEFORE proposing a rule, call list_rules (or mailbox_overview) and check what already exists. If a similar rule exists (same sender/domain/subject), propose an UPDATE instead of a near-duplicate: pass updates_rule_id with the rule as it should look afterwards (name/conditions/actions). If you propose something that overlaps an existing rule without updates_rule_id, the app flags it to the user, so handle it yourself first.
- Heuristic classifiers (train_classifier / list_classifiers / manage_classifier / evaluate_classifier): deterministic trained models that run BEFORE the LLM in triage. Suggest them when the user wants less LLM dependence, when a category has regular labelled mail (tags), or when classification feels inconsistent. decision_list suits sender/keyword patterns, naive_bayes fuzzier ones; retrain via retrain_id as labels grow; evaluate before claiming quality. After a tagging session, suggest training one when a category has around 8+ tagged examples.
- Only tell the user a rule was proposed once propose_rule has returned ok:true in this turn; never claim a proposal you did not actually make.
- MULTI-STEP AUTOMATIONS ARE FLOWS: when a request has an ordered sequence ("move it AND then draft/tag/star it", "prepare a draft that says ..."), call propose_flow with the steps in order - NOT propose_rule. A fixed draft body is fully supported (step {type:"draft", mode:"fixed", body:"..."}), and an LLM draft takes "instructions" (what the reply should say - tone, points, what to reference; e.g. "thank them and ask for the PO number"). WHEN conditions can be FUZZY: {kind:"category", value:"<one of the app's categories>"} fires right after the classifier sorts the mail that way - use it for "if it's a <type> email" asks (see the CATEGORIES list in CURRENT STATE); {kind:"topic", value:"<short description with the boundary>"} matches by MEANING via embeddings - use it for "if it's related to / about X" asks; write the description like the boundary, e.g. "parcels and deliveries - shipping notices, courier updates, pickup codes. NOT marketing." Optional threshold 0.2-0.95 (default 0.45). Keep deterministic from/subject conditions alongside when the user names a sender; deterministic conditions are checked first and cost nothing, AI conditions run only after they pass. Plain single-action requests stay rules. Check list_flows and list_rules first; flows run after rules, first matching flow wins.
- Rule housekeeping: call list_rules (or list_flows) for the exact ids. If the user asks to REMOVE/DELETE a rule (for example an exact duplicate), call delete_rule with that id - delete one of a duplicate pair and keep the other. If they only want it paused ("turn it off for now"), call set_rule_enabled. Only touch rules and flows the user asked about; if it is unclear which one, ask one short question first. delete_flow and set_flow_enabled do the same for flows.
- Decide once, then act. State one short plan in your thinking, call the tools, then answer. Never repeat the same reasoning paragraph; if a task needs a capability you do not have, say so in ONE sentence and offer the closest alternative instead of re-reading your tool list.
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
    _fn("delete_rule",
        "Permanently delete one filter rule (rule_id from list_rules). Use when the user asks to remove/delete a rule - e.g. an exact duplicate (delete one and keep the other). Deleting cannot be undone; only delete rules the user has explicitly asked about. To just pause a rule use set_rule_enabled instead.",
        {"rule_id": {"type": "integer", "description": "id shown by list_rules"}},
        required=("rule_id",)),
    _fn("set_rule_enabled",
        "Enable or disable one filter rule without deleting it. Use for \"turn it off for now\" or \"stop sorting Y\" requests.",
        {"rule_id": {"type": "integer"},
         "enabled": {"type": "boolean", "description": "false pauses the rule, true resumes it"}},
        required=("rule_id", "enabled")),
    _fn("list_flows",
        "List the multi-step flow automations (id, enabled, WHEN conditions, THEN steps). Flows run AFTER rules; first matching flow wins. Check this before proposing a flow so you update instead of duplicating.",
        {}),
    _fn("propose_flow",
        "Propose a multi-step FLOW for the user to approve with one click. Use when a request needs an ordered sequence - e.g. 'move it to X AND then draft a reply', 'tag it, then star it'. Steps run in order: move / draft / tag / mark_read / flag. A draft step with mode \"fixed\" saves a literal message body (placeholders {sender} {subject} {date} {my_name} allowed); mode \"template\" fills a saved template; mode \"llm\" lets the LLM write it - pass \"instructions\" to say what the reply should contain (tone, points, what to reference); template is optional guidance. Nothing is ever sent - drafts land in the Drafts folder. WHEN conditions can be FUZZY: {kind:'category', value:'<category from the app's list>'} fires after classification assigns that category; {kind:'topic', value:'<short description of the kind of mail>'} matches by MEANING (no exact words needed). Single-action requests should stay plain rules (propose_rule).",
        {"name": {"type": "string", "description": "short flow name"},
         "match_mode": {"type": "string", "enum": ["all", "any"]},
         "conditions": {"type": "array", "description": "1-4 WHEN conditions. Deterministic: {field, op, value}. FUZZY: {kind:'category', value:'<category>'} fires when the classifier tags the message (checked right after classification); {kind:'topic', value:'<description with its boundary>', threshold:0.2-0.95} matches by meaning. Deterministic conditions evaluate first and skip the AI cost when they fail.", "items": {
             "type": "object",
             "properties": {
                 "kind": {"type": "string", "enum": ["field", "category", "topic"]},
                 "field": {"type": "string", "enum": ["from", "to", "subject", "body"]},
                 "op": {"type": "string", "enum": ["contains", "equals", "regex", "plugin"]},
                 "value": {"type": "string"},
                 "plugin": {"type": "string", "description": "op=plugin only: a matcher plugin id (mt-cjk-matcher checks for CJK text); the plugin decides by its own logic"},
                 "min_confidence": {"type": "number", "description": "category conditions: minimum classifier confidence 0-1"},
                 "threshold": {"type": "number", "description": "topic conditions: similarity threshold 0.2-0.95 (default 0.45)"}}}},
         "steps": {"type": "array", "description": "THEN steps, in order (1-10)", "items": {
             "type": "object",
             "properties": {
                 "type": {"type": "string", "enum": ["move", "draft", "tag", "mark_read", "flag"]},
                 "folder": {"type": "string", "description": "for move steps"},
                 "mode": {"type": "string", "enum": ["fixed", "template", "llm"], "description": "for draft steps"},
                 "body": {"type": "string", "description": "literal message for a fixed draft"},
                 "template_id": {"type": "integer", "description": "for template/llm drafts"},
                 "instructions": {"type": "string", "description": "for llm draft steps: what the reply should say (tone, points to include, what to reference)"},
                 "tag": {"type": "string", "description": "for tag steps"}}}},
         "rationale": {"type": "string", "description": "one short sentence why"},
         "updates_flow_id": {"type": "integer",
                             "description": "id of an EXISTING flow this proposal changes (from list_flows); the proposal then replaces that flow's name/conditions/steps"}}),
    _fn("delete_flow",
        "Permanently delete one flow automation (flow_id from list_flows). Use only when the user explicitly asks to remove/delete that flow; to just pause it use set_flow_enabled.",
        {"flow_id": {"type": "integer"}},
        required=("flow_id",)),
    _fn("set_flow_enabled",
        "Enable or disable one flow automation without deleting it.",
        {"flow_id": {"type": "integer"},
         "enabled": {"type": "boolean", "description": "false pauses the flow, true resumes it"}},
        required=("flow_id", "enabled")),
    _fn("propose_rule",
        "Propose a filter rule for the user to approve with one click. Approved rules sort matching mail automatically (top to bottom, first match wins). A rule with NO actions is a GUARD: matching mail stays put and nothing else (later rules, LLM filing) can move it.",
        {"name": {"type": "string", "description": "short rule name"},
         "match_mode": {"type": "string", "enum": ["all", "any"]},
         "conditions": {"type": "array", "description": "1-4 conditions", "items": {
             "type": "object",
             "properties": {
                 "field": {"type": "string", "enum": ["from", "to", "subject", "body"]},
                 "op": {"type": "string", "enum": ["contains", "equals", "regex", "plugin"]},
                 "value": {"type": "string"},
                 "plugin": {"type": "string", "description": "op=plugin only: a matcher plugin id (mt-cjk-matcher checks for CJK text); the plugin decides by its own logic"}}}},
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
    _fn("classify_message",
        "Classify ONE message now with the normal pipeline (category, confidence, summary, needs-reply) and store the result; filing follows the auto-filing setting exactly like the Classify button.",
        {"message_id": {"type": "integer"},
         "folder": {"type": "string"},
         "uid": {"type": "integer"}}),
    _fn("tag_message",
        "Tag one message with a short label (like the user's own tagging), or untag it with add=false. Your tags are recorded as assistant-sourced.",
        {"tag": {"type": "string", "description": "short label"},
         "add": {"type": "boolean", "description": "false removes the tag (default true)"},
         "message_id": {"type": "integer"},
         "folder": {"type": "string"},
         "uid": {"type": "integer"}},
        ("tag",)),
    _fn("draft_reply",
        "Write a reply to one message and save it in the Drafts folder for the user to review and send. Nothing is sent; sending is a separate, dangerous capability.",
        {"message_id": {"type": "integer"},
         "folder": {"type": "string"},
         "uid": {"type": "integer"},
         "template_id": {"type": "integer", "description": "optional reply template id"}}),
    _fn("delete_message",
        "DANGEROUS (permission-gated): move one message to the Trash folder. It stays recoverable until the server purges Trash.",
        {"message_id": {"type": "integer"},
         "folder": {"type": "string"},
         "uid": {"type": "integer"}}),
    _fn("send_message",
        "DANGEROUS (permission-gated): send mail as the account owner - a new message, a reply, or reply-all. Replies get proper threading and a copy lands in Sent.",
        {"mode": {"type": "string", "enum": ["new", "reply", "reply_all"], "description": "default new"},
         "to": {"type": "string", "description": "recipient(s) for a new message; replies derive it from the original"},
         "subject": {"type": "string"},
         "body": {"type": "string"},
         "message_id": {"type": "integer", "description": "for reply modes"},
         "folder": {"type": "string"},
         "uid": {"type": "integer"}},
        ("body",)),
]


# ---------------------------------------------------------------- agent permissions
# Fine-grained, enforced boundaries for the assistant (docs/agent-permissions.md).
# (capability, label, risk, tools, description)

AGENT_CAPS = [
    ("classify", "Classify messages", "safe", ["classify_message"],
     "Run the classification pipeline on a message (same as the Classify button)."),
    ("flag", "Flag & read state", "caution", ["flag_message"],
     "Mark mail read/unread and starred."),
    ("tag", "Tag messages", "caution", ["tag_message"],
     "Apply short labels (recorded as assistant-sourced)."),
    ("move", "Move messages", "caution", ["move_message"],
     "Move mail between folders. Never deletes."),
    ("create_folder", "Create folders", "safe", ["create_folder"],
     "Create new (empty) folders."),
    ("classifiers", "Manage classifiers", "caution", ["train_classifier", "manage_classifier"],
     "Train, enable or delete heuristic classifiers."),
    ("rules", "Delete rules & flows", "caution",
     ["delete_rule", "delete_flow"],
     "Remove a rule or flow for good. Deletion needs your click by default; proposals to create stay one-click."),
    ("rules_toggle", "Pause or resume rules & flows", "caution",
     ["set_rule_enabled", "set_flow_enabled"],
     "Turn a rule or flow off or on without deleting anything."),
    ("draft", "Draft replies", "safe", ["draft_reply"],
     "Write a reply and save it to Drafts for review."),
    ("delete", "Delete messages", "dangerous", ["delete_message"],
     "Moves mail to Trash (recoverable until the server purges it)."),
    ("send", "Send mail", "dangerous", ["send_message"],
     "Send mail as the account owner, through the connected proxy."),
]
AGENT_CAP_OF_TOOL = {t: cap for cap, _l, _r, tools, _d in AGENT_CAPS for t in tools}
AGENT_PERM_LEVELS = ("off", "ask", "auto")
AGENT_ASK_DEFAULT = ("rules",)   # destructive rule/flow removals ask by default


def agent_permissions():
    """Effective permission level per capability (off | ask | auto)."""
    out = {}
    for cap, _label, risk, _tools, _desc in AGENT_CAPS:
        lvl = store.get_setting("perm_" + cap, None)
        if lvl not in AGENT_PERM_LEVELS:
            lvl = ("ask" if cap in AGENT_ASK_DEFAULT
                   else ("off" if risk == "dangerous" else "auto"))
        out[cap] = lvl
    # plugin capabilities: one per enabled plugin, keyed "plugin:<id>"; the
    # default level follows the plugin's declared tool side effects
    try:
        import plugins as _plugins
        for row in _plugins.list_rows(enabled_only=True):
            cap = "plugin:" + row["id"]
            lvl = store.get_setting("perm_" + cap, None)
            if lvl not in AGENT_PERM_LEVELS:
                lvl = _plugins.default_agent_level(row)
            out[cap] = lvl
    except Exception:
        pass
    return out


def agent_permissions_text():
    perms = agent_permissions()
    buckets = {"auto": [], "ask": [], "off": []}
    for cap, _label, _risk, _tools, _desc in AGENT_CAPS:
        buckets[perms[cap]].append(cap)
    for cap, lvl in perms.items():
        if cap.startswith("plugin:") and lvl in buckets:
            buckets[lvl].append(cap)
    parts = []
    if buckets["auto"]:
        parts.append("may do directly: " + ", ".join(buckets["auto"]))
    if buckets["ask"]:
        parts.append("requires the user's approval (pending-action card): " + ", ".join(buckets["ask"]))
    if buckets["off"]:
        parts.append("disabled: " + ", ".join(buckets["off"]))
    return "; ".join(parts) or "no action capabilities enabled"


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
        cs = joiner.join(
            ('matches plugin "%s"' % c.get("plugin")) if (c.get("op") or "") == "plugin"
            else ('%s %s "%s"' % (c.get("field"), c.get("op"), c.get("value")))
            for c in conds)
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
ALLOWED_OPS = ("contains", "equals", "regex", "plugin")


def _flow_cond_text(c):
    """Human line for one WHEN condition (deterministic or fuzzy)."""
    kind = (c.get("kind") or "field").lower()
    if kind == "category":
        s = 'AI category is "%s"' % c.get("value")
        if c.get("min_confidence"):
            s += " (>=%d%% trust)" % round(float(c["min_confidence"]) * 100)
        return s
    if kind == "topic":
        try:
            th = float(c.get("threshold") or _TOPIC_MIN_DEFAULT)
        except (TypeError, ValueError):
            th = _TOPIC_MIN_DEFAULT
        return 'is about "%s" (>=%.2f)' % (c.get("value"), th)
    if (c.get("op") or "").lower() == "plugin":
        return 'matches plugin "%s"' % c.get("plugin")
    return '%s %s "%s"' % (c.get("field"), c.get("op"), c.get("value"))


def _flow_when_text(flow):
    conds = _safe_json(flow.get("conditions"), [])
    joiner = " AND " if (flow.get("match_mode") or "all") == "all" else " OR "
    return joiner.join(_flow_cond_text(c) for c in conds) or "(no conditions)"


def _flow_steps_text(steps):
    """Human line for a list of flow steps (list of dicts)."""
    parts = []
    for st in (steps or []):
        t = (st.get("type") or "").lower()
        if t == "move":
            parts.append("move to %s" % st.get("folder"))
        elif t == "mark_read":
            parts.append("mark read")
        elif t == "flag":
            parts.append("star")
        elif t == "tag":
            parts.append('tag "%s"' % st.get("tag"))
        elif t == "draft":
            mode = (st.get("mode") or "template").lower()
            if mode == "plugin":
                parts.append('draft via plugin "%s" and save to Drafts' % st.get("plugin"))
            elif mode == "fixed":
                body = (st.get("body") or "").strip()
                parts.append('draft a fixed reply ("%s%s") and save to Drafts'
                             % (body[:40], "..." if len(body) > 40 else ""))
            elif mode == "llm":
                ins = (st.get("instructions") or "").strip()
                if ins:
                    parts.append('draft with the LLM guided by "%s%s" and save to Drafts'
                                 % (ins[:40], "..." if len(ins) > 40 else ""))
                else:
                    parts.append("draft with the LLM and save to Drafts")
            else:
                parts.append("draft from template #%s and save to Drafts" % st.get("template_id"))
    return ", then ".join(parts) or "(no steps)"


def _flow_brief(f):
    """UI/prompt-friendly snapshot of a flow."""
    return {"id": f.get("id"), "name": f.get("name") or ("flow %s" % f.get("id")),
            "enabled": bool(f.get("enabled")), "match_mode": f.get("match_mode") or "all",
            "conditions": _safe_json(f.get("conditions"), []),
            "steps": _safe_json(f.get("actions"), [])}


def _flows_to_text(flows):
    if not flows:
        return "(none yet)"
    lines = []
    for i, f in enumerate(flows, 1):
        conds = _safe_json(f.get("conditions"), [])
        steps = _safe_json(f.get("actions"), [])
        joiner = " AND " if (f.get("match_mode") or "all") == "all" else " OR "
        cs = joiner.join(_flow_cond_text(c) for c in conds)
        lines.append("%d. %s%s: IF %s -> %s"
                     % (i, "" if f.get("enabled") else "[disabled] ",
                        f.get("name") or ("flow %d" % f.get("id")), cs, _flow_steps_text(steps)))
    return "\n".join(lines)


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
        if op == "plugin":
            pid = str(c.get("plugin") or "").strip()
            if not pid:
                errors.append("plugin op needs a plugin id")
                continue
            import plugins as _plugins
            if not _plugins.get(pid):
                errors.append("unknown plugin %r (see the Plugins page)" % pid)
                continue
            conditions.append({"field": field or "subject", "op": "plugin", "plugin": pid})
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


def normalize_flow(proposal):
    """Validate an LLM-proposed flow; returns a clean dict or None."""
    return _validate_flow(proposal)[0]


def _validate_flow(proposal):
    """Validate a proposed multi-step flow. Returns (normalized | None, [errors])."""
    errors = []
    if not isinstance(proposal, dict):
        return None, ["proposal must be an object"]
    name = str(proposal.get("name") or "").strip()[:80] or "Assistant flow"
    mode = "any" if str(proposal.get("match_mode") or "").lower() == "any" else "all"
    conditions = []
    for c in proposal.get("conditions") or []:
        if not isinstance(c, dict):
            errors.append("each condition must be an object")
            continue
        kind = str(c.get("kind") or "field").lower()
        if kind == "category":
            cat = str(c.get("value") or "").strip()
            if not cat:
                errors.append('AI category condition needs the category name in "value"')
                continue
            cond = {"kind": "category", "value": cat[:60]}
            try:
                conf_ = float(c.get("min_confidence") or 0)
            except (TypeError, ValueError):
                conf_ = 0.0
            if conf_ > 0:
                cond["min_confidence"] = round(max(0.0, min(1.0, conf_)), 2)
            conditions.append(cond)
            continue
        if kind == "topic":
            desc = str(c.get("value") or "").strip()
            if not desc:
                errors.append('about (topic) condition needs a description in "value"')
                continue
            try:
                th = (float(c.get("threshold"))
                      if c.get("threshold") not in (None, "") else _TOPIC_MIN_DEFAULT)
            except (TypeError, ValueError):
                th = _TOPIC_MIN_DEFAULT
            conditions.append({"kind": "topic", "value": desc[:300],
                               "threshold": round(max(0.2, min(0.95, th)), 2)})
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
        if op == "plugin":
            pid = str(c.get("plugin") or "").strip()
            if not pid:
                errors.append("plugin op needs a plugin id")
                continue
            import plugins as _plugins
            if not _plugins.get(pid):
                errors.append("unknown plugin %r (see the Plugins page)" % pid)
                continue
            conditions.append({"field": field or "subject", "op": "plugin", "plugin": pid})
            continue
        if not value:
            errors.append("empty value for %s %s" % (field, op))
            continue
        conditions.append({"field": field, "op": op, "value": value[:300]})
    steps = []
    for st in proposal.get("steps") or []:
        if not isinstance(st, dict):
            errors.append("each step must be an object")
            continue
        kind = str(st.get("type") or "").lower()
        if kind == "move":
            folder = str(st.get("folder") or "").strip()
            if folder:
                steps.append({"type": "move", "folder": folder[:80]})
            else:
                errors.append("move step needs a folder")
        elif kind == "draft":
            dmode = (str(st.get("mode") or "fixed").lower())
            if dmode == "fixed":
                body = str(st.get("body") or "").strip()
                if body:
                    steps.append({"type": "draft", "mode": "fixed", "body": body[:4000]})
                else:
                    errors.append("fixed draft needs a body")
            elif dmode == "template":
                try:
                    tid = int(st.get("template_id") or 0)
                except (TypeError, ValueError):
                    tid = 0
                if tid and store.get_template(tid):
                    steps.append({"type": "draft", "mode": "template", "template_id": tid})
                else:
                    errors.append("template draft needs a valid template_id "
                                  "(see the templates page; or use mode \"fixed\" with a body)")
            elif dmode == "llm":
                tid = st.get("template_id") or None
                try:
                    tid = int(tid) if tid else None
                except (TypeError, ValueError):
                    tid = None
                step_ = {"type": "draft", "mode": "llm", "template_id": tid}
                ins = str(st.get("instructions") or "").strip()
                if ins:
                    step_["instructions"] = ins[:1000]
                steps.append(step_)
            elif dmode == "plugin":
                pid = str(st.get("plugin") or "").strip()
                import plugins as _plugins
                prow = _plugins.get(pid) if pid else None
                if not pid:
                    errors.append("plugin draft needs a plugin id")
                elif not prow:
                    errors.append("unknown plugin %r (see the Plugins page)" % pid)
                elif "draft-provider" not in (prow["manifest"].get("kind") or []):
                    errors.append("plugin %r is not a draft provider" % pid)
                else:
                    step_ = {"type": "draft", "mode": "plugin", "plugin": pid}
                    ins = str(st.get("instructions") or "").strip()
                    if ins:
                        step_["instructions"] = ins[:1000]
                    steps.append(step_)
            else:
                errors.append("bad draft mode %r (use fixed/template/llm/plugin)" % dmode)
        elif kind == "tag":
            tag = str(st.get("tag") or "").strip()
            if tag:
                steps.append({"type": "tag", "tag": tag[:40]})
            else:
                errors.append("tag step needs a tag")
        elif kind in ("mark_read", "flag"):
            steps.append({"type": kind})
        else:
            errors.append("unknown step type %r (use move/draft/tag/mark_read/flag)" % kind)
    if len(steps) > 10:
        steps = steps[:10]
        errors.append("only the first 10 steps are kept")
    norm = {"kind": "flow", "name": name, "match_mode": mode,
            "conditions": conditions, "steps": steps,
            "rationale": str(proposal.get("rationale") or "")[:300]}
    return norm, errors


def _assistant_context():
    settings = store.all_settings()
    with store.db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return (
        "CURRENT STATE\n"
        "Rules (top to bottom, first match wins):\n%s\n\n"
        "Flows (multi-step automations; run after rules, first matching flow wins):\n%s\n\n"
        "LLM classifier categories: %s\n"
        "Category → folder map: %s\n"
        "Watched folders: %s | check interval: %ss\n"
        "Indexed messages: %d | assistant permissions: %s\n"
        % (_rules_to_text(store.list_rules()),
           _flows_to_text(store.list_flows()),
           ", ".join(settings.get("categories") or []),
           ", ".join("%s=%s" % (k, v) for k, v in (settings.get("category_folders") or {}).items()) or "(none)",
           ", ".join(settings.get("watch_folders") or ["INBOX"]),
           settings.get("poll_interval", 90), total,
           agent_permissions_text())
    )


def assistant_page_context(path):
    """The page the user is looking at, for the assistant's system prompt.
    Returns (kind, short_desc, block, key). Never includes credentials. The key is
    a compact context id (message:3563, flow:15, ...) used by the drawer to scope
    chat sessions to the page."""
    path = (path or "").strip()[:300]
    p = path.split("?", 1)[0]
    q = path.split("?", 1)[1] if "?" in path else ""

    def filt_note():
        m2 = re.search(r"(?:^|&)f=([a-z_]+)", q)
        return (" (list filter: %s)" % m2.group(1)) if m2 else ""

    m = re.match(r"^/messages/(\d+)", p)
    if m:
        row = store.get_message(int(m.group(1)))
        if not row:
            return "message", "message #%s (no longer exists)" % m.group(1), "", "message:%s" % m.group(1)
        conf = row.get("llm_confidence")
        block = (
            "CURRENT PAGE: the user is reading ONE specific email right now%s.\n"
            "Message #%d\n"
            "Subject: %r | From: %s | To: %s | Date: %s | Folder: %s\n"
            "Status: %s | action_taken: %s | user tag: %r | needs_reply: %s | snoozed: %s\n"
            "LLM verdict: %r%s | summary: %r | reason: %r\n"
            "When the user says \u201cthis email\u201d, \u201cit\u201d or otherwise refers to something "
            "on screen, they mean THIS message. Load it by id %d when you need the body, and "
            "pass id %d to any tool that acts on it."
            % (filt_note(), row["id"], (row.get("subject") or "(no subject)"),
               row.get("from_addr") or "-", row.get("to_addr") or "-", row.get("date") or "-",
               row.get("folder") or "-", row.get("status") or "-", row.get("action_taken") or "-",
               row.get("user_tag") or "", "yes" if row.get("llm_needs_reply") else "no",
               time.strftime("%Y-%m-%d %H:%M", time.localtime(row["snoozed_until"])) if row.get("snoozed_until") else "no",
               row.get("llm_category") or "(not classified)",
               (" %.0f%%" % (conf * 100)) if conf is not None else "",
               row.get("llm_summary") or "", row.get("llm_reason") or "",
               row["id"], row["id"]))
        return "message", "message \u00b7 %s" % ((row.get("subject") or "(no subject)")[:70]), block, "message:%d" % row["id"]
    m = re.match(r"^/flows/(\d+)", p)
    if m:
        fl = store.get_flow(int(m.group(1)))
        if fl:
            block = ("CURRENT PAGE: the user is viewing one flow. When they say \u201cthis flow\u201d or "
                     "\u201cit\u201d, they mean flow #%d below - pass this id to flow tools when acting.\n\n%s"
                     % (fl["id"], _flows_to_text([fl])))
            return "flow", "flow \u00b7 %s" % ((fl.get("name") or ("#%d" % fl["id"]))[:70]), block, "flow:%d" % fl["id"]
    if p == "/flows/new":
        return ("flow", "new flow editor",
                "CURRENT PAGE: the user is in the NEW FLOW editor building a draft flow. A bare "
                "\u201cthis flow\u201d refers to that draft; they likely want help designing conditions "
                "and steps.", "flow:new")
    m = re.match(r"^/rules/(\d+)", p)
    if m:
        ru = store.get_rule(int(m.group(1)))
        if ru:
            block = ("CURRENT PAGE: the user is viewing one rule. When they say \u201cthis rule\u201d or "
                     "\u201cit\u201d, they mean rule #%d below - pass this id to rule tools when acting.\n\n%s"
                     % (ru["id"], _rules_to_text([ru])))
            return "rule", "rule \u00b7 %s" % ((ru.get("name") or ("#%d" % ru["id"]))[:70]), block, "rule:%d" % ru["id"]
    if p == "/rules/new":
        return ("rule", "new rule editor",
                "CURRENT PAGE: the user is in the NEW RULE editor. A bare \u201cthis rule\u201d refers to "
                "that draft.", "rule:new")
    m = re.match(r"^/classifiers/(\d+)/dataset", p)
    if m:
        h = store.get_heuristic(int(m.group(1)))
        if h:
            block = ("CURRENT PAGE: the user is on the DATASET REVIEW page for classifier #%d %r - "
                     "the exact training samples (positives plus sampled negatives), each with its "
                     "label source. Samples can be removed / re-included (applies to every future "
                     "training run) and reclassified with the dropdown (rewrites the underlying "
                     "label; the sample then moves in or out of the set)."
                     % (h["id"], h.get("name")))
            return "classifier", "dataset \u00b7 %s" % ((h.get("name") or ("#%d" % h["id"]))[:60]), block, "classifier:%d/dataset" % h["id"]
    m = re.match(r"^/classifiers/(\d+)", p)
    if m:
        h = store.get_heuristic(int(m.group(1)))
        if h:
            block = ("CURRENT PAGE: the user is looking at classifier #%d %r (kind %s, category %r, "
                     "enabled: %s). \u201cthis classifier\u201d means it."
                     % (h["id"], h.get("name"), h.get("kind"), h.get("category"),
                        "yes" if h.get("enabled") else "no"))
            return "classifier", "classifier \u00b7 %s" % ((h.get("name") or ("#%d" % h["id"]))[:70]), block, "classifier:%d" % h["id"]
    if p == "/templates/new":
        return ("template", "new template editor",
                "CURRENT PAGE: the user is creating a NEW reply template. A bare \u201cthis "
                "template\u201d refers to it.", "template:new")
    m = re.match(r"^/templates/(\d+)", p)
    if m:
        t2 = store.get_template(int(m.group(1)))
        if t2:
            body = (t2.get("body") or "")[:600]
            block = ("CURRENT PAGE: the user is editing reply template #%d %r. Its current text:\n%s"
                     % (t2["id"], t2.get("name"), body))
            return "template", "template \u00b7 %s" % ((t2.get("name") or ("#%d" % t2["id"]))[:70]), block, "template:%d" % t2["id"]
    if p == "/messages":
        try:
            n = store.count_messages((re.search(r"(?:^|&)f=([a-z_]+)", q) or [None, "all"])[1] or "all")
        except Exception:
            n = 0
        return ("messages", "messages list%s" % filt_note(),
                "CURRENT PAGE: the user is on the messages list%s (%d matching). No single message "
                "is selected. Rows show sender, subject with a one-line AI summary, tag and status "
                "badges; clicking a row opens the reader. The chips at the top pick the filter "
                "(All / needs reply / queued / moved / tagged / snoozed); the toolbar holds bulk "
                "actions (Tag, Untag, Classify selected, Classify all, Learn rules from tags); the "
                "pager controls page size." % (filt_note(), n), "messages")
    if p == "/simulate":
        return ("simulator", "simulator (dry run)",
                "CURRENT PAGE: the user is on the SIMULATOR \u2014 a dry-run page where a drafted "
                "email (fields From, To, Subject, Body) is pushed through rules, flows and the "
                "classifier without changing anything in the mailbox. The page can prefill the "
                "draft: the \u201cPrefill from\u201d dropdown picks a flow or rule and \u201cGenerate "
                "an example draft\u201d writes a matching example; /simulate?flow=<id> and "
                "/simulate?rule=<id> prefill the same way. When the user asks for help filling the "
                "fields or testing a flow/rule (e.g. \u201ctest the PhD flow\u201d): find that "
                "flow/rule first (list_flows / list_rules), then either give them concrete "
                "From/To/Subject/Body values that would exercise it, or point them at the prefill: "
                "a link like [Test \u201c<name>\u201d \u2192](/simulate?flow=<id>), or the Prefill "
                "from dropdown + \u201cGenerate an example draft\u201d. The draft is test input: do "
                "NOT search the mailbox, drafts or messages, and do not ask which email they mean. "
                "Reply with the draft values or a line of guidance, not a play-by-play of tools.",
                "page:simulate")
    if p == "/":
        try:
            _nr = store.count_messages("needs_reply")
            _q = store.count_messages("queued")
        except Exception:
            _nr = _q = 0
        return ("dashboard", "dashboard",
                "CURRENT PAGE: the user is on the DASHBOARD. Top to bottom: the system status strip "
                "(Triage / Proxy / Search index / LLM, expandable on phones); the needs-reply hero "
                "count; triage metrics (sorted by rules / LLM classified / rules / flows / "
                "classifiers) with the live-mode chips; recent filings - machine moves with Undo "
                "buttons; the Recent mail feed (newest first, click a row to open it); the Search "
                "index card (Index now / Rebuild) and the Activity feed (Full log link). Buttons: "
                "Check now, Open messages. Right now: %d need a reply, %d waiting for the LLM."
                % (_nr, _q), "page:dashboard")
    if p == "/rules":
        try:
            _n = len(store.list_rules())
        except Exception:
            _n = 0
        return ("page", "rules list",
                "CURRENT PAGE: the user is on the RULES page - the ordered list of deterministic "
                "rules, checked top to bottom, first match wins. Rows show each rule's conditions "
                "and actions in plain words with enable/disable, edit, delete and move-to-top "
                "controls; guard rules (no actions) are labelled; there is a rule tester box: type "
                "a sample sender/subject and it shows which rule would catch it. New rule opens "
                "the editor. Rules act on live mail. There are %d rule(s) right now." % _n,
                "page:rules")
    if p == "/flows":
        try:
            _n = len(store.list_flows())
        except Exception:
            _n = 0
        return ("page", "flows list",
                "CURRENT PAGE: the user is on the FLOWS page - multi-step automations that run "
                "after rules (first matching flow wins). Each flow card shows its WHEN filters "
                "and its steps in plain English; controls: enable/disable, edit (the canvas "
                "builder), delete, and a test-it-in-the-simulator link. New flow opens the "
                "builder. There are %d flow(s) right now." % _n, "page:flows")
    if p == "/classifiers":
        return ("page", "classifiers list",
                "CURRENT PAGE: the user is on the CLASSIFIERS page - the deep management list of "
                "fast-path classifiers (small deterministic models trained from tags or the AI's "
                "verdicts; they answer BEFORE the LLM). Columns: name, kind, category, samples, "
                "labels, matches, updated. Row controls: toggle, retrain, open the dataset "
                "review, delete. The Learning page is the friendlier overview of the same "
                "models.", "page:classifiers")
    if p == "/templates":
        return ("page", "templates list",
                "CURRENT PAGE: the user is on the TEMPLATES page - reusable reply templates "
                "(name + body; placeholders allowed) used by flows' draft steps and the message "
                "viewer's draft button. Row actions: edit, delete; New template opens the "
                "editor.", "page:templates")
    if p == "/settings":
        try:
            _modes = "rules %s / LLM %s / auto-filing %s" % (
                "live" if store.get_setting("rules_apply", True) else "dry-run",
                "on" if store.get_setting("llm_suggest", True) else "off",
                "ON" if store.get_setting("llm_apply") else "off")
        except Exception:
            _modes = ""
        return ("page", "settings",
                "CURRENT PAGE: the user is on SETTINGS - task-grouped cards (Mailbox; Sorting & "
                "classification; Filing & drafts; Search; General; Status), each saved on its "
                "own. Here live the LLM endpoint + thinking mode, categories and the category-to-"
                "folder map, watch folders and poll interval, auto-filing (llm_apply), the "
                "search backend, display timezone and the AGENT PERMISSIONS. The Status card has "
                "Test buttons (LLM / embeddings / rerank). Currently %s." % _modes,
                "page:settings")
    if p == "/accounts" or p.startswith("/accounts/"):
        return ("page", "accounts",
                "CURRENT PAGE: the user is on ACCOUNTS - the mail connection page: a proxy "
                "status strip (listener ports, log link, restart) and one card per mail account "
                "showing its sign-in state with Authorise / Re-authorise buttons, a More menu "
                "(reset tokens, delete) and a reading badge on the account in use. This is the "
                "fix for \u201clogin failed\u201d or expired tokens - point at the buttons; "
                "never handle or ask for credentials.", "page:accounts")
    if p == "/log":
        return ("page", "activity log",
                "CURRENT PAGE: the user is on the LOG page - the app's event feed, newest first, "
                "with level badges (info / warn / error), a text filter and a minutes window, "
                "and a pauseable live refresh. The place to look for \u201cwhy did this "
                "happen\u201d or error questions.", "page:log")
    if p == "/proxy/log":
        return ("page", "proxy log",
                "CURRENT PAGE: the user is on the RAW PROXY LOG - the embedded mail proxy's own "
                "output for connection-level debugging (IMAP / OAuth). Relevant when accounts "
                "show sign-in trouble.", "page:proxy/log")
    if p == "/welcome":
        return ("page", "welcome page",
                "CURRENT PAGE: the welcome page - a full-screen first-run setup wizard with a step "
                "rail (Mailbox, LLM, Search index) and intro/finish bookends. Steps auto-detect "
                "state; the LLM step edits the same settings as the Settings page and offers a "
                "connection test; the index step starts the real indexer and shows live counts. "
                "Help the user through setup; hardware guidance for choosing an LLM sits on this "
                "page and in docs/getting-started.md.",
                "page:welcome")
    if p.startswith("/plugins/"):
        return ("page", "plugin detail",
                "CURRENT PAGE: the plugin detail view of one plugin (%s) - what it does, its "
                "access permissions in plain language, the assistant permission (off / ask / "
                "auto), pipeline opt-ins, its settings and recent activity. The toggle in the "
                "header switches it on or off. When enabled and permitted, its tools appear in "
                "your inventory as plugin__<id>__<tool>."
                % p.rsplit("/", 1)[-1][:60], "page:plugin")
    if p == "/plugins":
        try:
            import plugins as _plugins
            _np = len(_plugins.list_rows())
            _en = len(_plugins.list_rows(enabled_only=True))
        except Exception:
            _np = _en = 0
        return ("page", "plugins page",
                "CURRENT PAGE: the user is on the PLUGINS page - a list of sandboxed extensions "
                "(classifiers, tools, integrations) with an on/off toggle per row; clicking a row "
                "opens its detail page (access permissions in plain language, assistant permission "
                "off/ask/auto, pipeline opt-ins, settings, activity). Your plugin tools are named "
                "plugin__<plugin-id>__<tool> and are gated by each plugin's assistant permission. "
                "Rescan re-reads the plugins directory; user plugins go in the app data dir under "
                "plugins/. Plugins are read-only over mail: they act only by proposing cards or "
                "through their own tools. There are %d plugin(s) installed, %d enabled."
                % (_np, _en), "page:plugins")
    if p == "/more":
        return ("page", "more",
                "CURRENT PAGE: the user is on the MORE page - the directory of all sections "
                "(Rules, Simulator, Flows, Learning, Templates, Accounts, Log, Settings) with a "
                "one-line description of each; the user lands here to find a page by name.",
                "page:more")
    if p == "/learning":
        try:
            _rep = learning.status_report()
            _cl = _rep.get("classifiers") or []
            _bits = ["%d fast-path(s), %d deciding live"
                     % (len(_cl), sum(1 for c in _cl if c.get("status") == "live"))] if _cl else []
            _sh = [s2 for s2 in (_rep.get("specialists") or []) if s2.get("status") == "shadow"]
            if _sh:
                _bits.append("%d learner(s) watching" % len(_sh))
            _pr = ((_rep.get("eval") or {}).get("progress") or {})
            _bits.append(("test set %d/%d labeled" % (_pr.get("done", 0), _pr.get("total", 0)))
                         if _pr.get("total") else "no test set yet")
            _brief = " Right now: " + "; ".join(_bits) + "."
        except Exception:
            _brief = ""
        return ("page", "learning",
                "CURRENT PAGE: the user is on the LEARNING page - the home of the small models "
                "that handle mail so the AI does not have to. Sections: \u201cWorking on your "
                "mail\u201d (cards for fast-paths - status deciding live / paused, self-check "
                "accuracy, examples learned - and for learners - watching quietly / live, "
                "agreement with the AI; controls: pause, retrain, review dataset, let it take "
                "over, retire); \u201cTest sets\u201d (build a ~50-email set the user labels by "
                "hand - the only truth-based score); \u201cThe newest learner\u201d (its "
                "lifecycle steps and recorded checks).%s" % _brief, "page:learning")
    if p == "/learning/eval":
        try:
            _pg = learning.eval_progress()
        except Exception:
            _pg = {}
        return ("page", "test-set labeling",
                "CURRENT PAGE: the user is on the TEST-SET LABELING page - one real email at a "
                "time, blind (no model's opinion is shown), and the user answers 1-2 questions "
                "per message (needs reply? category?; \u201cnot sure\u201d is allowed). "
                "Progress: %d of %d labeled. These hand answers are the frozen benchmark every "
                "model is scored against." % (_pg.get("done", 0), _pg.get("total", 0)),
                "page:learning/eval")
    if p:
        return ("page", (p.rstrip("/")[:60] or "/"),
                "CURRENT PAGE: the user is on %s. No specific item is selected." % p,
                "page:" + p.strip("/"))
    return "", "", "", ""


def _repetition_loop(text, tail=200):
    """True when the last `tail` chars already appeared earlier in the same turn -
    catches the degenerate \"keeps repeating the same paragraph\" failure mode
    before it eats the whole token budget."""
    if len(text) < tail * 2 + 40:
        return False
    probe = text[-tail:]
    return probe in text[:-tail]


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

    def __init__(self, session_id=0, page_path=None):
        self.mc = None
        self.session_id = int(session_id or 0)
        self.page_path = (page_path or "").strip()[:300]
        self.proposals = []
        self.tools_log = []
        self.perms = agent_permissions()
        self.pending_actions = []
        # back-compat only: True when any folder/flag action runs live
        self.actions_apply = any(self.perms.get(k) == "auto"
                                 for k in ("move", "flag", "create_folder"))
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

    def call_tool(self, name, args, approved=False):
        fn = getattr(self, "_tool_" + str(name or ""), None)
        plugin_ref = None
        if fn is None:
            try:
                import plugins as _plugins
                plugin_ref = _plugins.split_tool_name(name)
            except Exception:
                plugin_ref = None
        if fn is None and not plugin_ref:
            return {"ok": False, "summary": "unknown tool %r" % name,
                    "result": {"error": "unknown tool",
                               "available": [t["function"]["name"] for t in ASSISTANT_TOOLS]}}
        args = args if isinstance(args, dict) else {}
        cap = AGENT_CAP_OF_TOOL.get(str(name))
        if cap is None and plugin_ref:
            cap = "plugin:" + plugin_ref[0]
        row_id = None
        if cap and not approved:
            lvl = self.perms.get(cap, "auto")
            if lvl == "off":
                store.log_event("info", "agent: %s denied - '%s' is off in Agent permissions" % (name, cap))
                return {"ok": False, "permission_denied": cap,
                        "summary": "%s is disabled (Agent permissions: %s = off)" % (name, cap),
                        "result": {"error": "permission_denied", "capability": cap, "level": "off",
                                   "note": ("This capability is switched off in Settings - AI settings - "
                                            "Agent permissions. Tell the user; do not retry.")}}
            if lvl == "ask":
                pending = self._make_pending(cap, name, args)
                if pending.get("error"):
                    return {"ok": False, "summary": pending["error"],
                            "result": {"error": pending["error"]}}
                self.pending_actions.append(pending)
                store.log_event("info", "agent: %s pending approval [action %s] %s"
                                % (name, pending["id"], pending["preview"]))
                return {"ok": True, "pending_approval": True, "action_id": pending["id"],
                        "summary": "waiting for approval: " + pending["preview"],
                        "result": {"pending_approval": True, "action_id": pending["id"],
                                   "preview": pending["preview"],
                                   "note": ("This action needs the user's approval - it is queued on the "
                                            "Assistant page. Tell the user it is waiting; never claim it happened.")}}
            row_id = store.add_agent_action(cap, name, "", {"tool": name, "args": args},
                                            session_id=self.session_id)
        try:
            if plugin_ref:
                import plugin_rt
                out = plugin_rt.runtime.invoke(plugin_ref[0], plugin_ref[1], args,
                                               session_id=self.session_id)
                out = dict(out or {})
            else:
                out = fn(args)
        except Exception as exc:
            if row_id:
                store.set_agent_action(row_id, "failed", json.dumps({"error": repr(exc)}))
            return {"ok": False, "summary": "tool %s failed: %r" % (name, exc),
                    "result": {"error": repr(exc)}}
        out.setdefault("ok", True)
        out.setdefault("summary", str(name))
        out.setdefault("result", {})
        if row_id:
            store.set_agent_action(row_id, "applied" if out.get("ok") else "failed",
                                   json.dumps({"summary": out.get("summary"), "ok": bool(out.get("ok"))},
                                              ensure_ascii=False))
            out["action_id"] = row_id
            if out.get("ok"):
                store.log_event("info", "agent: %s applied [action %s] %s"
                                % (name, row_id, out.get("summary")))
        return out

    def _make_pending(self, cap, name, args):
        preview, resolved = self._pending_preview(name, args)
        if preview is None:
            return {"error": resolved}
        aid = store.add_agent_action(cap, name, preview,
                                     {"tool": name, "args": args, "resolved": resolved},
                                     session_id=self.session_id)
        return {"id": aid, "capability": cap, "tool": name, "preview": preview, "args": args}

    def _pending_preview(self, name, args):
        """-> (human preview | None, resolved info | error text)."""
        pr = None
        try:
            import plugins as _plugins
            pr = _plugins.split_tool_name(name)
        except Exception:
            _plugins = None
        if pr and _plugins:
            row = _plugins.get(pr[0])
            pname = ((row or {}).get("manifest") or {}).get("name") or pr[0]
            return ("Run plugin tool '%s' (%s)" % (pr[1], pname),
                    {"plugin": pr[0], "tool": pr[1], "args": args})
        if name == "create_folder":
            nm = (args.get("name") or "").strip()
            return (("Create folder '%s'" % nm), {"name": nm}) if nm else (None, "name is required")
        if name == "train_classifier":
            return ("Train classifier '%s' (%s, category %s)"
                    % (args.get("name") or "?", args.get("kind") or "decision_list",
                       args.get("category") or "?"), dict(args))
        if name == "manage_classifier":
            return ("Classifier %s #%s" % (args.get("action") or "?", args.get("id") or "?"), dict(args))
        if name == "send_message":
            mode = (args.get("mode") or "new").strip().lower()
            if mode == "new":
                to = (args.get("to") or "").strip()
                if not to:
                    return None, "to is required"
                return ("SEND new mail to %s: '%s'"
                        % (to, _truncate(args.get("subject") or "(no subject)", 60)), dict(args))
        if name in ("delete_flow", "set_flow_enabled"):
            try:
                fid = int(args.get("flow_id") or 0)
            except (TypeError, ValueError):
                fid = 0
            flow = store.get_flow(fid) if fid else None
            if not flow:
                return None, "flow %s not found (use list_flows)" % args.get("flow_id")
            if name == "delete_flow":
                return "Delete flow #%d '%s'" % (fid, flow["name"]), {"flow_id": fid}
            en = _as_bool(args.get("enabled"))
            return ("%s flow #%d '%s'" % ("Enable" if en else "Disable", fid, flow["name"]),
                    {"flow_id": fid, "enabled": bool(en)})
        if name in ("delete_rule", "set_rule_enabled"):
            try:
                rid = int(args.get("rule_id") or 0)
            except (TypeError, ValueError):
                rid = 0
            rule = store.get_rule(rid) if rid else None
            if not rule:
                return None, "rule %s not found (use list_rules)" % args.get("rule_id")
            if name == "delete_rule":
                return "Delete rule #%d '%s'" % (rid, rule["name"]), {"rule_id": rid}
            en = _as_bool(args.get("enabled"))
            return ("%s rule #%d '%s'" % ("Enable" if en else "Disable", rid, rule["name"]),
                    {"rule_id": rid, "enabled": bool(en)})
        row, folder, uid, err = self._resolve_message(args)
        if err:
            return None, err
        label = _truncate((row or {}).get("subject") or ("message %s" % (args.get("message_id") or uid)), 60)
        base = {"folder": folder, "uid": uid, "message_id": (row or {}).get("id"), "label": label}
        if name == "move_message":
            return "Move '%s' to %s" % (label, (args.get("target_folder") or "?").strip() or "?"), base
        if name == "flag_message":
            bits = []
            seen = _as_bool(args.get("seen"))
            flg = _as_bool(args.get("flagged"))
            if seen is not None:
                bits.append("mark read" if seen else "mark unread")
            if flg is not None:
                bits.append("star" if flg else "unstar")
            return "%s '%s'" % (" + ".join(bits) or "Update flags on", label), base
        if name == "tag_message":
            if _as_bool(args.get("add")) is False:
                return "Remove tag from '%s'" % label, base
            return "Tag '%s' as '%s'" % (label, args.get("tag") or "?"), base
        if name == "draft_reply":
            return "Draft a reply to '%s' and save it to Drafts" % label, base
        if name == "delete_message":
            return "Move '%s' to Trash" % label, base
        if name == "classify_message":
            return "Classify '%s'" % label, base
        if name == "send_message":
            return "SEND reply to '%s': '%s'" % (label, _truncate(args.get("subject") or "(original subject)", 60)), base
        return name, dict(args)

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
            "assistant_actions_live": self.actions_apply, "assistant_permissions": self.perms,
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
        # opt-in retriever plugins re-rank the candidates (first opt-in wins)
        try:
            import plugins as _plugins
            import plugin_rt
            for _pid in (store.get_setting("plugin_retrievers", []) or []):
                _prow = _plugins.get(_pid)
                if not _prow or not _prow.get("enabled"):
                    continue
                _cands = []
                for _it in items:
                    _mrow = store.get_message(_it["message_id"])
                    _cands.append({
                        "id": _it["message_id"], "subject": _it["subject"], "from": _it["from"],
                        "snippet": _it["excerpt"],
                        "tags": ([_mrow["user_tag"]] if _mrow and _mrow.get("user_tag") else []),
                        "needs_reply": bool(_mrow.get("llm_needs_reply")) if _mrow else False})
                _ranked = plugin_rt.retriever(_pid, {"query": q, "candidates": _cands})
                if _ranked and _ranked.get("ids"):
                    _order = {mid: _i for _i, mid in enumerate(_ranked["ids"])}
                    items.sort(key=lambda _it: _order.get(_it["message_id"], len(_order)))
                    note = ((note + "; ") if note else "") + "re-ranked by plugin %s" % _pid
                break
        except Exception:
            pass
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
        reloc = self._relocate(mc, row, first=folder)
        if reloc:
            if row is None:
                row = store.find_message_by_uid(reloc[0], reloc[1])
            elif (row.get("folder"), row.get("uid")) != (reloc[0], reloc[1]):
                # self-heal, same as the message viewer: a moved message usually
                # sits in the SAME folder under a NEW uid (the counters drift)
                store.update_message(row["id"], folder=reloc[0], uid=reloc[1])
                row = dict(row)
                row["folder"], row["uid"] = reloc[0], reloc[1]
            return row, reloc[0], reloc[1], None
        return row, folder, uid, ("message uid %s is not in %r — it may have been moved or "
                                  "deleted; search_mail (live) or search_messages finds it if "
                                  "it still exists" % (uid, folder))

    def _relocate(self, mc, row, first=None):
        """Locate a message by Message-ID: the stored folder first (a moved
        message usually sits there under a NEW uid), then every other folder."""
        msgid = (row or {}).get("msgid") or ""
        if not msgid:
            return None
        needle = '"<%s>"' % msgid.strip().strip("<>")
        seen, order = set(), []
        for f in ([first] if first else []) + sorted(mc.folders()):
            if f and f not in seen:
                seen.add(f)
                order.append(f)
        for folder in order:
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
        mc = self._mail()
        try:
            mc.ensure_folder(target)
            mc.ensure_selected(folder)
            if row:
                store.record_move(row, target, "assistant", from_folder=folder)
            new_uid = mc.move(uid, target, msgid=(row or {}).get("msgid"))
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

    def _tool_delete_rule(self, a):
        try:
            rid = int(a.get("rule_id") or 0)
        except (TypeError, ValueError):
            rid = 0
        rule = store.get_rule(rid) if rid else None
        if not rule:
            return {"ok": False,
                    "summary": "no rule with id %s (call list_rules for the ids)" % a.get("rule_id"),
                    "result": {"error": "rule_not_found"}}
        store.delete_rule(rid)
        store.log_event("info", "assistant deleted rule #%d '%s'" % (rid, rule["name"]))
        return {"ok": True, "summary": "deleted rule #%d '%s'" % (rid, rule["name"]),
                "result": {"deleted": {"id": rid, "name": rule["name"],
                                       "conditions": _safe_json(rule["conditions"], []),
                                       "actions": _safe_json(rule["actions"], {})}}}

    def _tool_set_rule_enabled(self, a):
        try:
            rid = int(a.get("rule_id") or 0)
        except (TypeError, ValueError):
            rid = 0
        rule = store.get_rule(rid) if rid else None
        if not rule:
            return {"ok": False,
                    "summary": "no rule with id %s (call list_rules for the ids)" % a.get("rule_id"),
                    "result": {"error": "rule_not_found"}}
        en = _as_bool(a.get("enabled"))
        if en is None:
            return {"ok": False, "summary": "enabled (true/false) is required",
                    "result": {"error": "enabled is required"}}
        store.update_rule(rid, enabled=1 if en else 0)
        store.log_event("info", "assistant %s rule #%d '%s'"
                        % ("enabled" if en else "disabled", rid, rule["name"]))
        return {"ok": True,
                "summary": "%s rule #%d '%s'" % ("enabled" if en else "disabled", rid, rule["name"]),
                "result": {"rule": {"id": rid, "name": rule["name"], "enabled": bool(en)}}}

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

    def _tool_list_flows(self, a):
        flows = store.list_flows()
        items = []
        for f in flows:
            b = _flow_brief(f)
            b["position"] = f.get("position")
            b["steps_text"] = _flow_steps_text(b["steps"])
            items.append(b)
        return {"ok": True, "summary": "%d flow(s)" % len(items),
                "result": {"flows": items, "text": _flows_to_text(flows)}}

    def _tool_propose_flow(self, a):
        norm, errors = _validate_flow(a)
        if not norm or not norm["conditions"] or not norm["steps"]:
            if norm and not norm["conditions"]:
                errors.append("at least one WHEN condition with a value is required")
            if norm and not norm["steps"]:
                errors.append("at least one THEN step is required")
            return {"ok": False, "summary": "flow invalid: " + "; ".join(errors[:3]),
                    "result": {"errors": errors,
                               "hint": ("A flow needs 1-4 conditions and 1-10 ordered steps "
                                        "(move/draft/tag/mark_read/flag). Example: conditions "
                                        "[{field:\"from\", op:\"contains\", value:\"x@y.com\"}], steps "
                                        "[{type:\"move\", folder:\"Personal\"}, {type:\"draft\", "
                                        "mode:\"fixed\", body:\"Thank you for your email...\"}]. Fix and "
                                        "propose again.")}}
        target_id = a.get("updates_flow_id")
        if target_id not in (None, "", 0):
            try:
                target_id = int(target_id)
            except (TypeError, ValueError):
                target_id = None
        if target_id:
            target = store.get_flow(target_id)
            if target is None:
                return {"ok": False, "summary": "no flow #%s to update" % target_id,
                        "result": {"errors": ["updates_flow_id %s does not exist" % target_id],
                                   "hint": "Call list_flows to see the current flow ids."}}
            norm["updates_flow_id"] = target_id
            norm["updates_flow"] = _flow_brief(target)
        self.proposals = [p for p in self.proposals
                          if (p.get("name") or "").lower() != (norm.get("name") or "").lower()
                          or (p.get("kind") or "rule") != "flow"]
        self.proposals.append(norm)
        result = {"status": "queued for the user's one-click approval",
                  "proposal_index": len(self.proposals) - 1, "flow": norm,
                  "steps_text": _flow_steps_text(norm["steps"])}
        if norm.get("updates_flow"):
            summary = ("flow proposed as an UPDATE of #%s '%s'"
                       % (target_id, norm["updates_flow"]["name"]))
        else:
            summary = "flow proposed: %s (%s)" % (norm["name"], _flow_steps_text(norm["steps"]))
        return {"ok": True, "summary": summary, "result": result}

    def _tool_delete_flow(self, a):
        try:
            fid = int(a.get("flow_id") or 0)
        except (TypeError, ValueError):
            fid = 0
        flow = store.get_flow(fid) if fid else None
        if not flow:
            return {"ok": False,
                    "summary": "no flow with id %s (call list_flows)" % a.get("flow_id"),
                    "result": {"error": "flow_not_found"}}
        store.delete_flow(fid)
        store.log_event("info", "assistant deleted flow #%d '%s'" % (fid, flow["name"]))
        return {"ok": True, "summary": "deleted flow #%d '%s'" % (fid, flow["name"]),
                "result": {"deleted": {"id": fid, "name": flow["name"]}}}

    def _tool_set_flow_enabled(self, a):
        try:
            fid = int(a.get("flow_id") or 0)
        except (TypeError, ValueError):
            fid = 0
        flow = store.get_flow(fid) if fid else None
        if not flow:
            return {"ok": False,
                    "summary": "no flow with id %s (call list_flows)" % a.get("flow_id"),
                    "result": {"error": "flow_not_found"}}
        en = _as_bool(a.get("enabled"))
        if en is None:
            return {"ok": False, "summary": "enabled (true/false) is required",
                    "result": {"error": "enabled is required"}}
        store.update_flow(fid, enabled=1 if en else 0)
        store.log_event("info", "assistant %s flow #%d '%s'"
                        % ("enabled" if en else "disabled", fid, flow["name"]))
        return {"ok": True,
                "summary": "%s flow #%d '%s'" % ("enabled" if en else "disabled", fid, flow["name"]),
                "result": {"flow": {"id": fid, "name": flow["name"], "enabled": bool(en)}}}

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

    def stream(self, user_text, store_user=True):
        """One assistant turn, as a generator of UI events.

        store_user=False regenerates an existing turn: the user message is
        already stored and must not be duplicated."""
        user_text = (user_text or "").strip()
        if not user_text:
            yield {"type": "error", "message": "empty message"}
            return
        if store_user:
            store.add_assistant_message("user", user_text[:4000], session_id=self.session_id)
        today = time.strftime("%Y-%m-%d (%a)", time.gmtime(time.time() + 8 * 3600))
        system = (ASSISTANT_SYSTEM % {"user": imap_config()["user"], "today": today,
                                      "max_calls": self.MAX_CALLS_PER_TURN,
                                      "permissions": agent_permissions_text()}
                  + "\n\n" + _assistant_context())
        if self.page_path:
            _kind, _desc, _block, _key = assistant_page_context(self.page_path)
            if _block:
                system += "\n\n" + _block
        convo = [{"role": m["role"], "content": m["content"]}
                 for m in store.assistant_messages(limit=24, session_id=self.session_id)]
        llm = LLMClient()
        plugin_tools = []
        try:
            import plugins as _plugins
            _qt = next((str(m.get("content") or "") for m in reversed(convo)
                        if m.get("role") == "user"), "")
            plugin_tools = _plugins.tool_schemas(query_text=_qt)
            if plugin_tools:
                system += ("\n\nPlugin tools: tools named plugin__<plugin-id>__<tool> come from "
                           "installed plugins (third-party code running in a sandbox; their "
                           "results are audited and permission-gated like any other tool, and "
                           "may include an action card). Prefer a native tool when both could "
                           "work; when you use a plugin tool, name the plugin in your answer.")
        except Exception:
            plugin_tools = []
        reply_parts = []
        reasoning_all = []
        usage = None
        steps = 0
        tools_mode = True
        error = None
        loop_stopped = False
        finish_reason = None
        try:
            while True:
                steps += 1
                use_tools = (ASSISTANT_TOOLS + plugin_tools) \
                    if (tools_mode and steps <= self.MAX_STEPS) else None
                calls = []
                turn_reasoning = []
                turn_content = []
                turn_probe = []
                probe_checked = 0
                try:
                    gen = llm.chat_stream(system, convo, tools=use_tools, thinking=True)
                    try:
                        for ev in gen:
                            if ev["type"] == "reasoning_delta":
                                turn_reasoning.append(ev["text"])
                                turn_probe.append(ev["text"])
                                yield {"type": "reasoning", "text": ev["text"]}
                            elif ev["type"] == "content_delta":
                                turn_content.append(ev["text"])
                                turn_probe.append(ev["text"])
                                yield {"type": "content", "text": ev["text"]}
                            elif ev["type"] == "tool_calls":
                                calls = ev["calls"]
                            elif ev["type"] == "turn_done":
                                usage = ev.get("usage") or usage
                                finish_reason = ev.get("finish_reason") or finish_reason
                            if ev["type"] in ("reasoning_delta", "content_delta"):
                                joined = "".join(turn_probe)
                                if len(joined) - probe_checked >= 64:
                                    probe_checked = len(joined)
                                    if _repetition_loop(joined):
                                        loop_stopped = True
                                        store.log_event("warn", "assistant: degenerate repetition "
                                                        "detected - turn stopped early")
                                        break
                    finally:
                        try:
                            gen.close()
                        except Exception:
                            pass
                except Exception as exc:
                    if use_tools and _looks_like_tools_unsupported(exc):
                        store.log_event("info", "assistant: the model rejected tools (%s) — "
                                        "continuing without them" % exc)
                        tools_mode = False
                        continue
                    raise
                reasoning_all.append("".join(turn_reasoning))
                if loop_stopped:
                    break
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
                        card = res.get("card") if isinstance(res.get("card"), dict) else None
                        self.tools_log.append({"name": c["name"],
                                               "args": _truncate(json.dumps(args, ensure_ascii=False), 300),
                                               "ok": bool(res.get("ok")),
                                               "summary": _truncate(res.get("summary") or "", 300),
                                               "dry_run": bool(res.get("dry_run")),
                                               "pending": bool(res.get("pending_approval")),
                                               "elapsed": res["elapsed"]})
                        yield {"type": "tool_end", "id": c["id"], "name": c["name"],
                               "ok": bool(res.get("ok")),
                               "summary": _truncate(res.get("summary") or "", 400),
                               "dry_run": bool(res.get("dry_run")),
                               "pending": bool(res.get("pending_approval")),
                               "card": card, "elapsed": res["elapsed"]}
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
            if loop_stopped:
                reply = ("I caught myself going in circles and stopped before doing anything — nothing was "
                         "changed. Try phrasing it differently, or split it into smaller asks.")
            elif self.proposals:
                reply = "Proposed %d rule(s) — add them below, or ask for changes." % len(self.proposals)
            elif finish_reason == "length":
                reply = ("That turn ran out of room while I was still working it out — nothing was changed. "
                         "Try again with a more direct request.")
            else:
                reply = "(no reply)"
        reasoning_text = "\n".join(r for r in reasoning_all if r)
        thought_summary = self._summarize_thoughts(reasoning_text) if reasoning_text else ""
        meta = {"reasoning": _truncate(reasoning_text, 20000),
                "reasoning_summary": thought_summary,
                "tools": self.tools_log, "steps": steps, "usage": usage,
                "loop_stopped": loop_stopped, "finish_reason": finish_reason,
                "actions_live": self.actions_apply, "permissions": self.perms}
        msg_id = store.add_assistant_message("assistant", reply[:4000],
                                             proposals=json.dumps(self.proposals),
                                             meta=json.dumps(meta, ensure_ascii=False),
                                             session_id=self.session_id)
        if self.proposals:
            yield {"type": "proposals", "proposals": self.proposals}
        if self.pending_actions:
            yield {"type": "action_proposals", "actions": self.pending_actions}
        yield {"type": "done", "message_id": msg_id, "reply": reply, "steps": steps}
        if thought_summary:
            # after done so the final answer never waits on the summary call
            yield {"type": "thought_summary", "text": thought_summary}

    # ---- fine-grained permission tools

    def _tool_classify_message(self, a):
        row, folder, uid, err = self._resolve_message(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        if not row:
            return {"ok": False, "summary": "the message is not in the local index yet",
                    "result": {"error": "not_indexed"}}
        msg = store.get_message(row["id"])
        if not msg:
            return {"ok": False, "summary": "message not found", "result": {"error": "not_found"}}
        try:
            res = classify_and_store(msg, store.all_settings())
        except Exception as exc:
            return {"ok": False, "summary": "classification failed: %r" % exc, "result": {"error": repr(exc)}}
        fresh = store.get_message(row["id"]) or {}
        moved = str(res.get("_moved_to") or "")
        store.log_event("info", "assistant classified msg %s ('%s') -> %s%s"
                        % (row["id"], _truncate(row.get("subject") or "", 40), res.get("category"),
                           (" [filed to %s]" % moved) if moved else ""))
        return {"ok": True,
                "summary": "classified as %s%s" % (res.get("category"), (" [filed to %s]" % moved) if moved else ""),
                "result": {"message_id": row["id"], "category": res.get("category"),
                           "confidence": res.get("confidence"),
                           "summary": (fresh.get("llm_summary") or "")[:200],
                           "needs_reply": bool(fresh.get("llm_needs_reply")),
                           "moved_to": moved}}

    def _tool_tag_message(self, a):
        tag = (a.get("tag") or "").strip()[:40]
        add = _as_bool(a.get("add"))
        if add is None:
            add = True
        if add and not tag:
            return {"ok": False, "summary": "tag is required", "result": {"error": "tag is required"}}
        row, folder, uid, err = self._resolve_message(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        if not row:
            return {"ok": False, "summary": "the message is not in the local index yet",
                    "result": {"error": "not_indexed"}}
        store.tag_messages([row["id"]], tag if add else "", by="assistant")
        store.log_event("info", "assistant %s msg %s ('%s')"
                        % (("tagged '%s'" % tag) if add else "untagged",
                           row["id"], _truncate(row.get("subject") or "", 40)))
        return {"ok": True, "summary": ("tagged as '%s'" % tag) if add else "tag removed",
                "result": {"message_id": row["id"], "tag": tag if add else "", "source": "assistant"}}

    def _tool_draft_reply(self, a):
        row, folder, uid, err = self._resolve_message(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        if not row:
            return {"ok": False, "summary": "the message is not in the local index yet",
                    "result": {"error": "not_indexed"}}
        tid = None
        if a.get("template_id") not in (None, "", 0, "0"):
            try:
                tid = int(a.get("template_id"))
            except (TypeError, ValueError):
                tid = None
        try:
            draft = generate_draft(row["id"], tid)
            body = ""
            subject = ""
            if isinstance(draft, dict):
                for key in ("body", "draft", "text", "content"):
                    if draft.get(key):
                        body = str(draft[key])
                        break
                subject = str(draft.get("subject") or "")
            else:
                body = str(draft or "")
            if not body.strip():
                raise RuntimeError("the model returned an empty draft")
            saved_to = save_draft(row["id"], body)
        except Exception as exc:
            return {"ok": False, "summary": "draft failed: %r" % exc, "result": {"error": repr(exc)}}
        store.log_event("info", "assistant drafted a reply to msg %s ('%s') -> %s"
                        % (row["id"], _truncate(row.get("subject") or "", 40), saved_to))
        return {"ok": True, "summary": "draft saved to %s" % saved_to,
                "result": {"message_id": row["id"], "saved_to": saved_to,
                           "subject": subject, "body_preview": _truncate(body, 500)}}

    def _tool_delete_message(self, a):
        row, folder, uid, err = self._resolve_message(a)
        if err:
            return {"ok": False, "summary": err, "result": {"error": err}}
        mc = self._mail()
        trash = mc.find_special_use("\\Trash")
        if not trash:
            return {"ok": False, "summary": "no Trash folder on this account - delete is unavailable",
                    "result": {"error": "no_trash_folder"}}
        try:
            mc.ensure_selected(folder)
            if row:
                store.record_move(row, trash, "trash", from_folder=folder)
            new_uid = mc.move(uid, trash, msgid=(row or {}).get("msgid"))
        except Exception as exc:
            return {"ok": False, "summary": "move to Trash failed: %r" % exc, "result": {"error": repr(exc)}}
        if row:
            mv = {"status": "assistant-deleted", "action_taken": "trash", "folder": trash}
            if new_uid:
                mv["uid"] = new_uid
            store.update_message(row["id"], **mv)
        store.log_event("info", "assistant moved %s uid %s ('%s') to Trash"
                        % (folder, uid, _truncate((row or {}).get("subject") or "", 50)))
        return {"ok": True, "summary": "moved to Trash (%s)" % trash,
                "result": {"trashed": {"folder": folder, "uid": uid, "to": trash}}}

    def _tool_send_message(self, a):
        import smtplib
        from email.message import EmailMessage
        mode = (a.get("mode") or "new").strip().lower()
        if mode not in ("new", "reply", "reply_all"):
            mode = "new"
        body = str(a.get("body") or "")
        if not body.strip():
            return {"ok": False, "summary": "body is required", "result": {"error": "body is required"}}
        try:
            cap = int(store.get_setting("sends_per_hour", 5) or 0)
        except (TypeError, ValueError):
            cap = 5
        if cap:
            recent = store.agent_actions_since("send", int(time.time()) - 3600,
                                               statuses=("applied", "pending"))
            if recent > cap:
                store.log_event("warn", "agent send blocked by the hourly cap (%d)" % cap)
                return {"ok": False, "summary": "hourly send cap reached (%d/h)" % cap,
                        "result": {"error": "send_cap", "cap": cap}}
        icfg = imap_config()
        if not icfg.get("user"):
            return {"ok": False, "summary": "no mail account connected", "result": {"error": "no_account"}}
        smtp_port = 0
        try:
            acct = proxy.get_account(icfg.get("user"))
            smtp_port = int((acct or {}).get("smtp_local_port") or 0)
        except Exception:
            smtp_port = 0
        if not smtp_port:
            return {"ok": False, "summary": "the account has no SMTP listener - re-authorise it on the Accounts page",
                    "result": {"error": "no_smtp_listener"}}
        to_addr = (a.get("to") or "").strip()
        subject = (a.get("subject") or "").strip()
        orig_msgid = ""
        cc_addr = ""
        if mode in ("reply", "reply_all"):
            row, folder, uid, err = self._resolve_message(a)
            if err:
                return {"ok": False, "summary": err, "result": {"error": err}}
            try:
                mc = self._mail()
                mc.ensure_selected(folder)
                meta = mc.fetch_meta(uid) or {}
            except Exception as exc:
                return {"ok": False, "summary": "cannot read the original message: %r" % exc,
                        "result": {"error": repr(exc)}}
            to_addr = to_addr or (meta.get("from_addr") or "").strip()
            subject = subject or (meta.get("subject") or "").strip()
            if subject and not subject.lower().startswith("re:"):
                subject = "Re: " + subject
            orig_msgid = (meta.get("msgid") or "").strip()
            if mode == "reply_all":
                cc_addr = (meta.get("to_addr") or "").strip()
        if not to_addr:
            return {"ok": False, "summary": "no recipient address", "result": {"error": "no_to"}}
        em = EmailMessage()
        em["From"] = icfg["user"]
        em["To"] = to_addr
        if cc_addr and cc_addr.lower() != (icfg["user"] or "").lower():
            em["Cc"] = cc_addr
        em["Subject"] = subject or "(no subject)"
        if orig_msgid:
            mid_hdr = orig_msgid if orig_msgid.startswith("<") else "<%s>" % orig_msgid
            em["In-Reply-To"] = mid_hdr
            em["References"] = mid_hdr
        em.set_content(body)
        try:
            with smtplib.SMTP("127.0.0.1", smtp_port, timeout=45) as smtp:
                smtp.ehlo()
                try:
                    smtp.login(icfg["user"], icfg.get("password") or "")
                except smtplib.SMTPException as exc:
                    store.log_event("debug", "send: local SMTP AUTH not accepted (%r) - sending without" % exc)
                smtp.send_message(em)
        except Exception as exc:
            return {"ok": False, "summary": "send failed: %r" % exc, "result": {"error": repr(exc)}}
        sent_note = ""
        try:
            mc2 = self._mail()
            sent = mc2.find_special_use("\\Sent")
            if sent:
                mc2.append_message(sent, em.as_bytes(), flags=r"(\Seen)")
                sent_note = sent
        except Exception as exc:
            store.log_event("debug", "send: could not append to Sent (%r)" % exc)
        store.log_event("info", "assistant sent mail to %s ('%s')%s"
                        % (to_addr, _truncate(subject, 60), (" [%s]" % sent_note) if sent_note else ""))
        return {"ok": True, "summary": "sent to %s" % to_addr,
                "result": {"to": to_addr, "subject": subject, "mode": mode, "saved_to": sent_note}}

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


def assistant_respond(user_text, session_id=0):
    """Run one assistant turn to completion (no streaming). Returns (reply, proposals)."""
    agent = AssistantAgent(session_id=session_id)
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
