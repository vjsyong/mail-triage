#!/usr/bin/env python3
"""
Mail Triage — smart email management on top of the email-oauth2-proxy.

Container-friendly web app: quick filter rules (move/flag/read), LLM escalation for
classification of the rest, reply templates with LLM-drafted replies saved to Drafts.

Run:  python app.py            (serves the UI and starts the background worker)
      python app.py --check    (read-only connectivity check, prints JSON)
"""
import gzip
import hashlib
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
from html import escape as html_escape
from urllib.parse import quote

from flask import Flask, Response, flash, jsonify, redirect, render_template_string, request, url_for

import config
import engine
import heuristics
import learning
import plugin_rt
import plugin_ui
import plugins
import proxy
import rag
import stage_worker
import store
import ux
import replies

app = Flask(__name__)


@app.after_request
def _gzip_response(resp):
    """Compress text responses (skips streams/SSE, small bodies, binary media)."""
    try:
        if (resp.status_code == 200 and not resp.direct_passthrough
                and "gzip" in (request.headers.get("Accept-Encoding") or "")
                and resp.mimetype in ("text/html", "text/css", "text/javascript",
                                      "application/javascript", "application/json",
                                      "text/plain", "image/svg+xml")):
            data = resp.get_data() or b""
            if len(data) >= 800:
                resp.set_data(gzip.compress(data, 6))
                resp.headers["Content-Encoding"] = "gzip"
                resp.headers["Content-Length"] = str(len(resp.get_data()))
                vary = resp.headers.get("Vary")
                resp.headers["Vary"] = (vary + ", Accept-Encoding") if vary else "Accept-Encoding"
    except Exception:
        pass
    return resp


@app.after_request
def _no_store_html(resp):
    """Dynamic pages: never let a browser/tab serve a stale shell from cache or
    bfcache - inline CSS+JS means a cached HTML pins the whole old UI."""
    try:
        if resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            resp.headers.pop("ETag", None)
    except Exception:
        pass
    return resp

app.secret_key = os.environ.get("APP_SECRET", "mail-triage-local")

worker = engine.Worker()
indexer = rag.Indexer()
body_fetcher = engine.BodyFetcher()
act_runner = engine.ActRunner()
classifier = engine.ClassifyJob()
llm_health = engine.LLMHealthMonitor()

_TZ_CACHE = {"at": 0.0, "off": 8.0}  # display timezone offset cache (see tz_offset_hours)

MSG_REF_RE = re.compile(r"\[msg:(\d+)\]")
_MD_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def linkify(text):
    """Escape text, then turn [msg:123] references into links to the message page."""
    out = html_escape(text or "")
    return MSG_REF_RE.sub(
        lambda m: '<a href="/messages/%s">[msg:%s]</a>' % (m.group(1), m.group(1)), out)


def _md_msg_link(s):
    return MSG_REF_RE.sub(
        lambda m: '<a href="/messages/%s">[msg:%s]</a>' % (m.group(1), m.group(1)), s)


def md_to_html(text):
    """Small safe markdown renderer for chat replies.

    Escapes first, then structures: fenced + inline code, bold/italic, links,
    [msg:ID] refs, headings, bullet/numbered lists, blockquotes, hr, line breaks.
    """
    text = (text or "").replace("\r\n", "\n")
    code_blocks = []

    def _stash_fence(m):
        code_blocks.append(m.group(1))
        return "\x00C%d\x00" % (len(code_blocks) - 1)

    text = _MD_FENCE.sub(_stash_fence, text)
    lines = text.split("\n")
    out = []
    i, n = 0, len(lines)

    def inline(s):
        s = html_escape(s)
        spans = []

        def _stash_span(m):
            spans.append(m.group(1))
            return "\x00I%d\x00" % (len(spans) - 1)

        s = re.sub(r"`([^`\n]+)`", _stash_span, s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"__([^_]+)__", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
        s = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", s)
        def _linkify(m2):
            label, href = m2.group(1), m2.group(2)
            if href.startswith("http"):
                return '<a href="%s" target="_blank" rel="noopener">%s</a>' % (href, label)
            return '<a href="%s">%s</a>' % (href, label)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|/(?![/\\])[^)\s\\]*)\)", _linkify, s)
        for k, v in enumerate(spans):
            s = s.replace("\x00I%d\x00" % k, "<code>%s</code>" % v)
        return s

    def _is_block_start(s2):
        return (not s2 or re.match(r"([-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>)", s2)
                or re.fullmatch(r"\x00C\d+\x00", s2) or re.fullmatch(r"(-{3,}|\*{3,})", s2))

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        m = re.fullmatch(r"\x00C(\d+)\x00", stripped)
        if m:
            out.append('<pre class="md-pre">%s</pre>'
                       % html_escape(code_blocks[int(m.group(1))]))
            i += 1
            continue
        if re.fullmatch(r"(-{3,}|\*{3,})", stripped):
            out.append("<hr>")
            i += 1
            continue
        m = re.match(r"#{1,6}\s+(.*)$", stripped)
        if m:
            out.append('<div class="md-h">%s</div>' % _md_msg_link(inline(m.group(1))))
            i += 1
            continue
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(inline(lines[i].strip().lstrip(">").strip()))
                i += 1
            out.append("<blockquote>%s</blockquote>" % "<br>".join(buf))
            continue
        m = re.match(r"[-*+]\s+(.*)$", stripped)
        if m:
            items = []
            while i < n:
                mm = re.match(r"\s*[-*+]\s+(.*)$", lines[i])
                if not mm:
                    break
                items.append("<li>%s</li>" % _md_msg_link(inline(mm.group(1))))
                i += 1
            out.append("<ul>%s</ul>" % "".join(items))
            continue
        m = re.match(r"\d+[.)]\s+(.*)$", stripped)
        if m:
            items = []
            while i < n:
                mm = re.match(r"\s*\d+[.)]\s+(.*)$", lines[i])
                if not mm:
                    break
                items.append("<li>%s</li>" % _md_msg_link(inline(mm.group(1))))
                i += 1
            out.append("<ol>%s</ol>" % "".join(items))
            continue
        buf = []
        while i < n and not _is_block_start(lines[i].strip()):
            buf.append(_md_msg_link(inline(lines[i].strip())))
            i += 1
        if buf:
            out.append("<p>%s</p>" % "<br>".join(buf))
    return "\n".join(out)


app.jinja_env.globals["linkify"] = linkify
app.jinja_env.globals["md"] = md_to_html

HKT = 8 * 3600  # legacy default; the live value is the display_tz_offset setting


def tz_offset_hours():
    """Display timezone offset (hours from UTC) from settings, cached for 5 s."""
    now = time.time()
    if now - _TZ_CACHE["at"] > 5:
        try:
            _TZ_CACHE["off"] = float(store.get_setting("display_tz_offset", 8))
        except (TypeError, ValueError):
            _TZ_CACHE["off"] = 8.0
        _TZ_CACHE["at"] = now
    return _TZ_CACHE["off"]


def tz_label():
    off = float(tz_offset_hours())
    return "UTC%s%g" % ("+" if off >= 0 else "-", abs(off))


def fmt_ts(ts):
    if not ts:
        return "—"
    return time.strftime("%m-%d %H:%M",
                         time.gmtime(int(ts) + int(round(tz_offset_hours() * 3600))))


def rel_time(ts):
    if not ts:
        return "never"
    delta = int(time.time()) - int(ts)
    if delta < 60:
        return "%ds ago" % delta
    if delta < 3600:
        return "%dm ago" % (delta // 60)
    if delta < 86400:
        return "%dh ago" % (delta // 3600)
    return "%dd ago" % (delta // 86400)


def stats():
    with store.db() as conn:
        def one(q, *args):
            return conn.execute(q, args).fetchone()[0]
        G = " WHERE " + store.REAL_MSG
        return {
            "total": one("SELECT COUNT(*) FROM messages" + G),
            "queued": one("SELECT COUNT(*) FROM messages" + G + " AND status IN ('new','queued')"),
            "classified": one("SELECT COUNT(*) FROM messages" + G + " AND status IN ('classified','llm-moved')"),
            "moved": one("SELECT COUNT(*) FROM messages" + G + " AND action_taken LIKE 'move%'"),
            "needs_reply": one("SELECT COUNT(*) FROM messages" + G + " AND llm_needs_reply=1"
                               " AND coalesce(snoozed_until,0) <= %d" % int(time.time())),
            "errors": one("SELECT COUNT(*) FROM messages" + G + " AND status='error'"),
            "rules": one("SELECT COUNT(*) FROM rules WHERE enabled=1"),
            "flows": one("SELECT COUNT(*) FROM flows WHERE enabled=1"),
            "classifiers": one("SELECT COUNT(*) FROM heuristics WHERE enabled=1"),
        }


def index_status():
    st = dict(indexer.state)
    if engine.external_stages():
        # the index stage runs in the supervised stage-worker process: its live
        # state is published to stage_state, so the dashboard shows real
        # progress instead of this process's idle indexer.
        ext = store.get_stage_state("index")
        for k, v in ext.items():
            if k != "_updated_at":
                st[k] = v
        st["external"] = True
        age = int(time.time()) - int(ext.get("_updated_at") or 0)
        if age > 30:
            # the child publishes every second; silence means it died/stopped
            st["running"] = False
            st["remaining"] = None
            st["last_error"] = ("stage worker is not reporting (last state %s ago)"
                                % rel_time(ext.get("_updated_at")))
    st["last_ok_r"] = rel_time(st.get("last_ok"))
    try:
        s = rag.index_stats()
        st["messages"] = s["messages"]
        st["chunks"] = s["chunks"]
        st["backend"] = s.get("backend", "legacy")
        ov = store.index2_overview() if s.get("backend") == "lite" else store.index_overview()
        st["folders_done"] = sum(1 for r in ov if r.get("status") == "done")
        st["folders_total"] = len(ov)
    except Exception:
        st.setdefault("messages", 0)
        st.setdefault("chunks", 0)
        st.setdefault("folders_done", 0)
        st.setdefault("folders_total", 0)
    return st


def setup_state():
    """Setup wizard state: 3 steps + counts, shared by /welcome and the dashboard banner."""
    accounts = []
    try:
        accounts = proxy.list_accounts()
    except Exception:
        accounts = []
    llm = {"base": "", "model": ""}
    try:
        _c = engine.llm_config()
        llm = {"base": (_c.get("base") or "").strip(), "model": (_c.get("model") or "").strip()}
    except Exception:
        pass
    indexed, chunks = 0, 0
    try:
        _st = rag.index_stats() or {}
        indexed = int(_st.get("messages") or 0)
        chunks = int(_st.get("chunks") or 0)
    except Exception:
        pass
    steps = [
        {"id": "mailbox", "label": "Mailbox",
         "done": bool(accounts), "user": (accounts[0].get("email") if accounts else "") or ""},
        {"id": "llm", "label": "LLM", "done": bool(llm["base"]),
         "base": llm["base"], "model": llm["model"]},
        {"id": "index", "label": "Search", "done": indexed > 0,
         "messages": indexed, "chunks": chunks},
    ]
    return {"steps": steps, "done": sum(1 for x in steps if x["done"]), "total": len(steps),
            "dismissed": bool(store.get_setting("welcome_done", 0)),
            "accounts": accounts, "llm": llm, "index": {"messages": indexed, "chunks": chunks}}


def _fresh_install():
    """True when a brand-new install has nothing configured (first-run takeover)."""
    try:
        if store.get_setting("welcome_done", 0) or store.get_setting("welcome_skipped", 0):
            return False
        if proxy.list_accounts():
            return False
        if (engine.llm_config().get("base") or "").strip():
            return False
        return int(store.count_messages() or 0) == 0
    except Exception:
        return False


def summarize_conditions(rule):
    try:
        conds = json.loads(rule.get("conditions") or "[]")
    except (TypeError, ValueError):
        conds = []
    if not conds:
        return "(no conditions — never matches)"
    joiner = " AND " if (rule.get("match_mode") or "all") == "all" else " OR "
    return joiner.join(
        ('matches plugin "%s"' % c.get("plugin")) if (c.get("op") or "") == "plugin"
        else ('%s %s "%s"' % (c.get("field", "?"), c.get("op", "?"), c.get("value", "")))
        for c in conds)


def summarize_actions(rule):
    try:
        actions = json.loads(rule.get("actions") or "{}")
    except (TypeError, ValueError):
        actions = {}
    parts = []
    if actions.get("move_to"):
        parts.append("move → %s" % actions["move_to"])
    if actions.get("mark_read"):
        parts.append("mark read")
    if actions.get("flag"):
        parts.append("flag")
    if not parts:
        parts.append("keep in place (guard)")
    return ", ".join(parts) or "(none)"


STATUS_BADGES = {
    "matched": ("ok", "sorted"),
    "matched-dry": ("warn", "rule (dry-run)"),
    "llm-moved": ("ok", "LLM → folder"),
    "assistant-moved": ("ok", "assistant → folder"),
    "kept": ("ok", "kept (undo)"),
    "classified": ("acc", "classified"),
    "queued": ("warn", "queued"),
    "error": ("err", "error"),
    "new": ("", "new"),
}


BASE_TMPL = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Mail Triage">
<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/icon-180.png">
<link rel="icon" href="/static/icons/favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="32x32" href="/static/icons/favicon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png">
<meta name="turbo-cache-control" content="no-cache">
<meta name="view-transition" content="same-origin">
<script src="/static/turbo.js?v=8.0.12" defer></script>
<script src="/static/ux.js?v=2" defer></script>
<meta name="theme-color" content="#fafafa">
<meta name="color-scheme" content="light">
<title>Mail Triage</title>
<style>
@font-face{font-family:'Geist';src:url('/fonts/geist.woff2') format('woff2');font-weight:100 900;font-style:normal;font-display:swap}
@font-face{font-family:'Geist Mono';src:url('/fonts/geist-mono.woff2') format('woff2');font-weight:100 900;font-style:normal;font-display:swap}
:root{--bg:#fafafa;--card:#ffffff;--card2:#f5f5f5;--line:#dcdcdc;--line2:#8f8f8f;--fg:#000000;--ink:#000000;--dim:#525252;
--acc:#0070f3;--ok:#067a46;--warn:#b25e09;--err:#d1242f;
--tint-acc:#f0f7ff;--tint-ok:#edfbf2;--tint-warn:#fff8ea;--tint-err:#fff1f1;
--panel:#000000;--panel-dim:#a3a3a3;--codebg:#0a0a0a;--codefg:#ededed;
--hover:#f4f4f4;--active:#ececec;--focus:rgba(0,112,243,.5);
--mono:'Geist Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box;border-radius:0 !important}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 "Geist",-apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.skip{position:absolute;left:-9999px;top:0;background:#000;color:#fff;padding:8px 12px;z-index:200}
.vh{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.backlink{margin:0 0 4px;font-size:.84rem}
.qbar{display:flex;align-items:center;gap:8px;margin:0 0 12px;flex-wrap:wrap}
.qbar .qoff{opacity:.45;pointer-events:none}
.qbar .sp{flex:1}
#rules th:nth-child(1),#rules td:nth-child(1){white-space:nowrap}
#rules .tbl th:nth-child(3),#rules .tbl td:nth-child(3){min-width:140px}
#bulk .tbl th:nth-child(2),#bulk .tbl td:nth-child(2){white-space:nowrap}
#classifiers .tbl th:nth-child(7),#classifiers .tbl td:nth-child(7){white-space:nowrap}
#classifiers .tbl th:nth-child(2),#classifiers .tbl td:nth-child(2){white-space:nowrap}
.skip:focus{left:8px}
/* ---- app shell ---- */
.app{display:flex;min-height:100vh}
.side{width:236px;position:fixed;inset:0 auto 0 0;background:var(--card);border-right:1px solid var(--line);
display:flex;flex-direction:column;z-index:40}
.side-top{padding:16px 16px 10px}
.brand{display:flex;align-items:center;gap:9px;color:var(--fg)} .brand:hover{text-decoration:none}
.brand-mark{width:24px;height:24px;background:#000;color:#fff;display:inline-flex;align-items:center;justify-content:center;
font:700 11px/1 var(--mono);letter-spacing:.02em}
.brand-name{font-weight:700;letter-spacing:-.02em;font-size:.98rem}
.brand-sub{color:var(--dim);font-size:.76rem;margin:6px 0 0 33px}
.nav{flex:1;overflow-y:auto;padding:4px 8px 8px}
.nav-label{font-size:.66rem;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:#5f5f5f;
padding:14px 10px 4px}
.nav-item{display:flex;align-items:center;gap:10px;padding:7px 10px;margin:1px 0;color:#3f3f46;font-size:.9rem;
font-weight:500;border-left:2px solid transparent}
.nav-item:hover{background:var(--hover);text-decoration:none;color:#000}
.nav-item.active{background:var(--active);border-left-color:var(--acc);color:#000;font-weight:600}
.nav-item svg{width:16px;height:16px;flex:none;opacity:.72}
.nav-item.active svg{opacity:1}
.nav-sub{margin:0 0 4px 20px;border-left:1px solid var(--line);padding-left:2px}
.nav-sub .nav-item{padding:5px 8px;margin:0;font-size:.84rem;gap:8px}
.nav-sub .nav-item svg{width:14px;height:14px}
.side-foot{border-top:1px solid var(--line);padding:12px 16px;font-size:.78rem;color:var(--dim)}
.side-foot .sf-row{display:flex;align-items:center;gap:7px;margin:3px 0}
.main{flex:1;margin-left:236px;min-width:0;display:flex;flex-direction:column}
.content{max-width:1160px;width:100%;margin:0 auto;padding:26px 26px 96px;flex:1}
.topbar{display:none}
.scrim{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:35}
/* ---- typography ---- */
h1{font-size:1.4rem;margin:0;font-weight:700;letter-spacing:-.03em}
h2{font-size:1.06rem;margin:24px 0 8px;font-weight:600;letter-spacing:-.02em}
h3{font-size:.98rem;margin:0 0 6px;font-weight:600;letter-spacing:-.01em}
h4{font-size:.9rem;margin:14px 0 4px;font-weight:600;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:.86rem}
.mono,code{font-family:var(--mono);font-size:.84rem;background:var(--card2);border:1px solid var(--line);
padding:1px 5px;overflow-wrap:anywhere}
.page-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;margin:0 0 14px}
.page-title{font-size:1.3rem;font-weight:700;letter-spacing:-.03em;margin:0}
.page-desc{color:var(--dim);font-size:.88rem;margin-top:3px}
/* ---- cards & layout ---- */
.card{background:var(--card);border:1px solid var(--line);padding:16px 18px;margin:14px 0}
.card.flush{padding:0;overflow:hidden}
.card-h{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 10px}
.card-h h3{margin:0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.grid3{display:grid;grid-template-columns:150px 130px 1fr;gap:8px}
.grid5{display:grid;grid-template-columns:118px 104px 92px minmax(0,1fr) 86px;gap:8px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.spread{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}
.stack>*+*{margin-top:10px}
/* ---- buttons ---- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:34px;padding:0 13px;
border:1px solid var(--line2);background:#fff;color:var(--fg);font-size:.88rem;font-weight:500;cursor:pointer;
font-family:inherit;white-space:nowrap}
.btn:hover{border-color:#000;background:var(--bg);text-decoration:none}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary{background:#000;border-color:#000;color:#fff;font-weight:600}
.btn.primary:hover{background:#333;border-color:#333}
.btn.danger{color:var(--err)} .btn.danger:hover{border-color:var(--err);background:var(--tint-err)}
.btn.ghost{border-color:transparent;background:none}
.btn.ghost:hover{border-color:transparent;background:var(--hover)}
.btn.small{height:27px;padding:0 9px;font-size:.79rem}
.btn svg{width:14px;height:14px}
.iconbtn{width:34px;height:34px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line2);
background:#fff;cursor:pointer;color:var(--fg)}
.iconbtn:hover{border-color:#000;background:var(--bg)}
.iconbtn svg{width:16px;height:16px}
form.inline{display:inline}
/* ---- forms ---- */
input[type=text],input[type=number],input[type=password],input[type=search],select,textarea{background:#fff;
border:1px solid var(--line2);color:var(--fg);padding:8px 10px;font-size:.92rem;width:100%;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:#000;box-shadow:0 0 0 3px var(--focus)}
input::placeholder,textarea::placeholder{color:#767676}
textarea{font-family:var(--mono);font-size:.86rem;min-height:120px;line-height:1.5}
label{display:block;font-size:.84rem;color:var(--dim);margin:10px 0 4px;font-weight:500}
label.check{display:flex;align-items:center;gap:8px;color:var(--fg);margin:8px 0;cursor:pointer}
label.check input{width:auto;margin:0;accent-color:#000;width:15px;height:15px}
label.check span{font-size:.88rem;color:var(--fg);font-weight:400}
input[type=checkbox]{accent-color:#000}
fieldset{border:1px solid var(--line);padding:12px 14px;margin:14px 0}
legend{font-size:.8rem;font-weight:600;padding:0 6px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
/* ---- badges / chips / dots ---- */
.badge{display:inline-block;font-size:.66rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
padding:2px 7px;border:1px solid var(--line);color:var(--dim);white-space:nowrap;background:#fff}
.badge.ok{color:var(--ok);border-color:var(--ok)} .badge.err{color:var(--err);border-color:var(--err)}
.badge.warn{color:var(--warn);border-color:var(--warn)} .badge.acc{color:var(--acc);border-color:var(--acc)}
.badge.solid{background:#000;color:#fff;border-color:#000}
.chip{display:inline-flex;align-items:center;gap:6px;height:29px;padding:0 11px;border:1px solid var(--line2);
background:#fff;color:#3f3f46;font-size:.82rem;font-weight:500;cursor:pointer;white-space:nowrap}
.chip:hover{border-color:#000;color:#000;text-decoration:none}
.chip.active{background:#000;border-color:#000;color:#fff}
.chip .n{font-family:var(--mono);font-size:.74rem;opacity:.75}
.dot{width:7px;height:7px;display:inline-block;flex:none;background:#8a8a8a}
.dot.ok{background:var(--ok)} .dot.err{background:var(--err)} .dot.warn{background:var(--warn)} .dot.acc{background:var(--acc)}
.kbd{font-family:var(--mono);font-size:.72rem;border:1px solid var(--line2);border-bottom-width:2px;
padding:1px 5px;background:#fff;color:#3f3f46}
/* ---- tables ---- */
.tablewrap{overflow-x:auto}
table.tbl{width:100%;border-collapse:collapse}
.tbl th,.tbl td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-size:.87rem;vertical-align:top}
.tbl th{color:var(--dim);font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;
white-space:nowrap;background:#f7f7f7;position:sticky;top:0;z-index:2}
.tbl tr:hover td{background:var(--hover)}
.tbl .r{text-align:right}
.tbl td.sel,.tbl th.sel{width:34px;padding-right:2px}
.tbl input[type=checkbox]{width:15px;height:15px;display:block}
.rowacts{display:inline-flex;gap:6px;opacity:0;transition:opacity .12s}
.cond-more{display:none}
.rowacts .ra-menu{display:none}
tr:hover .rowacts,tr:focus-within .rowacts{opacity:1}
@media(hover:none){.rowacts{opacity:1}}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--line);background:#fff}
.tchips,.tactions{display:contents}
.tactions>button:first-child{margin-left:auto}
.bulkbar{display:none;align-items:center;gap:12px;padding:8px 12px;background:#0a0a0a;color:#fff;position:sticky;top:0;z-index:6}
.bulkbar.on{display:flex}
.bulkbar .n{font-weight:600}
.bulkbar .btn{border-color:#3f3f46;background:transparent;color:#fff;height:28px;font-size:.8rem}
.bulkbar .btn:hover{border-color:#fff;background:#27272a}
.bulkbar .btn.primary{background:#fff;color:#000;border-color:#fff}
.bulkbar .grow{flex:1}
.pager{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:10px 12px;color:var(--dim);font-size:.84rem}
/* ---- misc blocks ---- */
.stat{display:inline-block;background:var(--panel);border:1px solid var(--panel);color:#fff;padding:12px 16px;
margin:0 8px 8px 0;min-width:104px}
.stat b{display:block;font-size:1.35rem;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{color:var(--panel-dim);font-size:.74rem}
.kv{display:grid;grid-template-columns:auto 1fr;gap:5px 14px;font-size:.9rem;margin:10px 0}
.kv .k{color:var(--dim);white-space:nowrap}
/* Shared settings row: used by Settings, Controls and the filing card. */
.setrow{display:grid;grid-template-columns:minmax(0,1fr) minmax(220px,300px);gap:8px 18px;padding:11px 0;border-top:1px solid var(--line);align-items:start}
.setrow:first-of-type{border-top:0;padding-top:2px}
.setrow .st-l b{display:block;font-size:.87rem;font-weight:600}
.setrow .st-l .sub{display:block;margin-top:2px;font-size:.78rem}
.setrow .st-c input[type=text],.setrow .st-c input[type=password],.setrow .st-c input[type=number],.setrow .st-c select{width:100%}
.setrow .st-c textarea{width:100%}
.setrow .st-c .check{margin:2px 0 0}
.setrow .st-c input:disabled,.setrow .st-c select:disabled{background:var(--hover);color:var(--dim);opacity:.7;cursor:not-allowed}
@media(max-width:900px){.setrow{grid-template-columns:1fr}}
.note{background:var(--tint-acc);border:1px solid var(--acc);padding:10px 12px;font-size:.86rem;color:#003a8c}
.msg{padding:10px 12px;margin:10px 0;font-size:.9rem;border:1px solid var(--line);background:#fff}
.msg.ok{background:var(--tint-ok);border-color:var(--ok)}
.msg.warn{background:var(--tint-warn);border-color:var(--warn)}
.msg.err{background:var(--tint-err);border-color:var(--err)}
.empty{padding:44px 18px;text-align:center;color:var(--dim)}
.empty svg{width:30px;height:30px;opacity:.4}
.empty h4{color:#000;margin:10px 0 4px;font-size:.98rem}
.empty p{margin:0 auto;max-width:420px;font-size:.88rem}
.empty .btn{margin-top:14px}
.progress{height:6px;background:var(--card2);border:1px solid var(--line);overflow:hidden}
.progress i{display:block;height:100%;background:#000;transition:width .4s}
.skeleton{background:linear-gradient(90deg,#ececec 25%,#f6f6f6 37%,#ececec 63%);background-size:400% 100%;
animation:sk 1.3s ease infinite}
@keyframes sk{0%{background-position:100% 0}100%{background-position:0 0}}
pre.log{background:var(--codebg);border:1px solid var(--codebg);color:var(--codefg);padding:12px;font-size:.78rem;
line-height:1.5;overflow:auto;max-height:70vh;white-space:pre-wrap;overflow-wrap:anywhere}
.logpanel{background:var(--codebg);border:1px solid var(--codebg);padding:12px 14px}
.logpanel .logrow{font-size:.8rem;color:#d4d4d4;padding:2px 0;line-height:1.6;display:flex;gap:8px;align-items:baseline}
.logpanel .mono{background:none;border:0;padding:0;color:#8f8f8f;font-size:.76rem;flex:none}
.logpanel .badge{color:#a3a3a3;border-color:#3f3f46;background:none;flex:none}
.logpanel .badge.ok{color:#4ade80;border-color:#4ade80}
.logpanel .badge.err{color:#f87171;border-color:#f87171}
.logpanel .badge.warn{color:#fbbf24;border-color:#fbbf24}
.logpanel .lmsg{min-width:0;overflow-wrap:anywhere}
.foot{margin-top:34px;color:var(--dim);font-size:.78rem;border-top:1px solid var(--line);padding-top:12px}
/* ---- toasts (JS) ---- */
.toasts{position:fixed;right:16px;top:16px;z-index:300;display:flex;flex-direction:column;gap:8px;max-width:min(420px,92vw)}
.toast2{background:#0a0a0a;color:#fff;padding:11px 14px;font-size:.86rem;box-shadow:0 8px 24px rgba(0,0,0,.25);
border-left:3px solid #a3a3a3;animation:tin .18s ease}
.toast2.ok{border-left-color:var(--ok)} .toast2.err{border-left-color:var(--err)}
.turbo-progress-bar{height:2px !important;background:#000 !important}
/* view transitions: chrome stays put; desktop breathes, mobile pushes horizontally */
@media (prefers-reduced-motion: no-preference){
  .side{view-transition-name:mt-side}
  .topbar{view-transition-name:mt-topbar}
  .bottom-nav{view-transition-name:mt-nav}
  #asb{view-transition-name:mt-asb}
}
@media (prefers-reduced-motion: no-preference) and (min-width:768px){
  html::view-transition-old(root){animation:mtvt-out .14s ease both}
  html::view-transition-new(root){animation:mtvt-in .24s cubic-bezier(.2,.7,.3,1) both}
}
@media (prefers-reduced-motion: no-preference) and (max-width:767px){
  html::view-transition-old(root){animation:mtvt-fwd-out .28s cubic-bezier(.2,.7,.3,1) both}
  html::view-transition-new(root){animation:mtvt-fwd-in .28s cubic-bezier(.2,.7,.3,1) both}
  html[data-vt-dir="back"]::view-transition-old(root){animation:mtvt-back-out .28s cubic-bezier(.2,.7,.3,1) both}
  html[data-vt-dir="back"]::view-transition-new(root){animation:mtvt-back-in .28s cubic-bezier(.2,.7,.3,1) both}
}
@keyframes mtvt-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes mtvt-out{from{opacity:1;transform:none}to{opacity:0;transform:translateY(-4px)}}
@keyframes mtvt-fwd-out{from{transform:translateX(0)}to{transform:translateX(-100%)}}
@keyframes mtvt-fwd-in{from{transform:translateX(100%)}to{transform:translateX(0)}}
@keyframes mtvt-back-out{from{transform:translateX(0)}to{transform:translateX(100%)}}
@keyframes mtvt-back-in{from{transform:translateX(-100%)}to{transform:translateX(0)}}
@media (prefers-reduced-motion: reduce){
  ::view-transition-group(*),::view-transition-old(*),::view-transition-new(*){animation:none !important}
}
.toast2.warn{border-left-color:var(--warn)}
@keyframes tin{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.toast{position:fixed;right:18px;bottom:18px;background:#000;color:#fff;padding:11px 16px;font-size:.85rem;
z-index:299;box-shadow:0 6px 20px rgba(0,0,0,.28);max-width:420px}
.toast b{font-weight:600}
/* ---- auth panel / copy / misc ---- */
.auth-panel{margin-top:14px;padding:14px;border:1px dashed var(--line2);background:#fcfcfc}
.hr{border-top:1px solid var(--line);margin:14px 0}
.sec-h{font-size:1.04rem;font-weight:700;letter-spacing:-.02em;margin:30px 0 2px}
.sec-desc{color:var(--dim);font-size:.86rem;margin:0 0 4px}
section[id]{scroll-margin-top:70px}
details.menu{position:relative;display:inline-block}
details.menu > summary{list-style:none;cursor:pointer}
details.menu > summary::-webkit-details-marker{display:none}
.menu-pop{position:absolute;right:0;top:calc(100% + 4px);background:#fff;border:1px solid var(--line);
box-shadow:0 10px 30px rgba(0,0,0,.14);min-width:210px;z-index:30;padding:4px;text-align:left}
.menu-item{display:block;width:100%;text-align:left;background:none;border:0;padding:8px 10px;
font:inherit;font-size:.86rem;color:var(--fg);cursor:pointer}
.menu-item:hover{background:var(--hover)}
.menu-item.danger{color:var(--err)}
.menu-item.danger:hover{background:var(--tint-err)}
.savebar{position:sticky;bottom:10px;background:rgba(250,250,250,.94);backdrop-filter:blur(4px);
border:1px solid var(--line);padding:10px 12px;display:flex;align-items:center;gap:10px;margin-top:14px;z-index:4;flex-wrap:wrap}
.seg{display:inline-flex}
.seg .btn+.btn{border-left:0}
.copy{cursor:pointer;user-select:none;color:var(--dim);border:1px solid var(--line2);padding:1px 7px;font-size:.76rem;
margin-left:6px;display:inline-block;background:#fff}
.copy:hover{color:var(--acc);border-color:var(--acc)}
.hidden{display:none !important}
/* ---- markdown (chat + bodies) ---- */
.md p{margin:6px 0}
.md .md-h{font-weight:600;margin:10px 0 4px}
.md ul,.md ol{margin:6px 0 6px 22px;padding:0}
.md blockquote{border-left:3px solid var(--line);margin:6px 0;padding:2px 10px;color:var(--dim)}
.md pre.md-pre{background:var(--codebg);border:1px solid var(--codebg);color:var(--codefg);padding:10px;overflow:auto;
white-space:pre-wrap;font-family:var(--mono);font-size:.85rem}
.md code{font-family:var(--mono);font-size:.85rem;background:var(--card2);border:1px solid var(--line);padding:1px 5px}
/* ---- responsive ---- */
@media(max-width:1023px){
  .side{transform:translateX(-101%);transition:transform .18s ease;box-shadow:0 0 40px rgba(0,0,0,.18)}
  .side.open{transform:none}
  .main{margin-left:0}
  .topbar{display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:30;background:rgba(250,250,250,.94);
  backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:10px 14px}
  .topbar .tb-title{font-weight:700;letter-spacing:-.02em;font-size:.95rem}
  .topbar .tb-status{margin-left:auto;display:flex;align-items:center;gap:7px;color:var(--dim);font-size:.78rem}
  .content{padding:16px 14px 80px}
}
@media(max-width:767px){
  .grid2{grid-template-columns:1fr}
  .grid3{grid-template-columns:1fr 1fr}
  .grid5{grid-template-columns:1fr 1fr}
  .grid5 input[type=text]{grid-column:1/-1}
  .card{padding:13px 14px}
  .tbl.mcards{border:0}
  .tbl.mcards thead{display:none}
  .tbl.mcards tr{display:block;border:1px solid var(--line);background:#fff;margin:8px 0;padding:8px 10px}
  .tbl.mcards tr:hover td{background:none}
  .tbl.mcards td{display:block;border:0;padding:2px 0;background:none}
  .tbl.mcards td.sel{float:right;width:auto}
  .tbl.mcards .rowacts{opacity:1}
  #bulk .tbl.mcards tr{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px;position:relative;padding:10px 46px 10px 12px}
  #bulk .tbl.mcards td.sel{float:none;position:absolute;top:12px;right:12px}
  #bulk .tbl.mcards td.sel input{width:24px;height:24px}
  #bulk .tbl.mcards td:nth-child(2){order:2;margin-left:auto;white-space:nowrap}
  #bulk .tbl.mcards td:nth-child(3){order:1;font-weight:600;color:var(--fg);max-width:72%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #bulk .tbl.mcards td:nth-child(4){order:3;flex:1 1 100%;min-width:0}
  #bulk .tbl.mcards td:nth-child(4) div.sub{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-top:3px}
  #bulk .tbl.mcards td:nth-child(5),#bulk .tbl.mcards td:nth-child(6),#bulk .tbl.mcards td:nth-child(7){order:4;margin-top:7px}
  #bulk .tbl.mcards td:nth-child(7){margin-left:auto;text-align:right}
  #bulk .tbl.mcards td:empty{display:none}
  .empty{padding:28px 14px}
  .empty .btn{width:100%;display:inline-flex;justify-content:center}
  .r-head{flex-direction:column;align-items:stretch}
  .r-head .row{display:flex;flex-wrap:wrap;gap:8px}
  .r-head .row .btn{flex:1;justify-content:center;text-align:center}
  .r-head .row form.inline{flex:1 1 100%}
  .r-head .row form.inline .btn{width:100%}
  .rowacts .ra-inline{display:none}
  .rowacts .ra-menu{display:inline-block}
  #rules .tbl.mcards tr{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px}
  #rules .tbl.mcards td{display:block;padding:1px 0}
  #rules .tbl.mcards td:nth-child(1)::after{content:'·';margin-left:7px;color:var(--dim)}
  #rules .tbl.mcards td:nth-child(2){order:2;flex:1;min-width:0}
  #rules .tbl.mcards td:nth-child(3){order:3;flex:1 1 100%;white-space:normal}
  #rules .tbl.mcards td:nth-child(4){order:4;flex:1 1 100%}
  #rules .tbl.mcards td:nth-child(5){order:5;flex:1 1 100%;margin-top:6px}
  #rules .tbl.mcards .rowacts{flex-wrap:wrap;justify-content:flex-start !important;gap:8px;min-height:44px}
  #classifiers .tbl.mcards tr{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px}
  #classifiers .tbl.mcards td{display:block;padding:1px 0}
  #classifiers .tbl.mcards td:nth-child(1){order:1;flex:1 1 100%;min-width:0}
  #classifiers .tbl.mcards td:nth-child(2),#classifiers .tbl.mcards td:nth-child(3),#classifiers .tbl.mcards td:nth-child(4),#classifiers .tbl.mcards td:nth-child(5){font-size:.75rem}
  #classifiers .tbl.mcards td:nth-child(2){order:2}
  #classifiers .tbl.mcards td:nth-child(3){order:3}
  #classifiers .tbl.mcards td:nth-child(4){order:4}
  #classifiers .tbl.mcards td:nth-child(5){order:5}
  #classifiers .tbl.mcards td:nth-child(6){display:none}
  #classifiers .tbl.mcards td:nth-child(7){order:6;flex:1 1 100%;font-size:.72rem}
  #classifiers .tbl.mcards td:nth-child(8){order:7;flex:1 1 100%;margin-top:6px}
  #classifiers .tbl.mcards .rowacts{flex-wrap:wrap;justify-content:flex-start !important;gap:8px;min-height:44px}
  #templates .tbl.mcards tr{display:flex;flex-wrap:wrap;gap:2px 8px}
  #templates .tbl.mcards td{display:block;padding:1px 0}
  #templates .tbl.mcards td:nth-child(1){order:1;flex:1 1 100%}
  #templates .tbl.mcards td:nth-child(2){order:2;flex:1 1 100%;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  #templates .tbl.mcards td:nth-child(3){order:3;flex:1 1 100%;margin-top:6px}
  #templates .tbl.mcards .rowacts{justify-content:flex-start !important;gap:8px;min-height:44px}
  #rules .tbl.mcards .rowacts .btn,#classifiers .tbl.mcards .rowacts .btn,#templates .tbl.mcards .rowacts .btn{min-height:44px}
  .row.chiprow{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;padding-bottom:2px;max-width:100%;-webkit-mask-image:linear-gradient(to right,#000 calc(100% - 22px),transparent);mask-image:linear-gradient(to right,#000 calc(100% - 22px),transparent)}
  .row.chiprow::-webkit-scrollbar{display:none}
  .row.chiprow>*{flex:none}
  .row.chiprow .chip{height:44px}
  .dsx .tbl.mcards tr{display:flex;flex-wrap:wrap;gap:2px 8px}
  .dsx .tbl.mcards td{display:block;padding:1px 0}
  .dsx .tbl.mcards td:nth-child(1){order:1;flex:1 1 100%;min-width:0}
  .dsx .tbl.mcards td:nth-child(1) a{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .dsx .tbl.mcards td:nth-child(1) div.sub{display:none}
  .dsx .tbl.mcards td:nth-child(2){order:2;flex:1 1 100%;font-size:.75rem}
  .dsx .tbl.mcards td:nth-child(3){order:3;flex:1 1 100%;margin-top:4px}
  .dsx .tbl.mcards td:nth-child(3) select{width:100% !important}
  .dsx .tbl.mcards td:nth-child(3) form.inline::before{content:'Label: ';font-size:.78rem;color:var(--dim)}
  .dsx .tbl.mcards td:nth-child(4){order:4;flex:1 1 100%;margin-top:6px;text-align:left}
  .dsx .tbl.mcards td:nth-child(4) .btn{min-height:44px}
  .auth-paste .row{flex-wrap:wrap !important}
  .auth-paste .row input{flex:1 1 100%}
  .auth-paste .row .btn{flex:1 1 100%}
  .menu-pop{max-width:calc(100vw - 24px)}
  .spread>div:first-child{min-width:0}
  .spread h3{overflow-wrap:anywhere}
  .card .spread + .row .btn{min-height:44px}
  .chat-head{padding-top:env(safe-area-inset-top)}
  .sheet-h{padding-top:calc(8px + env(safe-area-inset-top))}
  .dw-head{padding-top:max(10px, env(safe-area-inset-top))}
  .content{padding-left:max(14px, env(safe-area-inset-left));padding-right:max(14px, env(safe-area-inset-right))}
  .toolbar{display:block;padding:10px 0 10px 12px}
  .tchips{display:flex;gap:6px;overflow-x:auto;padding-right:12px;scrollbar-width:none;-webkit-overflow-scrolling:touch;-webkit-mask-image:linear-gradient(to right,#000 calc(100% - 22px),transparent);mask-image:linear-gradient(to right,#000 calc(100% - 22px),transparent)}
  .tchips::-webkit-scrollbar{display:none}
  .tchips .chip{flex:none;height:44px}
  .tactions{display:flex;gap:8px;margin:10px 12px 0 0}
  .tactions .btn{flex:1;min-height:44px}
  .mhide{display:none}
  .pager{padding:12px;gap:8px}
  .pager>a.btn{flex:1;display:flex;align-items:center;justify-content:center;min-height:40px}
  .pager>a.btn.plast{display:none}
  .pager .pp{display:none}
  .pager .pageno{flex:1 1 100%;text-align:center}
  .stat{min-width:calc(50% - 10px);margin-right:8px}
}
@media(min-width:768px){
  #bulk .tbl.mcards td:nth-child(4) a{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  #bulk .tbl.mcards td:nth-child(4) div.sub{margin-top:3px}
}
@media(prefers-reduced-motion:reduce){
  *{animation-duration:.01ms !important;animation-iteration-count:1 !important;transition-duration:.01ms !important}
}

/* ---- assistant chat (shared: page + sidebar) ---- */
.chat-head{display:none}
.assistant-shell{display:grid;grid-template-columns:240px minmax(0,1fr);gap:18px;align-items:start}
.assistant-rail{position:sticky;top:14px;display:flex;flex-direction:column;border:1px solid var(--line);background:#fff;max-height:calc(100vh - 120px);overflow:auto}
.assistant-rail .arow{display:flex;gap:8px;align-items:center;padding:10px 11px;border-bottom:1px solid var(--line)}
.assistant-rail .arow:last-child{border-bottom:0}
.assistant-rail .arow.cur{background:#fff;box-shadow:inset 2px 0 0 #000}
.assistant-rail .arow:hover{background:var(--hover)}
.assistant-rail .arow .t{flex:1;min-width:0;font-size:.84rem;font-weight:500;color:var(--fg);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.assistant-rail .arow .t:hover{text-decoration:underline}
.assistant-rail .arow .when{font-size:.7rem;color:var(--dim);white-space:nowrap}
.chat-spin{width:11px;height:11px;flex:0 0 auto;border:2px solid var(--line);border-top-color:#000;border-radius:50%;animation:chatspin .8s linear infinite}
@keyframes chatspin{to{transform:rotate(360deg)}}
.rail-h{padding:8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:#fff;z-index:2}
.rail-h input{width:100%;padding:6px 9px;border:1px solid var(--line);font:inherit;font-size:.82rem;background:var(--bg)}
.rgroup{font-size:.66rem;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);padding:9px 11px 3px}
.assistant-main .jumpwrap,.assistant-main #pending-panel,.assistant-main #aform{max-width:820px;width:100%;margin-left:auto;margin-right:auto}
.assistant-flex{display:flex;flex-direction:column;height:calc(100vh - 300px);min-height:480px}
.jumpwrap{position:relative;flex:1;min-height:0;display:flex}
.chat{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:18px;background:var(--bg);border:1px solid var(--line);padding:18px 16px;scroll-behavior:smooth}
#jump{position:absolute;right:16px;bottom:12px;z-index:5;box-shadow:0 4px 14px rgba(0,0,0,.15)}
.crow{display:flex;gap:10px;align-items:flex-start}
.crow.user{flex-direction:row-reverse}
.crow:not(.user) + .crow:not(.user){margin-top:-10px}
.crow.user + .crow.user{margin-top:-10px}
.chatlive{display:flex;flex-direction:column;gap:18px}
.avatar{flex:0 0 30px;width:30px;height:30px;display:flex;align-items:center;justify-content:center;font-size:.62rem;font-weight:700;letter-spacing:.05em;border:1px solid var(--line)}
.avatar.you{background:#000;color:#fff;border-color:#000}
.avatar.ai{background:var(--acc);color:#fff;border-color:var(--acc)}
.bubble{max-width:75%;padding:10px 14px;font-size:.92rem;line-height:1.55;overflow-wrap:anywhere}
.bubble.user{background:#000;border:1px solid #000;color:#fff}
.bubble.user .meta{color:#aaa}
.bubble.ai{background:#fff;border:1px solid #000;min-width:180px}
.bubble .meta{font-size:.72rem;color:var(--dim);margin-top:8px;display:flex;gap:10px;align-items:center;justify-content:flex-end}
.bubble.user .meta{justify-content:flex-start}
.status{font-size:.78rem;color:var(--dim);margin-bottom:6px}
.think{margin:2px 0 8px}
.think summary{cursor:pointer;font-size:.8rem;color:var(--dim);user-select:none;list-style:none}
.think summary::-webkit-details-marker{display:none}
.think summary::before{content:'\25B8 ';font-size:.7rem}
.think[open] summary::before{content:'\25BE '}
.think pre{white-space:pre-wrap;font-family:var(--mono);font-size:.78rem;color:#444;margin:6px 0 2px;padding:8px 10px;background:#fff;border:1px solid var(--line);max-height:260px;overflow:auto}
/* audit rows + reasoning traces (viewer + simulator) - keep in the base sheet: a page
   <style> block is only served on its own page (the audit <pre> overflowed this way) */
.arow2{display:flex;flex-direction:column;gap:6px;padding:9px 0;border-top:1px solid var(--line);font-size:.84rem}
.arow2:first-of-type{border-top:0;padding-top:2px}
.arow2 .ahead{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
.arow2 .atime{font-size:.72rem;color:var(--dim)}
.arow2 .adetail{min-width:0;overflow-wrap:anywhere}
.audit-pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.78rem;color:var(--dim);margin:6px 0 0;max-height:280px;overflow:auto}
.simul{margin:4px 0 0;padding-left:18px;font-size:.88rem}
.simul li{margin:3px 0}
/* key -> value stat rows (dashboard + learning page) - shared on purpose */
.dsrow{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:9px 0;border-top:1px solid var(--line);font-size:.88rem}
.dsrow:last-child{border-bottom:1px solid var(--line)}
.dsk{color:var(--dim)}
.dsv{font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
.dsv .dsp{font-weight:400;color:var(--dim);font-size:.78rem;margin-left:3px}
.tools{margin:2px 0 8px}
.tools>summary{cursor:pointer;list-style:none;font-size:.75rem;font-family:var(--mono);color:var(--dim);border:1px solid var(--line);background:#fff;padding:2px 22px 2px 10px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;position:relative}
.tools>summary::-webkit-details-marker{display:none}
.tools>summary:hover{color:#000;border-color:#000}
.tools>summary::after{content:'\25B8';position:absolute;right:8px;top:1px;font-size:.7rem}
.tools[open]>summary::after{content:'\25BE'}
.tools-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.tools[open] .tool-chip{white-space:normal;overflow-wrap:anywhere}
.tool-chip{font-size:.75rem;font-family:var(--mono);border:1px solid var(--line);padding:2px 10px;color:var(--dim);background:#fff;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tool-chip.ok{color:var(--ok);border-color:var(--ok)}
.tool-chip.err{color:var(--err);border-color:var(--err)}
.copy{cursor:pointer;background:#fff;border:1px solid var(--line2);color:var(--dim);font-size:.7rem;padding:1px 7px}
.regen{cursor:pointer;background:#fff;border:1px solid var(--line2);color:var(--dim);font-size:.85rem;line-height:1;padding:2px 8px;font-family:inherit}
.regen:hover{color:#000;border-color:#000}
.copy:hover{color:#000;border-color:#000}
.proposal{background:#fff;border:1px solid var(--line);padding:10px 12px;margin:10px 0 2px}
.p-tag{display:block;font-size:.64rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--acc);margin:0 0 7px}
.p-tag.warn{color:var(--warn)}
.composer{background:#fff;border:1px solid var(--line);padding:10px 12px 8px;margin-top:10px}
.composer:focus-within{border-color:#000}
.composer textarea{width:100%;border:none;background:transparent;color:var(--fg);font:inherit;resize:none;outline:none;min-height:26px;max-height:190px;display:block}
.composer textarea::placeholder{color:#767676}
.comp-row{display:flex;gap:8px;align-items:flex-end;margin-top:6px;flex-wrap:wrap}
.hint{font-size:.75rem;color:var(--dim);margin-top:6px;border-top:1px solid var(--line);padding-top:6px}
.chat-empty{margin:auto;text-align:center;max-width:600px;padding:30px 10px}
.ce-icon{font-size:1.8rem;opacity:.35}
.ce-title{font-size:1.15rem;font-weight:700;letter-spacing:-.02em;margin:8px 0 6px}
.chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:14px}
@media (max-width:640px){.bubble{max-width:86%}.assistant-flex{height:calc(100vh - 250px);min-height:400px}.chat{padding:12px 10px}
  .assistant-shell{grid-template-columns:1fr}.assistant-rail{position:static;max-height:180px}}
/* ---- assistant sidebar (docked right; collapses to a rail) ---- */
.asb{position:fixed;top:0;right:0;bottom:0;z-index:45;width:46px;background:#fff;border-left:1px solid var(--line);display:none;flex-direction:row}
body.with-asb .asb{display:flex}
body.with-asb .main{margin-right:46px;transition:margin-right .15s ease}
.asb{transition:width .15s ease}
.asb-grip{position:absolute;left:0;top:0;bottom:0;width:6px;cursor:col-resize;z-index:2;touch-action:none}
.asb-grip:hover,.asb-grip:focus-visible{background:var(--hover);outline:none}
html:not(.asb-open) .asb-grip{display:none}
html.asb-resizing .asb,html.asb-resizing body.with-asb .main{transition:none}
html.asb-resizing body{user-select:none;-webkit-user-select:none}
html.asb-open body.with-asb .main{margin-right:var(--asb-w,352px)}
html.asb-open .asb{width:var(--asb-w,352px)}
.asb-rail{width:46px;flex:none;display:flex;flex-direction:column;align-items:center;gap:10px;padding-top:12px;background:#fff;cursor:pointer}
.asb-rail:hover{background:var(--hover)}
.asb-rail button{border:0;background:none;font-size:1.05rem;color:var(--ink);cursor:pointer;padding:6px;line-height:1}
.asb-rail .lab{writing-mode:vertical-rl;font-size:.7rem;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
html.asb-open .asb-rail{display:none}
html:not(.asb-open) .asb-main{display:none}
.asb-main{flex:1;min-width:0;display:flex;flex-direction:column}
@media(max-width:1023px){
  body.with-asb .asb,html.asb-open body.with-asb .asb{display:none}
  body.with-asb .main{margin-right:0 !important}
}
.dw-head{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line);background:#fff}
.dw-ctx{font-size:.73rem;color:var(--dim);padding:5px 12px;border-bottom:1px solid var(--line);background:var(--card2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.am-ctx{display:inline-flex;align-items:center;gap:6px;font-size:.72rem;color:var(--dim);border:1px solid var(--line);background:#fff;padding:3px 9px;width:fit-content;margin:0 0 7px}
  .am-ctx[hidden]{display:none}
.dw-hist{border-bottom:1px solid var(--line);max-height:42vh;overflow:auto;background:#fff}
.dhist-item{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.dhist-item:hover{background:var(--hover)}
.dhist-item .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.84rem}
.dhist-item .when{font-size:.7rem;color:var(--dim);white-space:nowrap}
.dhist-item.cur{background:var(--hover)}
.chat-del,.arm-del{transition:background .12s ease,color .12s ease,border-color .12s ease}
.chat-del svg,.arm-del svg{display:block}
.chat-del.armed,.arm-del.armed{background:var(--err);border-color:var(--err);color:#fff}
.dw-body{flex:1;min-height:0;display:flex;flex-direction:column;background:#fff}
.dw-body .chat{border:0;background:#fff;padding:14px 12px}
.dw-comp{margin:0;border-left:0;border-right:0;border-bottom:0}
/* ================= mobile shell (docs/mobile-ui.md) ================= */
:root{color-scheme:light}
*{-webkit-tap-highlight-color:transparent}
html{touch-action:manipulation;overscroll-behavior-y:contain}
.btn,.nav-item,.iconbtn,.bottom-nav a,.more-row{transition:transform .1s ease,opacity .1s ease}
.btn:active,.nav-item:active,.iconbtn:active,.bottom-nav a:active,.more-row:active{transform:scale(.97);opacity:.85}
.bottom-nav{display:none}
@media (pointer:coarse){
  .btn{min-height:44px}
  .btn.small,.iconbtn{min-height:44px;min-width:44px}
  .menu-item{min-height:44px}
  .px-sw{min-height:44px;min-width:44px;justify-content:center}
  .setrow input:not([type=checkbox]):not([type=radio]),
  input[type=text],input[type=number],input[type=password],input[type=search],
  input[type=email],input[type=url],input[type=tel],select,textarea{font-size:16px}
  input:not([type=checkbox]):not([type=radio]),select{min-height:44px}
  .nav-item{min-height:48px}
  .copy{padding:8px 10px}
}
@media(max-width:1023px){
  .topbar{padding-top:calc(env(safe-area-inset-top) + 10px)}
  .assistant-flex{height:calc(100dvh - 300px)}
  .assistant-shell{grid-template-columns:1fr}
  .assistant-rail{position:static;max-height:220px}
}
@media(max-width:767px){
  .bottom-nav{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:180;
    background:#fff;border-top:1px solid var(--line);
    padding:4px 6px;padding-left:calc(6px + env(safe-area-inset-left));
    padding-right:calc(6px + env(safe-area-inset-right));
    padding-bottom:calc(4px + env(safe-area-inset-bottom))}
  .bottom-nav a{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
    min-height:48px;font-size:.68rem;letter-spacing:.01em;color:var(--dim);text-decoration:none;font-weight:500}
  .bottom-nav a svg{width:21px;height:21px;stroke-width:1.7;opacity:.8}
  .bottom-nav a.on{color:#000;font-weight:600}
  .bottom-nav a.on svg{opacity:1}
  .topbar #menuBtn{display:none}
  .content{padding-bottom:calc(92px + env(safe-area-inset-bottom))}
  .savebar{bottom:calc(72px + env(safe-area-inset-bottom))}
  .page-desc{display:none}
  .foot{display:none}
  .am-head{display:none}
  .assistant-shell{display:block;margin-top:0}
  .assistant-shell > .assistant-rail{display:none}
  .chat-head{display:flex;align-items:center;gap:4px;margin:0 0 8px}
  .chat-head .ch-title{flex:1;text-align:center;font-weight:600;font-size:.95rem}
  .chat-head .iconbtn{min-width:44px;min-height:44px;display:flex;align-items:center;justify-content:center;color:var(--fg);border:0;background:transparent}
  .assistant-main{display:flex;flex-direction:column;height:calc(100vh - 160px);height:calc(100dvh - 160px)}
  .assistant-flex{height:auto;flex:1;min-height:0}
  .jumpwrap{min-height:0}
  .composer .hint{display:none}
  .composer .comp-row .sub{display:none}
  .composer{display:flex;align-items:flex-end;gap:8px;flex-wrap:wrap}
  .composer .am-ctx{flex-basis:100%;margin-bottom:4px}
  .composer .aspecline{flex-basis:100%;margin-top:2px}
  .composer .comp-row,.composer .comp-row .row{display:contents}
  .composer textarea{flex:1;min-width:0;min-height:30px;max-height:120px}
  .composer .btn{flex:none;min-height:40px}
  .chat-empty{padding:14px 6px}
  .chat-empty .sub{display:none}
  .chat-empty .chips{margin-top:10px}
  .chat-empty .chip{font-size:.8rem;padding:7px 10px;background:var(--hover);border-color:transparent}
  .chat{display:flex;flex-direction:column}
  body:has(.assistant-main) .topbar{display:none}
  body:has(.assistant-main) .assistant-main{height:calc(100vh - 115px);height:calc(100dvh - 115px)}
  body.kb-open:has(.assistant-main){overflow:hidden}
  body.kb-open:has(.assistant-main) .assistant-main{height:var(--kb-h, calc(100dvh - 115px))}
  .crow .avatar{display:none}
  .bubble{max-width:88%}
  .bubble.ai{background:#fff;border:1px solid #000;padding:9px 12px;min-width:0}
  .sheet-ov{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:190}
  .sheet{position:absolute;top:0;bottom:0;left:0;width:min(86vw,340px);background:#fff;border-right:1px solid var(--line);display:flex;flex-direction:column}
  .sheet-h{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid var(--line)}
  .sheet-b{flex:1;overflow-y:auto}
  .sheet-b .assistant-rail{display:flex;position:static;max-height:none;border:0}
  .sheet-b .assistant-rail .arow{padding:13px 12px}
  body.kb-open .bottom-nav{display:none}
  .toasts{top:auto;bottom:calc(82px + env(safe-area-inset-bottom));left:12px;right:12px;max-width:none}
  .toast2{max-width:none}
  body:has(.assistant-main) .toasts{top:calc(env(safe-area-inset-top) + 8px);bottom:auto}
  .page-desc.msgfrom{display:block;font-size:.8rem;overflow-wrap:anywhere}
  .page-desc.logdesc{display:block;font-size:.8rem}
  .cond-head{display:none}
  .row{flex-wrap:wrap}
  .msgrid>*{min-width:0}
  .emailbody{overflow-wrap:anywhere}
  .emailbody table{width:auto !important;max-width:100% !important}
  .emailbody img{max-width:100% !important;height:auto !important}
  .note{overflow-wrap:anywhere}
  .msgrid form.inline select{min-width:0 !important;width:100% !important}
  .grid3 input[type=text]{grid-column:1/-1}
  .stepcard{grid-template-columns:26px minmax(0,1fr)}
  .steptools{grid-column:2;justify-content:flex-end;margin-top:6px;flex-wrap:wrap}
  .stepfields{grid-template-columns:1fr}
  .cond-extra{display:none}
  .cond-more{display:inline-flex;margin:2px 0 6px}
  .jumpwrap{min-height:420px}
  body.kb-open .jumpwrap{min-height:0}
  .setrow input:not([type=checkbox]):not([type=radio]),
  input[type=text],input[type=number],input[type=password],input[type=search],
  input[type=email],input[type=url],input[type=tel],select,textarea{font-size:16px}
}
@media(max-width:640px){pre.log{font-size:.78rem}}

/* plugins: list + detail share these */
.px-ico{width:36px;height:36px;flex:0 0 36px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line);background:var(--card2);color:var(--fg)}
.px-ico svg{width:18px;height:18px}
.px-grp{margin:18px 4px 6px;font-size:.76rem;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
.px-list{border:1px solid var(--line);background:var(--card)}
.px-row{display:flex;align-items:center;gap:12px;padding:12px 14px;border-top:1px solid var(--line)}
.px-row:first-child{border-top:0}
.px-row:hover{background:var(--card2)}
.px-main{flex:1;min-width:0;display:block;color:inherit}
.px-main:hover{text-decoration:none}
.px-nm{display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-weight:600;font-size:.95rem;color:var(--fg)}
.px-dz{display:block;color:var(--dim);font-size:.83rem;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* toggle switch: strict square corners, knob inset by --sw-pad on every side */
.px-sw{--sw-w:36px;--sw-h:21px;--sw-knob:15px;--sw-pad:2px;--sw-bw:1px;
  --sw-travel:calc(var(--sw-w) - var(--sw-knob) - 2*var(--sw-pad) - 2*var(--sw-bw));
  display:inline-flex;align-items:center;cursor:pointer;margin:0;position:relative}
.px-sw input{position:absolute;opacity:0;width:1px;height:1px}
form.px-swf{display:flex;align-items:center}
.px-sw .px-tr{width:var(--sw-w);height:var(--sw-h);border:var(--sw-bw) solid var(--line2);background:var(--line);border-radius:0;position:relative;display:inline-block;transition:background-color .15s ease,border-color .15s ease}
.px-sw .px-tr::after{content:"";box-sizing:border-box;position:absolute;top:var(--sw-pad);left:var(--sw-pad);width:var(--sw-knob);height:var(--sw-knob);background:var(--card);border:var(--sw-bw) solid var(--line2);border-radius:0;transition:transform .15s ease,border-color .15s ease}
.px-sw input:checked+.px-tr{background:var(--acc);border-color:var(--acc)}
.px-sw input:checked+.px-tr::after{transform:translateX(var(--sw-travel));border-color:transparent}
.px-sw input:focus-visible+.px-tr{outline:2px solid var(--acc);outline-offset:2px}
.px-go{color:var(--dim);font-size:1.2rem;line-height:1;padding:0 2px}
.px-go:hover{color:var(--fg);text-decoration:none}
</style>
<style>
/* Workbench primitives: shared by list, editor previews and settings. */
[hidden]{display:none!important}
.ux-search{display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin:14px 0}
.ux-search>label{flex:1;min-width:160px;margin:0}.ux-search input{margin-top:5px}
.ux-filters{margin:8px 0 14px}.ux-filters .grid3{margin-top:10px}
.ux-preview{margin:14px 0;scroll-margin-top:18px}.ux-preview>summary{cursor:pointer;font-weight:600}
.ux-preview label{margin-top:10px}.ux-preview-output{margin-top:12px;white-space:normal}
.ux-preview-output li{margin:6px 0}.ux-preview-output pre{white-space:pre-wrap;overflow-wrap:anywhere}
.ux-context{display:flex;align-items:center;gap:8px}.ux-context button{margin-left:auto;min-width:32px;min-height:32px;background:none;border:0;cursor:pointer}
.ux-advanced{margin:12px 0}.ux-advanced>summary{cursor:pointer;font-weight:600;padding:8px 0}
.ux-demo{padding:10px 14px;border:1px solid var(--acc);background:var(--hover);margin-bottom:14px;font-size:.85rem}
@media(min-width:1024px) and (max-width:1599px){html.asb-open body.with-asb .main{margin-right:46px}html.asb-open .asb{box-shadow:-10px 0 30px #0002}}
</style>
<script>try{var v=localStorage.getItem('asb_open');if(v==='1')document.documentElement.classList.add('asb-open');var w=parseInt(localStorage.getItem('asb_w')||'',10);if(w>=280)document.documentElement.style.setProperty('--asb-w',Math.min(720,w)+'px');}catch(e){}</script>
</head><body{% if setup or show_asb %} class="{{ (('setup ' if setup else '') + ('with-asb' if show_asb else ''))|trim }}"{% endif %}>
<a class="skip" href="#main">Skip to content</a>
{% set p = request.path %}
{% macro navitem(href, label, active, icon, badge=0, current=True) -%}
<a class="nav-item{{ ' active' if active else '' }}" href="{{ href }}"{{ ' aria-current="page"'|safe if (active and current) else '' }}>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{{ icon|safe }}</svg>{{ label }}{% if badge %}<span class="badge warn" style="margin-left:auto">{{ badge }}</span>{% endif %}</a>
{%- endmacro %}
{% macro navsub(href, label, active, icon) -%}
<a class="nav-item nav-sub-item{{ ' active' if active else '' }}" href="{{ href }}"{{ ' aria-current="page"'|safe if active else '' }}>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{{ icon|safe }}</svg>{{ label }}</a>
{%- endmacro %}
<div class="app">
  <div class="scrim hidden" id="scrim"></div>
  <aside class="side" id="side" aria-label="Main navigation">
    <div class="side-top">
      <a class="brand" href="{{ url_for('dashboard') }}"><span class="brand-mark">MT</span><span class="brand-name">Mail Triage</span></a>
      <div class="brand-sub">{{ info.imap_user or 'not connected' }}</div>
    </div>
    <nav class="nav">
      <div class="nav-label">Mail</div>
      {{ navitem(url_for('dashboard'), 'Dashboard', p == '/', '
        <rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/>') }}
      {{ navitem(url_for('messages'), 'Messages', p.startswith('/messages'), '
        <path d="M4 6h16v12H4z"/><path d="m4 7 8 6 8-6"/>') }}
      {{ navitem(url_for('assistant'), 'Assistant', p.startswith('/assistant'), '
        <path d="M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z"/>', pend) }}
      {% for it in ext_nav if it.group == 'mail' %}{{ navitem(it.url, it.label, p.startswith(it.base), it.icon) }}{% endfor %}
      <div class="nav-label">Automation</div>
      {{ navitem(url_for('automation'), 'Automation', ws_section is not none, '
        <path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="7" cy="18" r="2"/>', current=False) }}
      {% if ws_section %}
      <div class="nav-sub">
        {{ navsub(url_for('automation'), 'Overview', ws_section == 'overview', '
          <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>') }}
        {{ navsub(url_for('rules'), 'Rules', ws_section == 'rules', '
          <path d="M4 5h16M7 12h10M10 19h4"/>') }}
        {{ navsub(url_for('flows'), 'Flows', ws_section == 'flows', '
          <path d="M5 5h6a4 4 0 0 1 4 4v6a4 4 0 0 0 4 4h1"/><circle cx="4" cy="5" r="2"/><circle cx="20" cy="19" r="2"/>') }}
        {{ navsub(url_for('automation_categories'), 'Categories & filing', ws_section == 'categories', '
          <path d="M3 7h7l2 2h9v10H3z"/>') }}
        {{ navsub(url_for('templates'), 'Drafting', ws_section == 'drafting', '
          <path d="M12 20h9"/><path d="M16.5 3.5 20.5 7.5 8 20H4v-4z"/>') }}
        {{ navsub(url_for('automation_controls'), 'Controls', ws_section == 'controls', '
          <rect x="3" y="7" width="18" height="10" rx="5"/><circle cx="15" cy="12" r="2.5"/>') }}
      </div>
      {% endif %}
      {{ navitem(url_for('simulate'), 'Simulator', p.startswith('/simulate'), '
        <path d="M9 3v6l-5 8a2 2 0 0 0 1.7 3h12.6a2 2 0 0 0 1.7-3l-5-8V3"/><path d="M7 3h10"/>') }}
      {{ navitem(url_for('learning_page'), 'Learning', p.startswith('/learning'), '
        <path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>') }}
      {% for it in ext_nav if it.group == 'automation' %}{{ navitem(it.url, it.label, p.startswith(it.base), it.icon) }}{% endfor %}
      <div class="nav-label">System</div>
      {{ navitem(url_for('accounts'), 'Accounts', p.startswith('/accounts'), '
        <circle cx="9" cy="8" r="3"/><path d="M3 20c0-3 3-5 6-5s6 2 6 5"/><path d="M16 8h5M18.5 5.5v5"/>') }}
      {{ navitem(url_for('settings'), 'Settings', p.startswith('/settings'), '
        <circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.5-2.4 1a7 7 0 0 0-1.7-1L14.5 3h-5L9 6a7 7 0 0 0-1.7 1l-2.4-1-2 3.5 2 1.5a7 7 0 0 0 0 2l-2 1.5 2 3.5 2.4-1a7 7 0 0 0 1.7 1l.5 3h5l.5-3a7 7 0 0 0 1.7-1l2.4 1 2-3.5-2-1.5c.06-.3.1-.66.1-1z"/>') }}
      {{ navitem(url_for('plugins_page'), 'Plugins', p.startswith('/plugins'), '
        <path d="M9 3v5M15 3v5M6 8h12v4a6 6 0 0 1-12 0z"/><path d="M12 18v3"/>') }}
      {{ navitem(url_for('log'), 'Log', p.startswith('/log') or p.startswith('/proxy/log'), '
        <path d="M4 4h16v16H4z"/><path d="m8 9 3 3-3 3M13 15h4"/>') }}
      {% for it in ext_nav if it.group == 'system' %}{{ navitem(it.url, it.label, p.startswith(it.base), it.icon) }}{% endfor %}
    </nav>
    <div class="side-foot">
      <div class="sf-row">
        <span class="dot {{ 'ok' if not w.err else 'err' }}"></span>
        <span>{{ 'checks every %ss'|format(w.interval) if not w.err else 'last check failed' }}</span>
      </div>
      <div class="sf-row"><span><time datetime="{{ w.last_ok_iso }}" data-rel data-label="Last check ">Last check {{ w.last_ok_r }}</time></span></div>
      <div class="sf-row"><span>Times in {{ tz }}</span></div>
    </div>
  </aside>
  <div class="main">
    <header class="topbar">
      <button class="iconbtn" id="menuBtn" aria-label="Open navigation" aria-expanded="false" aria-controls="side">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
      </button>
      <span class="tb-title">Mail Triage</span>
      <span class="tb-status"><span class="dot {{ 'ok' if not w.err else 'err' }}"></span>{% if w.err %}last check failed{% else %}<time datetime="{{ w.last_ok_iso }}" data-rel data-label="Last check ">Last check {{ w.last_ok_r }}</time>{% endif %}</span>
    </header>
    <main id="main" class="content">
      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for cat, msg in messages %}<div class="msg {{ cat }}" role="status">{{ msg }}</div>{% endfor %}
      {% endwith %}
      {% if cfg.get('UX_DEMO') == '1' %}<div class="ux-demo"><b>UX demo</b> · Synthetic mailbox and mock AI · Changes affect this sandbox only.</div>{% endif %}
      {{ body|safe }}
      <div class="foot">Times in {{ tz }} · app data in {{ cfg.DATA_DIR }} · no permanent deletion · optional Move to Trash follows your mail provider's retention policy</div>
    </main>
  </div>
</div>
<nav class="bottom-nav" aria-label="Mobile navigation">
  <a href="{{ url_for('dashboard') }}" class="{{ 'on' if p == '/' else '' }}" {{ 'aria-current="page"'|safe if p == '/' else '' }}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>
    <span>Dashboard</span></a>
  <a href="{{ url_for('messages') }}" class="{{ 'on' if p.startswith('/messages') else '' }}" {{ 'aria-current="page"'|safe if p.startswith('/messages') else '' }}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16v12H4z"/><path d="m4 7 8 6 8-6"/></svg>
    <span>Messages</span></a>
  <a href="{{ url_for('assistant') }}" class="{{ 'on' if p.startswith('/assistant') else '' }}" {{ 'aria-current="page"'|safe if p.startswith('/assistant') else '' }}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z"/></svg>
    <span>Assistant</span></a>
  {% set morepaths = ('/more','/welcome','/automation','/rules','/flows','/classifiers','/templates','/accounts','/settings','/plugins','/extensions','/log','/proxy') %}
  <a href="{{ url_for('more') }}" class="{{ 'on' if p.startswith(morepaths) else '' }}" {{ 'aria-current="page"'|safe if p.startswith(morepaths) else '' }}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>
    <span>More</span></a>
</nav>
<div class="toasts" id="toasts" aria-live="polite"></div>
<script>
function toast(msg, kind){
  var box = document.getElementById('toasts');
  if(!box){ alert(msg); return; }
  var el = document.createElement('div');
  el.className = 'toast2' + (kind ? ' ' + kind : '');
  el.setAttribute('role', 'status');
  el.textContent = msg;
  box.innerHTML = '';
  box.appendChild(el);
  setTimeout(function(){ el.style.opacity = '0'; el.style.transition = 'opacity .4s'; }, 4600);
  setTimeout(function(){ el.remove(); }, 5200);
}
if(!window.__mtTicker){ window.__mtTicker = 1;
(function(){
  function relAge(iso){
    var d=(Date.now()-new Date(iso).getTime())/1000; if(d<0) d=0;
    if(d<90) return Math.round(d)+'s ago';
    if(d<5400) return Math.round(d/60)+'m ago';
    if(d<129600) return Math.round(d/3600)+'h ago';
    return Math.round(d/86400)+'d ago';
  }
  function tick(){
    document.querySelectorAll('time[data-rel]').forEach(function(t){
      var iso=t.getAttribute('datetime'); if(!iso) return;
      t.textContent=(t.getAttribute('data-label')||'')+relAge(iso);
    });
  }
  setInterval(function(){ if(!document.hidden) tick(); }, 30000);
})();
}
/* two-step delete: first activation arms (red trash / "Confirm delete"), the
   second one deletes. Used by the chat lists and every list-item delete. */
if(!window.__mtArmDel){ window.__mtArmDel = 1;
(function(){
  var TRASH='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M10 7V5h4v2m-6 0 1 13h6l1-13M10 11v6M14 11v6"/></svg>';
  function disarm(b){
    if(!b.classList.contains('armed')) return;
    b.classList.remove('armed');
    if(b._armSaved){
      b.innerHTML=b._armSaved.html;
      if(b._armSaved.al !== null){ b.setAttribute('aria-label', b._armSaved.al); } else { b.removeAttribute('aria-label'); }
      b._armSaved=null;
    }
    if(b._armT){ clearTimeout(b._armT); b._armT=null; }
  }
  function disarmAll(except){ document.querySelectorAll('.chat-del.armed,.arm-del.armed').forEach(function(b){ if(b!==except) disarm(b); }); }
  function arm(b){
    disarmAll(b);
    if(b.classList.contains('armed')) return;
    if(!b._armSaved) b._armSaved={html:b.innerHTML, al:b.getAttribute('aria-label')};
    b.classList.add('armed');
    if(b.classList.contains('chat-del')){
      b.innerHTML=TRASH;
      b.setAttribute('aria-label', b.getAttribute('data-arm-label') || 'Press again to delete this chat');
    } else {
      b.textContent='Confirm delete';
      b.setAttribute('aria-label', b.getAttribute('data-arm-label') || 'Press again to delete');
    }
    b._armT=setTimeout(function(){ disarm(b); }, 4000);
  }
  document.addEventListener('click', function(e){
    var b = e.target && e.target.closest ? e.target.closest('.chat-del,.arm-del') : null;
    if(!b){ disarmAll(null); return; }
    if(b.classList.contains('armed')) return;
    e.preventDefault(); e.stopPropagation();
    arm(b);
  }, true);
  document.addEventListener('keydown', function(e){ if(e.key==='Escape') disarmAll(null); });
  document.addEventListener('toggle', function(e){ if(e.target && e.target.tagName==='DETAILS' && !e.target.open) e.target.querySelectorAll('.armed').forEach(disarm); }, true);
})();
}
function cp(text, el){
  function done(){ if(el){ var t = el.textContent; el.textContent = 'copied'; setTimeout(function(){ el.textContent = t; }, 900); } }
  if(navigator.clipboard && window.isSecureContext){ navigator.clipboard.writeText(text).then(done, fallback); }
  else { fallback(); }
  function fallback(){
    var ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select(); try{ document.execCommand('copy'); }catch(e){} ta.remove(); done();
  }
}
(function(){
  var side = document.getElementById('side'), scrim = document.getElementById('scrim'),
      btn = document.getElementById('menuBtn');
  function setOpen(open){
    if(!side) return;
    side.classList.toggle('open', open);
    if(scrim) scrim.classList.toggle('hidden', !open);
    if(btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  if(btn) btn.addEventListener('click', function(){ setOpen(!side.classList.contains('open')); });
  if(scrim) scrim.addEventListener('click', function(){ setOpen(false); });
  document.querySelectorAll('.nav-item').forEach(function(a){ a.addEventListener('click', function(){ setOpen(false); }); });
  if(!window.__mtSide1){
    window.__mtSide1 = 1;
    document.addEventListener('keydown', function(e){ if(e.key === 'Escape'){
      var s=document.getElementById('side'); if(s) s.classList.remove('open');
      var sc=document.getElementById('scrim'); if(sc) sc.classList.add('hidden');
      var b=document.getElementById('menuBtn'); if(b) b.setAttribute('aria-expanded','false');
    } });
    document.addEventListener('click', function(e){
      document.querySelectorAll('details.menu[open]').forEach(function(d){
        if(!d.contains(e.target)) d.removeAttribute('open');
      });
    });
  }
})();
/* in-flight guard: a real form submission disables its submit button (after any
   confirm()); reset before Turbo caches the page so Back stays usable. */
if(!window.__mtSubmitBusy){ window.__mtSubmitBusy = 1;
(function(){
  function clearBusy(){
    document.querySelectorAll('[data-busy]').forEach(function(b){
      b.removeAttribute('data-busy');
      if(b.tagName === 'BUTTON') b.disabled = false;
    });
  }
  document.addEventListener('submit', function(e){
    if(e.defaultPrevented) return;
    var f = e.target;
    if(!f || f.tagName !== 'FORM' || f.hasAttribute('data-no-busy')) return;
    var b = e.submitter || f.querySelector('button[type=submit],input[type=submit]');
    if(!b || b.disabled) return;
    b.setAttribute('data-busy', '1');
    b.disabled = true;
  });
  document.addEventListener('turbo:before-cache', clearBusy);
  window.addEventListener('pageshow', clearBusy);
})();
}
</script>

{% if show_asb %}
<aside id="asb" class="asb" data-turbo-permanent aria-label="Assistant sidebar">
  <div class="asb-grip" id="asb-grip" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Resize the assistant sidebar" title="Drag to resize — double-click to reset"></div>
  <div class="asb-rail" id="asb-rail" title="Expand the assistant">
    <button type="button" id="asb-toggle" aria-label="Expand the assistant">&#10022;</button>
    <span class="lab">Assistant</span>
  </div>
  <div class="asb-main">
  <div class="dw-head">
    <b style="font-size:.9rem">&#10022; Assistant</b>
    <span class="row" style="margin-left:auto;gap:6px">
      <button type="button" class="btn small" id="dnew">New</button>
      <button type="button" class="btn small" id="dhist">History</button>
      <button type="button" class="btn small" id="dclose" aria-label="Collapse the assistant" title="Collapse">&#187;</button>
    </span>
  </div>
  <div id="dhistlist" class="dw-hist hidden"></div>
  <div class="dw-ctx" id="dwctx" hidden></div>
  <div class="dw-body"><div class="chat" id="dchat"></div></div>
  <form id="dform" class="composer dw-comp">
    <textarea id="dmsg" rows="1" enterkeyhint="send" placeholder="Message the assistant&hellip;"></textarea>
    <div class="comp-row">
      <span class="sub" style="font-size:.75rem">Enter sends &middot; Shift+Enter new line</span>
      <span class="row" style="margin-left:auto">
        <button class="btn danger" type="button" id="dstop" style="display:none">Stop</button>
        <button class="btn primary" type="submit" id="dsend">Send</button>
      </span>
    </div>
  </form>
  </div>
</aside>
{% endif %}

<script>
/* ---- assistant chat: shared engine for the page and the sidebar ---- */
(function(){
function esc(s){return (s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function mdRender(src){
  var raw=(src||'').replace(/\r\n/g,'\n');
  var lines=raw.split('\n'), out=[], i=0;
  function link(s){return s.replace(/\[msg:(\d+)\]/g,'<a href="/messages/$1">[msg:$1]</a>');}
  function inline(s){
    s=esc(s);
    var spans=[];
    s=s.replace(/`([^`\n]+)`/g,function(m,p1){spans.push(p1);return '\x01I'+(spans.length-1)+'\x01';});
    s=s.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>');
    s=s.replace(/__([^_]+)__/g,'<b>$1</b>');
    s=s.replace(/(^|[^\w*])\*([^*\n]+)\*(?![\w*])/g,'$1<i>$2</i>');
    s=s.replace(/(^|[^\w_])_([^_\n]+)_(?![\w_])/g,'$1<i>$2</i>');
    s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|\/(?![\/\\])[^)\s\\]*)\)/g,function(m,l,h){return h.indexOf('http')===0?'<a href="'+h+'" target="_blank" rel="noopener">'+l+'</a>':'<a href="'+h+'">'+l+'</a>';});
    for(var k=0;k<spans.length;k++) s=s.replace('\x01I'+k+'\x01','<code>'+spans[k]+'</code>');
    return s;
  }
  while(i<lines.length){
    var st=lines[i].trim();
    if(!st){i++;continue;}
    if(/^```/.test(st)){
      var buf=[]; i++;
      while(i<lines.length && !/^\s*```/.test(lines[i])){buf.push(lines[i]);i++;}
      i++;
      out.push('<pre class="md-pre">'+esc(buf.join('\n'))+'</pre>');
      continue;
    }
    if(/^(-{3,}|\*{3,})$/.test(st)){out.push('<hr>');i++;continue;}
    var hm=st.match(/^#{1,6}\s+(.*)$/);
    if(hm){out.push('<div class="md-h">'+link(inline(hm[1]))+'</div>');i++;continue;}
    if(/^>/.test(st)){
      var bq=[];
      while(i<lines.length && /^\s*>/.test(lines[i])){bq.push(link(inline(lines[i].trim().replace(/^>\s?/,''))));i++;}
      out.push('<blockquote>'+bq.join('<br>')+'</blockquote>');
      continue;
    }
    if(/^[-*+]\s+/.test(st)){
      var ul=[];
      while(i<lines.length && /^\s*[-*+]\s+/.test(lines[i])){ul.push('<li>'+link(inline(lines[i].trim().replace(/^[-*+]\s+/,'')))+'</li>');i++;}
      out.push('<ul>'+ul.join('')+'</ul>');
      continue;
    }
    if(/^\d+[.)]\s+/.test(st)){
      var ol=[];
      while(i<lines.length && /^\s*\d+[.)]\s+/.test(lines[i])){ol.push('<li>'+link(inline(lines[i].trim().replace(/^\d+[.)]\s+/,'')))+'</li>');i++;}
      out.push('<ol>'+ol.join('')+'</ol>');
      continue;
    }
    var par=[];
    while(i<lines.length){
      var s2=lines[i].trim();
      if(!s2 || /^([-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>|```)/.test(s2) || /^(-{3,}|\*{3,})$/.test(s2)) break;
      par.push(link(inline(s2))); i++;
    }
    if(par.length) out.push('<p>'+par.join('<br>')+'</p>');
  }
  return out.join('\n');
}
window.mdRender = mdRender;

/* view-transition direction: pages sit in a fixed order (tab-bar order, then
   depth within each section - edit VTPOS when adding pages). Moving to a LATER
   page slides in from the right, moving to an EARLIER page slides in from the
   left. Backlink clicks always count as back; unknown pairs fall back to
   Turbo's visit action. */
if(!window.__mtVt){
  window.__mtVt = 1;
  var vtRoot = document.documentElement;
  var VTPOS = [
    ['/', 1, 0],            /* dashboard */
    ['/messages/', 0, 11],  /* message viewer */
    ['/messages', 1, 10],   /* message list (filters/pages share this) */
    ['/assistant', 0, 20],
    ['/automation/categories', 1, 35],
    ['/automation/controls', 1, 38],
    ['/automation', 1, 30],
    ['/rules/', 0, 32], ['/rules', 1, 31],
    ['/flows/', 0, 34], ['/flows', 1, 33],
    ['/templates/', 0, 37], ['/templates', 1, 36],
    ['/simulate', 0, 39],
    ['/classifiers/', 0, 41], ['/classifiers', 1, 40],
    ['/learning', 0, 42],
    ['/accounts/', 0, 71], ['/accounts', 1, 70],
    ['/settings', 0, 80],
    ['/more', 0, 90],
    ['/log', 0, 91],
    ['/proxy', 0, 92],
    ['/extensions', 0, 95]
  ];
  function vtPos(path){
    for (var i = 0; i < VTPOS.length; i++){
      var p = VTPOS[i][0];
      if (VTPOS[i][1] ? path === p : path.indexOf(p) === 0) return VTPOS[i][2];
    }
    return null;
  }
  function vtDir(toPath){
    var from = vtPos(location.pathname), to = vtPos(toPath);
    if (from !== null && to !== null && from !== to) return to > from ? 'forward' : 'back';
    return null;
  }
  vtRoot.setAttribute('data-vt-dir', 'forward');
  document.addEventListener('click', function(e){
    var b = e.target && e.target.closest ? e.target.closest('.backlink') : null;
    if(b && e.target.closest('a')) window.__mtBack = true;
  }, true);
  document.addEventListener('turbo:visit', function(e){
    var a = e.detail && e.detail.action;
    var toPath = '';
    try { toPath = new URL((e.detail && e.detail.url) || '', location.href).pathname; } catch(err){}
    var dir = window.__mtBack ? 'back' : vtDir(toPath);
    if(!dir) dir = (a === 'restore') ? 'back' : 'forward';
    window.__mtBack = false;
    vtRoot.setAttribute('data-vt-dir', dir);
  });
}
if(!window.__mtCopy){ window.__mtCopy = 1;
document.addEventListener('click', function(e){
  var c=e.target.closest('.copy');
  if(!c) return;
  var txt=c.dataset.copy||'';
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(txt).then(function(){ c.textContent='copied'; setTimeout(function(){ c.textContent='copy'; },1500); }); }
  else { c.textContent='n/a'; }
});
}
if(!window.__mtRegen){ window.__mtRegen = 1;
document.addEventListener('click', function(e){
  var b=e.target.closest('.regen');
  if(!b) return;
  e.preventDefault();
  var scope=b.closest('.assistant-main, #asb')||document;
  var f=scope.querySelector('form.composer');
  var inst=f&&f.__chat;
  if(inst && inst.regen) inst.regen(b);
});
}

window.assistantChat = function(opts){
  var root=opts.root, form=opts.form, ta=opts.ta, btn=opts.sendBtn, stopBtn=opts.stopBtn,
      jumpBtn=opts.jump||null, sid=opts.sessionId||0, onSession=opts.onSession||null;
  if(!root||!form||!ta) return null;
  if(!(window.fetch && window.ReadableStream && window.TextDecoder)) return null;
  if(form.__chat){ form.__chat.setSession(sid); return form.__chat; }
  var live=document.createElement('div'); live.setAttribute('aria-live','polite'); live.setAttribute('aria-atomic','false');
  live.className='chatlive';
  root.appendChild(live);
  function mk(tag,cls,text){var d=document.createElement(tag); if(cls) d.className=cls; if(text!=null) d.textContent=text; return d;}
  function autosize(){ ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,190)+'px'; }
  ta.addEventListener('input', autosize);
  ta.addEventListener('keydown', function(e){ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); form.requestSubmit(); } });
  root.addEventListener('click', function(e){
    var ch=e.target.closest('[data-fill]');
    if(!ch) return;
    ta.value=ch.dataset.fill||''; autosize(); ta.focus();
  });
  function nearBottom(){ return (root.scrollHeight - root.scrollTop - root.clientHeight) < 120; }
  function scrollBottom(force){ if(force||nearBottom()){ root.scrollTop=root.scrollHeight; } if(jumpBtn){ jumpBtn.classList.toggle('hidden', nearBottom()); } }
  root.addEventListener('scroll', function(){ if(jumpBtn){ jumpBtn.classList.toggle('hidden', nearBottom()); } });
  if(jumpBtn){ jumpBtn.addEventListener('click', function(){ root.scrollTop=root.scrollHeight; }); }
  var currentAbort=null;
  var activeRunId=0;      /* server-side run id of the stream being tailed */
  var stopRequested=false;
  var reconnectTries=0;
  /* a chat with a running turn shows a spinner next to its title in the history
     rail (assistant page) and drawer; while any spinner is visible we poll
     sessions.json so a run finishing elsewhere clears it. */
  function setRowWorking(sid, on){
    if(!sid) return;
    [].slice.call(document.querySelectorAll('.arow[data-sid="'+sid+'"], .dhist-item[data-sid="'+sid+'"]')).forEach(function(row){
      var sp=row.querySelector('.chat-spin');
      if(on && !sp){
        sp=document.createElement('span'); sp.className='chat-spin';
        sp.setAttribute('role','status');
        sp.setAttribute('aria-label','Assistant is working in this chat');
        sp.title='Assistant is working in this chat';
        var t=row.querySelector('.t');
        if(t && t.parentNode===row) row.insertBefore(sp, t); else row.appendChild(sp);
      } else if(!on && sp){ sp.remove(); }
    });
  }
  var spinTimer=null;
  function scheduleSpin(){
    var any=!!document.querySelector('.chat-spin') || !!currentAbort;
    if(any && !spinTimer){ spinTimer=setInterval(refreshRuns, 5000); }
    else if(!any && spinTimer){ clearInterval(spinTimer); spinTimer=null; }
  }
  function refreshRuns(){
    fetch('/assistant/sessions.json').then(function(r){ return r.json(); }).then(function(d){
      (d.sessions||[]).forEach(function(s){ setRowWorking(s.id, !!s.active); });
      scheduleSpin();
    }).catch(function(){});
  }
  scheduleSpin();
  if(stopBtn){ stopBtn.addEventListener('click', function(){
    if(!currentAbort) return;
    stopRequested=true;
    stopBtn.disabled=true;
    fetch('/assistant/stop', { method:'POST',
        headers: {'Content-Type':'application/x-www-form-urlencoded'},
        body: 'session='+encodeURIComponent(sid)+'&run='+encodeURIComponent(activeRunId||0)
    }).catch(function(){}).then(function(){ if(currentAbort) currentAbort.abort(); });
  }); }
  function resyncPanel(){
    if(!sid) return;
    var u='/assistant/panel?sid='+encodeURIComponent(sid)
        +'&path='+encodeURIComponent(window.mtCtxPath ? window.mtCtxPath() : '');
    fetch(u).then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.text(); })
      .then(function(html){
        root.innerHTML=html;
        if(!live.parentNode) root.appendChild(live);
        root.scrollTop=root.scrollHeight;
        if(opts.onDone) opts.onDone();
      }).catch(function(){});
  }
  function attachNow(onMiss){
    if(currentAbort || !sid) return false;
    return !!run('', {mid:0, row:null, user:true, onMiss:onMiss}, true);
  }
  form.addEventListener('submit', function(e){
    var text=ta.value.trim();
    if(!text){ e.preventDefault(); return; }
    e.preventDefault();
    run(text);
  });
  function run(text, regen, attach){
    regen = regen || null; attach = !!attach;
    if(attach && (currentAbort || !sid)) return null;
    if(regen && regen.row && regen.row.parentNode) regen.row.remove();
    if(currentAbort) currentAbort.abort();
    currentAbort=new AbortController();
    activeRunId=0; stopRequested=false;
    setRowWorking(sid, true); scheduleSpin();
    var ce=root.querySelector('.chat-empty'); if(ce) ce.remove();
    if(btn) btn.disabled=true;
    if(stopBtn){ stopBtn.style.display=''; stopBtn.disabled=false; }
    if(!regen && !attach){ ta.value=''; ta.style.height='auto'; }
    if(!regen && !attach){
    var urow=mk('div','crow user');
    urow.appendChild(mk('div','avatar you','You'));
    var ub=mk('div','bubble user'); ub.textContent=text;
    urow.appendChild(ub); live.appendChild(urow);
    }
    var arow=mk('div','crow ai');
    arow.appendChild(mk('div','avatar ai','AI'));
    var box=mk('div','bubble ai');
    var status=mk('div','status sub','thinking\u2026');
    var det=document.createElement('details'); det.className='think'; det.open=false; det.style.display='none';
    var detSum=mk('summary','','Thinking\u2026'); det.appendChild(detSum);
    var pre=mk('pre'); det.appendChild(pre);
    var toolsBox=document.createElement('details'); toolsBox.className='tools'; toolsBox.style.display='none';
    var toolsSum=mk('summary');
    var toolsList=mk('div','tools-list');
    toolsBox.appendChild(toolsSum); toolsBox.appendChild(toolsList);
    var content=mk('div','md'); content.style.whiteSpace='pre-wrap'; var rawText='';
    var meta=mk('div','meta'); meta.style.display='none';
    box.appendChild(status); box.appendChild(det); box.appendChild(toolsBox);
    box.appendChild(content); box.appendChild(meta);
    arow.appendChild(box); live.appendChild(arow);
    var t0=Date.now(); var timer=null; var finished=false;
    function secs(){ return Math.round((Date.now()-t0)/1000); }
    function setStatus(label){ if(!finished) status.textContent=label+' \u00b7 '+secs()+'s'; }
    setStatus('thinking\u2026');
    scrollBottom(true);
    timer=setInterval(function(){ if(!document.body.contains(status)){ clearInterval(timer); return; } if(!finished && status.dataset.label) status.textContent=status.dataset.label+' \u00b7 '+secs()+'s'; }, 500);
    function stopTimer(){ if(timer){ clearInterval(timer); timer=null; } }
    function label(x){ if(!finished){ status.dataset.label=x; status.textContent=x+' \u00b7 '+secs()+'s'; } }
    var cards=[], cardOrder=[], cardInfo={};
    function toolsLabel(){
      var n=cardOrder.length;
      var txt='\u2699 '+n+' tool call'+(n===1?'':'s');
      var id=cardOrder[n-1];
      if(id!=null && cardInfo[id] && cardInfo[id].label) txt+=' \u00b7 '+cardInfo[id].label;
      toolsSum.textContent=txt;
    }
    function toolCard(id,name,args){
      var c=mk('span','tool-chip');
      var a='';
      try{ a=JSON.stringify(args||{}); }catch(err){ a=''; }
      if(a.length>90) a=a.slice(0,90)+'\u2026';
      c.textContent='\u23f3 '+name+' '+a;
      if(!cards[id]) cardOrder.push(id);
      cards[id]=c;
      cardInfo[id]={name:name, label:'running '+name+'\u2026'};
      toolsBox.style.display=''; toolsList.appendChild(c);
      toolsLabel();
    }
    function toolDone(id,ok,summary,dry,pending,card){
      var c=cards[id]; if(!c) return;
      c.className='tool-chip '+(ok?'ok':'err');
      var tx=c.textContent.replace(/^[\u23f3\u2713\u2717]\s*/,'');
      c.textContent=(ok?'\u2713 ':'\u2717 ')+tx+' \u2192 '+(pending?'[awaiting approval] ':(dry?'[dry-run] ':''))+summary;
      cardInfo[id].label=(ok?'\u2713 ':'\u2717 ')+cardInfo[id].name+' \u2192 '+(pending?'[awaiting approval] ':(dry?'[dry-run] ':''))+summary;
      toolsLabel();
      renderToolCard(card);
    }
    function renderToolCard(card){
      if(!card || !card.title) return;
      var w=mk('div','proposal');
      w.appendChild(mk('div','p-tag','\u2726 '+String(card.title)));
      if(card.markdown) w.appendChild(mk('div','sub',String(card.markdown)));
      (card.fields||[]).forEach(function(f){
        var r=mk('div','spread');
        r.appendChild(mk('b','',String(f.label||'')));
        r.appendChild(mk('span','sub',String(f.value||'')));
        w.appendChild(r);
      });
      var links=(card.actions||[]).filter(function(a){ return a && a.kind==='link' && a.url; });
      if(links.length){
        var row=mk('div','row'); row.style.flexWrap='wrap'; row.style.marginTop='8px';
        links.forEach(function(a){
          var el=mk('a','btn small primary',String(a.label||a.url));
          el.href=a.url; el.target='_blank'; el.rel='noopener noreferrer';
          row.appendChild(el);
        });
        w.appendChild(row);
      }
      box.appendChild(w);
      scrollBottom();
    }
    function actsText(a){
      a=a||{}; var out=[];
      if(a.move_to) out.push('move \u2192 '+a.move_to);
      if(a.mark_read) out.push('mark read');
      if(a.flag) out.push('flag');
      return out.join(', ')||'keep in place (guard)';
    }
    function makeForm(msgId,idx,extra){
      var f=mk('form'); f.method='post'; f.action='/assistant/apply'; f.className='inline';
      var i1=mk('input'); i1.type='hidden'; i1.name='msg_id'; i1.value=msgId; f.appendChild(i1);
      var i2=mk('input'); i2.type='hidden'; i2.name='idx'; i2.value=idx; f.appendChild(i2);
      var i3=mk('input'); i3.type='hidden'; i3.name='session'; i3.value=sid; f.appendChild(i3);
      var ib=mk('input'); ib.type='hidden'; ib.name='back'; ib.value=location.pathname; f.appendChild(ib);
      Object.keys(extra||{}).forEach(function(k){ var i4=mk('input'); i4.type='hidden'; i4.name=k; i4.value=extra[k]; f.appendChild(i4); });
      f.onsubmit=function(){ return guardApply(f); };
      return f;
    }
    function addProposal(p,idx,msgId){
      var isFlow=(p.kind==='flow');
      var updObj=p.updates||p.updates_rule||null;
      var simObj=p.similar||p.similar_rule||null;
      var tgt=updObj||simObj||null;
      var w=mk('div','proposal');
      w.appendChild(mk('div','p-tag','\u2726 Proposed '+(isFlow?'flow':'rule')));
      var h=mk('div','spread');
      var left=mk('div');
      left.appendChild(mk('b','',p.name));
      left.appendChild(document.createTextNode(' ('+(p.match_mode||'all')+')'));
      if(updObj){ var b1=mk('span','badge warn',' updates #'+updObj.id+' "'+updObj.name+'"'); b1.style.marginLeft='6px'; left.appendChild(b1); }
      else if(simObj){ var b2=mk('span','badge warn',' overlaps #'+simObj.id); b2.style.marginLeft='6px'; left.appendChild(b2); }
      h.appendChild(left);
      var row=mk('div','row'); row.style.whiteSpace='nowrap';
      if(tgt){
        var fu=makeForm(msgId,idx,{mode:'update',rule_id:tgt.id});
        var bu=mk('button','btn small primary',(isFlow?'Update flow #':'Update rule #')+tgt.id); bu.type='submit';
        fu.appendChild(bu); row.appendChild(fu);
      }
      var fa=makeForm(msgId,idx,{});
      var ba=mk('button','btn small'+(tgt?'':' primary'),isFlow?'Add flow':'Add rule'); ba.type='submit';
      fa.appendChild(ba); row.appendChild(fa);
      var fd=makeForm(msgId,idx,{disabled:'1'});
      var bd=mk('button','btn small','Add (disabled)'); bd.type='submit';
      fd.appendChild(bd); row.appendChild(fd);
      h.appendChild(row);
      w.appendChild(h);
      if(updObj){
        w.appendChild(mk('div','note','Updates '+(isFlow?'flow':'rule')+' #'+updObj.id+' "'+updObj.name+'" — currently '+(p.updates_actions||'')+'.'));
      } else if(simObj){
        w.appendChild(mk('div','note','⚠ Similar rule exists: #'+simObj.id+' "'+simObj.name+'" — '+actsText(simObj.actions)+'. Updating it avoids a duplicate.'));
      }
      var conds = p.summary ? p.summary : (p.conditions||[]).map(function(c){ return (c.op==='plugin') ? ('matches plugin "'+(c.plugin||'?')+'"') : (c.field+' '+c.op+' "'+c.value+'"'); }).join((p.match_mode==='any')?' OR ':' AND ');
      w.appendChild(mk('div','mono',conds));
      w.appendChild(mk('div','sub', p.actions_summary ? p.actions_summary : actsText(p.actions)));
      if(p.rationale) w.appendChild(mk('div','sub',p.rationale));
      box.appendChild(w);
    }
    
    function addPendingAction(a){
      var w=mk('div','proposal');
      w.appendChild(mk('div','p-tag warn','\u2726 Needs your approval'));
      var h=mk('div','spread');
      var left=mk('div');
      left.appendChild(mk('b','',a.preview||a.tool||'action'));
      if(a.capability){ left.appendChild(mk('span','sub',' · '+a.capability)); }
      if(a.capability==='send'||a.capability==='delete'){ var bd=mk('span','badge err',' dangerous'); bd.style.marginLeft='6px'; left.appendChild(bd); }
      h.appendChild(left);
      var fa=mk('form'); fa.method='post'; fa.action='/agent/actions/'+a.id+'/apply'; fa.className='inline';
      var i1=mk('input'); i1.type='hidden'; i1.name='session'; i1.value=sid; fa.appendChild(i1);
      var ba=mk('button','btn small primary','Approve'); ba.type='submit'; fa.appendChild(ba);
      var fd=mk('form'); fd.method='post'; fd.action='/agent/actions/'+a.id+'/dismiss'; fd.className='inline';
      var i2=mk('input'); i2.type='hidden'; i2.name='session'; i2.value=sid; fd.appendChild(i2);
      var bd2=mk('button','btn small','Dismiss'); bd2.type='submit'; fd.appendChild(bd2);
      var row=mk('div','row'); row.appendChild(fa); row.appendChild(fd); h.appendChild(row);
      w.appendChild(h);
      w.appendChild(mk('div','note','Nothing happens until you click Approve.'));
      box.appendChild(w);
    }
    var proposals=[], pendingActions=[], doneMsgId=null, userMsgId=0;
    function addRetryButton(host, mid){
      var rb=mk('button','regen','\u21bb Retry'); rb.type='button';
      rb.title='Retry this message';
      rb.addEventListener('click', function(){
        if(currentAbort) return;
        rb.disabled=true;
        run('', {mid: mid||0, row: null, user: true});
      });
      host.appendChild(rb);
      return rb;
    }
    function stopFinish(){
      if(finished) return; finished=true;
      stopTimer();
      status.textContent='stopped';
      if(btn) btn.disabled=false; if(stopBtn) stopBtn.style.display='none';
      currentAbort=null; activeRunId=0;
      setRowWorking(sid, false); scheduleSpin();
      var lm=root.querySelector('.live-run'); if(lm) lm.remove();
      var mrow=mk('div','meta'); addRetryButton(mrow, userMsgId); box.appendChild(mrow);
      scrollBottom();
    }
    function finish(){
      if(finished) return; finished=true;
      stopTimer();
      status.textContent='done in '+secs()+'s';
      if(det.style.display!=='none' && !det.dataset.summary){ detSum.textContent='Thought for '+secs()+'s'; }
      if(content.textContent) content.innerHTML=mdRender(content.textContent);
      content.style.whiteSpace='';
      meta.style.display='';
      var cp=mk('button','copy','copy'); cp.type='button'; cp.dataset.copy=rawText||content.textContent;
      meta.appendChild(cp);
      meta.appendChild(mk('span','',new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})));
      if(doneMsgId!=null){ var rg=mk('button','regen','\u21bb'); rg.type='button'; rg.dataset.mid=doneMsgId; rg.title='Regenerate reply'; meta.appendChild(rg); }
      if(proposals.length && doneMsgId!=null) proposals.forEach(function(p,i){ addProposal(p,i,doneMsgId); });
      if(pendingActions.length) pendingActions.forEach(function(a){ addPendingAction(a); });
      var lm=root.querySelector('.live-run'); if(lm) lm.remove();
      if(btn) btn.disabled=false; if(stopBtn) stopBtn.style.display='none'; currentAbort=null; activeRunId=0;
      setRowWorking(sid, false); scheduleSpin();
      scrollBottom();
    }
    function fail(msg){
      if(finished) return; finished=true;
      stopTimer();
      status.textContent='failed';
      var e2=mk('div','msg err','Assistant failed: '+msg);
      box.appendChild(e2);
      var mrow=mk('div','meta'); addRetryButton(mrow, userMsgId); box.appendChild(mrow);
      var lm=root.querySelector('.live-run'); if(lm) lm.remove();
      if(btn) btn.disabled=false; if(stopBtn) stopBtn.style.display='none'; currentAbort=null; activeRunId=0;
      setRowWorking(sid, false); scheduleSpin();
      scrollBottom();
    }
    /* transport loss is not failure: the run lives server-side, so quietly
       re-attach (replaying its events) until it is done or we give up. */
    function reconnect(){
      if(finished) return;
      if(reconnectTries>=8){
        finished=true; stopTimer(); status.textContent='connection lost';
        if(btn) btn.disabled=false; if(stopBtn) stopBtn.style.display='none';
        currentAbort=null; activeRunId=0;
        if(arow.parentNode) arow.remove();
        return;
      }
      reconnectTries++;
      finished=true; stopTimer();
      if(arow.parentNode) arow.remove();
      if(btn) btn.disabled=false; if(stopBtn) stopBtn.style.display='none';
      currentAbort=null; activeRunId=0;
      status.textContent='reconnecting\u2026';
      setTimeout(function(){ if(!currentAbort && sid) attachNow(); },
                 Math.min(1200*reconnectTries, 8000));
    }
    function handle(raw){
      var ev=null, data='';
      raw.split('\n').forEach(function(line){
        if(line.indexOf('event:')===0) ev=line.slice(6).trim();
        else if(line.indexOf('data:')===0) data+=line.slice(5).trim();
      });
      if(!ev) return;
      var d={};
      if(data){ try{ d=JSON.parse(data); }catch(err){ return; } }
      if(ev==='session'){ sid=d.sid; activeRunId=d.run||0; reconnectTries=0; setRowWorking(sid, true); scheduleSpin(); if(onSession) onSession(sid); }
      else if(ev==='user_saved'){ userMsgId=d.message_id||0; }
      else if(ev==='reasoning'){ det.style.display=''; detSum.textContent='Thinking\u2026'; pre.textContent+=(d.text||''); label('thinking\u2026'); }
      else if(ev==='content'){ content.textContent+=(d.text||''); rawText+=(d.text||''); label('writing\u2026'); }
      else if(ev==='content_break'){ if(content.textContent){ content.textContent+='\n\n'; rawText+='\n\n'; } }
      else if(ev==='tool_start'){ label('running '+d.name+'\u2026'); toolCard(d.id,d.name,d.args); }
      else if(ev==='tool_end'){
        if(d.ui && d.ui.action==='fill_simulator' && window.mtSimFill){
          try{ window.mtSimFill(d.ui.fields||{}, d.ui.note||''); }catch(e){}
        }
        if(d.ui && d.ui.action==='fill_flow' && window.mtFlowFill){
          try{ window.mtFlowFill(d.ui.fields||{}, d.ui.note||''); }catch(e){}
        }
        toolDone(d.id,d.ok,d.summary,d.dry_run,d.pending,d.card); label('thinking\u2026');
      }
      else if(ev==='proposals'){ proposals=d.proposals||[]; }
      else if(ev==='action_proposals'){ pendingActions=(d.actions||[]); }
      else if(ev==='thought_summary'){ if(det.style.display!=='none' && d.text){ det.dataset.summary='1'; detSum.textContent=d.text; } }
      else if(ev==='done'){
        doneMsgId=d.message_id;
        if(d.reply && !content.textContent){ content.textContent=d.reply; rawText=d.reply; }
        finish();
        if(opts.onDone) opts.onDone();
      }
      else if(ev==='error'){ fail(d.message||'unknown error'); }
      else if(ev==='stopped'){ stopFinish(); }
      scrollBottom();
    }
    var _state = !window.__mtContextDetached && window.mtCtxState ? window.mtCtxState() : '';
    var _body = regen
        ? ('session='+encodeURIComponent(sid)+'&mid='+encodeURIComponent(regen.mid||0)
           +'&path='+encodeURIComponent(window.mtCtxPath ? window.mtCtxPath() : '')
           +'&state='+encodeURIComponent(_state))
        : ('message='+encodeURIComponent(text)+'&session='+encodeURIComponent(sid)
           +'&path='+encodeURIComponent(window.mtCtxPath ? window.mtCtxPath() : '')
           +'&state='+encodeURIComponent(_state));
    var _url = attach ? ('/assistant/live?sid='+encodeURIComponent(sid))
            : (regen ? '/assistant/regenerate' : '/assistant/stream');
    var _opts = attach
        ? { method:'GET', headers: {'Accept':'text/event-stream'}, signal: currentAbort.signal }
        : { method:'POST', headers: {'Content-Type':'application/x-www-form-urlencoded'},
            body: _body, signal: currentAbort.signal };
    fetch(_url, _opts).then(function(resp){
      if(attach && resp.status===404){
        finished=true; stopTimer();
        if(arow.parentNode) arow.remove();
        if(btn) btn.disabled=false; if(stopBtn) stopBtn.style.display='none';
        currentAbort=null; activeRunId=0;
        resyncPanel();
        if(regen && regen.onMiss) regen.onMiss();
        return;
      }
      if(!resp.ok || !resp.body) throw new Error('HTTP '+resp.status);
      var rd=resp.body.getReader(); var dec=new TextDecoder(); var buf='';
      function pump(){
        return rd.read().then(function(r){
          if(r.done){ if(!finished) reconnect(); return; }
          buf+=dec.decode(r.value,{stream:true});
          var i;
          while((i=buf.indexOf('\n\n'))>=0){
            var raw2=buf.slice(0,i);
            buf=buf.slice(i+2);
            handle(raw2);
          }
          return pump();
        });
      }
      return pump();
    }).catch(function(err){
      if(finished) return;
      if(err && err.name==='AbortError'){
        if(stopRequested){ stopFinish(); }
        else {
          finished=true; stopTimer(); status.textContent='stopped';
          if(btn) btn.disabled=false; if(stopBtn) stopBtn.style.display='none';
          currentAbort=null; activeRunId=0;
        }
      } else { reconnect(); }
    });
  }
  var inst = { liveEl: live, setSession: function(ns){ sid=ns; }, refreshLive: function(){ if(!live.parentNode) root.appendChild(live); },
               isBusy: function(){ return !!currentAbort; },
               abort: function(){ if(currentAbort) currentAbort.abort(); },
               attach: function(){ if(!sid || currentAbort) return false;
                                   if(!root.querySelector('.live-run')) return false;
                                   return attachNow(); },
               regen: function(b){ if(currentAbort) return; var row=b.closest('.crow'); var isUser=!!b.closest('.crow.user');
                                   var m=b.getAttribute('data-mid')||'0';
                                   if(isUser) b.remove();
                                   run('', {mid: m, row: isUser?null:row, user:isUser}); },
               clearLive: function(){ live.innerHTML=''; } };
  form.__chat = inst;
  return inst;
};

/* ---- assistant sidebar (docked right; collapses to a rail) ---- */
window.guardApply = function(f){
  var b = f.querySelector('button[type=submit]');
  if(!b || b.disabled) return false;
  b.disabled = true; b.textContent = 'Adding…';
  return true;
};
(function(){
  var asb = document.getElementById('asb');
  if(!asb || asb.__wired) return;
  asb.__wired = 1;
  var chat = document.getElementById('dchat'), histList = document.getElementById('dhistlist');
  var inited = false, curSid = null;
  function expanded(){ return document.documentElement.classList.contains('asb-open'); }
  function setOpen(open){
    document.documentElement.classList.toggle('asb-open', open);
    try{ localStorage.setItem('asb_open', open ? '1' : '0'); }catch(e){}
    if(open){ ensure(); setTimeout(function(){ if(window.__mtCtxKey && window.frameCtxKey) window.frameCtxKey(window.__mtCtxKey); }, 450); }
  }
  document.getElementById('asb-toggle').addEventListener('click', function(ev){ ev.stopPropagation(); setOpen(true); });
  document.getElementById('asb-rail').addEventListener('click', function(ev){ if(ev.target === this || ev.target.tagName === 'SPAN') setOpen(true); });
  document.getElementById('dclose').addEventListener('click', function(){ setOpen(false); });
  /* ---- resize (drag the left edge; width persists) ---- */
  var grip = document.getElementById('asb-grip');
  var DEF_W = 352, MIN_W = 280;
  var curW = DEF_W;
  try{ var sw = parseInt(localStorage.getItem('asb_w') || '', 10); if(sw >= MIN_W) curW = sw; }catch(e){}
  function maxW(){ return Math.min(720, Math.max(320, window.innerWidth - 480)); }
  function clampW(w){ return Math.max(MIN_W, Math.min(maxW(), Math.round(w))); }
  function applyW(w){
    curW = clampW(w);
    document.documentElement.style.setProperty('--asb-w', curW + 'px');
    grip.setAttribute('aria-valuenow', String(curW));
    grip.setAttribute('aria-valuemin', String(MIN_W));
    grip.setAttribute('aria-valuemax', String(maxW()));
  }
  function saveW(){ try{ localStorage.setItem('asb_w', String(curW)); }catch(e){} }
  applyW(curW);
  grip.addEventListener('pointerdown', function(ev){
    if(!expanded()) return;
    ev.preventDefault();
    var startX = ev.clientX, startW = curW;
    document.documentElement.classList.add('asb-resizing');
    try{ grip.setPointerCapture(ev.pointerId); }catch(e){}
    function mv(e2){ applyW(startW + (startX - e2.clientX)); }
    function end(){
      document.documentElement.classList.remove('asb-resizing');
      saveW();
      grip.removeEventListener('pointermove', mv);
      grip.removeEventListener('pointerup', end);
      grip.removeEventListener('pointercancel', end);
    }
    grip.addEventListener('pointermove', mv);
    grip.addEventListener('pointerup', end);
    grip.addEventListener('pointercancel', end);
  });
  grip.addEventListener('dblclick', function(){ applyW(DEF_W); saveW(); });
  grip.addEventListener('keydown', function(ev){
    if(ev.key === 'ArrowLeft'){ applyW(curW + 24); saveW(); ev.preventDefault(); }
    else if(ev.key === 'ArrowRight'){ applyW(curW - 24); saveW(); ev.preventDefault(); }
  });
  if(!window.__mtAsbRz){
    window.__mtAsbRz = 1;
    window.addEventListener('resize', function(){ if(window.__asbRz) window.__asbRz(); });
  }
  window.__asbRz = function(){ if(expanded() && clampW(curW) !== curW){ applyW(curW); saveW(); } };
  function post(url, data){ return fetch(url, {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:data||''}); }
  function newSid(){ return post('/assistant/new.json').then(function(r){ return r.json(); }).then(function(d){ return d.sid; }); }
  function loadHist(){
    return fetch('/assistant/sessions.json').then(function(r){ return r.json(); }).then(function(d){
      histList.innerHTML = '';
      (d.sessions||[]).forEach(function(s){
        var row = document.createElement('div'); row.className = 'dhist-item' + (String(s.id) === String(curSid) ? ' cur' : '');
        row.dataset.sid = s.id;
        if(s.active){
          var sp = document.createElement('span'); sp.className = 'chat-spin';
          sp.setAttribute('role', 'status');
          sp.setAttribute('aria-label', 'Assistant is working in this chat');
          sp.title = 'Assistant is working in this chat';
          row.appendChild(sp);
        }
        var t1 = document.createElement('span'); t1.className = 't'; t1.textContent = s.title || 'Untitled chat'; row.appendChild(t1);
        var w = document.createElement('span'); w.className = 'when'; w.textContent = s.when || ''; row.appendChild(w);
        var del = document.createElement('button'); del.type = 'button'; del.className = 'btn small chat-del'; del.textContent = '✕';
        del.setAttribute('aria-label', 'Delete chat: ' + (s.title || 'Untitled chat'));
        del.addEventListener('click', function(ev){ ev.stopPropagation();
          if(!del.classList.contains('armed')) return;
          post('/assistant/session/' + s.id + '/delete', 'json=1').then(function(){
            if(String(curSid) === String(s.id)) curSid = null;
            if(window.toast) toast('Chat deleted.', 'ok');
            loadHist();
          });
        });
        row.appendChild(del);
        row.addEventListener('click', function(){ histList.classList.add('hidden'); openSession(s.id); });
        histList.appendChild(row);
      });
    });
  }
  function remember(sid){ try{ localStorage.setItem('assistant_sid', String(sid)); }catch(e){} }
  function bind(){
    var form = document.getElementById('dform');
    if(form.__chat){ form.__chat.setSession(curSid); form.__chat.refreshLive(); return form.__chat; }
    return window.assistantChat({ root: chat, form: form, ta: document.getElementById('dmsg'),
      sendBtn: document.getElementById('dsend'), stopBtn: document.getElementById('dstop'), sessionId: curSid,
      onSession: function(ns){ curSid = ns; remember(ns); loadHist(); },
      onDone: function(){ loadHist(); } });
  }
  function loadPanel(sid){
    var u = '/assistant/panel?sid=' + sid + '&path=' + encodeURIComponent(location.pathname);
    return fetch(u).then(function(r){ if(!r.ok) throw new Error('gone'); return r.text(); }).then(function(html){
      chat.innerHTML = html;
      chat.scrollTop = chat.scrollHeight;
    });
  }
  function bindAndAttach(){
    var inst = bind();
    loadHist();
    if(inst && inst.attach) inst.attach();
    return inst;
  }
  /* The live box accumulates every turn this tab has streamed, and refreshLive()
     re-appends it into each freshly loaded panel - so switching chats used to show
     the previous conversation under the new chat's empty state ("New" looked dead).
     Drop it on panel loads: always when idle (the fragment already carries the
     persisted turns), and abort the running turn first when switching mid-stream. */
  function dropLive(switching){
    var fm = document.getElementById('dform');
    if(!fm || !fm.__chat) return;
    if(fm.__chat.isBusy()){
      if(!switching) return;
      fm.__chat.abort();
    }
    fm.__chat.clearLive();
  }
  function openSession(sid){
    var switching = String(curSid) !== String(sid);
    curSid = sid;
    try{ localStorage.setItem('assistant_sid', String(sid)); }catch(e){}
    dropLive(switching);
    loadPanel(sid).then(function(){ bindAndAttach(); }).catch(function(){
      newSid().then(function(ns){ curSid = ns; dropLive(true); return loadPanel(ns); }).then(function(){ bindAndAttach(); });
    });
  }
  function ensure(){
    if(inited) return;
    inited = true;
    var sid = null; try{ sid = parseInt(localStorage.getItem('assistant_sid') || '', 10) || null; }catch(e){}
    if(sid){
      loadPanel(sid).then(function(){ curSid = sid; bindAndAttach(); }).catch(function(){
        newSid().then(function(ns){ curSid = ns; remember(ns); return loadPanel(ns); }).then(function(){ bindAndAttach(); });
      });
    } else {
      newSid().then(function(ns){ curSid = ns; remember(ns); return loadPanel(ns); }).then(function(){ bindAndAttach(); });
    }
  }
  document.getElementById('dnew').addEventListener('click', function(){ newSid().then(function(ns){ openSession(ns); }); });
  /* contextual intelligence: when the page context changes (new tab / entity),
     start a fresh session so the chat is scoped to what is on screen. Old chats
     stay in History. */
  var sessCtx = null;
  try{ sessCtx = sessionStorage.getItem('mtSessCtx'); }catch(e){}
  function frameCtxKey(k){
    if(!k) return;
    if(sessCtx === null){ sessCtx = k; try{ sessionStorage.setItem('mtSessCtx', k); }catch(e){} return; }
    if(k === sessCtx) return;
    sessCtx = k; try{ sessionStorage.setItem('mtSessCtx', k); }catch(e){}
    var empty = !!document.querySelector('#dchat .chat-empty');
    var sb = document.getElementById('dsend');
    var busy = !!(sb && sb.disabled);
    if(empty || busy || !inited) return;
    newSid().then(function(ns){ openSession(ns); });
  }
  window.frameCtxKey = frameCtxKey;
  window.addEventListener('mt:ctxkey', function(e){ frameCtxKey(e.detail || ''); });
  if(window.__mtCtxKey) frameCtxKey(window.__mtCtxKey);
  document.getElementById('dhist').addEventListener('click', function(){
    histList.classList.toggle('hidden');
    if(!histList.classList.contains('hidden')) loadHist();
  });
  if(expanded() && window.matchMedia && window.matchMedia('(min-width:1024px)').matches) ensure();
})();
})();
</script>
<script>
/* assistant page context: remember the last meaningful page so the chat carries
   "this email" / "this flow" context even from the Assistant tab, and keep the
   context chip in the drawer + assistant page up to date. */
(function(){
  window.mtCtxPath = function(){
    if(window.__mtContextDetached) return '';
    var p = location.pathname;
    if (p.indexOf('/assistant') !== 0) return p + location.search;
    try { return sessionStorage.getItem('mtLastCtx') || ''; } catch(e){ return ''; }
  };
  var lastFetched = '';
  function updateChips(){
    var chips = [document.getElementById('dwctx'), document.getElementById('amctx')];
    if (!chips[0] && !chips[1]) return;
    var path = window.mtCtxPath();
    if (path === lastFetched) return;
    lastFetched = path;
    if (!path){ chips.forEach(function(c){ if(c) c.hidden = true; }); return; }
    fetch('/assistant/context.json?path=' + encodeURIComponent(path))
      .then(function(r){ return r.json(); })
      .then(function(d){
        if(window.__mtContextDetached || window.mtCtxPath() !== path) return;
        chips.forEach(function(c){
          if (!c) return;
           c.textContent = d.desc ? ('Using context: ' + d.desc) : '';
           c.classList.add('ux-context');
           if(d.desc){var remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','Remove assistant context');remove.addEventListener('click',function(){window.__mtContextDetached=true;lastFetched=null;updateChips();window.__mtCtxKey='';window.dispatchEvent(new CustomEvent('mt:ctxkey',{detail:''}));});c.appendChild(remove);}
          c.hidden = !d.desc;
        });
        window.__mtCtxKey = d.key || '';
        try { window.dispatchEvent(new CustomEvent('mt:ctxkey', {detail: window.__mtCtxKey})); } catch(e){}
      })
      .catch(function(){});
  }
  function remember(){
    if (location.pathname.indexOf('/assistant') !== 0){
      window.__mtContextDetached = false;
      try { sessionStorage.setItem('mtLastCtx', location.pathname + location.search); } catch(e){}
    }
    updateChips();
  }
  if (!window.__mtCtx){
    window.__mtCtx = 1;
    document.addEventListener('turbo:load', function(){ setTimeout(remember, 30); });
  }
  remember();
})();
</script>
<script>
/* keyboard-follow: with the on-screen keyboard up, the tab bar hides and the
   page composer must stay visible above it. Two strategies:
   - .assistant-main (assistant page) is resized to the visual viewport height
     (CSS var --kb-h), so its last-chain composer always lands above the keyboard;
   - #dform (drawer) + .savebar are lifted with translateY(-kb).
   iOS never fires reliable events alone, so we also poll briefly on focus. */
(function(){
  if(!window.visualViewport || !window.matchMedia || !matchMedia('(pointer:coarse)').matches) return;
  var root = document.documentElement, poll = null;
  /* Keyboard height needs a device-agnostic baseline: iOS keeps innerHeight when
     the keyboard opens (visual-only resize) while Android + Chrome with
     interactive-widget=resizes-content shrinks the LAYOUT viewport too - there the
     plain innerHeight - vv.height difference reads ~0, kb-open never fired and the
     tab bar covered the composer. Track the largest innerHeight seen as the
     no-keyboard baseline; width changes (rotation) reset it, and the threshold sits
     above Chrome-Android's URL-bar resize (~56px) and below any real keyboard. */
  var baseH = window.innerHeight || 0, lastW = window.innerWidth || 0;
  function fit(){
    var vv = window.visualViewport; if(!vv) return;
    var ih = window.innerHeight || 0;
    if((window.innerWidth || 0) !== lastW){ lastW = window.innerWidth || 0; baseH = ih; }
    if(ih > baseH) baseH = ih;
    var kb = Math.max(0, baseH - vv.height - (vv.offsetTop || 0));
    var open = kb > 120;
    document.body.classList.toggle('kb-open', open);
    document.querySelectorAll('#dform,.savebar').forEach(function(el){
      el.style.transform = open ? ('translateY(-' + Math.round(kb) + 'px)') : '';
    });
    var main = document.querySelector('.assistant-main');
    if(open && main){
      // rect.top is in visual-viewport coords, so this lands the container's
      // bottom exactly on the visible bottom edge (works even when panned)
      var h = Math.round(vv.height - main.getBoundingClientRect().top);
      if(h > 160) root.style.setProperty('--kb-h', h + 'px');
    } else {
      root.style.removeProperty('--kb-h');
    }
  }
  window.__mtKbFit = fit;
  function startPoll(){ stopPoll(); var n = 0; poll = setInterval(function(){ fit(); if(++n > 12) stopPoll(); }, 250); }
  function stopPoll(){ if(poll){ clearInterval(poll); poll = null; } }
  if(!window.__mtKb){
    window.__mtKb = 1;
    var vv0 = window.visualViewport;
    vv0.addEventListener('resize', fit); vv0.addEventListener('scroll', fit);
    window.addEventListener('resize', fit);
    document.addEventListener('focusin', function(){ setTimeout(fit, 120); setTimeout(fit, 400); startPoll(); });
    document.addEventListener('focusout', function(){ setTimeout(fit, 80); setTimeout(fit, 450); stopPoll(); });
  }
  fit();
})();
</script>
</body></html>
"""


_TPL_CACHE = {}


def _render_src(src, **ctx):
    """render_template_string with a compiled-template cache. The templates are
    module-level constants; recompiling them per request cost ~40ms/page."""
    tpl = _TPL_CACHE.get(src)
    if tpl is None:
        tpl = app.jinja_env.from_string(src)
        _TPL_CACHE[src] = tpl
    return tpl.render(**ctx)


def render(body, setup=False):
    ws = dict(worker.state)
    info = dict(_header_info())
    info["imap_user"] = (engine.imap_config().get("user") or "").strip()
    try:
        ext_nav = plugins.ui_nav_items()
    except Exception:
        ext_nav = []
    ws_section = ux.automation_section(request.path) if not setup else None
    return _render_src(BASE_TMPL, body=body, cfg=config, tz=tz_label(),
                                  info=info,
                                  ws_section=ws_section,
                                  show_asb=(not setup) and not request.path.startswith("/assistant"),
                                  setup=setup,
                                  ext_nav=ext_nav,
                                  pend=store.count_pending_agent_actions(),
                                  w={"err": ws.get("last_error"), "last_ok_r": rel_time(ws.get("last_ok")),
                                     "last_ok_iso": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws.get("last_ok"))) if ws.get("last_ok") else ""),
                                     "interval": int(store.get_setting("poll_interval", 90) or 90)})


def _header_info():

    try:
        llm = engine.llm_config()
        llm_txt = ("%s @ %s" % (llm["model"], llm["base"])) if llm["base"] else "not configured"
    except Exception:
        llm_txt = "?"
    try:
        ic = engine.imap_config()
        imap_txt = "%s @ %s:%s (%s)" % (ic["user"] or "?", ic["host"] or "?", ic["port"],
                                       ic["mode"])
    except Exception:
        imap_txt = "?"
    return {"imap": imap_txt, "llm": llm_txt}


MORE_TMPL = """
<style>
.more-list{border:1px solid var(--line);background:#fff}
.more-row{display:flex;align-items:center;gap:12px;min-height:54px;padding:10px 14px;border-bottom:1px solid var(--line);
color:var(--fg);text-decoration:none}
.more-row:last-child{border-bottom:0}
.more-row:hover{background:var(--hover);text-decoration:none}
.more-row .grow{flex:1;min-width:0}
.more-row b{font-size:.92rem;font-weight:600;display:block}
.more-row .sub{font-size:.78rem}
.more-row svg{width:16px;height:16px;flex:none;color:var(--dim);stroke-width:1.7}
.more-list button.more-row{width:100%;font:inherit;text-align:left;cursor:pointer;background:none;border:0;color:var(--fg)}
</style>
<div class="page-head">
  <div>
    <h1 class="page-title">More</h1>
    <div class="page-desc">All sections of the app.</div>
  </div>
</div>
<div class="more-list" style="margin-bottom:16px">
  <a class="more-row" href="{{ url_for('welcome') }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 4.5-5"/></svg><span class="grow"><b>Get started</b><span class="sub">Setup checklist: mailbox, LLM, search index</span></span><span aria-hidden="true">&#8250;</span></a>
</div>
<div class="nav-label" style="margin:2px 2px 8px">Automation</div>
<div class="more-list">
  <a class="more-row" href="{{ url_for('automation') }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="7" cy="18" r="2"/></svg><span class="grow"><b>Automation</b><span class="sub">Rules, flows, categories, drafting and controls — one workspace</span></span><span aria-hidden="true">&#8250;</span></a>
  <a class="more-row" href="{{ url_for('simulate') }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3v6l-5 8a2 2 0 0 0 1.7 3h12.6a2 2 0 0 0 1.7-3l-5-8V3"/><path d="M7 3h10"/></svg><span class="grow"><b>Simulator</b><span class="sub">Draft an email, see how rules and flows would handle it</span></span><span aria-hidden="true">&#8250;</span></a>
  <a class="more-row" href="{{ url_for('learning_page') }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg><span class="grow"><b>Learning</b><span class="sub">Models deciding &amp; learning on your mail</span></span><span aria-hidden="true">&#8250;</span></a>
</div>
<div class="nav-label" style="margin:14px 2px 8px">System</div>
<div class="more-list">
  <a class="more-row" href="{{ url_for('accounts') }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3"/><path d="M3 20c0-3 3-5 6-5s6 2 6 5"/><path d="M16 8h5M18.5 5.5v5"/></svg><span class="grow"><b>Accounts</b><span class="sub">Mail account and sign-in</span></span><span aria-hidden="true">&#8250;</span></a>
  <a class="more-row" href="{{ url_for('log') }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4h16v16H4z"/><path d="m8 9 3 3-3 3M13 15h4"/></svg><span class="grow"><b>Log</b><span class="sub">Recent events and activity</span></span><span aria-hidden="true">&#8250;</span></a>
  <a class="more-row" href="{{ url_for('settings') }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.5-2.4 1a7 7 0 0 0-1.7-1L14.5 3h-5L9 6a7 7 0 0 0-1.7 1l-2.4-1-2 3.5 2 1.5a7 7 0 0 0 0 2l-2 1.5 2 3.5 2.4-1a7 7 0 0 0 1.7 1l.5 3h5l.5-3a7 7 0 0 0 1.7-1l2.4 1 2-3.5-2-1.5c.06-.3.1-.66.1-1z"/></svg><span class="grow"><b>Settings</b><span class="sub">App, AI, mail and agent permissions</span></span><span aria-hidden="true">&#8250;</span></a>
  <a class="more-row" href="{{ url_for('plugins_page') }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3v5M15 3v5M6 8h12v4a6 6 0 0 1-12 0z"/><path d="M12 18v3"/></svg><span class="grow"><b>Plugins</b><span class="sub">Sandboxed extensions: classifiers, tools, integrations</span></span><span aria-hidden="true">&#8250;</span></a>
</div>
<div class="more-list" style="margin-top:14px">
  <button class="more-row hidden" id="install-app" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4M5 21h14"/></svg><span class="grow"><b>Install app</b><span class="sub" id="install-note">Add Mail Triage to your home screen</span></span></button>
</div>
<div class="sub" style="margin:14px 2px 0;display:flex;align-items:center;gap:7px"><span class="dot {{ 'ok' if not w.err else 'err' }}"></span><time datetime="{{ w.last_ok_iso }}" data-rel data-label="Last check ">Last check {{ w.last_ok_r }}</time> · Times in {{ tz }}</div>
<script>
(function(){
  var r=document.getElementById('install-app'); if(!r) return;
  var ios=/iphone|ipad|ipod/i.test(navigator.userAgent);
  var standalone=window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone===true;
  if(standalone){ r.remove(); return; }
  var note=document.getElementById('install-note');
  if(!window.__mtBip){
    window.__mtBip = 1;
    window.addEventListener('beforeinstallprompt', function(e){ e.preventDefault(); window.__bip=e; var rr=document.getElementById('install-app'); if(rr) rr.classList.remove('hidden'); });
    window.addEventListener('appinstalled', function(){ var rr=document.getElementById('install-app'); if(rr) rr.classList.add('hidden'); });
  }
  r.addEventListener('click', function(){
    if(window.__bip){ window.__bip.prompt(); window.__bip=null; r.classList.add('hidden'); }
    else if(ios && note){ note.textContent='In Safari: tap Share → Add to Home Screen'; }
  });
  if(ios){ r.classList.remove('hidden'); }
})();
</script>
"""


@app.route("/more")
def more():
    ws = dict(worker.state)
    return render(_render_src(MORE_TMPL, tz=tz_label(), w={
        "err": ws.get("last_error"), "last_ok_r": rel_time(ws.get("last_ok")),
        "last_ok_iso": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws.get("last_ok"))) if ws.get("last_ok") else "")}))


@app.route("/manifest.webmanifest")
def manifest():
    import json as _json
    body = {
        "id": "/",
        "name": "Mail Triage",
        "short_name": "MailTriage",
        "description": "Smart mail triage - rules, flows and an AI assistant for your mailbox.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "theme_color": "#fafafa",
        "background_color": "#fafafa",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Messages", "url": "/messages"},
            {"name": "Assistant", "url": "/assistant"},
        ],
    }
    return Response(_json.dumps(body, ensure_ascii=False), mimetype="application/manifest+json")


@app.route("/static/icons/<path:name>")
def static_icon(name):
    from flask import send_from_directory
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    return send_from_directory(base, name, max_age=2592000)


@app.route("/favicon.ico")
def favicon():
    from flask import send_from_directory
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    return send_from_directory(base, "favicon.ico", max_age=604800)


@app.route("/static/turbo.js")
def static_turbo():
    from flask import send_from_directory
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    return send_from_directory(base, "turbo.js", max_age=604800)


# ---------------------------------------------------------------- dashboard
DASH_TMPL = """
<style>
.sys{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px 30px;align-items:flex-start}
@media(max-width:1239px){.sys{grid-template-columns:repeat(2,minmax(0,1fr))}}
.sysitem{display:flex;gap:8px;align-items:flex-start;min-width:0}
.sysitem .dot{margin-top:6px}
.sysitem b{display:block;font-size:.84rem;font-weight:600}
.sysitem .sub{font-size:.78rem;line-height:1.4}
.sysitem .syserr{color:var(--err);word-break:break-word}
.sysline{margin-top:10px;padding-top:10px;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center}
/* hero metric strip: CSS grid so numbers sit in aligned columns at every width
   (flex-wrap stretched each row to its own widths and orphaned the last item) */
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px 26px;align-items:start}
.metric{padding:0;min-width:0}
.metric b{display:block;font-size:1.4rem;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.metric.primary b{font-size:1.85rem}
.metric .lbl{display:block;font-size:.76rem;color:var(--dim);margin-top:2px}
.metric .ctx{display:block;font-size:.72rem;color:var(--dim);opacity:.8;margin-top:1px}
.metric.hot b{color:var(--err)} .metric.warm b{color:var(--warn)} .metric.calm b{color:var(--ok)}
.metric a{font-weight:700}
/* dashboard hero: stat rows (phones) + automation status chips - one fact per
   line, right-aligned tabular numbers, chips are atomic (never wrap mid-phrase) */
.dstat{display:none}
.dsc{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
.dschip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);padding:5px 10px;font-size:.74rem;line-height:1.2;color:var(--fg);white-space:nowrap;background:var(--card)}
.dschip::before{content:'';width:6px;height:6px;background:var(--ok);flex:0 0 auto}
.dschip.off{color:var(--dim)} .dschip.off::before{background:var(--line2)}
.dschip.chg{color:var(--acc)}
.dschip.chg::before{display:none}
.frow{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
.frow:first-of-type{border-top:0;padding-top:2px}
.frow .fmain{flex:1;min-width:0}
.frow .fmain b{display:block;font-size:.86rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.frow .fmain .sub{display:block;font-size:.76rem;margin-top:1px}
.frow .fsrc{display:inline-block;font-size:.68rem;font-weight:500;color:var(--dim);border:1px solid var(--line);padding:1px 6px;margin-right:6px;vertical-align:1px}
.dashgrid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:14px;align-items:start;margin-top:14px}
@media(max-width:1023px){.dashgrid{grid-template-columns:1fr}}
.dashgrid .card{margin:0}
.syswrap>summary{display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:600}
.dash-workbench{display:flex;flex-direction:column}.dash-workbench>[data-order="1"]{order:1}.dash-workbench>[data-order="2"]{order:2}.dash-workbench>[data-order="3"]{order:3}.dash-workbench>[data-order="4"]{order:4}.dash-workbench>[data-order="5"]{order:5}
.dash-attention{display:flex;gap:16px;flex-wrap:wrap}.dash-attention a{display:flex;align-items:center;gap:8px;font-weight:600;padding:8px 0}.dash-attention b{font-size:1.35rem}
.dash-workbench .dashgrid{grid-template-columns:1fr}.dash-workbench .metrics{margin-top:14px}.dash-workbench .sys{margin-top:14px}.dash-workbench .frow:nth-of-type(n+5){display:none}
.actwrap>summary{display:flex;align-items:center;gap:10px;padding:14px 16px 10px;cursor:pointer;list-style:none}
.actwrap>summary::-webkit-details-marker{display:none}
.actwrap>summary h3{margin:0}
.act-lnk{margin-left:auto}
.act-chev,.sys-chev{color:var(--dim);font-size:.78rem}
.act-chev::after{content:'▾'}
.actwrap:not([open]) .act-chev::after{content:'▸'}
.dashactions,.mlines,.sys-ix-actions{display:none}
/* Recent mail = a stacked feed, not a 5-col table: this card's column is only
   218-673px wide (assistant rail open/collapsed) while the table needed ~690px,
   so the subject got crushed into a sliver and the rest hid behind a horizontal
   scrollbar (measured: a 686px table inside a 396px card). Every line gets the
   full column width instead. Ref: CSS-Tricks "Responsive Data Tables" reflow +
   Material 3 lists (two-line anatomy: primary + supporting text). */
.mfeed{display:block}
.mrow{display:block;padding:9px 16px 10px;border-bottom:1px solid var(--line);color:var(--fg);text-decoration:none}
.mrow:last-child{border-bottom:0}
.mrow:hover{background:#fcfcfc;text-decoration:none}
.mrow:hover .mr-subj{text-decoration:underline}
.mr-top{display:flex;align-items:baseline;gap:10px;min-width:0}
.mr-from{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.8rem;color:var(--dim)}
.mr-when{flex:none;font-family:var(--mono);font-size:.74rem;color:var(--dim)}
.mr-subj{display:block;margin-top:2px;font-size:.92rem;font-weight:500;color:var(--acc);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mr-sum{display:block;margin-top:2px;font-size:.78rem;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mr-meta{display:flex;align-items:center;gap:8px;margin-top:6px;min-width:0}
.mr-meta .badge{flex:none}
.mr-llm{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.74rem;color:var(--dim)}
/* Activity rows: inline time+level left the message a ~60px sliver in this
   column (events wrapped 13 lines deep). Flow the message full-width under
   the inline prefix instead. Scoped to the dashboard card - /log untouched. */
.actwrap .logpanel .logrow{display:block;padding:4px 0}
@media(max-width:767px){
  .dh-actions{display:none}
  .syswrap>summary{display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none;padding:2px 0}
  .syswrap>summary::-webkit-details-marker{display:none}
  .sys-sum-t{font-weight:600;font-size:.86rem}
  .sys-chev{margin-left:auto}
  .sys-chev::after{content:'▸'}
  .syswrap[open] .sys-chev::after{content:'▾'}
  .metrics{grid-template-columns:1fr}
  .metrics .metric:not(.primary){display:none}
  .metric.primary{padding:6px 0}
  .sys{grid-template-columns:1fr}
  .metric.primary b{font-size:2.2rem}
  .dstat{display:block;margin-top:14px}
  .dsc{display:grid;grid-template-columns:1fr 1fr;gap:6px}
  .dashactions{display:flex;gap:8px;margin-top:12px}
  .dashactions .btn{flex:1;text-align:center;justify-content:center}
  .sys-ix-actions{display:flex;gap:8px;margin-top:10px;align-items:center}
  .ixcard{display:none}
  .mfeed .mrow:nth-child(n+5){display:none}
  .actwrap .logpanel{max-height:240px}
}
</style>
<div class="page-head">
  <div>
    <h1 class="page-title">Dashboard</h1>
    <div class="page-desc">System status, workload and what arrived recently.</div>
  </div>
  <div class="row dh-actions">
    <form class="inline" method="post" action="{{ url_for('check_now') }}"><button class="btn primary" type="submit" {{ 'disabled' if worker_state.running else '' }}>Check now</button></form>
    <a class="btn" href="{{ url_for('messages') }}">Open messages</a>
  </div>
</div>

{% if setup and setup.done < setup.total and not setup.dismissed %}
<div class="card" id="getstarted" style="border-left:3px solid var(--acc);margin-top:14px">
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <div style="flex:1;min-width:0"><b>Getting started: {{ setup.done }} of {{ setup.total }} steps done.</b>
    <span class="sub">Connect your mailbox, point at an LLM, build the index.</span></div>
    <a class="btn small" href="{{ url_for('welcome') }}">Continue setup</a>
  </div>
</div>
{% endif %}
<div class="dash-workbench" id="dash-workbench">
<section class="card" data-order="1"><div class="card-h"><h3>Needs your attention</h3></div><div class="dash-attention">
{% if st.needs_reply %}<a href="{{ url_for('messages', f='needs_reply') }}"><b>{{ st.needs_reply }}</b> need{{ 's' if st.needs_reply == 1 else '' }} a reply</a>{% endif %}
{% if st.errors %}<a href="{{ url_for('messages', f='errors') }}"><b>{{ st.errors }}</b> error{{ 's' if st.errors != 1 else '' }}</a>{% endif %}
{% if pending_approvals %}<a href="{{ url_for('assistant') }}"><b>{{ pending_approvals }}</b> pending approval{{ 's' if pending_approvals != 1 else '' }}</a>{% endif %}
</div>{% if not st.needs_reply and not st.errors and not pending_approvals %}<p class="sub">You're caught up — browse recent mail below.</p>{% endif %}
  <div class="dsc" role="group" aria-label="Automation status">
    <a class="dschip{{ ' off' if not settings.rules_apply else '' }}" href="{{ url_for('automation_controls') }}#ctl-rules" title="{{ 'Rules act live' if settings.rules_apply else 'Rules act in dry-run (suggest only)' }}">Rules {{ 'live' if settings.rules_apply else 'dry-run' }}</a>
    <a class="dschip{{ ' off' if not settings.llm_suggest else '' }}" href="{{ url_for('automation_controls') }}#ctl-classify" title="{{ 'LLM classification on' if settings.llm_suggest else 'LLM classification off' }}">LLM {{ 'on' if settings.llm_suggest else 'off' }}</a>
    <a class="dschip{{ ' off' if not settings.llm_apply else '' }}" href="{{ url_for('automation_categories') }}#filing-switch" title="{{ 'Auto-filing ON' if settings.llm_apply else 'Auto-filing off (suggests only)' }}">Auto-filing {{ 'ON' if settings.llm_apply else 'off' }}</a>
    <a class="dschip chg" href="{{ url_for('settings') }}">Settings →</a>
  </div>
</section>
<details class="card syswrap" data-order="5" {{ 'open' if sys_alert else '' }} data-alert="{{ '1' if sys_alert else '0' }}">
  <summary class="sys-sum"><span class="dot {{ 'err' if sys_alert else 'ok' }}"></span><span class="sys-sum-t">{% if sys_alert %}Something needs attention{% else %}All systems normal{% endif %}</span><span class="sys-chev" aria-hidden="true"></span></summary>
  <div class="sys">
    <div class="sysitem">
      <span class="dot {{ 'err' if worker_state.last_error else ('acc' if worker_state.running else 'ok') }}"></span>
      <div>
        <b>Triage</b>
        {% if worker_state.last_error %}
        <span class="sub syserr">{{ worker_state.last_error[:110] }} — <a href="{{ url_for('log') }}">log</a></span>
        {% elif worker_state.running %}
        <span class="sub">checking now…</span>
        {% elif worker_state.last_ok %}
        <span class="sub">connected · checked {{ worker_state.last_ok_r }} · next in ~{{ worker_state.next_in }}s</span>
        {% else %}
        <span class="sub">starting up…</span>
        {% endif %}
      </div>
    </div>
    <div class="sysitem">
      <span class="dot {{ 'ok' if px.running else ('warn' if px.installed else 'err') }}"></span>
      <div>
        <b>{{ 'Mail connection' if px.external else 'Proxy' }}</b>
        {% if px.external %}<span class="sub">External IMAP · managed outside this app</span>
        {% elif px.running %}
        <span class="sub">running · {% for l in px.listener_rows %}<span class="mono" style="font-size:.74rem">127.0.0.1:{{ l.port }}</span>{% if not loop.last %} · {% endif %}{% endfor %}</span>
        {% elif not px.installed %}
        <span class="sub syserr">emailproxy package missing</span>
        {% else %}
        <span class="sub">stopped{% if px.last_error %} — {{ px.last_error[:60] }}{% endif %} — <a href="{{ url_for('accounts') }}">accounts</a></span>
        {% endif %}
      </div>
    </div>
    <div class="sysitem">
      <span class="dot {{ 'acc' if ix.running else ('err' if ix.last_error else ('ok' if ix.chunks else 'warn')) }}"></span>
      <div>
        <b>Search index</b>
        {% if ix.running %}
        <span class="sub">{{ ix.progress or 'indexing…' }}</span>
        {% elif ix.last_error %}
        <span class="sub syserr">{{ ix.last_error[:90] }}</span>
        {% elif ix.chunks %}
        <span class="sub">ready · {{ ix.chunks }} chunks · folders {{ ix.folders_done }}/{{ ix.folders_total }}{% if ix.last_ok %} · last run {{ ix.last_ok_r }}{% endif %}</span>
        {% else %}
        <span class="sub">not built — run it below</span>
        {% endif %}
      </div>
    </div>
    <div class="sysitem">
      <span class="dot {{ lh.dot }}" id="llm-dot"></span>
      <div>
        <b>LLM</b>
        {% if llm.base %}
        <span class="sub{{ ' syserr' if lh.down else '' }}" id="llm-txt" data-ok="{{ llm.model }} · {{ llm_used }}/{{ settings.max_llm_per_hour }} calls this hour{% if llm.fallback %} · fallback {{ llm.fallback.model }}{% endif %}">{% if lh.down %}unreachable{% if lh.error %} — {{ lh.error[:90] }}{% endif %}{% if llm.fallback %} · fallback {{ llm.fallback.model }}{% endif %}{% else %}{{ llm.model }} · {{ llm_used }}/{{ settings.max_llm_per_hour }} calls this hour{% if llm.fallback %} · fallback {{ llm.fallback.model }}{% endif %}{% endif %}</span>
        <span class="sub" id="llm-settings"{% if not lh.down %} hidden{% endif %}>· <a href="{{ url_for('settings') }}">settings</a></span>
        <span class="sub" id="llm-checked"{% if not lh.checked_at %} hidden{% endif %}>· checked {{ lh.checked_r }}</span>
        {% else %}
        <span class="sub syserr">not configured — <a href="{{ url_for('settings') }}">settings</a></span>
        {% endif %}
      </div>
    </div>
  </div>
  {% if st.errors %}
  <div class="sysline" role="alert">
    <span class="badge err">{{ st.errors }} parked</span>
    <span class="sub">Messages parked after repeated LLM failures — fix the endpoint, then retry.</span>
    <form class="inline" method="post" action="{{ url_for('retry_errors') }}"><button class="btn small" type="submit">Retry parked</button></form>
  </div>
  {% elif st.queued and settings.llm_suggest %}
  <div class="sysline">
    <span class="badge warn">{{ st.queued }} waiting</span>
    <span class="sub">Queued for LLM classification — they are picked up each check.</span>
    <a class="btn small" href="{{ url_for('messages', f='queued') }}">View queue</a>
  </div>
  {% endif %}
  <div class="sysline" style="display:flex">
    <span class="sub" style="font-weight:600">Search index</span>
    <form class="inline" method="post" action="{{ url_for('index_run') }}"><button class="btn small" type="submit" {{ 'disabled' if ix.running else '' }}>Index now</button></form>
    <form class="inline" method="post" action="{{ url_for('index_rebuild') }}" onsubmit="return confirm('Rebuild the search index from scratch? Mail is untouched.');"><button class="btn small" type="submit" {{ 'disabled' if ix.running else '' }}>Rebuild</button></form>
  </div>
</details>

<details class="card" data-order="4"><summary style="cursor:pointer;font-weight:600">Automation &amp; historical statistics</summary><p class="sub">Filing and classification counts overlap; they are not shares of one total.</p>
  <div class="metrics">
    <div class="metric primary">
      <b>{% if st.needs_reply %}<a href="{{ url_for('messages', f='needs_reply') }}">{{ st.needs_reply }}</a>{% else %}{{ st.needs_reply }}{% endif %}</b>
      <span class="lbl">need a reply</span>
      <span class="ctx">flagged by the LLM · <a href="{{ url_for('messages', f='needs_reply') }}">view →</a></span>
    </div>
    <div class="metric">
      <b>{{ st.queued }}</b>
      <span class="lbl">waiting for LLM</span>
      <span class="ctx">queue length</span>
    </div>
    <div class="metric">
      <b>{{ st.moved }}</b>
      <span class="lbl">filed by automation or you</span>
      <span class="ctx">{{ (st.moved * 100 // st.total) if st.total else 0 }}% of {{ st.total }} seen</span>
    </div>
    <div class="metric">
      <b>{{ st.classified }}</b>
      <span class="lbl">currently classified</span>
      <span class="ctx">{{ (st.classified * 100 // st.total) if st.total else 0 }}% of {{ st.total }} seen</span>
    </div>
    <div class="metric">
      <b><a href="{{ url_for('rules') }}">{{ st.rules }}</a></b>
      <span class="lbl">rules active</span>
      <span class="ctx">checked top to bottom</span>
    </div>
    <div class="metric">
      <b><a href="{{ url_for('flows') }}">{{ st.flows }}</a></b>
      <span class="lbl">flows active</span>
      <span class="ctx">multi-step automations</span>
    </div>
    <div class="metric">
      <b><a href="{{ url_for('classifiers') }}">{{ st.classifiers }}</a></b>
      <span class="lbl">classifiers active</span>
      <span class="ctx">trained heuristics</span>
    </div>
  </div>
  <div class="dstat">
    <div class="dsrow"><span class="dsk">Filed messages</span><span class="dsv">{{ "{:,}".format(st.moved) }}</span></div>
    <div class="dsrow"><span class="dsk">Currently classified</span><span class="dsv">{{ "{:,}".format(st.classified) }}<span class="dsp">{{ (st.classified * 100 // st.total) if st.total else 0 }}% of {{ "{:,}".format(st.total) }}</span></span></div>
    <div class="dsrow"><span class="dsk">Rules active</span><span class="dsv"><a href="{{ url_for('rules') }}">{{ st.rules }}</a></span></div>
    <div class="dsrow"><span class="dsk">Flows active</span><span class="dsv"><a href="{{ url_for('flows') }}">{{ st.flows }}</a></span></div>
    <div class="dsrow"><span class="dsk">Classifiers active</span><span class="dsv"><a href="{{ url_for('classifiers') }}">{{ st.classifiers }}</a></span></div>
  </div>
  <div class="dashactions">
    <a class="btn primary" href="{{ url_for('messages') }}">Open messages</a>
    <form class="inline" method="post" action="{{ url_for('check_now') }}"><button class="btn" type="submit" {{ 'disabled' if worker_state.running else '' }}>Check now</button></form>
  </div>
</details>

{% if filings %}
<div class="card" id="filings" data-order="3">
  <div class="card-h" style="margin-bottom:2px"><h3>Recent filings</h3><span class="sub">Machine moves — Undo puts one back and keeps automation off it.</span></div>
  {% for f in filings %}
  <div class="frow">
    <div class="fmain">
      <b><span class="fsrc">{{ f.who }}</span>{{ f.subject|clip(70) or '(no subject)' }}</b>
      <span class="sub">→ {{ f.to_folder }}{% if f.from_folder and f.from_folder != f.to_folder %} (was {{ f.from_folder }}){% endif %} · {{ f.when_h }}</span>
    </div>
    {% if f.stale %}
    <span class="sub">moved on</span>
    {% else %}
    <form class="inline" method="post" action="{{ url_for('undo_move', lid=f.id) }}"><button class="btn small" type="submit">Undo</button></form>
    {% endif %}
  </div>
  {% endfor %}
</div>
{% endif %}

<div class="dashgrid" data-order="2">
  <div class="card flush">
    <div class="card-h" style="padding:14px 16px 10px;margin:0">
      <h3>Recent mail</h3>
      <a class="sub" href="{{ url_for('messages') }}">All messages →</a>
    </div>
    {% if messages %}
    <div class="mfeed">
      {% for m in messages %}
      <a class="mrow" href="{{ url_for('message_detail', mid=m.id) }}">
        <span class="mr-top">
          <span class="mr-from" title="{{ m.from_addr }}">{{ m.from_addr|clip(44) }}</span>
          <span class="mr-when">{{ m.when }}</span>
        </span>
        <span class="mr-subj" title="{{ m.subject }}">{{ m.subject|clip(90) or '(no subject)' }}</span>
        {% if m.llm_summary %}<span class="mr-sum" title="{{ m.llm_summary }}">{{ m.llm_summary|clip(140) }}</span>{% endif %}
        <span class="mr-meta"><span class="badge {{ m.badge[0] }}">{{ m.badge[1] }}</span><span class="mr-llm">{{ m.llm }}</span></span>
      </a>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty">
      <h4>No mail processed yet</h4>
      <p>When the watcher runs its first pass, new mail shows up here.</p>
    </div>
    {% endif %}
  </div>
</div>
<details class="card flush actwrap" data-order="5"><summary class="act-sum"><h3>Recent activity</h3><a class="sub act-lnk" href="{{ url_for('log') }}">Full log</a><span class="act-chev" aria-hidden="true"></span></summary><div class="logpanel">{% for e in events[:6] %}<div class="logrow"><span>{{ e.when }}</span> <span class="badge {{ e.cls }}">{{ e.level }}</span> <span>{{ e.message }}</span></div>{% endfor %}</div></details>
</div>
<script>
(function(){
  if(!window.matchMedia || !window.matchMedia('(max-width:767px)').matches) return;
  var sw=document.querySelector('.syswrap');
  if(sw && sw.getAttribute('data-alert')!=='1') sw.removeAttribute('open');
  var aw=document.querySelector('.actwrap'); if(aw) aw.removeAttribute('open');
})();
</script>
<script>
(function(){
  var dot=document.getElementById('llm-dot'), txt=document.getElementById('llm-txt'),
      lnk=document.getElementById('llm-settings'), chk=document.getElementById('llm-checked');
  if(!dot||!txt) return;
  function show(el, on){
    if(!el) return;
    if(on) el.removeAttribute('hidden'); else el.setAttribute('hidden','');
  }
  function paint(d){
    if(!d || d.configured===false) return;
    if(d.reachable===true){
      dot.className='dot ok'; txt.classList.remove('syserr');
      txt.textContent=txt.getAttribute('data-ok')||txt.textContent;
    } else if(d.reachable===false){
      dot.className='dot err'; txt.classList.add('syserr');
      txt.textContent='unreachable'+(d.error?' — '+String(d.error).slice(0,90):'')
        +(d.fallback?' · fallback '+d.fallback:'');
    }
    show(lnk, d.reachable===false);
    if(chk){ chk.textContent='· checked '+(d.checked_r||''); show(chk, !!d.checked_r); }
  }
  function poll(){
    fetch('/llm/health.json',{headers:{'Accept':'application/json'}})
      .then(function(r){return r.json();}).then(paint).catch(function(){});
  }
  setInterval(function(){ if(!document.hidden) poll(); }, 15000);
})();
</script>
{% if ix.running %}<script>(function(){ var me=location.pathname; (function r(){ setTimeout(function(){ if(location.pathname!==me) return; if(document.hidden){ r(); } else { location.reload(); } }, 8000); })(); })();</script>{% endif %}
"""






@app.route("/")
def dashboard():
    if _fresh_install():
        return redirect(url_for("welcome"))
    ws = dict(worker.state)
    ws["last_ok_r"] = rel_time(ws.get("last_ok"))
    interval = int(store.get_setting("poll_interval", 90))
    ws["interval"] = interval
    ws["next_in"] = max(0, int(interval - (time.time() - ws.get("last_cycle", 0))))
    msgs = store.messages(limit=10)
    for m in msgs:
        m["when"] = fmt_ts(m.get("processed_at"))
        m["badge"] = STATUS_BADGES.get(m.get("status"), ("", m.get("status", "")))
        llm = _category_caption(m)
        if m.get("llm_needs_reply"):
            llm += " · needs reply"
        m["llm"] = llm
    events = [e for e in store.recent_events(40) if e.get("level") != "debug"][:15]
    for e in events:
        e["when"] = fmt_ts(e["ts"])
        e["cls"] = {"error": "err", "info": "ok", "warn": "warn", "debug": ""}.get(e.get("level"), "")
    try:
        px = proxy.manager.status()
    except Exception as exc:
        px = {"running": False, "installed": True, "ports": {}, "restarts": 0,
              "last_error": repr(exc)}
    px['external'] = store.get_setting('proxy_mode') == 'external'
    px["listener_rows"] = [{"port": port} for port, _up in sorted((px.get("ports") or {}).items())]
    llm_cfg = engine.llm_config()
    lh = dict(llm_health.state)
    lh["checked_r"] = rel_time(lh.get("checked_at"))
    lh["down"] = bool(llm_cfg.get("base")) and lh.get("reachable") is False
    lh["dot"] = "ok" if (llm_cfg.get("base") and lh.get("reachable")) else (
        "warn" if (llm_cfg.get("base") and lh.get("reachable") is None) else "err")
    ix_st = index_status()
    filings = store.recent_moves(limit=8)
    for f in filings:
        f["when_h"] = fmt_ts(f.get("ts"))
        f["who"] = {"rule": "rule", "flow": "flow", "auto-file": "LLM", "assistant": "assistant",
                    "manual": "you", "trash": "→ Trash"}.get(f.get("source") or "", f.get("source") or "move")
    sys_alert = bool(ws.get("last_error") or (not px.get('external') and not px.get("running"))
                     or ix_st.get("last_error") or not llm_cfg.get("base")
                     or lh.get("down"))
    return render(_render_src(
        DASH_TMPL, worker_state=ws, st=stats(), messages=msgs, events=events,
        settings=store.all_settings(), ix=ix_st, px=px, filings=filings,
        llm=llm_cfg, lh=lh, llm_used=store.llm_count_last_hour(), sys_alert=sys_alert,
        setup=setup_state(), pending_approvals=store.count_pending_agent_actions()))


@app.route("/check", methods=["POST"])
def check_now():
    worker.trigger()
    flash("Check triggered — give it a few seconds and reload.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/messages/<int:mid>/sweep", methods=["POST"])
def message_sweep(mid):
    m = store.get_message(mid)
    if not m:
        flash("No such message.", "err")
        return redirect(url_for("messages"))
    n = engine.sweep_msg_events(msg_id=mid)
    if n:
        store.log_msg_event(mid, "backfill", "%d event%s reconstructed from stored state"
                            % (n, "" if n == 1 else "s"))
        flash("Rebuilt %d event%s from stored state." % (n, "" if n == 1 else "s"), "ok")
    else:
        flash("Nothing to rebuild — no stored signals for this message.", "err")
    return redirect(url_for("message_detail", mid=mid))


@app.route("/messages/<int:mid>/snooze", methods=["POST"])
def message_snooze(mid):
    m = store.get_message(mid)
    if not m:
        flash("No such message.", "err")
        return redirect(url_for("messages"))
    try:
        hours = float(request.form.get("hours") or 0)
    except ValueError:
        hours = 0
    if hours > 0:
        until = int(time.time() + hours * 3600)
        store.snooze_message(mid, until)
        store.log_event("info", "snoozed message %d ('%s') until %s"
                        % (mid, (m.get("subject") or "")[:50], fmt_ts(until)))
        store.log_msg_event(mid, "snooze", "snoozed until %s" % fmt_ts(until))
        learning.observe(mid, "snooze", "%.0fh" % hours, source="ui")
        flash("Snoozed until %s." % fmt_ts(until), "ok")
    else:
        store.snooze_message(mid, 0)
        store.log_event("info", "woke message %d ('%s')"
                        % (mid, (m.get("subject") or "")[:50]))
        store.log_msg_event(mid, "wake", "back in the lists")
        learning.observe(mid, "wake", "", source="ui")
        flash("Back in your lists.", "ok")
    return redirect(request.referrer or url_for("message_detail", mid=mid))


@app.route("/undo/<int:lid>", methods=["POST"])
def undo_move(lid):
    ok, msg = engine.undo_filing(lid)
    flash(msg, "ok" if ok else "err")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/retry-errors", methods=["POST"])
def retry_errors():
    ids = store.parked_error_ids()
    n = store.retry_parked_errors()
    if ids:
        # explicit user action: run the manual classifier on exactly these ids so
        # the scheduled worker's hourly budget cannot starve the retry
        classifier.trigger(ids)
    worker.trigger()
    flash("Re-queued %d message(s) for classification." % n, "ok")
    return redirect(url_for("dashboard"))


def _trigger_index(rebuild=False):
    """Start an index pass: queue a job for the stage worker, or wake the
    in-process indexer when the stages run here (dev and tests)."""
    if engine.external_stages():
        jid = stage_worker.enqueue_index(rebuild=rebuild, manual=True)
        if jid is None:
            # a singleton pass is already active (running or queued)
            return {"started": False, "note": "an index pass is already in flight"}
        return {"started": True, "job": jid}
    indexer.trigger(rebuild=rebuild)
    return {"started": True}


@app.route("/index/run", methods=["POST"])
def index_run():
    res = _trigger_index(rebuild=False)
    if res.get("started"):
        flash("Indexing started — progress shows on the dashboard and the Log page.", "ok")
    else:
        flash("An index pass is already in flight.", "warn")
    nxt = (request.values.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("dashboard"))


@app.route("/index/rebuild", methods=["POST"])
def index_rebuild():
    res = _trigger_index(rebuild=True)
    if res.get("started"):
        flash("Rebuilding the search index from scratch — mail itself is untouched.", "ok")
    else:
        flash("An index pass is already in flight; rebuild queued with it.", "warn")
    nxt = (request.values.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- rules

RULES_TMPL = """
<div class="page-head r-head">
  <div>
    <h1 class="page-title">Rules</h1>
    <div class="page-desc">Evaluated top to bottom — first match wins. Rules act {{ 'live' if settings.rules_apply else 'in dry-run (suggest only)' }}. A rule with no actions is a guard: matching mail stays put.</div>
  </div>
  <div class="row">
    <form class="inline" method="post" action="{{ url_for('rules_test') }}">
      <button class="btn" type="submit">Test against last {{ test_limit }} messages</button></form>
    <a class="btn small" href="{{ url_for('simulate') }}">Simulate a draft</a>
    <a class="btn primary" href="{{ url_for('rule_new') }}">New rule</a>
  </div>
</div>
{% if test_results %}
<div class="card">
  <div class="card-h"><h3>Dry-run test <span class="sub">(nothing was changed) — of the last {{ test_limit }} messages</span></h3></div>
  <div class="tablewrap"><table class="tbl" style="max-width:560px">
    <thead><tr><th>rule</th><th class="r">matches</th></tr></thead>
    <tbody>
     {% for t in test_results %}<tr><td>{{ t.name }}{% if not t.count %}<div class="sub">No winning matches in this sample; an earlier rule may claim them.</div>{% endif %}</td><td class="r mono">{{ t.count }}</td></tr>{% endfor %}
    <tr><td class="sub">unmatched (would go to LLM)</td><td class="r mono">{{ test_unmatched }}</td></tr>
    </tbody></table></div>
</div>
{% endif %}
<div class="card flush" id="rules">
  {% if rules %}
  <div class="tablewrap"><table class="tbl mcards">
    <thead><tr><th style="width:44px">#</th><th>rule</th><th>if</th><th>actions</th><th class="r"></th></tr></thead>
    <tbody>
    {% for r in rules %}
    <tr{% if not r.enabled %} style="opacity:.55"{% endif %}>
      <td class="sub mono">{{ loop.index }}</td>
      <td><b>{{ r.name }}</b>{% if not r.enabled %} <span class="badge">disabled</span>{% endif %}{% if r.diagnostic %}<div class="sub" style="color:var(--warn)">{{ r.diagnostic }}</div>{% endif %}</td>
      <td class="mono" style="font-size:.79rem">{{ r.summary }}</td>
      <td class="sub">{{ r.actions }}</td>
      <td class="r"><span class="rowacts" style="justify-content:flex-end">
        <span class="seg" role="group" aria-label="Reorder">
        <form class="inline" method="post" action="{{ url_for('rule_move', rule_id=r.id) }}"><input type="hidden" name="dir" value="top"><button class="btn small" type="submit" title="move to top" aria-label="Move rule to top">⤒</button></form>
        <form class="inline" method="post" action="{{ url_for('rule_move', rule_id=r.id) }}"><input type="hidden" name="dir" value="up"><button class="btn small" type="submit" title="move up" aria-label="Move rule up">↑</button></form>
        <form class="inline" method="post" action="{{ url_for('rule_move', rule_id=r.id) }}"><input type="hidden" name="dir" value="down"><button class="btn small" type="submit" title="move down" aria-label="Move rule down">↓</button></form>
        </span>
        <form class="inline px-swf" method="post" action="{{ url_for('rule_toggle', rule_id=r.id) }}">
          <label class="px-sw" title="{{ 'Disable' if r.enabled else 'Enable' }} {{ r.name }}"><input type="checkbox" {{ 'checked' if r.enabled }} onchange="this.form.requestSubmit()" aria-label="{{ 'Disable' if r.enabled else 'Enable' }} {{ r.name }}"><span class="px-tr"></span></label>
        </form>
        <a class="btn small ra-inline" href="{{ url_for('rule_edit', rule_id=r.id) }}">Edit</a>
        <form class="inline ra-inline" method="post" action="{{ url_for('rule_delete', rule_id=r.id) }}">
          <button class="btn small danger arm-del" type="submit" data-arm-label="Press again to delete rule {{ r.name }}" aria-label="Delete rule {{ r.name }}">Delete</button></form>
        <details class="menu ra-menu">
          <summary class="btn small" aria-haspopup="menu" aria-label="More actions">⋯</summary>
          <div class="menu-pop" role="menu">
            <a class="menu-item" href="{{ url_for('rule_edit', rule_id=r.id) }}">Edit</a>
            <form method="post" action="{{ url_for('rule_delete', rule_id=r.id) }}">
              <button class="menu-item danger arm-del" type="submit" data-arm-label="Press again to delete rule {{ r.name }}" aria-label="Delete rule {{ r.name }}">Delete…</button></form>
          </div>
        </details>
      </span></td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% else %}
  <div class="empty">
    <h4>No rules yet</h4>
    <p>Create one like “from contains newsletter@ → move to Newsletters”, or tell the assistant what to sort and it will propose one.</p>
    <a class="btn primary" href="{{ url_for('rule_new') }}">New rule</a>
  </div>
  {% endif %}
</div>
"""




PLUGINS_TMPL = """
<div class="page-head">
  <div>
    <h1 class="page-title">Plugins</h1>
    <div class="page-desc">Sandboxed extensions for the mail pipeline and the assistant. They stay inert
    until switched on, and every host call they make is permission-gated and audited.</div>
  </div>
  <form method="post" action="{{ url_for('plugins_rescan') }}"><button class="btn">Rescan</button></form>
</div>

{% if not px %}
<div class="card"><div class="sub">No plugins installed yet. Drop a folder with a
<span class="mono">manifest.json</span> and a JS bundle into <span class="mono">{{ proots.user }}</span>, then Rescan.</div></div>
{% else %}
<div class="sub" style="margin:2px 2px 0">{{ n_on }} of {{ n_all }} running</div>

{% if running %}
<div class="px-grp">Running</div>
<div class="px-list">
  {% for p in running %}
  <div class="px-row">
    <span class="px-ico" aria-hidden="true">{{ p.icon|safe }}</span>
    <a class="px-main" href="{{ url_for('plugin_detail', pid=p.id) }}">
      <span class="px-nm">{{ p.name }} <span class="badge">{{ p.version }}</span> <span class="badge">{{ p.kind_label }}</span>{% if p.needs_regrant %} <span class="badge warn">needs re-grant</span>{% endif %}{% if p.problem %} <span class="badge warn">issue</span>{% endif %}</span>
      <span class="px-dz">{{ p.oneliner }}</span>
    </a>
    <form method="post" action="{{ url_for('plugins_update', pid=p.id) }}" class="inline px-swf">
      <input type="hidden" name="action" value="disable">
      <label class="px-sw" title="Disable {{ p.name }}"><input type="checkbox" checked onchange="this.form.requestSubmit()" aria-label="Disable {{ p.name }}"><span class="px-tr"></span></label>
    </form>
    <a class="px-go" href="{{ url_for('plugin_detail', pid=p.id) }}" aria-label="Open {{ p.name }} details">&rsaquo;</a>
  </div>
  {% endfor %}
</div>
{% endif %}

{% if stopped %}
<div class="px-grp">Off</div>
<div class="px-list">
  {% for p in stopped %}
  <div class="px-row">
    <span class="px-ico" aria-hidden="true">{{ p.icon|safe }}</span>
    <a class="px-main" href="{{ url_for('plugin_detail', pid=p.id) }}">
      <span class="px-nm">{{ p.name }} <span class="badge">{{ p.version }}</span> <span class="badge">{{ p.kind_label }}</span>{% if p.needs_regrant %} <span class="badge warn">needs re-grant</span>{% endif %}{% if p.problem %} <span class="badge warn">issue</span>{% endif %}</span>
      <span class="px-dz">{{ p.oneliner }}</span>
    </a>
    <form method="post" action="{{ url_for('plugins_update', pid=p.id) }}" class="inline px-swf">
      <input type="hidden" name="action" value="enable">
      <label class="px-sw" title="Enable {{ p.name }}"><input type="checkbox" onchange="this.form.requestSubmit()" aria-label="Enable {{ p.name }}"><span class="px-tr"></span></label>
    </form>
    <a class="px-go" href="{{ url_for('plugin_detail', pid=p.id) }}" aria-label="Open {{ p.name }} details">&rsaquo;</a>
  </div>
  {% endfor %}
</div>
{% endif %}

<details class="card" style="margin-top:20px">
  <summary class="sub" style="cursor:pointer">Developer details</summary>
  <div class="sub" style="margin-top:8px">SDK {{ sdk }} &middot; built-ins: <span class="mono">{{ proots.builtin }}</span> &middot;
  user plugins: <span class="mono">{{ proots.user }}</span>. The assistant sees a plugin&rsquo;s tools as
  <span class="mono">plugin__&lt;id&gt;__&lt;tool&gt;</span>. To write one, see <span class="mono">sdk/README.md</span> and
  <span class="mono">docs/plugins-authoring.md</span>.</div>
</details>
{% endif %}
"""


PLUGIN_DETAIL_TMPL = """
<style>
 .pxd-seg{display:inline-flex;border:1px solid var(--line2)}
 .pxd-seg label{display:flex;margin:0}
 .pxd-seg input{position:absolute;opacity:0;width:1px;height:1px}
 .pxd-seg span{display:block;padding:7px 13px;font-size:.85rem;border-left:1px solid var(--line);cursor:pointer;color:var(--dim);background:#fff}
 .pxd-seg label:first-child span{border-left:0}
 .pxd-seg input:checked+span{background:#000;color:#fff}
 .pxd-seg input:focus-visible+span{outline:2px solid var(--acc);outline-offset:-2px}
 .pxd-perm{display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-top:1px solid var(--line);cursor:pointer;margin:0}
 .pxd-perm:first-of-type{border-top:0;padding-top:2px}
 .pxd-perm input{margin:3px 0 0;width:15px;height:15px;accent-color:#000;flex:0 0 auto}
 .pxd-perm .pxd-pt{flex:1;min-width:0}
 .pxd-perm .pxd-pt b{display:block;font-size:.9rem}
 .pxd-dl{display:grid;grid-template-columns:150px minmax(0,1fr);gap:5px 16px;font-size:.88rem;margin:10px 0 0}
 .pxd-dl dt{color:var(--dim)} .pxd-dl dd{margin:0;min-width:0}
 .pxd-ev{display:flex;gap:10px;padding:7px 0;border-top:1px solid var(--line);font-size:.85rem;align-items:baseline}
 .pxd-ev:first-of-type{border-top:0}
 .pxd-ev time{color:var(--dim);font-family:var(--mono);font-size:.76rem;white-space:nowrap;flex:0 0 auto}
 .pxd-set{display:grid;grid-template-columns:minmax(0,1fr) minmax(200px,340px);gap:8px 18px;padding:12px 0;border-top:1px solid var(--line);align-items:start}
 .pxd-set:first-of-type{border-top:0;padding-top:4px}
 .pxd-set .st-l b{display:block;font-size:.87rem;font-weight:600}
 .pxd-set .st-l .sub{display:block}
 @media(max-width:900px){ .pxd-set{grid-template-columns:1fr} }
 .pxd-tn{flex:0 0 150px}
 @media(max-width:640px){ .pxd-ev{flex-direction:column;align-items:flex-start;gap:3px} .pxd-tn{flex:0 0 auto} }
 @media (max-width:640px){ .pxd-dl{grid-template-columns:1fr;gap:1px} .pxd-dl dt{margin-top:8px} }
</style>
<div class="page-head">
  <div style="min-width:0">
    <a class="backlink sub" href="{{ url_for('plugins_page') }}">&lsaquo; All plugins</a>
    <h1 class="page-title" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><span class="px-ico" aria-hidden="true">{{ p.icon|safe }}</span> <span>{{ p.name }}</span>{% if p.root == 'builtin' %} <span class="badge">built-in</span>{% endif %} <span class="badge">{{ p.version }}</span></h1>
    <div class="page-desc">{{ p.tagline }}</div>
  </div>
  <form method="post" action="{{ url_for('plugins_update', pid=p.id) }}" class="inline" style="display:flex;align-items:center;gap:10px">
    <input type="hidden" name="action" value="{{ 'disable' if p.enabled else 'enable' }}">
    <input type="hidden" name="next" value="detail">
    <span class="sub" style="font-weight:600">{{ 'Running' if p.enabled else 'Off' }}</span>
    <label class="px-sw" title="{{ 'Disable' if p.enabled else 'Enable' }} {{ p.name }}"><input type="checkbox" {{ 'checked' if p.enabled }} onchange="this.form.requestSubmit()" aria-label="{{ 'Disable' if p.enabled else 'Enable' }} {{ p.name }}"><span class="px-tr"></span></label>
  </form>
</div>

<div class="card">
  <div class="card-h"><h3>What it does</h3></div>
  <div>{{ p.description }}</div>
  <dl class="pxd-dl">
    {% for k2, v2 in p.roles %}<dt>{{ k2 }}</dt><dd>{{ v2|safe }}</dd>{% endfor %}
    <dt>Plugin ID</dt><dd class="mono">{{ p.id }}</dd>
    <dt>Source</dt><dd>{{ 'shipped with the app' if p.root == 'builtin' else 'user plugin' }} &middot; v{{ p.version }}</dd>
  </dl>
  {% if p.tools %}
  {% if p.enabled %}<p><a class="btn primary" href="{{ url_for('assistant', prompt='Use the ' ~ p.name ~ ' plugin to help me with my mail. Ask me for any missing details first.') }}">Use in Assistant</a> <span class="sub">Opens an editable request; your permissions still apply.</span></p>{% endif %}
  <div class="sub" style="margin-top:14px;font-weight:600">Assistant tools</div>
  {% for t in p.tools %}
  <div class="pxd-ev"><span class="mono pxd-tn">{{ t.name }}</span><span class="sub" style="min-width:0">{{ t.description }}</span></div>
  {% endfor %}
  {% endif %}
</div>

{% if p.ui_mode %}
<div class="card" id="ui">
  <div class="card-h"><h3>Browser view</h3>
    <span class="sub">{{ 'Sandboxed composed page - the host renders a validated component tree; no plugin browser code runs.' if p.ui_mode == 'composed' else 'Trusted browser bundle - runs plugin code in an isolated frame.' }}</span>
  </div>
  {% if p.ui_pages %}
  <div class="sub" style="margin-bottom:8px">Pages:
    {% for pg in p.ui_pages %}<a href="{{ url_for('extension_page', pid=p.id, page=pg.id) }}">{{ pg.title }}</a>{% if not loop.last %}, {% endif %}{% endfor %}
  </div>
  {% endif %}
  {% if p.ui_trusted %}
  <div class="msg warn" style="margin:0 0 10px">A trusted browser view runs plugin code that can transmit any data it
  receives &mdash; including mail text &mdash; by navigating itself. The plugin&rsquo;s <span class="mono">net.http</span>
  grants do <b>not</b> constrain it. Approve only if you trust this exact plugin content.</div>
  <div class="spread">
    <div class="sub">{{ ('Approved for the current content (v' ~ p.ui_approval.get('version','?') ~ ').') if p.ui_approved else 'Not approved &mdash; its page will not run.' }} Editing or updating the plugin invalidates this approval.</div>
    <div class="row">
      {% if p.ui_approved %}
      <form method="post" action="{{ url_for('plugin_ui_revoke', pid=p.id) }}"><button class="btn danger" type="submit">Revoke approval</button></form>
      {% else %}
      <form method="post" action="{{ url_for('plugin_ui_approve', pid=p.id) }}" onsubmit="return confirm('Approve this browser view? It runs plugin code that can transmit mail text by navigating itself.');"><button class="btn danger" type="submit">Approve browser view</button></form>
      {% endif %}
    </div>
  </div>
  {% endif %}
</div>
{% endif %}

<div class="card" id="access">
  <div class="card-h"><h3>Access</h3><span class="sub">{{ 'What this plugin may touch. Unchecking revokes it at the host level — the sandbox cannot call it at all.' if p.permissions else 'No host access — runs pure compute.' }}</span></div>
  <form method="post" action="{{ url_for('plugins_update', pid=p.id) }}">
    <input type="hidden" name="action" value="save">
    <input type="hidden" name="next" value="detail">
    {% for g in p.perms_detail %}
    <label class="pxd-perm">
      <input type="checkbox" name="grant_{{ g.name }}" value="1" {{ 'checked' if g.granted else '' }}>
      <span class="pxd-pt"><b>{{ g.title }}</b><span class="sub">{{ g.why }}</span></span>
      <span class="mono sub">{{ g.name }}</span>
    </label>
    {% endfor %}
    {% if p.has_tools %}
    <div class="pxd-set">
      <div class="st-l"><b>Assistant permission</b><span class="sub">When you ask the assistant in chat. Ask = it queues a card you approve; Auto = runs directly (still audited).</span></div>
      <div class="st-c"><div class="pxd-seg" role="radiogroup" aria-label="Assistant permission for {{ p.name }}">
        {% for lvl in ('off','ask','auto') %}<label><input type="radio" name="agent_level" value="{{ lvl }}" {{ 'checked' if p.agent_level == lvl else '' }}><span>{{ lvl }}</span></label>{% endfor %}
      </div></div>
    </div>
    {% endif %}
    {% if p.optin_matcher or p.optin_retriever or p.optin_classifier %}
    <div class="pxd-set">
      <div class="st-l"><b>Pipeline use</b><span class="sub">The kernel only calls this plugin inside the mail pipeline when ticked.</span></div>
      <div class="st-c" style="flex-direction:column;align-items:flex-end;gap:2px">
        {% if p.optin_classifier %}<label class="check" style="margin:2px 0"><input type="checkbox" name="opt_in_classifier" value="1" {{ 'checked' if p.in_classifiers else '' }}> <span>classification fast-path</span></label>
        {% if p.classifier_heuristics %}<label class="check" style="margin:2px 0"><span class="sub">bound heuristic (confidence gate)</span> <select name="classifier_heuristic" style="width:auto;padding:3px 6px;font-size:.82rem" aria-label="Heuristic binding for the classifier plugin">
          <option value="0">— pick one —</option>
          {% for h in p.classifier_heuristics %}<option value="{{ h.id }}"{{ ' selected' if p.bound_heuristic_id == h.id else '' }}>{{ h.name }}{% if not h.enabled %} (parked){% endif %}</option>{% endfor %}
        </select></label>{% endif %}{% endif %}
        {% if p.optin_matcher %}<label class="check" style="margin:2px 0"><input type="checkbox" name="opt_in_matcher" value="1" {{ 'checked' if p.in_matchers else '' }}> <span>rule conditions</span></label>{% endif %}
        {% if p.optin_retriever %}<label class="check" style="margin:2px 0"><input type="checkbox" name="opt_in_retriever" value="1" {{ 'checked' if p.in_retrievers else '' }}> <span>search re-ranking</span></label>{% endif %}
      </div>
    </div>
    {% endif %}
    {% if p.permissions or p.has_tools or p.optin_matcher or p.optin_retriever or p.optin_classifier %}
    <div class="savebar"><button class="btn primary" type="submit">Save</button></div>
    {% endif %}
  </form>
</div>

{% if p.config_fields %}
<div class="card">
  <div class="card-h"><h3>Settings</h3><span class="sub">Stored per install; the plugin reads these via ctx.config.</span></div>
  <form method="post" action="{{ url_for('plugins_update', pid=p.id) }}">
    <input type="hidden" name="action" value="config">
    <input type="hidden" name="next" value="detail">
    {% for f in p.config_fields %}
    <div class="pxd-set">
      <div class="st-l"><b class="mono" style="font-size:.84rem">{{ f.name }}</b><span class="sub">{{ f.label }}</span></div>
      <div class="st-c">
        {% if f.type == 'boolean' %}<label class="check"><input type="checkbox" name="cfg_{{ f.name }}" value="1" {{ 'checked' if f.value else '' }}> <span>enabled</span></label>
        {% else %}<input type="text" name="cfg_{{ f.name }}" value="{{ f.value }}" class="mono" aria-label="{{ f.name }}">{% endif %}
      </div>
    </div>
    {% endfor %}
    <div class="savebar"><button class="btn primary" type="submit">Save settings</button></div>
  </form>
</div>
{% endif %}

<div class="card">
  <div class="card-h"><h3>Activity</h3><span class="sub">Recent log lines mentioning this plugin.</span></div>
  {% if p.last_error %}<div class="sub" style="margin-bottom:8px">last note: <span class="badge warn">{{ p.last_error }}</span></div>{% endif %}
  {% if p.events %}
  {% for e in p.events %}<div class="pxd-ev"><time>{{ e.when }}</time><span class="sub mono" style="font-size:.75rem">{{ e.level }}</span><span style="min-width:0">{{ e.message }}</span></div>{% endfor %}
  {% else %}<div class="sub">Nothing logged yet.</div>{% endif %}
</div>

<details class="card">
  <summary class="sub" style="cursor:pointer">Developer details</summary>
  <div class="sub" style="margin-top:8px">
    entrypoint <span class="mono">{{ p.entry }}</span> &middot; runtime quickjs (sandboxed worker process) &middot; SDK {{ sdk }}<br>
    limits: {{ p.limits_text }} &middot; docs: <span class="mono">sdk/README.md</span> &middot; <span class="mono">docs/plugins-authoring.md</span>
  </div>
</details>
"""

WELCOME_TMPL = """<style>
body.setup .side,body.setup .topbar,body.setup #asb,body.setup .foot{display:none !important}
body.setup .main{margin-left:0 !important;margin-right:0 !important}
body.setup .bottom-nav{display:none !important}
.wz{max-width:680px;margin:0 auto;padding:26px 18px 64px;min-height:100dvh;display:flex;flex-direction:column}
.wz-top{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:6px 2px 18px}
.wz-brand{font-weight:700;letter-spacing:-.01em}
.wz-brand .sub{color:var(--dim);font-weight:400;margin-left:7px}
.wz-exit{color:var(--dim);font-size:.86rem;text-decoration:none}
.wz-exit:hover{color:var(--fg);text-decoration:underline}
.wz-rail{list-style:none;display:flex;gap:6px;margin:0 0 20px;padding:0;flex-wrap:wrap}
.wz-step{display:flex;align-items:center;gap:7px;padding:7px 11px;border:1px solid var(--line);color:var(--dim);font-size:.84rem;background:#fff}
.wz-step.reach{cursor:pointer}
.wz-step.reach:hover{border-color:var(--fg)}
.wz-step .wz-dot{width:20px;height:20px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line2);font-size:.72rem;font-weight:600}
.wz-step.cur{border-color:var(--fg);color:var(--fg);font-weight:600}
.wz-step.done .wz-dot{background:#000;color:#fff;border-color:#000}
.wz-body{flex:1}
.wz.live .wz-screen{display:none}
.wz.live .wz-screen.on{display:block;animation:wzIn .22s ease both}
.wz.live.back .wz-screen.on{animation-name:wzInBack}
@keyframes wzIn{from{opacity:0;transform:translateX(14px)}to{opacity:1;transform:none}}
@keyframes wzInBack{from{opacity:0;transform:translateX(-14px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){.wz.live .wz-screen.on{animation:none}}
.wz-card{border:1px solid var(--line);background:#fff;padding:26px 26px 22px}
.wz-hero{border:1px solid var(--line);background:#0a0a0a;color:#fff;padding:34px 30px 30px}
.wz-hero h1{font-size:1.6rem;margin:0 0 10px;letter-spacing:-.02em}
.wz-hero p{color:#c9c9c9;margin:0 0 16px;line-height:1.55}
.wz-hero ul{margin:0 0 22px;padding:0;list-style:none}
.wz-hero li{padding:5px 0 5px 22px;position:relative;color:#e4e4e4;font-size:.93rem}
.wz-hero li:before{content:'';position:absolute;left:2px;top:13px;width:7px;height:7px;background:#fff}
.wz-hero .btn{background:#fff;color:#000;border-color:#fff}
.wz-hero .wz-skip{color:#b9b9b9;margin-left:14px}
.wz-hero .wz-skip:hover{color:#fff}
.wz-card h2{margin:0 0 6px;font-size:1.25rem;letter-spacing:-.01em}
.wz-lede{color:var(--dim);margin:0 0 16px;line-height:1.55;font-size:.92rem}
.wz-note{border:1px solid var(--line);background:var(--hover,#f5f5f5);padding:10px 12px;color:var(--dim);font-size:.84rem;line-height:1.5;margin:14px 0 0}
.wz-ok{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);background:#fff;padding:12px 14px;margin:0 0 4px}
.wz-ok .dot{margin-top:5px}
.wz-field{margin:13px 0 0}
.wz-field label{display:block;font-size:.84rem;font-weight:600;margin-bottom:3px}
.wz-field .sub{color:var(--dim);font-size:.8rem;display:block;margin-bottom:4px}
.wz-field input{width:100%;padding:9px 10px;border:1px solid var(--line2);font:inherit;background:#fff}
.wz-actions{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:24px}
.wz-linkbtn{background:none;border:0;color:var(--dim);font:inherit;font-size:.86rem;cursor:pointer;padding:0}
.wz-linkbtn:hover{color:var(--fg);text-decoration:underline}
.wz-skip{color:var(--dim);font-size:.86rem;text-decoration:none}
.wz-skip:hover{color:var(--fg);text-decoration:underline}
.wz-foot{color:var(--dim);font-size:.78rem;padding:30px 2px 0;line-height:1.5}
.wz-recap{display:flex;gap:10px;align-items:flex-start;padding:11px 0;border-top:1px solid var(--line)}
.wz-recap:first-of-type{border-top:0}
.wz-recap .dot{margin-top:5px}
.wz-recap b{display:block}
.wz-recap .sub{color:var(--dim);font-size:.85rem}
@media (max-width:560px){.wz{padding:18px 14px 54px}.wz-step{padding:6px 8px;font-size:.78rem;gap:5px}.wz-step .wz-dot{width:17px;height:17px}.wz-card{padding:20px 16px 18px}.wz-hero{padding:26px 20px}}
</style>

<div class="wz" id="wz" data-start="{{ start }}">
  <header class="wz-top">
    <span class="wz-brand">Mail Triage<span class="sub">setup</span></span>
    <form method="post" action="{{ url_for('welcome') }}" class="inline">
      <input type="hidden" name="action" value="skip">
      <button type="submit" class="wz-linkbtn wz-exit">Exit setup</button>
    </form>
  </header>

  <ol class="wz-rail" id="wz-rail" aria-label="Setup steps">
    {% for x in st.steps %}
    <li class="wz-step{{ ' done' if x.done }}" data-i="{{ loop.index }}" data-id="{{ x.id }}">
      <span class="wz-dot">{{ '&#10003;'|safe if x.done else loop.index }}</span><span>{{ x.label }}</span>
    </li>
    {% endfor %}
  </ol>

  <div class="wz-body" id="wz-body">
    <section class="wz-screen" data-i="0" aria-label="Welcome">
      <div class="wz-hero">
        <h1>Your mail, sorted on your own machine.</h1>
        <p>Mail Triage reads your mailbox, files the repetitive mail with rules, and lets an LLM
        handle what rules cannot. Everything runs on this box - search and learning included.</p>
        <ul>
          <li>Rules sort the repetitive mail automatically</li>
          <li>An LLM classifies the rest - local or hosted, your choice</li>
          <li>Mail is never deleted. Worst case, it moves to a folder</li>
        </ul>
        <button type="button" class="btn" data-go="1">Set up my mailbox &#8594;</button>
        <form method="post" action="{{ url_for('welcome') }}" class="inline">
          <input type="hidden" name="action" value="skip">
          <button type="submit" class="wz-linkbtn wz-skip">Skip setup - take me to the app</button>
        </form>
      </div>
    </section>

    <section class="wz-screen" data-i="1" aria-label="Mailbox">
      <div class="wz-card">
        <h2>Connect your mailbox</h2>
        {% if st.steps[0].done %}
        <div class="wz-ok"><span class="dot ok"></span><span><b>Signed in as {{ st.steps[0].user or 'your account' }}</b><br>
        <span class="sub">Tokens stay on this machine.</span></span></div>
        {% else %}
        <p class="wz-lede">Sign in once through the embedded OAuth proxy. Credentials and tokens stay on
        this machine - the app talks to your mailbox directly.</p>
        <div class="wz-note">The Accounts page drives the sign-in and tells you exactly what to do.
        Come back here after - setup resumes where you left off.</div>
        {% endif %}
        <div class="wz-actions">
          {% if st.steps[0].done %}
          <button type="button" class="btn" data-go="2">Continue &#8594;</button>
          <a class="wz-skip" href="{{ url_for('accounts') }}">Manage accounts</a>
          {% else %}
          <a class="btn" href="{{ url_for('accounts') }}">Open Accounts &#8594;</a>
          <button type="button" class="wz-linkbtn" data-go="2">Do this later</button>
          {% endif %}
          <button type="button" class="wz-linkbtn" data-go="0">&#8592; Back</button>
        </div>
      </div>
    </section>

    <section class="wz-screen" data-i="2" aria-label="LLM">
      <div class="wz-card">
        <h2>Point at an LLM</h2>
        <p class="wz-lede">Classification and drafting use any OpenAI-compatible endpoint - a server on
        this machine, or a hosted API. Skip this and rules plus search still work.</p>
        {% if st.steps[1].done %}
        <div class="wz-ok"><span class="dot ok"></span><span><b>{{ st.steps[1].model or 'Model' }} at {{ st.steps[1].base }}</b><br>
        <span class="sub">Configured. Test it again any time from Settings.</span></span></div>
        {% else %}
        <div class="wz-note">No endpoint yet. This machine: <b>{{ hw.tier_label }}</b>. {{ hw.advice }}</div>
        {% endif %}
        <form method="post" action="{{ url_for('settings', next='/welcome?s=3') }}">
          <div class="wz-field"><label for="wz-base">Base URL</label>
            <input id="wz-base" type="text" name="llm_base_url" value="{{ s.llm_base_url }}" placeholder="{{ llm.base or 'https://api.example.com/v1  or  http://host:8000/v1' }}">
          </div>
          <div class="wz-field"><label for="wz-model">Model</label>
            <input id="wz-model" type="text" name="llm_model" value="{{ s.llm_model }}" placeholder="{{ llm.model or 'model name' }}">
          </div>
          <div class="wz-field"><label for="wz-key">API key</label>
            <span class="sub">Blank keeps the stored key. Most local servers do not need one.</span>
            <input id="wz-key" type="password" name="llm_api_key" value="" autocomplete="new-password" placeholder="{{ 'set - type to replace' if llm.key else 'not set' }}">
          </div>
          <div class="wz-actions">
            <button type="submit" class="btn">Save &amp; continue &#8594;</button>
            <button type="submit" class="wz-linkbtn" formaction="{{ url_for('welcome_test_llm') }}" formnovalidate>Save &amp; test</button>
            <button type="button" class="wz-linkbtn" data-go="3">Do this later</button>
            <button type="button" class="wz-linkbtn" data-go="1">&#8592; Back</button>
          </div>
          <div class="wz-note">Save &amp; test stores the endpoint above and checks it from this machine.</div>
        </form>
        <details>
          <summary class="sub" style="cursor:pointer;margin-top:14px">Choosing an LLM for this machine</summary>
          <div class="sub" style="margin-top:8px;line-height:1.6">
            <b>No GPU:</b> use a hosted OpenAI-compatible API (most providers work), or a small CPU
            model via ollama / llama.cpp.<br>
            <b>8-16GB GPU:</b> a quantized 7-14B instruct model served by vLLM or ollama fits well.<br>
            <b>24GB+ GPU:</b> the <span class="mono">gemma/</span> example serves a 26B MoE on a single
            24GB card.<br>
            Examples and copy-paste commands: <span class="mono">docs/getting-started.md</span>.
          </div>
        </details>
      </div>
    </section>

    <section class="wz-screen" data-i="3" aria-label="Search index">
      <div class="wz-card">
        <h2>Build the search index</h2>
        <p class="wz-lede">Semantic search over your archive - it powers search on the Messages page and
        the assistant. It runs on CPU in the background and is resumable; you can leave and it continues.</p>
        <div class="wz-ok"><span class="dot {{ 'ok' if st.index.messages else '' }}"></span><span id="wz-ix">
          {% if st.index.messages %}Indexed so far: <b>{{ st.index.messages }}</b> message(s), <b>{{ st.index.chunks }}</b> chunk(s).
          {% else %}Not started yet.{% endif %}</span></div>
        <div class="wz-actions">
          <form method="post" action="{{ url_for('index_run', next='/welcome?s=3') }}" class="inline">
            <button type="submit" class="btn">{{ 'Update index' if st.index.messages else 'Build index' }}</button>
          </form>
          <button type="button" class="btn small" data-go="4">Continue &#8594;</button>
          <button type="button" class="wz-linkbtn" data-go="2">&#8592; Back</button>
        </div>
        <div class="wz-note">You can also start it later from the dashboard - indexing status shows there too.</div>
      </div>
    </section>

    <section class="wz-screen" data-i="4" aria-label="Done">
      <div class="wz-card">
        <h2>You're set.</h2>
        <p class="wz-lede">Here is where things stand - everything below can be changed any time.</p>
        <div class="wz-recap"><span class="dot {{ 'ok' if st.steps[0].done }}"></span><span><b>Mailbox</b>
          <span class="sub">{{ ('Signed in as ' + st.steps[0].user) if st.steps[0].done else 'Not connected yet - the Accounts page can do this later.' }}</span></span></div>
        <div class="wz-recap"><span class="dot {{ 'ok' if st.steps[1].done }}"></span><span><b>LLM</b>
          <span class="sub">{{ (st.steps[1].model or 'Configured') + ' at ' + st.steps[1].base if st.steps[1].done else 'Not configured - classification and drafting stay off until you add one.' }}</span></span></div>
        <div class="wz-recap"><span class="dot {{ 'ok' if st.steps[2].done }}"></span><span><b>Search index</b>
          <span class="sub">{{ st.index.messages ~ ' message(s) indexed' if st.steps[2].done else 'Not built yet - start it from the dashboard.' }}</span></span></div>
        <div class="wz-note">Tip: tag messages as you triage. Your tags train the learning loop, and new
        rules can be learned straight from them.</div>
        <div class="wz-actions">
          <form method="post" action="{{ url_for('welcome') }}" class="inline">
            <input type="hidden" name="action" value="skip">
            <button type="submit" class="btn">Open the dashboard</button>
          </form>
          <a class="wz-skip" href="{{ url_for('messages') }}">Browse your mail</a>
          <form method="post" action="{{ url_for('welcome') }}" class="inline">
            <input type="hidden" name="action" value="dismiss">
            <button type="submit" class="wz-linkbtn">Hide setup help</button>
          </form>
        </div>
      </div>
    </section>
  </div>

  <div class="wz-foot">Mail Triage does not permanently delete mail. Optional Move to Trash follows your
  provider's retention policy. Reopen this setup any time from the More page.</div>
  <noscript><div class="wz-note">The guided view needs JavaScript. You can use the normal pages instead:
  <a href="{{ url_for('accounts') }}">Accounts</a>, <a href="{{ url_for('settings') }}">Settings</a>,
  <a href="{{ url_for('dashboard') }}">Dashboard</a>.</div></noscript>
</div>

<script>
(function(){
  var wz = document.getElementById('wz');
  if(!wz){ return; }
  var screens = [].slice.call(wz.querySelectorAll('.wz-screen'));
  var rail = [].slice.call(wz.querySelectorAll('.wz-step'));
  var timer = null;
  var cur = parseInt(wz.getAttribute('data-start'), 10);
  if(isNaN(cur)){ cur = 0; }
  var visited = {}; visited[cur] = true;
  function paint(focus){
    screens.forEach(function(el){ var i = +el.getAttribute('data-i');
      el.classList.toggle('on', i === cur); });
    rail.forEach(function(el){ var i = +el.getAttribute('data-i');
      el.classList.toggle('cur', i === cur);
      el.classList.toggle('reach', !!visited[i] || i < cur); });
    var on = screens[cur];
    if(focus && on){ var h = on.querySelector('h1,h2'); if(h){ h.setAttribute('tabindex','-1'); h.focus({preventScroll:true}); } }
    if(cur === 3){ startPoll(); } else { stopPoll(); }
  }
  function go(n){
    n = Math.max(0, Math.min(4, n));
    if(n === cur){ return; }
    wz.classList.toggle('back', n < cur);
    visited[n] = true; cur = n; paint(true);
  }
  function refresh(){
    fetch('/welcome/state.json', {headers:{'Accept':'application/json'}})
      .then(function(r){ return r.json(); })
      .then(function(j){
        if(!document.getElementById('wz')){ stopPoll(); return; }
        var live = document.getElementById('wz-ix');
        if(live && j && j.messages !== undefined){
          live.innerHTML = j.messages ?
            'Indexed so far: <b>' + j.messages + '</b> message(s), <b>' + j.chunks + '</b> chunk(s).'
            : 'Not started yet.';
        }
        rail.forEach(function(el){ var id = el.getAttribute('data-id');
          (j.steps || []).forEach(function(x){ if(x.id === id && x.done){ el.classList.add('done'); } }); });
      }).catch(function(){});
  }
  function startPoll(){ if(timer){ return; } refresh(); timer = setInterval(refresh, 4000); }
  function stopPoll(){ if(timer){ clearInterval(timer); timer = null; } }
  wz.addEventListener('click', function(e){
    var t = e.target.closest('[data-go]');
    if(!t){ return; }
    var n = parseInt(t.getAttribute('data-go'), 10);
    if(isNaN(n)){ return; }
    if(t.classList.contains('wz-step') && !(visited[n] || n < cur)){ return; }
    e.preventDefault(); go(n);
  });
  wz.classList.add('live');
  paint(false);
})();
</script>
"""




CLASSIFIERS_TMPL = """
<div class="page-head">
  <div>
    <h1 class="page-title">Classifiers</h1>
    <div class="page-desc">Deterministic heuristic models that run before the LLM. Trained from your labels (manual tags) or
    existing classified mail — a confident verdict is applied without any LLM call: faster, consistent, and immune to
    instructions hidden inside email content. The assistant can train, retrain, evaluate and retire these for you
    (“train a classifier for Receipts”, “evaluate classifier 2”).
    Part of the <a href="{{ url_for('learning_page') }}">Learning</a> system — see how fast-paths and learners fit together.</div>
  </div>
</div>
<div class="card flush" id="classifiers">
  {% if hx %}
  <div class="tablewrap"><table class="tbl mcards">
    <thead><tr><th>name</th><th>kind</th><th>category</th><th>samples</th><th>labels</th><th>matches on</th><th>updated</th><th class="r"></th></tr></thead>
    <tbody>
    {% for h in hx %}
    <tr>
      <td><b>{{ h.name }}</b>{% if not h.enabled %} <span class="badge">disabled</span>{% endif %}<div class="sub" style="font-size:.75rem">min conf {{ '%.2f' % (h.min_confidence or 0.8) }} · by {{ h.created_by }}</div></td>
      <td class="mono" style="font-size:.79rem">{{ h.kind }}</td>
      <td>{{ h.category }}</td>
      <td class="sub">{{ h.samples }}{% if h.excluded %} <span class="badge">{{ h.excluded }} removed</span>{% endif %}</td>
      <td class="sub">{{ h.label_source }}{% if h.weak_labels %} <span class="badge warn">weak</span>{% endif %}</td>
      <td class="sub" style="max-width:340px" title="{{ h.description }}">{{ h.description[:170] }}</td>
      <td class="sub">{{ h.when }}</td>
      <td class="r"><span class="rowacts" style="justify-content:flex-end">
        <a class="btn small" href="{{ url_for('classifier_dataset', hid=h.id) }}">Dataset</a>
        <form class="inline px-swf" method="post" action="{{ url_for('classifier_toggle', hid=h.id) }}">
          <label class="px-sw" title="{{ 'Disable' if h.enabled else 'Enable' }} {{ h.name }}"><input type="checkbox" {{ 'checked' if h.enabled }} onchange="this.form.requestSubmit()" aria-label="{{ 'Disable' if h.enabled else 'Enable' }} {{ h.name }}"><span class="px-tr"></span></label>
        </form>
        <details class="menu">
          <summary class="btn small" aria-haspopup="menu" aria-label="More actions">⋯</summary>
          <div class="menu-pop" role="menu">
            <form method="post" action="{{ url_for('classifier_retrain', hid=h.id) }}"><button class="menu-item" type="submit">Retrain</button></form>
            <form method="post" action="{{ url_for('classifier_delete', hid=h.id) }}">
              <button class="menu-item danger arm-del" type="submit" data-arm-label="Press again to delete classifier {{ h.name }}" aria-label="Delete classifier {{ h.name }}">Delete…</button></form>
          </div>
        </details>
      </span></td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% else %}
  <div class="empty">
    <h4>No classifiers yet</h4>
    <p>Tag some mail on the Messages page — or run “Classify all” so the LLM auto-tags older mail — then ask the
    assistant to train a classifier from those labels. Every classifier gets a dataset page for reviewing samples.</p>
    <a class="btn" href="{{ url_for('messages') }}">Go to messages</a>
  </div>
  {% endif %}
</div>
"""




def _plugin_config_fields(manifest, pid):
    """Simple config-form fields from manifest.config.properties (flat types)."""
    props = ((manifest.get("config") or {}).get("properties") or {})
    vals = plugins.get_config(pid)
    fields = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        t = spec.get("type") or "string"
        label = spec.get("description") or name
        val = vals.get(name, spec.get("default"))
        if t == "array":
            val = ", ".join(str(x) for x in val) if isinstance(val, list) \
                else ("" if val is None else str(val))
            fields.append({"name": name, "type": "array", "value": val, "label": label})
        elif t == "boolean":
            fields.append({"name": name, "type": "boolean", "value": bool(val), "label": label})
        else:
            fields.append({"name": name, "type": "text",
                           "value": "" if val is None else str(val), "label": label})
    return fields


PLUGIN_KIND_LABELS = {"tool": "tool", "classifier": "classifier", "matcher": "matcher",
                      "draft-provider": "draft provider", "retriever": "retriever",
                      "integration": "integration"}
PLUGIN_KIND_ICONS = {
    "tool": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
    "classifier": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M22 3H2l8 9.46V19l4 2v-8.54z"/></svg>',
    "matcher": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    "draft-provider": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
    "retriever": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 9 5-9 5-9-5z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/></svg>',
    "integration": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3v5M15 3v5M6 8h12v4a6 6 0 0 1-12 0z"/><path d="M12 18v3"/></svg>',
}
PLUGIN_PERM_INFO = {
    "mailbox.read": ("Read your mail",
                     "Search and read indexed messages: senders, subjects, snippets and bodies."),
    "llm.complete": ("Use the AI model", "Send prompts to the configured language model."),
    "llm.embed": ("Use embeddings", "Compute local text embeddings for similarity."),
    "net.http": ("Make web requests",
                 "Fetch URLs it declares \u2014 every call is audited and rate-capped."),
}


def _plugin_icon(manifest):
    for k in (manifest.get("kind") or []):
        if k in PLUGIN_KIND_ICONS:
            return PLUGIN_KIND_ICONS[k]
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
            'stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 21V13"/></svg>')


def _plugin_oneliner(manifest):
    d = (manifest.get("description") or "").strip()
    if not d:
        return ""
    cut = d.find(". ")
    s = d[:cut + 1] if cut != -1 else d
    return s[:170]


def _classifier_binding(pid):
    """The plugin_classifiers entry for one plugin: the functional dict form
    {"plugin", "heuristic_id"} or a legacy bare-id string; None when absent."""
    for x in (store.get_setting("plugin_classifiers", []) or []):
        if isinstance(x, dict) and str(x.get("plugin") or "") == pid:
            return x
        if isinstance(x, str) and x == pid:
            return x
    return None


def _plugin_detail_ctx(pid):
    """Everything the /plugins/<pid> detail page renders, or None."""
    row = plugins.get(pid)
    if not row:
        return None
    m = row["manifest"]
    kinds = m.get("kind") or []
    try:
        perms = engine.agent_permissions()
    except Exception:
        perms = {}
    tools = [{"name": t.get("name") or "", "description": t.get("description") or ""}
             for t in (m.get("tools") or [])]
    roles = []
    if "classifier" in kinds:
        roles.append(("Role", "Classifier &mdash; used by the classification fast-path when the "
                              "pipeline opt-in below is ticked"))
    if "matcher" in kinds:
        roles.append(("Role", "Rule condition &mdash; usable as the <span class=\"mono\">plugin</span> "
                              "operator in rules and flows"))
    if "draft-provider" in kinds:
        roles.append(("Role", "Draft provider &mdash; flow draft steps with mode "
                              "&ldquo;Draft via plugin&rdquo;"))
    if "retriever" in kinds:
        roles.append(("Role", "Search re-ranker &mdash; reorders the assistant&rsquo;s semantic "
                              "search when opted in"))
    if "integration" in kinds:
        roles.append(("Role", "Integration &mdash; receives <span class=\"mono\">mail.filed</span> "
                              "and <span class=\"mono\">mail.classified</span> events"))
    if "tool" in kinds:
        roles.append(("Role", "Assistant tools &mdash; callable in chat under the assistant "
                              "permission below"))
    sched = m.get("schedule") or {}
    if isinstance(sched, dict) and sched.get("every_minutes"):
        every = int(sched.get("every_minutes") or 0)
        when = ("every %d minutes" % every) if every < 1440 else (
            "every day" if every == 1440 else "every %d days" % (every // 1440))
        roles.append(("Schedule", "Runs automatically <b>%s</b> while enabled" % when))
    lim = m.get("limits") or {}
    limits_text = "memory %sMB &middot; timeout %sms" % (lim.get("memory_mb", "?"),
                                                         lim.get("timeout_ms", "?"))
    if "net.http" in (m.get("permissions") or []):
        net = m.get("net") or {}
        hosts = net.get("hosts") or []
        limits_text += " &middot; net: %s" % (", ".join(hosts) if hosts else "hosts from settings")
    events = []
    with store.db() as conn:
        er = conn.execute("SELECT ts, level, message FROM events WHERE message LIKE ? "
                          "ORDER BY id DESC LIMIT 6", ("%" + pid + "%",)).fetchall()
    for e in er:
        events.append({"when": time.strftime("%m-%d %H:%M", time.localtime(e["ts"] or 0)),
                       "level": e["level"] or "", "message": e["message"] or ""})
    _binding = _classifier_binding(pid)
    return {
        "id": row["id"], "name": m.get("name") or row["id"], "version": row["version"],
        "description": m.get("description") or "", "tagline": _plugin_oneliner(m),
        "root": row["root"], "enabled": bool(row["enabled"]), "icon": _plugin_icon(m),
        "kinds": kinds, "roles": roles,
        "tools": tools, "has_tools": bool(tools),
        "permissions": m.get("permissions") or [], "grants": row["grants"],
        "perms_detail": [{"name": g, "title": PLUGIN_PERM_INFO.get(g, (g, ""))[0],
                          "why": PLUGIN_PERM_INFO.get(g, (g, ""))[1],
                          "granted": g in row["grants"]}
                         for g in (m.get("permissions") or [])],
        "agent_level": perms.get("plugin:" + pid, "auto"),
        "optin_matcher": "matcher" in kinds, "optin_retriever": "retriever" in kinds,
        "optin_classifier": "classifier" in kinds,
        "in_matchers": pid in (store.get_setting("plugin_matchers", []) or []),
        "in_retrievers": pid in (store.get_setting("plugin_retrievers", []) or []),
        "in_classifiers": _binding is not None,
        "bound_heuristic_id": _binding.get("heuristic_id", 0) if isinstance(_binding, dict) else 0,
        "classifier_heuristics": [{"id": h["id"],
                                   "name": h.get("name") or ("heuristic %s" % h["id"]),
                                   "enabled": bool(h.get("enabled"))}
                                  for h in store.list_heuristics()],
        "config_fields": _plugin_config_fields(m, pid),
        "entry": m.get("entrypoint") or "", "limits_text": limits_text,
        "last_error": row["last_error"] or "", "events": events,
        "ui_mode": plugins.ui_mode(row) or "",
        "ui_pages": plugins.ui_pages(row),
        "ui_trusted": plugins.ui_mode(row) == "trusted",
        "ui_approved": plugin_ui.is_approved(row),
        "ui_approval": plugin_ui.approval(pid),
    }


@app.route("/plugins")
def plugins_page():
    rows = []
    try:
        engine.agent_permissions()
    except Exception:
        pass
    for r in plugins.list_rows():
        m = r["manifest"]
        kinds = m.get("kind") or []
        rows.append({
            "id": r["id"], "name": m.get("name") or r["id"], "version": r["version"],
            "enabled": bool(r["enabled"]),
            "oneliner": _plugin_oneliner(m),
            "icon": _plugin_icon(m),
            "kind_label": PLUGIN_KIND_LABELS.get(kinds[0], kinds[0]) if kinds else "extension",
            "needs_regrant": "re-grant" in (r["last_error"] or ""),
            "problem": bool(r["last_error"]) and "re-grant" not in (r["last_error"] or ""),
        })
    plugins.ensure_user_root()
    running = [p for p in rows if p["enabled"]]
    stopped = [p for p in rows if not p["enabled"]]
    return render(_render_src(PLUGINS_TMPL, px=rows, running=running, stopped=stopped,
                              n_on=len(running), n_all=len(rows), sdk=plugins.HOST_SDK_VERSION,
                              proots={"user": plugins.user_root(),
                                      "builtin": plugins.builtin_root()}))


@app.route("/plugins/<pid>")
def plugin_detail(pid):
    ctx = _plugin_detail_ctx(pid)
    if not ctx:
        flash("No plugin with id %r." % pid, "warn")
        return redirect(url_for("plugins_page"))
    return render(_render_src(PLUGIN_DETAIL_TMPL, p=ctx, sdk=plugins.HOST_SDK_VERSION))


@app.route("/welcome", methods=["GET", "POST"])
def welcome():
    if request.method == "POST":
        act = (request.form.get("action") or "").strip()
        if act == "skip":
            store.set_setting("welcome_skipped", 1)
            flash("Setup deferred - continue it from the dashboard or More page.", "ok")
            return redirect(url_for("dashboard"))
        if act == "dismiss":
            store.set_setting("welcome_done", 1)
            flash("Setup help hidden - reopen it from the More page any time.", "ok")
            return redirect(url_for("dashboard"))
        if act == "reset":
            store.set_setting("welcome_done", 0)
            store.set_setting("welcome_skipped", 0)
            return redirect(url_for("welcome"))
    st = setup_state()
    start = 0 if st["done"] == 0 else next((i + 1 for i, x in enumerate(st["steps"]) if not x["done"]), 4)
    try:
        start = max(0, min(4, int(request.args.get("s"))))
    except Exception:
        pass
    return render(_render_src(WELCOME_TMPL, st=st, hw=engine.detect_hardware(),
                              s=store.all_settings(), llm=engine.llm_config(),
                              start=start), setup=True)


@app.route("/welcome/test-llm", methods=["POST"])
def welcome_test_llm():
    """Wizard: save the endpoint above AND test it in one step."""
    _save_llm_settings()
    started = time.time()
    client = engine.LLMClient()
    base, key, model = client.base, client.key, client.model
    if not base:
        flash("No LLM endpoint configured yet - add a base URL first.", "err")
        return redirect(url_for("welcome") + "?s=2")
    try:
        out = client._chat_once(base, key, model,
                                "You are a connectivity test. Reply with the single word ok.",
                                [{"role": "user", "content": "Reply with the single word ok."}],
                                json_mode=False)
        flash("LLM OK in %.1fs - %s @ %s - replied: %s"
              % (time.time() - started, model, base, (out or "").strip()[:60]), "ok")
    except Exception as exc:
        flash("LLM test failed: %s" % repr(exc)[:200], "err")
    return redirect(url_for("welcome") + "?s=2")


@app.route("/welcome/state.json")
def welcome_state():
    st = setup_state()
    ix_ = {k: v for k, v in (st.get("index") or {}).items()}
    return jsonify({"done": st["done"], "total": st["total"],
                    "steps": [{"id": x["id"], "done": bool(x["done"])} for x in st["steps"]],
                    "messages": int(ix_.get("messages") or 0),
                    "chunks": int(ix_.get("chunks") or 0),
                    "running": False})


@app.route("/plugins/rescan", methods=["POST"])
def plugins_rescan():
    rep = plugins.scan()
    flash("Plugins rescanned: %d found, %d error(s)."
          % (len(rep["found"]), len(rep["errors"])),
          "warn" if rep["errors"] else "ok")
    return redirect(url_for("plugins_page"))


def _apply_pipeline_optins(pid, kinds):
    """The Pipeline use checkboxes. They ride in the Access form (action=save);
    before 2026-10-05 this branch never ran on that form, so the classifier
    opt-in silently did nothing from the UI. Idempotent: rebuilds each opt-in
    list from the form. The Settings form (action=config) does not carry these
    fields and must not touch the opt-ins."""
    if "matcher" in kinds:
        lst = [x for x in (store.get_setting("plugin_matchers", []) or []) if x != pid]
        if request.form.get("opt_in_matcher"):
            lst.append(pid)
        store.set_setting("plugin_matchers", lst)
    if "retriever" in kinds:
        lst = [x for x in (store.get_setting("plugin_retrievers", []) or []) if x != pid]
        if request.form.get("opt_in_retriever"):
            lst.append(pid)
        store.set_setting("plugin_retrievers", lst)
    if "classifier" in kinds:
        lst = [x for x in (store.get_setting("plugin_classifiers", []) or [])
               if not (isinstance(x, dict) and str(x.get("plugin") or "") == pid)
               and x != pid]
        if request.form.get("opt_in_classifier"):
            try:
                hid = int(request.form.get("classifier_heuristic") or 0)
            except ValueError:
                hid = 0
            lst.append({"plugin": pid, "heuristic_id": hid})
            if not hid:
                flash("Classification fast-path needs a bound heuristic "
                      "(confidence gate) to take effect - pick one and save again.",
                      "warn")
        store.set_setting("plugin_classifiers", lst)


@app.route("/plugins/<pid>", methods=["POST"])
def plugins_update(pid):
    action = (request.form.get("action") or "").strip()
    nxt = (request.form.get("next") or "").strip()
    if action == "enable":
        res = plugins.set_enabled(pid, True)
    elif action == "disable":
        res = plugins.set_enabled(pid, False)
    elif action == "save":
        row = plugins.get(pid)
        if row:
            declared = row["manifest"].get("permissions") or []
            grants = [g for g in declared if request.form.get("grant_" + g)]
            res = plugins.set_grants(pid, grants)
            lvl = (request.form.get("agent_level") or "").strip().lower()
            if lvl in ("off", "ask", "auto"):
                store.set_setting("perm_plugin:" + pid, lvl)
            _apply_pipeline_optins(pid, row["manifest"].get("kind") or [])
        else:
            res = {"ok": False, "error": "unknown plugin '%s'" % pid}
    elif action == "config":
        row = plugins.get(pid)
        if row:
            props = ((row["manifest"].get("config") or {}).get("properties") or {})
            values = {}
            for k2, spec in props.items():
                spec = spec if isinstance(spec, dict) else {}
                t2 = spec.get("type") or "string"
                if t2 == "boolean":
                    values[k2] = request.form.get("cfg_" + k2) not in (None, "", "0")
                elif t2 == "array":
                    raw = request.form.get("cfg_" + k2) or ""
                    values[k2] = [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]
                elif t2 in ("integer", "number"):
                    try:
                        values[k2] = int(request.form.get("cfg_" + k2) or 0)
                        if t2 == "number":
                            values[k2] = float(request.form.get("cfg_" + k2) or 0)
                    except ValueError:
                        values[k2] = 0
                else:
                    values[k2] = (request.form.get("cfg_" + k2) or "").strip()
            res = plugins.set_config(pid, values)
        else:
            res = {"ok": False, "error": "unknown plugin '%s'" % pid}
    else:
        res = {"ok": False, "error": "unknown action"}
    if res.get("ok") is False:
        flash(res.get("error") or "Plugin action failed.", "warn")
    else:
        flash("Saved.", "ok")
    if nxt == "detail":
        return redirect(url_for("plugin_detail", pid=pid))
    return redirect(url_for("plugins_page"))


# ---------------------------------------------------------------- plugin pages
#
# Two tiers (docs/plugin-pages.md):
#   * composed (default, sandboxed): the plugin's QuickJS bundle exports
#     synchronous uiOpen/uiDispatch/uiClose and returns a component tree. The
#     host validates it with plugin_ui (strict whitelist + bounds), owns the
#     bounded state / revision / event binding and renders it. No plugin browser
#     JS ever runs.
#   * trusted (browser bundle, opaque-origin iframe): only after an explicit
#     per-content user approval. Residual self-navigation egress is disclosed,
#     not denied; the host disposes the bridge on unexpected frame navigation.

_UI_BASE_CSS = (
    ":root{--fg:#000;--bg:#fff;--dim:#666;--line:#e5e5e5;--line2:#d4d4d4;--card:#fff;"
    "--card2:#f5f5f5;--acc:#0070f3;--ok:#067a46;--warn:#b25e09;--err:#d1242f;"
    "--hover:#f7f7f7;--focus:rgba(0,112,243,.35);"
    "--mono:'Geist Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}"
    "*{box-sizing:border-box}"
    "html,body{margin:0;height:100%}"
    "body{background:var(--bg);color:var(--fg);"
    "font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    ":focus-visible{outline:2px solid var(--acc);outline-offset:2px}"
    "#mt-root{display:flex;flex-direction:column;height:100%;min-height:0}"
    ".mt-sr{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}"
    ".mt-search{display:flex;gap:8px;padding:10px;border-bottom:1px solid var(--line);background:var(--card);flex:0 0 auto}"
    ".mt-search-input{flex:1 1 auto;min-width:0;border:1px solid var(--line);background:#fff;color:var(--fg);padding:8px 10px;font:inherit}"
    ".mt-btn{border:1px solid var(--line);background:#fff;color:var(--fg);font:inherit;padding:8px 12px;cursor:pointer}"
    ".mt-split{display:flex;flex:1 1 auto;height:100%;min-height:0;background:var(--card)}"
    ".mt-split>*{min-width:0;min-height:0;overflow:auto}"
    ".mt-split .mt-pane-list{flex:0 0 300px;border-right:1px solid var(--line)}"
    ".mt-split .mt-pane-reader{flex:1 1 auto}"
    "html[data-mt-layout=mobile] .mt-split{display:block}"
    "html[data-mt-layout=mobile] .mt-split .mt-pane-list,html[data-mt-layout=mobile] .mt-split .mt-pane-reader{display:none;border:0}"
    "html[data-mt-layout=mobile] .mt-split[data-view=list] .mt-pane-list{display:block}"
    "html[data-mt-layout=mobile] .mt-split[data-view=reader] .mt-pane-reader{display:block}"
    ".mt-msg{padding:9px 12px;border-bottom:1px solid var(--line);cursor:pointer}"
    ".mt-msg.on{background:var(--card2);box-shadow:inset 3px 0 0 var(--acc)}"
    ".mt-msg-top{display:flex;justify-content:space-between;gap:8px}"
    ".mt-msg-from{font-weight:600;font-size:.88rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".mt-msg-when{color:var(--dim);font-size:.75rem;white-space:nowrap;flex:0 0 auto}"
    ".mt-msg-subject{font-size:.9rem;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".mt-msg-snippet{color:var(--dim);font-size:.8rem;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".mt-reader{padding:16px 18px}"
    ".mt-reader-subject{font-size:1.1rem;margin:0 0 8px;font-weight:700}"
    ".mt-reader-meta{color:var(--dim);font-size:.83rem;display:grid;gap:2px;margin-bottom:12px}"
    ".mt-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}"
    ".mt-chip{border:1px solid var(--line);background:var(--card2);padding:1px 7px;font-size:.75rem}"
    ".mt-reader-body{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--card2);border:1px solid var(--line);padding:12px;margin:0;font:inherit;font-size:.9rem}"
    ".mt-hint{color:var(--dim);font-size:.76rem;margin-top:10px}"
    ".mt-state{padding:26px 18px;color:var(--dim);display:flex;flex-direction:column;gap:10px;align-items:flex-start}"
    ".mt-empty{padding:20px;color:var(--dim)}"
    ".mt-back{display:none;margin:10px}"
    "html[data-mt-layout=mobile] .mt-back{display:inline-flex}"
)

_UI_COMPONENT_CSS = (
    ".mtc-root{display:flex;flex-direction:column;gap:10px}"
    ".mtc-tree{display:block}"
    ".mtc-stack{display:flex;flex-direction:column;gap:10px}"
    ".mtc-stack.mtc-row{flex-direction:row;flex-wrap:wrap;align-items:center}"
    ".mtc-gap-none{gap:0}.mtc-gap-md{gap:16px}"
    ".mtc-grid{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:10px}"
    ".mtc-grid.mtc-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}"
    ".mtc-grid.mtc-cols-3{grid-template-columns:repeat(3,minmax(0,1fr))}"
    "@media(max-width:767px){.mtc-grid.mtc-cols-2,.mtc-grid.mtc-cols-3{grid-template-columns:1fr}}"
    ".mtc-split{display:grid;grid-template-columns:minmax(260px,320px) minmax(0,1fr);"
    "border:1px solid var(--line);background:var(--card);min-height:420px}"
    ".mtc-split>*{min-width:0;overflow:auto}"
    ".mtc-pane-list{border-right:1px solid var(--line)}"
    ".mtc-btn.mtc-mobile-back{display:none}"
    "@media(max-width:767px){.mtc-split{display:block}"
    ".mtc-split .mtc-pane-list,.mtc-split .mtc-pane-reader{display:none;border:0}"
    ".mtc-split.mtc-view-list .mtc-pane-list{display:block}"
    ".mtc-split.mtc-view-reader .mtc-pane-reader{display:block}"
    ".mtc-btn.mtc-mobile-back{display:inline-flex}}"
    ".mtc-text.mtc-title{font-size:1.1rem;font-weight:700;letter-spacing:-.02em}"
    ".mtc-text.mtc-caption{color:var(--dim);font-size:.82rem}"
    ".mtc-text.mtc-mono{font-family:var(--mono);font-size:.84rem}"
    ".mtc-dim{color:var(--dim)}"
    ".mtc-badge{border:1px solid var(--line);background:var(--card2);padding:1px 7px;font-size:.75rem;display:inline-block}"
    ".mtc-badge.mtc-warn{border-color:var(--warn);color:var(--warn)}"
    ".mtc-badge.mtc-ok{border-color:var(--ok);color:var(--ok)}"
    ".mtc-badge.mtc-err{border-color:var(--err);color:var(--err)}"
    ".mtc-sep{border:0;border-top:1px solid var(--line);margin:6px 0}"
    ".mtc-btn{display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 13px;"
    "border:1px solid var(--line);background:#fff;color:var(--fg);font:inherit;font-size:.88rem;cursor:pointer}"
    ".mtc-btn:hover{border-color:#000;background:var(--bg)}"
    ".mtc-btn.mtc-primary{background:#000;border-color:#000;color:#fff;font-weight:600}"
    ".mtc-btn:disabled{opacity:.45;cursor:not-allowed}"
    ".mtc-menu{display:flex;flex-direction:column;border:1px solid var(--line);background:var(--card);max-width:280px}"
    ".mtc-menu-item{text-align:left;border:0;border-bottom:1px solid var(--line);background:none;"
    "padding:9px 12px;font:inherit;cursor:pointer}"
    ".mtc-menu-item:last-child{border-bottom:0}.mtc-menu-item:hover{background:var(--hover)}"
    ".mtc-dialog{border:1px solid var(--line2);background:var(--card);padding:14px 16px}"
    ".mtc-dialog-title{font-weight:600;margin-bottom:8px}"
    ".mtc-list{border:1px solid var(--line);background:var(--card)}"
    ".mtc-list-item{padding:10px 12px;border-bottom:1px solid var(--line)}"
    ".mtc-list-item.on{background:var(--card2);box-shadow:inset 3px 0 0 var(--acc)}"
    ".mtc-field{display:flex;flex-direction:column;gap:4px;margin:0}"
    ".mtc-field .mtc-label{font-size:.8rem;color:var(--dim)}"
    ".mtc-field input{border:1px solid var(--line);background:#fff;color:var(--fg);padding:8px 10px;font:inherit;width:100%}"
    ".mtc-search input{font-size:16px}"
    ".mtc-sr{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}"
    ".mtc-state{padding:24px 18px;color:var(--dim);display:flex;flex-direction:column;gap:10px;align-items:flex-start}"
    ".mtc-state.mtc-error{color:var(--err)}"
    ".mtc-msgs{display:flex;flex-direction:column}"
    ".mtc-msg{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid var(--line);"
    "background:none;padding:9px 12px;font:inherit;cursor:pointer}"
    ".mtc-msg:hover{background:var(--hover)}.mtc-msg.on{background:var(--card2);box-shadow:inset 3px 0 0 var(--acc)}"
    ".mtc-msg-top{display:flex;justify-content:space-between;gap:8px}"
    ".mtc-msg-sub{display:block;font-size:.9rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".mtc-msg-snip{display:block;color:var(--dim);font-size:.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".mtc-reader{padding:16px 18px}"
    ".mtc-reader-sub{font-size:1.1rem;margin:0 0 8px;font-weight:700}"
    ".mtc-chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}"
    ".mtc-reader-body{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--card2);"
    "border:1px solid var(--line);padding:12px;margin:12px 0 0;font:inherit;font-size:.9rem}"
    ".mtc-hint{color:var(--dim);font-size:.76rem;margin-top:8px}"
    ".mtc-tabbar{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:10px}"
    ".mtc-tab{border:0;border-bottom:2px solid transparent;background:none;padding:8px 12px;font:inherit;cursor:pointer}"
    ".mtc-tab.on{border-bottom-color:var(--acc);font-weight:600}"
    ".ext-bad{margin:8px 0 0;color:var(--err);font-size:.85rem}"
    ".ext-warn{margin:0 0 10px;border:1px solid var(--warn);background:var(--tint-warn);color:#5c3300;"
    "padding:9px 12px;font-size:.84rem}"
    ".mt-ext-frame{width:100%;height:calc(100vh - 290px);min-height:380px;border:1px solid var(--line);"
    "background:var(--card);display:block}"
)

_UI_JSON_CAP = 64 * 1024
_SDK_UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sdk", "ui.js")
_sdk_ui_cache = {"src": None}


def _sdk_ui_src():
    if _sdk_ui_cache["src"] is None:
        try:
            with open(_SDK_UI_PATH, "r", encoding="utf-8") as fh:
                _sdk_ui_cache["src"] = fh.read()
        except OSError:
            _sdk_ui_cache["src"] = ""
    return _sdk_ui_cache["src"]


def _extension_srcdoc(nonce, page_title, sdk_src, plugin_src):
    """Trusted frame document: bootstrap + SDK + bundle wrapper.

    The plugin bundle is embedded only as the BODY of a function (a syntactic
    wrapper, not a security barrier); it does not execute at parse time and no
    eval / unsafe-eval is used. The actual barrier is the document-bound
    MessagePort: the host transfers it only after the hello/ack handshake, and
    operational messages travel only on that port, so a synchronous
    self-navigation at first execution cannot retain or re-establish the bridge
    (AR2-3). A remote page that navigated the frame has no port."""
    csp = ("default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; "
           "img-src data:; font-src 'none'; connect-src 'none'; media-src 'none'; "
           "object-src 'none'; frame-src 'none'; worker-src 'none'; "
           "form-action 'none'; base-uri 'none'") % (nonce, nonce)
    token = json.dumps(nonce)
    bootstrap = (
        "(function(){\"use strict\";var TOKEN=%s;var started=false;"
        "function start(port,boot){if(started)return;started=true;"
        "try{window.__MT_BOOT=boot||{};}catch(e){}"
        "if(window.__mt_sdk_connect)window.__mt_sdk_connect(port,(boot&&boot.sid)||'',boot||{});"
        "try{if(window.__mt_bundle)window.__mt_bundle();}catch(e){}"
        "try{if(window.MTUI&&window.MTUI.start)window.MTUI.start();}catch(e){}}"
        "window.addEventListener('message',function(e){"
        "if(e.source!==window.parent)return;var d=e.data;"
        "if(!d||d.__mt_ack!==1||d.token!==TOKEN)return;"
        "var port=e.ports&&e.ports[0];if(!port)return;start(port,d.boot);});"
        "var tries=0;function hello(){if(started)return;"
        "try{window.parent.postMessage({__mt_hello:1,token:TOKEN},'*');}catch(e){}"
        "if(tries++<40)setTimeout(hello,150);}hello();})();"
    ) % token
    out = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
           "<meta http-equiv=\"Content-Security-Policy\" content=\"%s\">" % csp,
           "<meta name=\"referrer\" content=\"no-referrer\">",
           "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
           "<title>%s</title>" % html_escape(page_title),
           "<style nonce=\"%s\">%s</style>" % (nonce, _UI_BASE_CSS),
           "</head><body><div id=\"mt-root\"></div>",
           "<script nonce=\"%s\">%s</script>" % (nonce, sdk_src),
           "<script nonce=\"%s\">%s</script>" % (nonce, bootstrap),
           "<script nonce=\"%s\">window.__mt_bundle=function(){\n%s\n};</script>" % (nonce, plugin_src),
           "</body></html>"]
    return "".join(out)



def _same_origin_ok():
    if (request.headers.get("Sec-Fetch-Site") or "") == "same-origin":
        return True
    base = request.host_url.rstrip("/")
    origin = (request.headers.get("Origin") or "").strip()
    if origin:
        return origin == base
    ref = (request.headers.get("Referer") or "").strip()
    return bool(ref) and (ref == base or ref.startswith(base + "/"))


def _read_json_capped(limit=_UI_JSON_CAP):
    """Bounded request reader: reject oversize/unknown-length bodies (AR1-2)."""
    cl = request.content_length
    if cl is not None and cl > limit:
        return None, "too_large"
    try:
        data = request.stream.read(limit + 1)
    except Exception:
        return None, "bad_body"
    if len(data) > limit:
        return None, "too_large"
    if not data:
        return {}, None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, "bad_json"
    # A valid JSON body that is not an object (array/null/string/number/bool) is
    # a client error, not a server crash: reject it as a typed bad request.
    if not isinstance(obj, dict):
        return None, "bad_json"
    return obj, None


def _ui_envelope_status(err_code):
    if err_code in ("forbidden", "disabled", "denied", "stale", "closed"):
        return 403
    if err_code == "not_found":
        return 404
    if err_code in ("invalid_args", "invalid_tree", "invalid_state", "too_large", "bad_json"):
        return 400
    if err_code == "busy":
        return 429
    return 200


def _ui_session_valid_now(pid, page, sid, mode):
    """Re-check authority AFTER a controller/tool op, before surfacing results."""
    row = plugins.get(pid)
    sess = plugin_ui.get_session(sid, pid, page)
    if not sess or not row or not row.get("enabled") or sess.get("mode") != mode:
        return False
    if mode == "trusted":
        rec = plugin_ui.approval(pid)
        if not plugin_ui.is_approved(row) or \
                sess.get("generation") != int(rec.get("approved_at") or 0):
            return False
    return True


def _composed_view(result):
    """Controller-declared mobile pane hint (host applies it to the split)."""
    return "reader" if (result or {}).get("view") == "reader" else "list"


def _ext_url(pid, page, spec):
    base = "/extensions/%s/%s" % (pid, page)
    if not isinstance(spec, dict):
        return base
    parts = []
    q = spec.get("q")
    m = spec.get("message")
    if isinstance(q, str) and q:
        parts.append("q=" + quote(q[:200]))
    if isinstance(m, (str, int)) and str(m):
        parts.append("message=" + quote(str(m)[:20]))
    return base + ("?" + "&".join(parts) if parts else "")


@app.after_request
def _ext_frame_guard(resp):
    """Host-level clickjacking guard for extension pages (AR1-6)."""
    try:
        if request.path.startswith("/extensions/"):
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    except Exception:
        pass
    return resp


@app.route("/extensions/<pid>/<page>")
def extension_page(pid, page):
    row = plugins.get(pid)
    if not row or not row.get("enabled") or not plugins.ui_page_declared(row, page):
        flash("No such extension page.", "warn")
        return redirect(url_for("plugins_page"))
    mode = plugins.ui_mode(row)
    page_info = next((p for p in plugins.ui_pages(row) if p["id"] == page),
                     {"title": page, "description": ""})
    name = row["manifest"].get("name") or pid
    initial = {"q": (request.args.get("q") or "")[:200],
               "message": (request.args.get("message") or "")[:20]}
    if mode == "composed":
        sid = plugin_ui.open_session(pid, page, "composed")
        with plugin_ui.slot(pid) as _slot:
            if not _slot.got:
                plugin_ui.dispose(sid)
                flash("This plugin page is busy; try again.", "warn")
                return redirect(url_for("plugin_detail", pid=pid))
            opened = plugin_rt.runtime.ui_open(pid, initial)
        if "error" in (opened or {}):
            plugin_ui.dispose(sid)
            flash("This plugin page failed to open: %s"
                  % ((opened.get("error") or {}).get("message") or "error"), "warn")
            return redirect(url_for("plugin_detail", pid=pid))
        try:
            result = opened.get("result") or {}
            if not isinstance(result, dict):
                raise plugin_ui.UiError("invalid_tree", "controller result must be an object")
            tree = plugin_ui.validate_tree(result.get("tree"))
            state = plugin_ui.validate_state(result.get("state") or initial)
        except plugin_ui.UiError as exc:
            plugin_ui.dispose(sid)
            flash("This plugin returned an invalid view (%s)." % exc, "warn")
            return redirect(url_for("plugin_detail", pid=pid))
        except Exception as exc:  # noqa: BLE001 - untrusted output must not 500
            app.logger.warning("composed ui open rejected: %r", exc)
            plugin_ui.dispose(sid)
            flash("This plugin returned an invalid view.", "warn")
            return redirect(url_for("plugin_detail", pid=pid))
        plugin_ui.set_tree(sid, tree, plugin_ui.tree_events(tree), state)
        cfg = {"mode": "composed", "sid": sid, "pid": pid, "page": page,
               "base": "/extensions/%s/%s" % (pid, page), "revision": 0,
               "view": _composed_view(result)}
        cfg_json = json.dumps(cfg).replace("</", "<\\/")
        return render(_render_src(EXTENSION_TMPL, mode="composed", p=page_info, pid=pid,
                                  page=page, name=name, tree_html=plugin_ui.render_tree(tree),
                                  cfg_json=cfg_json, srcdoc="", approved=False, comp_css=_UI_COMPONENT_CSS))
    if mode != "trusted":
        flash("This plugin has no runnable page.", "warn")
        return redirect(url_for("plugin_detail", pid=pid))
    if not plugin_ui.is_approved(row):
        flash("This plugin's browser view needs explicit approval on its page.", "warn")
        return redirect(url_for("plugin_detail", pid=pid))
    plugin_src = plugins.ui_entry_source(row)
    sdk_src = _sdk_ui_src()
    if plugin_src is None or not sdk_src:
        flash("This plugin's page bundle could not be loaded safely.", "warn")
        return redirect(url_for("plugin_detail", pid=pid))
    generation = int(plugin_ui.approval(pid).get("approved_at") or 0)
    sid = plugin_ui.open_session(pid, page, "trusted", generation=generation)
    nonce = secrets.token_urlsafe(18)
    srcdoc = _extension_srcdoc(nonce, name + " - " + page_info["title"],
                               sdk_src, plugin_src)
    cfg = {"mode": "trusted", "sid": sid, "pid": pid, "page": page,
           "base": "/extensions/%s/%s" % (pid, page), "bootToken": nonce,
           "ops": plugins.ui_operations(row), "initial": initial,
           "generation": generation}
    cfg_json = json.dumps(cfg).replace("</", "<\\/")
    return render(_render_src(EXTENSION_TMPL, mode="trusted", p=page_info, pid=pid,
                              page=page, name=name, tree_html="", cfg_json=cfg_json,
                              srcdoc=srcdoc, approved=True, comp_css=_UI_COMPONENT_CSS))


@app.route("/extensions/<pid>/<page>/dispatch", methods=["POST"])
def extension_dispatch(pid, page):
    if not _same_origin_ok():
        return jsonify({"ok": False, "error": {"code": "forbidden",
                                               "message": "cross-site request refused"}}), 403
    sid = (request.headers.get("X-MT-Session") or "").strip()
    payload, err = _read_json_capped()
    if err:
        return jsonify({"ok": False, "error": {"code": err, "message": "bad request"}}), \
            _ui_envelope_status(err)
    sess = plugin_ui.get_session(sid, pid, page)
    if not sess or sess.get("mode") != "composed":
        return jsonify({"ok": False, "error": {"code": "forbidden",
                                               "message": "view session is not valid"}}), 403
    if not plugin_ui.rate_ok(pid, "event"):
        return jsonify({"ok": False, "error": {"code": "busy",
                                               "message": "too many events"}}), 429
    ev = payload.get("event")
    if not isinstance(ev, str):
        return jsonify({"ok": False, "error": {"code": "invalid_args",
                                               "message": "event must be a string"}}), 400
    info = (sess.get("events") or {}).get(ev)
    if not info:
        return jsonify({"ok": False, "error": {"code": "forbidden",
                                               "message": "event is not part of the rendered view"}}), 403
    value = payload.get("value")
    if info.get("values") is not None:
        if str(value) not in info["values"]:
            return jsonify({"ok": False, "error": {"code": "forbidden",
                                                   "message": "unknown action"}}), 403
    elif value is not None:
        if not isinstance(value, str) or len(value) > plugin_ui.MAX_TEXT:
            return jsonify({"ok": False, "error": {"code": "invalid_args",
                                                   "message": "value too large"}}), 400
    event_obj = {"kind": ev}
    if value is not None:
        event_obj["value"] = value
    with plugin_ui.session_lock(sid):
        sess = plugin_ui.get_session(sid, pid, page)
        if not sess:
            return jsonify({"ok": False, "error": {"code": "forbidden",
                                                   "message": "view session is not valid"}}), 403
        try:
            rev = int(payload.get("revision"))
        except (TypeError, ValueError):
            rev = -1
        if rev != sess.get("revision"):
            return jsonify({"ok": False, "error": {"code": "stale",
                                                   "message": "view changed; reload"}}), 409
        with plugin_ui.slot(pid) as _slot:
            if not _slot.got:
                return jsonify({"ok": False, "error": {"code": "busy",
                                                       "message": "too many concurrent calls"}}), 429
            out = plugin_rt.runtime.ui_dispatch(pid, sess.get("state") or {}, event_obj)
        if "error" in (out or {}):
            e = out["error"]
            return jsonify({"ok": False, "error": {"code": e.get("code") or "internal",
                                                   "message": e.get("message") or "failed"}}), \
                _ui_envelope_status(e.get("code"))
        result = out.get("result") or {}
        if not isinstance(result, dict):
            return jsonify({"ok": False, "error": {"code": "invalid_tree",
                                                   "message": "controller result must be an object"}}), 400
        if not _ui_session_valid_now(pid, page, sid, "composed"):
            return jsonify({"ok": False, "error": {"code": "closed",
                                                   "message": "view was closed"}}), 403
        try:
            tree = plugin_ui.validate_tree(result.get("tree"))
            state = plugin_ui.validate_state(result.get("state") or {})
        except plugin_ui.UiError as exc:
            return jsonify({"ok": False, "error": {"code": exc.code,
                                                   "message": str(exc)}}), 400
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("composed ui dispatch rejected: %r", exc)
            return jsonify({"ok": False, "error": {"code": "invalid_tree",
                                                   "message": "controller returned an invalid view"}}), 400
        plugin_ui.set_tree(sid, tree, plugin_ui.tree_events(tree), state)
        newrev = plugin_ui.bump_revision(sid)
        try:
            store.log_event("plugin", "ui[%s] %s.%s dispatch ok (rev %d)"
                            % (plugin_ui.view_id(sid), pid, ev, newrev))
        except Exception:
            pass
        return jsonify({"ok": True, "html": plugin_ui.render_tree(tree),
                        "revision": newrev, "view": _composed_view(result),
                        "replace": bool(result.get("replace")),
                        "url": _ext_url(pid, page, result.get("url"))})


@app.route("/extensions/<pid>/<page>/rpc", methods=["POST"])
def extension_rpc(pid, page):
    if not _same_origin_ok():
        return jsonify({"ok": False, "error": {"code": "forbidden",
                                               "message": "cross-site request refused"}}), 403
    row = plugins.get(pid)
    if not row or not row.get("enabled") or plugins.ui_mode(row) != "trusted" \
            or not plugin_ui.is_approved(row):
        return jsonify({"ok": False, "error": {"code": "forbidden",
                                               "message": "browser view is not approved"}}), 403
    sid = (request.headers.get("X-MT-Session") or "").strip()
    sess = plugin_ui.get_session(sid, pid, page)
    if not sess or sess.get("mode") != "trusted" \
            or sess.get("generation") != int(plugin_ui.approval(pid).get("approved_at") or 0):
        return jsonify({"ok": False, "error": {"code": "forbidden",
                                               "message": "view session is not valid"}}), 403
    if not plugin_ui.rate_ok(pid, "call"):
        return jsonify({"ok": False, "error": {"code": "busy",
                                               "message": "rate limit reached"}}), 429
    payload, err = _read_json_capped()
    if err:
        return jsonify({"ok": False, "error": {"code": err, "message": "bad request"}}), \
            _ui_envelope_status(err)
    op = str(payload.get("op") or "")
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    hdr_op = (request.headers.get("X-MT-Op") or "").strip()
    if not op or (hdr_op and hdr_op != op):
        return jsonify({"ok": False, "error": {"code": "invalid_args",
                                               "message": "operation is required"}}), 400
    with plugin_ui.slot(pid) as _slot:
        if not _slot.got:
            return jsonify({"ok": False, "error": {"code": "busy",
                                                   "message": "too many concurrent calls"}}), 429
        res = plugin_rt.runtime.invoke_ui(pid, page, op, args)
    if not _ui_session_valid_now(pid, page, sid, "trusted"):
        return jsonify({"ok": False, "error": {"code": "closed",
                                               "message": "view was closed"}}), 403
    try:
        store.log_event("plugin", "ui[%s] %s.%s %s"
                        % (plugin_ui.view_id(sid), pid, op,
                           "ok" if res.get("ok") else "error"))
    except Exception:
        pass
    code = (res.get("error") or {}).get("code") if not res.get("ok") else ""
    return jsonify(res), (200 if res.get("ok") else _ui_envelope_status(code))


@app.route("/extensions/<pid>/<page>/dispose", methods=["POST"])
def extension_dispose(pid, page):
    if not _same_origin_ok():
        return jsonify({"ok": False, "error": {"code": "forbidden",
                                               "message": "cross-site request refused"}}), 403
    sid = (request.headers.get("X-MT-Session") or "").strip()
    sess = plugin_ui.get_session(sid, pid, page)
    if sess and sess.get("mode") == "composed":
        with plugin_ui.slot(pid) as _slot:
            if _slot.got:
                try:
                    plugin_rt.runtime.ui_close(pid, sess.get("state") or {})
                except Exception:
                    pass
    plugin_ui.dispose(sid)
    return jsonify({"ok": True})


@app.route("/extensions/<pid>/<page>/status")
def extension_status(pid, page):
    # Bounded-poll validity check. It has no cross-view effect: an unknown/forged
    # caller is reported invalid and nothing is dropped; only the caller's own
    # session is disposed (and only for a same-origin request) when it is no
    # longer valid.
    sid = (request.headers.get("X-MT-Session") or "").strip()
    sess = plugin_ui.get_session(sid, pid, page)
    if not sess:
        # Unknown/forged/session-less caller: report invalid but DO NOT drop other
        # live views (AR2-4).
        return jsonify({"ok": True, "valid": False, "revision": 0})
    row = plugins.get(pid)
    valid = bool(row and row.get("enabled"))
    if valid and plugins.ui_mode(row) == "trusted":
        valid = plugin_ui.is_approved(row) and \
            sess.get("generation") == int(plugin_ui.approval(pid).get("approved_at") or 0)
    if not valid and _same_origin_ok():
        plugin_ui.dispose(sid)
    return jsonify({"ok": True, "valid": valid,
                    "revision": sess.get("revision", 0)})


@app.route("/plugins/<pid>/ui-approve", methods=["POST"])
def plugin_ui_approve(pid):
    if not _same_origin_ok():
        return jsonify({"ok": False, "error": {"code": "forbidden",
                                               "message": "cross-site request refused"}}), 403
    row = plugins.get(pid)
    if not row or plugins.ui_mode(row) != "trusted":
        flash("No trusted browser view to approve.", "warn")
        return redirect(url_for("plugin_detail", pid=pid))
    plugin_ui.approve(pid, row)
    flash("Browser view approved for this exact content. It runs code that can transmit "
          "any data it receives (including mail) by navigating itself - this is NOT limited "
          "by the plugin's net.http grants.", "warn")
    return redirect(url_for("plugin_detail", pid=pid))


@app.route("/plugins/<pid>/ui-revoke", methods=["POST"])
def plugin_ui_revoke(pid):
    if not _same_origin_ok():
        return jsonify({"ok": False, "error": {"code": "forbidden",
                                               "message": "cross-site request refused"}}), 403
    plugin_ui.revoke(pid)
    flash("Browser view approval revoked; its page will not run.", "ok")
    return redirect(url_for("plugin_detail", pid=pid))


EXTENSION_TMPL = r"""
<style>
{{ comp_css|safe }}
.mt-ext-frame{width:100%;height:calc(100vh - 300px);min-height:380px;border:1px solid var(--line);
background:var(--card);display:block}
.ext-bad{margin:8px 0 0;color:var(--err);font-size:.85rem}
.ext-warn{margin:0 0 10px;border:1px solid var(--warn);background:var(--tint-warn);color:#5c3300;
padding:9px 12px;font-size:.84rem}
</style>
<div class="page-head">
  <div style="min-width:0">
    <a class="backlink sub" href="{{ url_for('plugin_detail', pid=pid) }}">&lsaquo; {{ name }}</a>
    <h1 class="page-title">{{ p.title }}</h1>
    <div class="page-desc">{{ p.description or 'Plugin page' }} &middot; <span class="mono">{{ pid }}</span></div>
  </div>
  <div class="sub" id="mt-ext-status" role="status" aria-live="polite">Loading&hellip;</div>
</div>
{% if mode == 'composed' %}
<div id="mt-ext" class="mtc-root" data-mode="composed">
  <div id="mt-tree" class="mtc-tree">{{ tree_html|safe }}</div>
  <div class="ext-bad" id="mt-ext-error" hidden></div>
</div>
{% else %}
<div id="mt-ext" data-mode="trusted">
  <div class="ext-warn" role="note">This view runs plugin browser code. It can send any data
  it receives - including mail text - by navigating itself, and that is not limited by the
  plugin's declared network grants. Only approve plugins you trust.</div>
  <iframe id="mt-ext-frame" class="mt-ext-frame" sandbox="allow-scripts"
          referrerpolicy="no-referrer" title="{{ p.title }}" srcdoc="{{ srcdoc }}"></iframe>
  <noscript><div class="ext-bad">This page needs JavaScript.</div></noscript>
  <div class="ext-bad" id="mt-ext-error" hidden></div>
</div>
{% endif %}
<script type="application/json" id="mt-ext-cfg">{{ cfg_json|safe }}</script>
<script>
(function(){
  "use strict";
  var cfg = {};
  try { cfg = JSON.parse((document.getElementById('mt-ext-cfg')||{}).textContent || '{}'); } catch(e){}
  var status = document.getElementById('mt-ext-status');
  var errorEl = document.getElementById('mt-ext-error');
  var disposed = false;
  var pollT = null;
  var cleanups = [];
  function on(target, type, fn){ if(!target) return; target.addEventListener(type, fn); cleanups.push(function(){ try{ target.removeEventListener(type, fn); }catch(e){} }); }
  function setStatus(t){ if(status){ status.textContent = t || ''; } }
  function showError(msg){ if(errorEl){ errorEl.hidden = false; errorEl.textContent = msg; } }
  function clearTree(){ var t=document.getElementById('mt-tree'); if(t) t.textContent=''; }
  function teardown(){
    if(disposed) return;
    disposed = true;
    if(pollT){ clearInterval(pollT); pollT = null; }
    while(cleanups.length){ try{ cleanups.pop()(); }catch(e){} }
    try {
      fetch(cfg.base + '/dispose', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-MT-Session':cfg.sid}, body:'{}', keepalive:true});
    } catch(e){}
  }
  function fail(msg){ clearTree(); showError(msg); teardown(); }
  function poll(){
    if(disposed) return;
    fetch(cfg.base + '/status', {credentials:'same-origin',
      headers:{'X-MT-Session': cfg.sid}}).then(function(r){ return r.json(); })
      .then(function(j){
        if(disposed) return;
        if(j && j.ok && j.valid) return;
        fail('This plugin page was closed (the plugin was disabled, unapproved or changed). Reload to retry.');
      }).catch(function(){});
  }
  on(window, 'pagehide', teardown);
  on(document, 'turbo:before-cache', teardown);
  on(document, 'turbo:before-render', teardown);
  on(document, 'turbo:before-visit', teardown);
  if(window.__mtExt && window.__mtExt.teardown){ try{ window.__mtExt.teardown(); }catch(e){} }
  window.__mtExt = {teardown: teardown};

  if(cfg.mode === 'composed'){
    var tree = document.getElementById('mt-tree');
    var revision = cfg.revision || 0;
    var busy = false;
    function applyView(v){
      v = (v === 'reader') ? 'reader' : 'list';
      tree.querySelectorAll('.mtc-split').forEach(function(s){
        s.classList.toggle('mtc-view-reader', v === 'reader');
        s.classList.toggle('mtc-view-list', v !== 'reader');
      });
      tree.querySelectorAll('[data-mt-event="back"]').forEach(function(b){ b.classList.add('mtc-mobile-back'); });
    }
    function apply(res){
      if(disposed || !res) return;
      if(!res.ok){
        showError(String((res.error && res.error.message) || 'Request failed.'));
        setStatus('');
        return;
      }
      errorEl.hidden = true;
      if(typeof res.html === 'string') tree.innerHTML = res.html;
      applyView(res.view || cfg.view);
      if(res.revision) revision = res.revision;
      if(res.url){
        var cur = location.pathname + location.search;
        if(res.url !== cur){
          history[res.replace ? 'replaceState' : 'pushState'](
            Object.assign({}, history.state || {}, {mtExt: 1}), '', res.url);
        }
      }
      setStatus('');
    }
    function dispatch(ev, value){
      if(disposed || busy) return;
      busy = true;
      setStatus('Working\u2026');
      var prior = document.activeElement;
      var priorEv = prior && prior.getAttribute ? prior.getAttribute('data-mt-event') : null;
      fetch(cfg.base + '/dispatch', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-MT-Session':cfg.sid},
        body: JSON.stringify({event: ev, value: (value === undefined ? null : value), revision: revision})
      }).then(function(r){ return r.json(); }).then(function(res){
        apply(res);
        if(priorEv){ var n=document.querySelector('[data-mt-event="'+priorEv+'"]'); if(n && n.focus) n.focus(); }
      }).catch(function(){ showError('Request failed. Reload to retry.'); })
        .then(function(){ busy = false; });
    }
    on(document, 'click', function(e){
      var m = e.target && e.target.closest ? e.target.closest('[data-mt-event]') : null;
      if(!m || m.tagName === 'INPUT') return;
      e.preventDefault();
      dispatch(m.getAttribute('data-mt-event'), m.getAttribute('data-mt-value'));
    });
    on(document, 'keydown', function(e){
      if(e.key !== 'Enter') return;
      var m = e.target && e.target.closest ? e.target.closest('[data-mt-event]') : null;
      if(m && m.tagName === 'INPUT'){ e.preventDefault(); dispatch(m.getAttribute('data-mt-event'), m.value); }
    });
    (function(){
      var tabs = document.getElementById('mt-tree');
      if(!tabs) return;
      on(tabs, 'click', function(e){
        var b = e.target && e.target.closest ? e.target.closest('[data-mt-tab]') : null;
        if(!b) return;
        var idx = parseInt(b.getAttribute('data-mt-tab'), 10);
        var bar = b.parentNode;
        bar.querySelectorAll('[data-mt-tab]').forEach(function(x){ x.classList.remove('on'); });
        b.classList.add('on');
        var wrap = b.closest('.mtc-tabs');
        if(wrap){ wrap.querySelectorAll('.mtc-tabpanel').forEach(function(p, i){ p.hidden = i !== idx; }); }
      });
    })();
    applyView(cfg.view);
    on(window, 'popstate', function(){ if(!disposed) location.reload(); });
    setStatus('');
  } else {
    var frame = document.getElementById('mt-ext-frame');
    var pending = {}, seq = 0, handshaken = false, bridged = false, port = null;
    var watchdog = null, heartbeatT = null, checkT = null, lastPong = 0;
    function post(msg){ if(port){ try{ port.postMessage(msg); }catch(e){} } }
    function theme(){
      var cs = window.getComputedStyle(document.documentElement);
      var names = ['--fg','--bg','--dim','--line','--line2','--card','--card2','--acc',
                   '--ok','--warn','--err','--hover','--focus','--mono'];
      var out = {};
      for(var i=0;i<names.length;i++){ var v=cs.getPropertyValue(names[i]); if(v) out[names[i]]=v.trim(); }
      return out;
    }
    function layout(){ return (window.matchMedia && window.matchMedia('(max-width:767px)').matches) ? 'mobile':'desktop'; }
    function rpc(op, args){
      if(disposed) return Promise.reject(new Error('disposed'));
      var id = 'r' + (++seq);
      var ac = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      pending[id] = ac;
      var timer = setTimeout(function(){ if(ac) ac.abort(); }, 20000);
      return fetch(cfg.base + '/rpc', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-MT-Session':cfg.sid,'X-MT-Op':op},
        body: JSON.stringify({op: op, args: args || {}}),
        signal: ac ? ac.signal : undefined
      }).then(function(r){ return r.json().catch(function(){ return {ok:false,error:{code:'internal',message:'invalid response'}}; }); })
        .then(function(j){ clearTimeout(timer); delete pending[id]; if(disposed) throw new Error('disposed'); return j; },
              function(err){ clearTimeout(timer); delete pending[id];
                var ab = err && err.name === 'AbortError';
                return {ok:false, error:{code: ab?'timeout':'network', message: ab?'request timed out':'request failed'}}; });
    }
    function onPortMessage(e){
      if(disposed) return;
      var d = e.data;
      if(!d || d.__mt !== 1) return;
      if(d.sid && d.sid !== cfg.sid) return;
      if(d.kind === 'ready'){
        post({__mt:1, sid:cfg.sid, kind:'theme', theme:theme(), config:cfg.initial});
        post({__mt:1, sid:cfg.sid, kind:'layout', layout:layout()});
        if(!bridged){
          bridged = true;
          lastPong = Date.now();
          heartbeatT = setInterval(function(){ if(!disposed) post({__mt:1, sid:cfg.sid, kind:'ping'}); }, 2000);
          checkT = setInterval(function(){
            if(!disposed && Date.now() - lastPong > 6000){
              fail('The plugin view stopped responding; its bridge was closed. Reload to retry.');
            }
          }, 2000);
        }
      } else if(d.kind === 'pong'){
        lastPong = Date.now();
      } else if(d.kind === 'call'){
        if(!d.op || cfg.ops.indexOf(d.op) < 0){
          post({__mt:1, sid:cfg.sid, kind:'result', id:d.id, ok:false, error:{code:'forbidden', message:'operation not allowed'}});
          return;
        }
        rpc(d.op, d.args).then(function(res){
          if(disposed) return;
          post({__mt:1, sid:cfg.sid, kind:'result', id:d.id, ok:!!res.ok, data:res.data, error:res.error, summary:res.summary});
        });
      } else if(d.kind === 'nav'){
        try {
          var url = new URL(location.href);
          if(typeof d.q === 'string'){ if(d.q) url.searchParams.set('q', d.q.slice(0,200)); else url.searchParams.delete('q'); }
          if(typeof d.message === 'string'){ if(d.message) url.searchParams.set('message', d.message.slice(0,20)); else url.searchParams.delete('message'); }
          var next = url.pathname + (url.searchParams.toString() ? '?' + url.searchParams.toString() : '');
          if(next !== location.pathname + location.search){
            history[d.replace ? 'replaceState' : 'pushState'](Object.assign({}, history.state || {}, {mtExt:1}), '', next);
          }
        } catch(e){}
      } else if(d.kind === 'log'){
        try{ console.log('[plugin ' + cfg.pid + '] ' + String(d.message||'').slice(0,500)); }catch(err){}
      }
    }
    function onHello(e){
      if(disposed || handshaken) return;
      if(e.source !== frame.contentWindow) return;
      var d = e.data;
      if(!d || d.__mt_hello !== 1 || d.token !== cfg.bootToken) return;
      handshaken = true;   // one-shot: a later remote page cannot re-establish
      var ch = new MessageChannel();
      port = ch.port1;
      port.onmessage = onPortMessage;
      if(port.start) port.start();
      if(watchdog){ clearTimeout(watchdog); watchdog = null; }
      try {
        frame.contentWindow.postMessage({__mt_ack:1, token:cfg.bootToken, sid:cfg.sid,
          boot:{v:1, pid:cfg.pid, page:cfg.page, ops:cfg.ops, state:cfg.initial}},
          '*', [ch.port2]);
      } catch(err){}
      setStatus('');
    }
    on(window, 'message', onHello);
    on(window, 'resize', function(){ if(!disposed) post({__mt:1, sid:cfg.sid, kind:'layout', layout:layout()}); });
    on(window, 'popstate', function(){
      var params = new URLSearchParams(location.search);
      post({__mt:1, sid:cfg.sid, kind:'state', state:{q:params.get('q')||'', message:params.get('message')||''}});
    });
    cleanups.push(function(){ if(heartbeatT){ clearInterval(heartbeatT); heartbeatT = null; } });
    cleanups.push(function(){ if(checkT){ clearInterval(checkT); checkT = null; } });
    cleanups.push(function(){ if(port){ try{ port.onmessage = null; port.close(); }catch(e){} port = null; } });
    cleanups.push(function(){ if(frame && frame.parentNode){ try{ frame.parentNode.removeChild(frame); }catch(e){} } });
    watchdog = setTimeout(function(){
      if(!handshaken && !disposed){ fail('The plugin view did not start.'); }
    }, 4000);
    setStatus('Loading\u2026');
  }
  pollT = setInterval(poll, 4000);
})();
</script>
"""
@app.route("/classifiers")
def classifiers():
    hx = []
    for h in store.list_heuristics():
        v = heuristics.view(h)
        v["when"] = fmt_ts(h.get("updated"))
        hx.append(v)
    return render(_render_src(CLASSIFIERS_TMPL, hx=hx))


@app.route("/classifiers/<int:hid>/toggle", methods=["POST"])
def classifier_toggle(hid):
    row = store.get_heuristic(hid)
    if row:
        new_state = 0 if row.get("enabled") else 1
        store.update_heuristic(hid, enabled=new_state)
        store.log_event("info", "classifier #%d '%s' %s (by ui)"
                        % (hid, row.get("name") or "", "enabled" if new_state else "disabled"))
        flash("Classifier '%s' %s." % (row.get("name") or hid,
                                       "enabled" if new_state else "disabled"), "ok")
    return redirect(url_for("classifiers"))


@app.route("/classifiers/<int:hid>/retrain", methods=["POST"])
def classifier_retrain(hid):
    row = store.get_heuristic(hid)
    if row:
        try:
            stats = json.loads(row.get("stats") or "{}")
            model, new_stats = heuristics.train_heuristic(
                row.get("kind") or "", row.get("category") or "",
                source=stats.get("source") or "tags", params=stats.get("params") or {},
                min_confidence=float(row.get("min_confidence") or 0.8), created_by="ui",
                exclude=heuristics.heuristic_excluded(row))
            store.update_heuristic(hid, model=json.dumps(model), stats=json.dumps(new_stats))
            store.log_event("info", "classifier #%d '%s' retrained (by ui, %d sample(s))"
                            % (hid, row.get("name") or "", new_stats.get("trained_label_count") or 0))
            flash("Classifier '%s' retrained." % (row.get("name") or hid), "ok")
        except Exception as exc:
            flash("Retrain failed: %s" % exc, "err")
    return redirect(url_for("classifiers"))


@app.route("/classifiers/<int:hid>/delete", methods=["POST"])
def classifier_delete(hid):
    row = store.get_heuristic(hid)
    store.delete_heuristic(hid)
    if row:
        store.log_event("info", "classifier #%d '%s' deleted (by ui)" % (hid, row.get("name") or ""))
    flash("Classifier '%s' deleted." % ((row or {}).get("name") or hid), "ok")
    return redirect(url_for("classifiers"))


CLASSIFIER_DATASET_TMPL = """
<div class="page-head">
  <div>
    <div class="backlink"><a href="{{ url_for('classifiers') }}">← Classifiers</a></div>
    <h1 class="page-title">{{ h.name }}</h1>
    <div class="page-desc">dataset review · {{ h.kind }} · {{ h.category }}</div>
  </div>
  <div class="row">
    <form class="inline" method="post" action="{{ url_for('classifier_retrain', hid=h.id) }}"><button class="btn primary" type="submit">Retrain with current dataset</button></form>
  </div>
</div>
<div class="card">
  <div class="row" style="margin-bottom:8px">
    <span class="badge">{{ h.kind }}</span>
    <span class="badge acc">{{ h.category }}</span>
    <span class="badge {{ 'warn' if ds.weak else 'ok' }}">labels: {{ 'LLM auto-tags' if ds.weak else 'your tags' }}</span>
    {% if not h.enabled %}<span class="badge">disabled</span>{% endif %}
  </div>
  <div class="sub">{{ ds.pos_total }} positive sample(s){% if ds.pos_excluded %} ({{ ds.pos_excluded }} removed){% endif %} ·
    {{ ds.neg_total }} negative sample(s){% if ds.neg_excluded %} ({{ ds.neg_excluded }} removed){% endif %}.
    Change a sample's label in the dropdown to reclassify it — it moves between the sets immediately;
    retrain to apply the new dataset to the model.
    {% if ds.weak %}These labels come from the LLM's own auto-classification, so correcting them here fixes both the
    dataset and the message's record.{% endif %}</div>
</div>

<div class="card flush dsx">
  <div class="card-h" style="padding:14px 16px 10px;margin:0"><h3>In the set <span class="sub">({{ ds.category }} positives)</span></h3></div>
  {% if ds.positives %}
  <div class="tablewrap"><table class="tbl mcards">
    <thead><tr><th>subject</th><th>from</th><th>labeled</th><th class="r"></th></tr></thead>
    <tbody>
    {% for s in ds.positives %}
    <tr{% if s.excluded %} style="opacity:.45"{% endif %}>
      <td><a href="{{ url_for('message_detail', mid=s.msg_id) }}">{{ s.subject or '(no subject)' }}</a>{% if s.summary %}<div class="sub" style="font-size:.75rem">{{ s.summary }}</div>{% endif %}</td>
      <td class="sub">{{ s.from }}</td>
      <td>
        <form class="inline" method="post" action="{{ url_for('classifier_dataset_relabel', hid=h.id) }}"><input type="hidden" name="msg_id" value="{{ s.msg_id }}"><select name="category" onchange="this.form.submit()" style="width:auto;padding:3px 6px;font-size:.82rem" aria-label="Reclassify sample — {{ (s.subject or 'no subject')[:40] }}">{% if ds.source == 'tags' %}{% if s.tag and s.tag not in options %}<option value="{{ s.tag }}" selected>{{ s.tag }}</option>{% endif %}{% for c in options %}<option value="{{ c }}"{{ ' selected' if s.tag == c else '' }}>{{ c }}</option>{% endfor %}{% else %}{% if s.llm_category and s.llm_category not in options %}<option value="{{ s.llm_category }}" selected>{{ s.llm_category }}</option>{% endif %}{% for c in options %}<option value="{{ c }}"{{ ' selected' if s.llm_category == c else '' }}>{{ c }}</option>{% endfor %}{% endif %}</select></form>
        <div class="sub" style="font-size:.72rem;margin-top:2px">{% if ds.source == 'tags' %}tag{% else %}LLM label{% endif %}{% if s.confidence is not none %} · {{ '%.0f' % (s.confidence*100) }}%{% endif %}{% if s.excluded %} · removed{% endif %}</div>
      </td>
      <td class="r">{% if s.excluded %}
        <form class="inline" method="post" action="{{ url_for('classifier_dataset_reinclude', hid=h.id) }}"><input type="hidden" name="msg_id" value="{{ s.msg_id }}"><button class="btn small" type="submit">Re-include</button></form>
      {% else %}
        <form class="inline" method="post" action="{{ url_for('classifier_dataset_remove', hid=h.id) }}"><input type="hidden" name="msg_id" value="{{ s.msg_id }}"><button class="btn small" type="submit">Remove</button></form>
      {% endif %}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% if ds.pos_total > ds.positives|length %}<div class="sub" style="padding:10px 16px">showing the first {{ ds.positives|length }} of {{ ds.pos_total }}</div>{% endif %}
  {% else %}<div class="empty"><h4>No positive samples yet</h4><p>Tag mail or let the LLM classify some first.</p></div>{% endif %}
</div>

<div class="card flush dsx">
  <div class="card-h" style="padding:14px 16px 10px;margin:0"><h3>Out of set <span class="sub">(negatives — samples of other categories)</span></h3></div>
  {% if ds.negatives %}
  <div class="tablewrap"><table class="tbl mcards">
    <thead><tr><th>subject</th><th>from</th><th>labeled</th><th class="r"></th></tr></thead>
    <tbody>
    {% for s in ds.negatives %}
    <tr{% if s.excluded %} style="opacity:.45"{% endif %}>
      <td><a href="{{ url_for('message_detail', mid=s.msg_id) }}">{{ s.subject or '(no subject)' }}</a></td>
      <td class="sub">{{ s.from }}</td>
      <td>
        <form class="inline" method="post" action="{{ url_for('classifier_dataset_relabel', hid=h.id) }}"><input type="hidden" name="msg_id" value="{{ s.msg_id }}"><select name="category" onchange="this.form.submit()" style="width:auto;padding:3px 6px;font-size:.82rem" aria-label="Reclassify sample — {{ (s.subject or 'no subject')[:40] }}">{% if ds.source == 'tags' %}{% if s.tag and s.tag not in options %}<option value="{{ s.tag }}" selected>{{ s.tag }}</option>{% endif %}{% for c in options %}<option value="{{ c }}"{{ ' selected' if s.tag == c else '' }}>{{ c }}</option>{% endfor %}{% else %}{% if s.llm_category and s.llm_category not in options %}<option value="{{ s.llm_category }}" selected>{{ s.llm_category }}</option>{% endif %}{% for c in options %}<option value="{{ c }}"{{ ' selected' if s.llm_category == c else '' }}>{{ c }}</option>{% endfor %}{% endif %}</select></form>
        <div class="sub" style="font-size:.72rem;margin-top:2px">{% if ds.source == 'tags' %}tag{% else %}LLM label{% endif %}{% if s.confidence is not none %} · {{ '%.0f' % (s.confidence*100) }}%{% endif %}{% if s.excluded %} · removed{% endif %}</div>
      </td>
      <td class="r">{% if s.excluded %}
        <form class="inline" method="post" action="{{ url_for('classifier_dataset_reinclude', hid=h.id) }}"><input type="hidden" name="msg_id" value="{{ s.msg_id }}"><button class="btn small" type="submit">Re-include</button></form>
      {% else %}
        <form class="inline" method="post" action="{{ url_for('classifier_dataset_remove', hid=h.id) }}"><input type="hidden" name="msg_id" value="{{ s.msg_id }}"><button class="btn small" type="submit">Remove</button></form>
      {% endif %}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% if ds.neg_total > ds.negatives|length %}<div class="sub" style="padding:10px 16px">showing the first {{ ds.negatives|length }} of {{ ds.neg_total }}</div>{% endif %}
  {% else %}<div class="empty"><h4>No negative samples yet</h4><p>Samples from other categories sharpen precision — tag some more mail.</p></div>{% endif %}
</div>
"""




@app.route("/classifiers/<int:hid>/dataset")
def classifier_dataset(hid):
    row = store.get_heuristic(hid)
    if not row:
        flash("No such classifier.", "err")
        return redirect(url_for("classifiers"))
    ds = heuristics.dataset_for(row)
    options = list(store.get_setting("categories") or [])
    if row.get("category") and row["category"] not in options:
        options.insert(0, row["category"])
    return render(_render_src(CLASSIFIER_DATASET_TMPL, h=heuristics.view(row),
                                         ds=ds, options=options))


@app.route("/classifiers/<int:hid>/dataset/relabel", methods=["POST"])
def classifier_dataset_relabel(hid):
    row = store.get_heuristic(hid)
    if row:
        try:
            mid = int(request.form.get("msg_id") or 0)
        except ValueError:
            mid = 0
        cat = (request.form.get("category") or "").strip()[:60]
        msg = store.get_message(mid) if mid else None
        if msg and cat:
            try:
                stats = json.loads(row.get("stats") or "{}")
            except (TypeError, ValueError):
                stats = {}
            if (stats.get("source") or "tags") == "tags":
                store.update_message(mid, user_tag=cat)
            else:
                store.update_message(mid, llm_category=cat, classified_by="user",
                                     llm_confidence=1.0)
            store.log_event("info", "dataset: message %d relabelled to '%s' (classifier '%s', by ui)"
                            % (mid, cat, row.get("name") or hid))
            learning.observe(mid, "relabel", cat, source="ui")
            store.record_label(mid, "category", json.dumps(cat), 1.0,
                               "explicit_user_correction", "dataset relabel")
            in_set = cat.strip().lower() == (row.get("category") or "").strip().lower()
            flash(("Sample moved to the in-set — %s." if in_set else
                   "Sample moved to the out-of-set — %s.") % cat, "ok")
    return redirect(url_for("classifier_dataset", hid=hid))


def _dataset_edit(hid, add):
    row = store.get_heuristic(hid)
    if row:
        try:
            mid = int(request.form.get("msg_id") or 0)
        except ValueError:
            mid = 0
        if mid:
            excl = heuristics.heuristic_excluded(row)
            if add:
                excl.add(mid)
            else:
                excl.discard(mid)
            store.update_heuristic(hid, excluded=json.dumps(sorted(excl)))
            store.log_event("info", "%s sample %d on classifier '%s' (%s)"
                            % ("removed" if add else "re-included", mid,
                               row.get("name") or hid, "retrain to apply" if add else "restored"))
            flash(("Sample removed from the dataset - retrain to apply."
                   if add else "Sample restored to the dataset - retrain to apply."), "ok")
    return redirect(url_for("classifier_dataset", hid=hid))


@app.route("/classifiers/<int:hid>/dataset/remove", methods=["POST"])
def classifier_dataset_remove(hid):
    return _dataset_edit(hid, True)


@app.route("/classifiers/<int:hid>/dataset/reinclude", methods=["POST"])
def classifier_dataset_reinclude(hid):
    return _dataset_edit(hid, False)


@app.route("/rules")
def rules():
    rules_list = store.list_rules()
    diagnostics = ux.rule_diagnostics(rules_list)
    for r in rules_list:
        r['diagnostic'] = diagnostics.get(r['id'], '')
        r["summary"] = summarize_conditions(r)
        r["actions"] = summarize_actions(r)
    tr, tu = None, None
    if request.args.get("tested") == "1":
        with store.db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 200")]
        enabled = [r for r in rules_list if r["enabled"]]
        tr = []
        matched_ids = set()
        for r in enabled:
            cnt = 0
            for m in rows:
                if m["id"] in matched_ids:
                    continue
                fields = {"from": m["from_addr"], "to": m["to_addr"],
                          "subject": m["subject"], "body": m["snippet"]}
                if engine.rule_matches(r, fields):
                    cnt += 1
                    matched_ids.add(m["id"])
            tr.append({"name": r["name"], "count": cnt})
        tu = len(rows) - len(matched_ids)
    return render(_render_src(
        RULES_TMPL, rules=rules_list, settings=store.all_settings(),
        test_results=tr, test_unmatched=tu, test_limit=200))


@app.route("/rules/test", methods=["POST"])
def rules_test():
    return redirect(url_for("rules", tested="1"))

RULE_EDIT_TMPL = """
{% if error %}<div class="msg err" role="alert" id="form-err" tabindex="-1">{{ error }}</div><script>try{document.getElementById("form-err").focus();}catch(e){}</script>{% endif %}
<div class="page-head">
  <div>
    <div class="backlink"><a href="{{ url_for('rules') }}">← Rules</a></div>
    <h1 class="page-title">{{ 'Edit rule' if rule else 'New rule' }}</h1>
    <div class="page-desc">Rules run before classifiers and the LLM, in list order — first match wins.</div>
  </div>
</div>
<form method="post" id="ruleform">
  <div class="card">
    <div class="card-h"><h3>Basics</h3></div>
    <div class="grid2">
      <div><label for="r-name">Name</label><input id="r-name" type="text" name="name" value="{{ rule.name if rule else '' }}" placeholder="e.g. Boss → Work"></div>
      <div><label for="r-mode">Match mode</label>
        <select id="r-mode" name="match_mode">
          <option value="all" {{ 'selected' if (rule.match_mode if rule else 'all')=='all' else '' }}>ALL conditions must match</option>
          <option value="any" {{ 'selected' if rule and rule.match_mode=='any' else '' }}>ANY condition matches</option>
        </select>
        <div class="sub" style="margin-top:4px">ALL requires every filled row to match; ANY needs just one.</div>
      </div>
    </div>
    <label class="check"><input type="checkbox" name="enabled" value="1" {{ 'checked' if (rule.enabled if rule else True) else '' }}> <span>Enabled — evaluated on every check</span></label>
  </div>
  <div class="card">
    <div class="card-h"><h3>Conditions</h3><span class="sub">empty rows are ignored</span></div>
    <div id="conds">
      <div class="grid3 sub cond-head" style="margin-bottom:2px"><div>field</div><div>operator</div><div>value</div></div>
      {% for i in range(5) %}
      {% set c = conditions[i] if conditions|length > i else {} %}
      <div class="grid3 rule-cond" {{ 'hidden' if i >= ([conditions|length,1]|max) else '' }} style="margin-bottom:6px">
        <select name="cond_field_{{ i }}" aria-label="Condition {{ i+1 }} field">
          {% for f in ['from','to','subject','body'] %}
          <option value="{{ f }}" {{ 'selected' if c.get('field')==f else '' }}>{{ f }}</option>{% endfor %}
        </select>
        <select name="cond_op_{{ i }}" aria-label="Condition {{ i+1 }} operator">
          {% for o in ['contains','equals','regex','plugin'] %}
          <option value="{{ o }}" {{ 'selected' if c.get('op')==o else '' }}>{{ o }}</option>{% endfor %}
        </select>
        <input type="text" name="cond_value_{{ i }}" value="{{ c.get('value','') if c.get('op') != 'plugin' else c.get('plugin','') }}" placeholder="value to match (or plugin id)" aria-label="Condition {{ i+1 }} value">
      </div>
      {% endfor %}
      <button type="button" class="btn small" data-add-rule-condition {{ 'hidden' if conditions|length >= 5 else '' }}>Add condition</button>
      <noscript><style>.rule-cond[hidden]{display:grid!important}</style></noscript>
    </div>
    <div class="sub" style="margin-top:6px">Values of 3 characters or fewer match whole words only — “PO” will not fire on “support”.</div>
  </div>
  <div class="card">
    <div class="card-h"><h3>Actions</h3></div>
    <div class="sub" style="margin-bottom:6px">Leave all blank to keep matching mail in place (a guard rule: no later rule or LLM filing can move it).</div>
    <div class="grid2">
      <div><label for="r-move">Move to folder <span class="sub">(blank = don't move; created if missing)</span></label>
        <input id="r-move" type="text" name="move_to" value="{{ actions.get('move_to','') }}" placeholder="e.g. Work"></div>
      <div>
        <label class="check"><input type="checkbox" name="mark_read" value="1" {{ 'checked' if actions.get('mark_read') else '' }}> <span>Mark as read</span></label>
        <label class="check"><input type="checkbox" name="flag" value="1" {{ 'checked' if actions.get('flag') else '' }}> <span>Flag / star</span></label>
      </div>
    </div>
  </div>
  {{ editor_preview('rule', rule.id if rule and rule.id else 0)|safe }}
  <div class="savebar"><button class="btn primary" type="submit">Save rule</button><button class="btn" type="button" data-open-preview>Test this draft</button><a class="btn" href="{{ url_for('rules') }}">Cancel</a></div>
</form>
"""






def _rule_from_form():
    name = (request.form.get("name") or "").strip() or "Untitled rule"
    match_mode = request.form.get("match_mode", "all")
    conditions = []
    for i in range(5):
        val = (request.form.get("cond_value_%d" % i) or "").strip()
        if not val:
            continue
        op = request.form.get("cond_op_%d" % i, "contains")
        if op == "plugin":
            conditions.append({"field": "subject", "op": "plugin", "plugin": val})
            continue
        conditions.append({"field": request.form.get("cond_field_%d" % i, "subject"),
                           "op": op,
                           "value": val})
    actions = {}
    if (request.form.get("move_to") or "").strip():
        actions["move_to"] = request.form.get("move_to").strip()
    if request.form.get("mark_read"):
        actions["mark_read"] = True
    if request.form.get("flag"):
        actions["flag"] = True
    enabled = bool(request.form.get("enabled"))
    return name, match_mode, conditions, actions, enabled


def _rule_form_context_from_post(error):
    name, mode, conds, actions, enabled = _rule_from_form()
    return {"rule": {"name": name, "match_mode": mode, "enabled": enabled},
            "conditions": conds, "actions": actions, "error": error}


def _rule_form_context(rule=None):
    conditions, actions = [], {}
    if rule:
        try:
            conditions = json.loads(rule.get("conditions") or "[]")
        except (TypeError, ValueError):
            conditions = []
        try:
            actions = json.loads(rule.get("actions") or "{}")
        except (TypeError, ValueError):
            actions = {}
    return {"rule": rule, "conditions": conditions, "actions": actions}


@app.route("/rules/new", methods=["GET", "POST"])
def rule_new():
    if request.method == "POST":
        name, mode, conds, actions, enabled = _rule_from_form()
        if not conds:
            return render(_render_src(RULE_EDIT_TMPL, **_rule_form_context_from_post(
                "Add at least one condition with a value.")))
        store.add_rule(name, mode, conds, actions, enabled)
        store.log_event("info", "rule '%s' added" % name)
        flash("Rule added.", "ok")
        return redirect(url_for("rules"))
    return render(_render_src(RULE_EDIT_TMPL, **_rule_form_context()))


@app.route("/rules/<int:rule_id>/edit", methods=["GET", "POST"])
def rule_edit(rule_id):
    rule = store.get_rule(rule_id)
    if not rule:
        flash("No such rule.", "err")
        return redirect(url_for("rules"))
    if request.method == "POST":
        name, mode, conds, actions, enabled = _rule_from_form()
        if not conds:
            return render(_render_src(RULE_EDIT_TMPL, **_rule_form_context_from_post(
                "Add at least one condition with a value.")))
        store.update_rule(rule_id, name=name, match_mode=mode,
                          conditions=json.dumps(conds), actions=json.dumps(actions),
                          enabled=1 if enabled else 0)
        flash("Rule saved.", "ok")
        return redirect(url_for("rules"))
    return render(_render_src(RULE_EDIT_TMPL, **_rule_form_context(rule)))


@app.route("/rules/<int:rule_id>/toggle", methods=["POST"])
def rule_toggle(rule_id):
    rule = store.get_rule(rule_id)
    if rule:
        new_state = 0 if rule["enabled"] else 1
        store.update_rule(rule_id, enabled=new_state)
        store.log_event("info", "rule #%d '%s' %s (by ui)"
                        % (rule_id, rule.get("name") or "",
                           "enabled" if new_state else "disabled"))
        flash("Rule '%s' %s." % (rule.get("name") or rule_id,
                                 "enabled" if new_state else "disabled"), "ok")
    return redirect(url_for("rules"))


@app.route("/rules/<int:rule_id>/delete", methods=["POST"])
def rule_delete(rule_id):
    rule = store.get_rule(rule_id)
    store.delete_rule(rule_id)
    if rule:
        store.log_event("info", "rule '%s' deleted (by ui)" % (rule.get("name") or rule_id))
    flash("Rule '%s' deleted." % ((rule or {}).get("name") or rule_id), "ok")
    return redirect(url_for("rules"))


@app.route("/rules/<int:rule_id>/move", methods=["POST"])
def rule_move(rule_id):
    d = request.form.get("dir")
    if d == "top":
        store.move_rule_top(rule_id)
    else:
        store.move_rule(rule_id, -1 if d == "up" else 1)
    return redirect(url_for("rules"))


# ---------------------------------------------------------------- flows (multi-step automations)

FLOWS_TMPL = """
<div class="page-head">
  <div>
    <h1 class="page-title">Flows</h1>
    <div class="page-desc">Multi-step automations — WHEN a message matches, THEN the steps run in order. Conditions can be exact fields or AI (category / about-topic); deterministic conditions are checked first and skip the AI cost when they fail. Rules stay for simple single-action cases; rules run first.</div>
  </div>
  <div class="row">
    <a class="btn small" href="{{ url_for('simulate') }}">Simulate a draft</a>
    <a class="btn primary" href="{{ url_for('flow_new') }}">New flow</a>
  </div>
</div>
{% if test_id %}
<div class="card" style="border-left:3px solid var(--acc)">
  <div class="row" style="align-items:center;gap:10px;flex-wrap:wrap">
    <b>Flow saved.</b>
    <span class="sub" style="flex:1;min-width:200px">Want to see it work? The simulator can write a sample email that matches it — nothing to type.</span>
    <a class="btn primary small" href="{{ url_for('simulate', flow=test_id) }}">Simulate a draft that tests it →</a>
  </div>
</div>
{% endif %}
{% if flows %}
<div class="stack">
  {% for f in flows %}
  <div class="card">
    <div class="card-h" style="flex-wrap:wrap">
      <h3 style="min-width:0">{{ f.name }}{% if not f.enabled %} <span class="badge">disabled</span>{% endif %}</h3>
      <div class="row">
        <span class="seg" role="group" aria-label="Reorder">
        <form class="inline" method="post" action="{{ url_for('flow_move', flow_id=f.id) }}"><input type="hidden" name="dir" value="up"><button class="btn small" type="submit" aria-label="Move up" {{ 'disabled' if loop.first else '' }}>&#8593;</button></form>
        <form class="inline" method="post" action="{{ url_for('flow_move', flow_id=f.id) }}"><input type="hidden" name="dir" value="down"><button class="btn small" type="submit" aria-label="Move down" {{ 'disabled' if loop.last else '' }}>&#8595;</button></form>
        </span>
        <form class="inline px-swf" method="post" action="{{ url_for('flow_toggle', flow_id=f.id) }}">
          <label class="px-sw" title="{{ 'Disable' if f.enabled else 'Enable' }} {{ f.name }}"><input type="checkbox" {{ 'checked' if f.enabled }} onchange="this.form.requestSubmit()" aria-label="{{ 'Disable' if f.enabled else 'Enable' }} {{ f.name }}"><span class="px-tr"></span></label>
        </form>
        <details class="menu">
          <summary class="btn small" aria-haspopup="menu" aria-label="More actions">⋯</summary>
          <div class="menu-pop" role="menu">
            <a class="menu-item" href="{{ url_for('flow_edit', flow_id=f.id) }}">Edit</a>
            <form method="post" action="{{ url_for('flow_delete', flow_id=f.id) }}">
              <button class="menu-item danger arm-del" type="submit" data-arm-label="Press again to delete flow {{ f.name }}" aria-label="Delete flow {{ f.name }}">Delete…</button></form>
          </div>
        </details>
      </div>
    </div>
    <div class="sub" style="margin-top:4px">{{ f.summary }}</div>
    {% if f.last_run %}<div class="sub" style="margin-top:4px">last ran {{ f.last_run }}</div>{% endif %}
  </div>
  {% endfor %}
</div>
{% else %}
<div class="empty">
  <h4>No flows yet</h4>
  <p>A flow is &ldquo;when a message matches&hellip; then do several things, in order&rdquo; &mdash; for example: subject contains &ldquo;invoice&rdquo; &rarr; move to Receipts, tag it, and create a draft from a template.</p>
  <a class="btn primary" href="{{ url_for('flow_new') }}">Build the first flow</a>
</div>
{% endif %}
"""

FLOW_EDIT_TMPL = """
{% if error %}<div class="msg err" role="alert" id="form-err" tabindex="-1">{{ error }}</div><script>try{document.getElementById("form-err").focus();}catch(e){}</script>{% endif %}
<style>
/* flow editor: trigger (dark) -> condition (blue) -> numbered action chain, with a live plain-language preview */
.fl-setup{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:12px;align-items:end;margin:0 0 14px}
.fl-setup label{margin:0 0 4px}
.fl-enable{display:flex;gap:10px;align-items:center;border:1px solid var(--line2);background:#fff;padding:11px 13px;cursor:pointer;margin:0;color:var(--fg)}
.fl-enable:hover{border-color:#000}
.fl-enable input[type=checkbox]{width:17px;height:17px;margin:0;flex:none}
.fl-enable b{display:block;font-size:.88rem;font-weight:600;color:var(--fg)}
.fl-enable em{display:block;font-style:normal;font-size:.76rem;color:var(--dim);margin-top:1px}
.fl-title{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.fl-state{display:inline-block;font-size:.62rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;padding:3px 8px;border:1px solid var(--line2);color:var(--dim)}
.fl-state.on{color:var(--ok);border-color:var(--ok);background:var(--tint-ok)}
.fl-state.off{color:var(--dim);background:var(--card2)}
.fl-summary{display:flex;gap:12px;align-items:baseline;background:#0a0a0a;border:1px solid #0a0a0a;color:#fff;padding:12px 14px;margin:0 0 14px;flex-wrap:wrap}
.fl-sum-lab{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:#a3a3a3;white-space:nowrap}
.fl-sum-text{font-size:.88rem;line-height:1.5;color:#ededed;min-width:0;overflow-wrap:anywhere}
.flowcanvas{border:1px solid var(--line2);background:#fdfdfd;background-image:radial-gradient(#dcdcdc 1.1px, transparent 1.1px);background-size:22px 22px;padding:28px 16px 22px;overflow:hidden}
.fl-col{max-width:680px;margin:0 auto;display:flex;flex-direction:column}
.fl-node{position:relative;background:#fff;border:1px solid var(--line2);box-shadow:0 1px 2px rgba(0,0,0,.05)}
.fl-head{display:flex;align-items:center;gap:10px;padding:10px 12px}
.fl-htxt{min-width:0}
.fl-eyebrow{display:block;font-size:.6rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin-bottom:1px}
.fl-ic{width:26px;height:26px;min-width:26px;background:#000;color:#fff;display:flex;align-items:center;justify-content:center;font-size:.82rem;line-height:1}
.fl-ic.ai{background:var(--acc)}
.fl-t{font-weight:700;font-size:.95rem;letter-spacing:-.01em}
.fl-sub{color:var(--dim);font-size:.8rem;margin-top:1px}
.fl-sp{flex:1}
.fl-handle{position:absolute;left:50%;width:8px;height:8px;background:#fff;border:2px solid #000;transform:translateX(-50%);z-index:1}
.fl-handle.t{top:-5px}.fl-handle.b{bottom:-5px}
.fl-edge{position:relative;height:34px;flex:none}
.fl-edge::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:2px;background:var(--line2);transform:translateX(-1px)}
.fl-edge::after{content:"";position:absolute;left:50%;bottom:3px;width:7px;height:7px;border-right:2px solid var(--line2);border-bottom:2px solid var(--line2);transform:translateX(-50%) rotate(45deg)}
.fl-ins{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:28px;height:28px;border:1px solid var(--line2);background:#fff;color:var(--fg);font-size:1rem;font-weight:600;line-height:1;cursor:pointer;opacity:.55;transition:opacity .12s,border-color .12s,box-shadow .12s;z-index:2;display:flex;align-items:center;justify-content:center;padding:0}
.fl-edge:hover .fl-ins,.fl-ins:focus-visible,.fl-ins.on{opacity:1;border-color:#000;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.fl-insert-menu{position:absolute;left:50%;top:34px;transform:translateX(-50%);background:#fff;border:1px solid #000;padding:8px;display:none;flex-wrap:wrap;gap:6px;z-index:5;box-shadow:0 12px 30px rgba(0,0,0,.14);width:max-content;max-width:94%}
.fl-insert-menu.on{display:flex}
.fl-trigger{background:#0a0a0a;border-color:#0a0a0a;color:#fff;box-shadow:0 2px 6px rgba(0,0,0,.16)}
.fl-trigger .fl-head{padding:11px 12px}
.fl-trigger .fl-ic{background:#fff;color:#000}
.fl-trigger .fl-eyebrow{color:#a3a3a3}
.fl-trigger .fl-t{color:#fff}
.fl-trigger .fl-sub{color:#a3a3a3}
.fl-filters{border-color:#bcd8fb}
.fl-filters .fl-head{background:var(--tint-acc);border-bottom:1px solid #cfe3fb}
.fl-filters .fl-eyebrow{color:var(--acc)}
.fl-filters .fl-t{color:#003a8c;font-size:.98rem}
.fl-filters .fl-handle{border-color:var(--acc)}
.empty-conds{color:var(--dim);font-size:.82rem;border:1px dashed var(--line2);padding:9px 10px;background:#fff}
.cond-row{border:1px solid var(--line2);border-left:3px solid #000;background:#fff;padding:8px 10px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.cond-row + .cond-row{margin-top:6px}
.cond-row.extra{display:none}
.cond-row.k-category,.cond-row.k-topic{border-left-color:var(--acc)}
.cond-row .k-sel{width:auto;max-width:170px;flex:none;border:1px solid var(--line2);background:#fff;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:5px 7px;color:var(--fg);cursor:pointer}
.cond-row.k-category .k-sel,.cond-row.k-topic .k-sel{color:var(--acc);border-color:var(--acc);background:var(--tint-acc)}
.k-field-f{display:flex;gap:6px;min-width:0}
.cond-row:not(.k-field) .k-field-f{display:none}
.cond-row:not(.k-field) .k-score,.cond-row:not(.k-field) .k-score-lab{display:none}
.cond-row.k-field .k-score,.cond-row.k-field .k-score-lab{display:none}
.cond-row .k-score:disabled{background:var(--card2);color:var(--dim)}
.cond-row .k-val{flex:1 1 90px;min-width:0}
.cond-row .k-score{width:72px;flex:none}
.k-label{font-size:.72rem;color:var(--dim);white-space:nowrap}
.cond-rm{border:0;background:none;color:var(--dim);cursor:pointer;font-size:.85rem;padding:4px 6px;line-height:1}
.cond-rm:hover{color:var(--err);background:var(--tint-err)}
.fl-help{margin-top:10px;border:1px solid var(--line);background:#fbfbfb}
.fl-help summary{cursor:pointer;font-size:.78rem;font-weight:600;color:var(--dim);padding:8px 10px;list-style:none;display:flex;align-items:center;gap:6px}
.fl-help summary::-webkit-details-marker{display:none}
.fl-help summary::before{content:"?";width:15px;height:15px;border:1px solid var(--line2);display:inline-flex;align-items:center;justify-content:center;font-size:.68rem;color:var(--dim);flex:none}
.fl-help[open] summary{border-bottom:1px solid var(--line);color:var(--fg)}
.fl-help > div{padding:9px 11px;font-size:.78rem;color:var(--dim);line-height:1.6}
.fl-help strong{color:var(--fg)}
.stepcard{position:relative;background:#fff;border:1px solid var(--line2)}
.stepcard.open{box-shadow:0 2px 8px rgba(0,0,0,.07)}
.fl-step-head{display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer}
.fl-step-head:hover{background:var(--hover)}
.stepcard.open .fl-step-head{background:#fbfbfb;border-bottom:1px solid var(--line)}
.fl-num{width:26px;height:26px;min-width:26px;background:#fff;color:#000;border:2px solid #000;display:flex;align-items:center;justify-content:center;font:700 .8rem/1 var(--mono)}
.stepcard.open .fl-num{background:#000;color:#fff}
.fl-glyph{font-family:var(--mono);color:var(--dim);font-weight:400;margin-right:2px}
.stepcard.open .fl-glyph{color:var(--fg)}
.fl-step-sum{color:var(--dim);font-size:.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:320px}
.stepfields{background:#fafafa;border-top:1px solid var(--line);padding:12px 14px;display:grid;grid-template-columns:132px minmax(0,1fr);gap:10px 12px;align-items:center}
.stepfields label{font-size:.8rem;color:var(--dim);margin:0}
.steptools{display:flex;gap:4px;align-items:center}
.fl-steps-empty{border:1px dashed var(--line2);background:rgba(255,255,255,.85);color:var(--dim);font-size:.82rem;padding:14px 12px;text-align:center}
.fl-pal{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;padding:12px;border:1px dashed var(--line2);background:rgba(255,255,255,.9)}
.fl-pal-lab{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);width:100%}
.fl-pal .btn.small{height:30px;padding:0 11px;background:#fff}
.fl-pal .btn.small:hover{border-color:#000;background:var(--hover)}
.fl-seg{display:inline-flex;border:1px solid var(--line2);background:#fff;flex:none}
.fl-seg label{padding:0;margin:0;cursor:pointer;position:relative;display:block}
.fl-seg label+label{border-left:1px solid var(--line2)}
.fl-seg input{position:absolute;opacity:0;pointer-events:none}
.fl-seg span{display:block;padding:5px 11px;font-size:.76rem;font-weight:600;color:var(--dim)}
.fl-seg input:checked + span{background:#000;color:#fff}
.fl-seg input:focus-visible + span{outline:2px solid var(--acc);outline-offset:-2px}
.fl-save-hint{margin-left:auto;font-size:.8rem;color:var(--dim)}
@media(max-width:767px){
  .flowcanvas{padding:16px 8px}
  .fl-ins{opacity:1}
  .fl-step-sum{max-width:150px}
  .stepfields{grid-template-columns:1fr}
  .stepfields label{margin-top:4px}
  .fl-summary{flex-direction:column;gap:4px}
  .fl-setup{grid-template-columns:1fr}
  .fl-col{max-width:100%}
  .fl-head{flex-wrap:wrap}
  .cond-row .k-val{flex-basis:100%}
  .fl-step-head{flex-wrap:wrap}
  .fl-step-sum{max-width:none;white-space:normal}
  .steptools{flex-basis:100%;justify-content:flex-end;margin-top:2px}
  .fl-save-hint{display:none}
}
</style>
<div class="page-head">
  <div>
    <div class="backlink"><a href="{{ url_for('flows') }}">← Flows</a></div>
    <div class="fl-title">
      <h1 class="page-title">{{ 'New flow' if is_new else 'Edit flow' }}</h1>
      <span class="fl-state {{ 'on' if (flow.enabled if flow else True) else 'off' }}" id="fl-state">{{ 'Enabled' if (flow.enabled if flow else True) else 'Disabled' }}</span>
    </div>
    <div class="page-desc">A new message arrives, the filters decide, the steps run in order. Deterministic filters are exact and free; AI filters run only after they pass.</div>
  </div>
</div>
{% if seed_note %}<div class="note" role="status" style="margin-bottom:12px">{{ seed_note }}</div>{% endif %}
<form method="post" id="flowform">
<input type="hidden" name="steps_json" id="steps_json">

<div class="fl-setup">
  <div>
    <label for="f-name">Flow name</label>
    <input id="f-name" type="text" name="name" value="{{ flow.name if flow else '' }}" placeholder="e.g. Invoice → file + draft ack">
  </div>
  <label class="fl-enable" for="f-enabled">
    <input id="f-enabled" type="checkbox" name="enabled" value="1" {{ 'checked' if (flow.enabled if flow else True) else '' }}>
    <span>
      <b>Enabled</b>
      <em>Evaluated on every check</em>
    </span>
  </label>
</div>

<div class="fl-summary">
  <span class="fl-sum-lab">Live preview</span>
  <div class="fl-sum-text" id="fl-sum-text" aria-live="polite">{{ summary_text }}</div>
</div>

<div class="flowcanvas">
  <div class="fl-col" id="fl-col">

    <div class="fl-node fl-trigger" id="fl-trigger">
      <div class="fl-head">
        <span class="fl-ic">▸</span>
        <div class="fl-htxt">
          <span class="fl-eyebrow">Trigger</span>
          <div class="fl-t">New mail arrives</div>
          <div class="fl-sub">watches <strong>{{ watch_text }}</strong> every {{ poll_interval }}s — rules run first, then flows</div>
        </div>
      </div>
      <span class="fl-handle b"></span>
    </div>

    <div class="fl-edge" id="fl-edge-0"></div>

    <div class="fl-node fl-filters" id="fl-filters">
      <div class="fl-head">
        <span class="fl-ic ai">✦</span>
        <div class="fl-htxt">
          <span class="fl-eyebrow">Condition</span>
          <div class="fl-t">Only when</div>
        </div>
        <span class="fl-sp"></span>
        <span class="fl-seg" role="group" aria-label="Match all or any filter">
          <label><input type="radio" name="match_mode" value="all" {{ 'checked' if not (flow and flow.match_mode == 'any') else '' }}><span>match all</span></label>
          <label><input type="radio" name="match_mode" value="any" {{ 'checked' if flow and flow.match_mode == 'any' else '' }}><span>match any</span></label>
        </span>
      </div>
      <div style="padding:12px">
        <div class="empty-conds" id="cond-empty" {% if conditions %}style="display:none"{% endif %}>No filters yet — this runs on every new message. Add one:</div>
        {% for i in range(5) %}
        {% set c = conditions[i] if conditions|length > i else {} %}
        {% set ck = c.get('kind') or 'field' %}
        <div class="cond-row k-{{ ck }}{{ ' extra' if not c else '' }}" data-row="{{ i }}">
          <select class="k-sel" name="cond_kind_{{ i }}" aria-label="Filter {{ i+1 }} kind" onchange="condKind(this)">
            <option value="field" {{ 'selected' if ck == 'field' else '' }}>match</option>
            <option value="category" {{ 'selected' if ck == 'category' else '' }}>✦ AI category</option>
            <option value="topic" {{ 'selected' if ck == 'topic' else '' }}>✦ about</option>
          </select>
          <span class="k-field-f">
            <select name="cond_field_{{ i }}" aria-label="Filter {{ i+1 }} field" {{ 'disabled' if ck != 'field' else '' }} style="width:86px">
              {% for f in ['from','to','subject','body'] %}
              <option value="{{ f }}" {{ 'selected' if c.get('field') == f else '' }}>{{ f }}</option>{% endfor %}
            </select>
            <select name="cond_op_{{ i }}" aria-label="Filter {{ i+1 }} operator" {{ 'disabled' if ck != 'field' else '' }} style="width:100px">
              {% for o in ['contains','equals','regex'] %}
              <option value="{{ o }}" {{ 'selected' if c.get('op') == o else '' }}>{{ o }}</option>{% endfor %}
            </select>
          </span>
          <input class="k-val" type="text" name="cond_value_{{ i }}" value="{{ c.get('value','') }}" placeholder="value to match" aria-label="Filter {{ i+1 }} value">
          <span class="k-label k-score-lab">{{ 'min trust' if ck == 'category' else 'min score' }}</span>
          <input class="k-score" type="number" name="cond_score_{{ i }}" value="{{ c.get('min_confidence') or c.get('threshold') or '' }}" min="0" max="0.95" step="0.05" placeholder="auto" aria-label="Filter {{ i+1 }} minimum score" title="AI category: minimum confidence (0-1). about: similarity threshold (default 0.45)." {{ 'disabled' if ck == 'field' else '' }}>
          <button type="button" class="cond-rm" aria-label="Remove filter {{ i+1 }}" onclick="condClear(this)">✕</button>
        </div>
        {% endfor %}
        <datalist id="fl-cats"></datalist>
        <div class="fl-pal" style="margin-top:10px">
          <span class="fl-pal-lab">Add a filter</span>
          <button type="button" class="btn small" onclick="condAdd('field')">+ match text</button>
          <button type="button" class="btn small" onclick="condAdd('category')">+ ✦ AI category</button>
          <button type="button" class="btn small" onclick="condAdd('topic')">+ ✦ about (topic)</button>
          <a class="sub" style="margin-left:auto;align-self:center" href="{{ url_for('automation_categories') }}">Manage categories →</a>
        </div>
        <details class="fl-help">
          <summary>Filter types &amp; scoring</summary>
          <div><strong>match text</strong> = exact words (checked first, free) &middot; <strong>✦ AI category</strong> fires when the classifier tags the message &middot; <strong>✦ about (topic)</strong> matches by meaning — describe the kind of mail with its boundary, e.g. &ldquo;parcels and deliveries - shipping notices, pickup codes. NOT marketing.&rdquo; &middot; min score: topic threshold (default 0.45) or category confidence floor. Text values of 3 letters or fewer match whole words only.</div>
        </details>
      </div>
      <span class="fl-handle b"></span>
    </div>

    <div id="steps"></div>

    <div class="fl-pal" id="step-pal">
      <span class="fl-pal-lab">Add a step</span>
      <button type="button" class="btn small" onclick="addStep('move')">+ Move to folder</button>
      <button type="button" class="btn small" onclick="addStep('draft')">+ Create a draft</button>
      <button type="button" class="btn small" onclick="addStep('tag')">+ Tag</button>
      <button type="button" class="btn small" onclick="addStep('mark_read')">+ Mark read</button>
      <button type="button" class="btn small" onclick="addStep('flag')">+ Star</button>
    </div>
    <noscript><div class="msg err" style="margin-top:8px">The step builder needs JavaScript — enable it to add steps.</div></noscript>
  </div>
</div>

{{ editor_preview('flow', request.view_args.get('flow_id', 0))|safe }}
<div class="savebar">
  <button class="btn primary" type="submit">Save flow</button>
  <button class="btn" type="button" data-open-preview>Test this draft</button>
  <a class="btn" href="{{ url_for('flows') }}">Cancel</a>
  <span class="fl-save-hint">A flow only moves, tags, marks, stars, or drafts — it never deletes mail.</span>
</div>
</form>
<script>
var stepsEl = document.getElementById('steps');
var stepsInput = document.getElementById('steps_json');
var sumEl = document.getElementById('fl-sum-text');
var TYPES = [['move','Move to folder','⇥'],['draft','Create a draft','✎'],['tag','Tag','#'],['mark_read','Mark as read','✓'],['flag','Star / flag','★']];
var TEMPLATES = {{ templates_meta|tojson }};
var PLUGINS = {{ plugins_meta|tojson }};
var CATS = {{ categories|tojson }};
var steps = {{ steps|tojson }};
steps.forEach(function(s){ s._open = false; });
function el(tag, cls, txt){ var e = document.createElement(tag); if(cls) e.className = cls; if(txt != null) e.textContent = txt; return e; }
function typeInfo(t){ for (var i = 0; i < TYPES.length; i++){ if(TYPES[i][0] === t) return TYPES[i]; } return [t || 'step', 'Step', '·']; }
function stepSummary(st){
  if(st.type === 'move') return st.folder ? ('move to ' + st.folder) : 'pick a folder';
  if(st.type === 'tag') return st.tag ? ('tag "' + st.tag + '"') : 'name the tag';
  if(st.type === 'mark_read') return 'mark as read';
  if(st.type === 'flag') return 'star it';
  if(st.type === 'draft'){
    st.mode = st.mode || 'template';
    if(st.mode === 'fixed') return 'fixed draft "' + String(st.body || '').slice(0, 40) + '" → Drafts';
    if(st.mode === 'plugin'){
      var pp = PLUGINS.filter(function(x){ return x.id === st.plugin; })[0];
      var pt = TEMPLATES.filter(function(x){ return String(x.id) === String(st.template_id); })[0];
      return 'plugin draft (' + (pp ? pp.name : (st.plugin || 'pick a plugin')) + ')' + (pt ? ' using "' + pt.name + '"' : '') + ' → Drafts';
    }
    if(st.mode === 'llm') return 'LLM draft' + (st.instructions ? ' guided by "' + String(st.instructions).slice(0, 40) + '"' : '') + ' → Drafts';
    var t = TEMPLATES.filter(function(x){ return String(x.id) === String(st.template_id); })[0];
    return 'draft from ' + (t ? t.name : '(pick a template)') + ' → Drafts';
  }
  return 'step';
}
function matchWord(){ var r = document.querySelector('input[name=match_mode]:checked'); return (r && r.value === 'any') ? ' OR ' : ' AND '; }
function condTexts(){
  var out = [];
  document.querySelectorAll('.cond-row').forEach(function(row){
    var i = row.getAttribute('data-row');
    var kind = row.querySelector('.k-sel').value;
    var val = row.querySelector('.k-val').value.trim();
    if(!val) return;
    var sc = row.querySelector('.k-score').value;
    var shown = (val.length > 60) ? (val.slice(0, 60) + '…') : val;
    if(kind === 'category') out.push('✦ classified as "' + shown + '"' + (sc ? ' (≥' + Math.round(sc * 100) + '%)' : ''));
    else if(kind === 'topic') out.push('✦ about "' + shown + '"' + (sc ? ' (≥' + sc + ')' : ''));
    else out.push(row.querySelector('[name^="cond_field_"]').value + ' ' + row.querySelector('[name^="cond_op_"]').value + ' "' + shown + '"');
  });
  return out;
}
function renderSummary(){
  var when = condTexts();
  var acts = steps.map(stepSummary);
  sumEl.textContent = 'new mail · ' + (when.length ? when.join(matchWord()) : 'no filters (every message)') + ' → ' + (acts.length ? acts.join(', then ') : 'no steps yet');
}
function sync(){
  stepsInput.value = JSON.stringify(steps.map(function(s){ var o = {}; for (var k in s){ if(k.charAt(0) !== '_') o[k] = s[k]; } return o; }));
  renderSummary();
}
function condKind(sel){
  var row = sel.closest('.cond-row');
  var kind = sel.value;
  row.classList.remove('k-field','k-category','k-topic');
  row.classList.add('k-' + kind);
  var f = row.querySelector('[name^="cond_field_"]'), o = row.querySelector('[name^="cond_op_"]'), s = row.querySelector('.k-score'), v = row.querySelector('.k-val'), lab = row.querySelector('.k-score-lab');
  if(f) f.disabled = (kind !== 'field');
  if(o) o.disabled = (kind !== 'field');
  if(s) s.disabled = (kind === 'field');
  if(lab) lab.textContent = (kind === 'category') ? 'min trust' : 'min score';
  if(v){
    if(kind === 'category'){ v.setAttribute('list','fl-cats'); v.placeholder = 'category name'; }
    else if(kind === 'topic'){ v.removeAttribute('list'); v.placeholder = 'describe it, include the boundary'; }
    else { v.removeAttribute('list'); v.placeholder = 'value to match'; }
  }
  condEmpty(); renderSummary();
}
function condEmpty(){
  var any = false;
  document.querySelectorAll('.cond-row').forEach(function(r){ if(r.querySelector('.k-val').value.trim()) any = true; });
  var e = document.getElementById('cond-empty'); if(e) e.style.display = any ? 'none' : '';
}
function condAdd(kind){
  var rows = Array.prototype.slice.call(document.querySelectorAll('.cond-row'));
  var target = rows.filter(function(r){
    return !r.classList.contains('extra') && !r.querySelector('.k-val').value.trim();
  })[0];
  if(!target) target = rows.filter(function(r){ return r.classList.contains('extra'); })[0];
  if(!target) return;
  target.classList.remove('extra');
  var sel = target.querySelector('.k-sel'); sel.value = kind; condKind(sel);
  var v = target.querySelector('.k-val'); v.focus();
}
function condClear(btn){
  var row = btn.closest('.cond-row');
  row.querySelector('.k-val').value = '';
  row.querySelector('.k-score').value = '';
  var sel = row.querySelector('.k-sel'); sel.value = 'field'; condKind(sel);
  row.classList.add('extra');
  condEmpty(); renderSummary();
}
function fieldsFor(st){
  var f = el('div','stepfields');
  if(st.type === 'move'){
    f.appendChild(el('label', null, 'Folder'));
    var inp = el('input'); inp.type = 'text'; inp.value = st.folder || ''; inp.placeholder = 'e.g. Receipts';
    inp.oninput = function(){ st.folder = inp.value; renderSummary(); sync(); };
    f.appendChild(inp);
  } else if(st.type === 'draft'){
    f.appendChild(el('label', null, 'How'));
    var m = el('select');
    [['fixed','Fixed message'],['template','Fill a template'],['llm','Draft with the LLM'],['plugin','Draft via plugin']].forEach(function(t){
      var o = el('option', null, t[1]); o.value = t[0]; if((st.mode || 'template') === t[0]) o.selected = true; m.appendChild(o);
    });
    m.onchange = function(){ st.mode = m.value; render(); sync(); };
    f.appendChild(m);
    if((st.mode || 'template') === 'fixed'){
      f.appendChild(el('label', null, 'Message'));
      var ta = document.createElement('textarea'); ta.rows = 3; ta.value = st.body || '';
      ta.placeholder = 'Thank you for your email, I will get back to you shortly';
      ta.oninput = function(){ st.body = ta.value; renderSummary(); sync(); };
      f.appendChild(ta);
    } else if(st.mode === 'plugin'){
      f.appendChild(el('label', null, 'Plugin'));
      var pSel = el('select');
      if(!PLUGINS.length){ var pnone = el('option', null, '(no draft plugins enabled)'); pnone.value = ''; pSel.appendChild(pnone); }
      PLUGINS.forEach(function(pp){ var o = el('option', null, pp.name); o.value = pp.id; if((st.plugin || '') === pp.id) o.selected = true; pSel.appendChild(o); });
      pSel.onchange = function(){ st.plugin = pSel.value; renderSummary(); sync(); };
      f.appendChild(pSel);
      f.appendChild(el('label', null, 'Template (optional)'));
      var ptSel = el('select');
      var ptnone = el('option', null, '(none)'); ptnone.value = ''; ptSel.appendChild(ptnone);
      TEMPLATES.forEach(function(t){ var o = el('option', null, t.name); o.value = String(t.id); if(String(st.template_id || '') === String(t.id)) o.selected = true; ptSel.appendChild(o); });
      ptSel.onchange = function(){ st.template_id = ptSel.value; renderSummary(); sync(); };
      f.appendChild(ptSel);
      var hint = el('div', 'sub', 'Blocks wrapped in {llm-infill}...{/llm-infill} are written by plugins that support them; everything else stays as typed.');
      hint.style.gridColumn = '1 / -1';
      f.appendChild(hint);
      f.appendChild(el('label', null, 'Extra instructions (optional)'));
      var pa = document.createElement('textarea'); pa.rows = 2; pa.value = st.instructions || '';
      pa.placeholder = 'e.g. keep it to three sentences';
      pa.oninput = function(){ st.instructions = pa.value; renderSummary(); sync(); };
      f.appendChild(pa);
    } else {
      f.appendChild(el('label', null, (st.mode === 'llm') ? 'Template (optional guidance)' : 'Template'));
      var tSel = el('select');
      var none = el('option', null, '(none)'); none.value = ''; tSel.appendChild(none);
      TEMPLATES.forEach(function(t){ var o = el('option', null, t.name); o.value = String(t.id); if(String(st.template_id || '') === String(t.id)) o.selected = true; tSel.appendChild(o); });
      tSel.onchange = function(){ st.template_id = tSel.value; renderSummary(); sync(); };
      f.appendChild(tSel);
      if(st.mode === 'llm'){
        f.appendChild(el('label', null, 'Reply instructions (optional)'));
        var ta2 = document.createElement('textarea'); ta2.rows = 2; ta2.value = st.instructions || '';
        ta2.placeholder = 'e.g. thank them and mention delivery within 5 working days';
        ta2.oninput = function(){ st.instructions = ta2.value; renderSummary(); sync(); };
        f.appendChild(ta2);
      }
    }
    var dlink = el('div', 'sub', 'Saved to the shared draft destination: ');
    var dl = document.createElement('a');
    dl.href = '{{ url_for("templates") }}#draft-destination';
    dl.textContent = 'Draft destination';
    dlink.appendChild(dl);
    dlink.style.gridColumn = '1 / -1';
    f.appendChild(dlink);
  } else if(st.type === 'tag'){
    f.appendChild(el('label', null, 'Tag'));
    var tin = el('input'); tin.type = 'text'; tin.value = st.tag || ''; tin.placeholder = 'e.g. Follow up';
    tin.oninput = function(){ st.tag = tin.value; renderSummary(); sync(); };
    f.appendChild(tin);
  }
  return f;
}
function insertStep(i, type){
  var st = {type: type};
  if(type === 'move') st.folder = '';
  if(type === 'draft'){ st.mode = 'template'; st.template_id = ''; }
  if(type === 'tag') st.tag = '';
  st._open = true;
  steps.splice(i, 0, st);
  render(); sync();
}
function edge(i){
  var e = el('div','fl-edge');
  var b = el('button','fl-ins','+'); b.type = 'button'; b.setAttribute('aria-label','Insert a step here');
  var m = el('div','fl-insert-menu');
  TYPES.forEach(function(t){
    var x = el('button','btn small','+ ' + t[1]); x.type = 'button';
    x.onclick = function(ev){ ev.stopPropagation(); m.classList.remove('on'); insertStep(i, t[0]); };
    m.appendChild(x);
  });
  b.onclick = function(ev){ ev.stopPropagation(); m.classList.toggle('on'); b.classList.toggle('on', m.classList.contains('on')); };
  e.appendChild(b); e.appendChild(m);
  return e;
}
function render(){
  stepsEl.innerHTML = '';
  stepsEl.appendChild(edge(0));
  if(!steps.length){
    stepsEl.appendChild(el('div','fl-steps-empty','No steps yet — add one below, or on the + above. It runs only when the filters match.'));
  }
  steps.forEach(function(st, i){
    if(st.type === 'draft' && st.mode === 'plugin' && !st.plugin && PLUGINS.length) st.plugin = PLUGINS[0].id;
    var card = el('div','stepcard fl-node' + (st._open ? ' open' : ''));
    var head = el('div','fl-step-head');
    var info = typeInfo(st.type);
    head.appendChild(el('span','fl-num', String(i + 1)));
    var tt = el('div','fl-htxt');
    var tl = el('div','fl-t');
    tl.appendChild(el('span','fl-glyph', info[2]));
    tl.appendChild(document.createTextNode(info[1]));
    tt.appendChild(tl);
    tt.appendChild(el('div','fl-step-sum', stepSummary(st)));
    head.appendChild(tt); head.appendChild(el('span','fl-sp'));
    var tools = el('div','steptools');
    var ed = el('button', null, st._open ? 'Done' : 'Edit'); ed.type = 'button'; ed.className = 'btn small';
    ed.title = st._open ? 'Collapse' : 'Edit this step'; ed.setAttribute('aria-label', ed.title);
    ed.onclick = function(ev){ ev.stopPropagation(); st._open = !st._open; render(); };
    var up = el('button', null, '↑'); up.type = 'button'; up.className = 'btn small'; up.disabled = (i === 0); up.setAttribute('aria-label','Move step ' + (i + 1) + ' up');
    up.onclick = function(ev){ ev.stopPropagation(); var t = steps[i-1]; steps[i-1] = steps[i]; steps[i] = t; render(); sync(); };
    var dn = el('button', null, '↓'); dn.type = 'button'; dn.className = 'btn small'; dn.disabled = (i === steps.length - 1); dn.setAttribute('aria-label','Move step ' + (i + 1) + ' down');
    dn.onclick = function(ev){ ev.stopPropagation(); var t = steps[i+1]; steps[i+1] = steps[i]; steps[i] = t; render(); sync(); };
    var rm = el('button', null, '✕'); rm.type = 'button'; rm.className = 'btn small'; rm.title = 'Remove step'; rm.setAttribute('aria-label','Remove step ' + (i + 1));
    rm.onclick = function(ev){ ev.stopPropagation(); steps.splice(i, 1); render(); sync(); };
    tools.appendChild(ed); tools.appendChild(up); tools.appendChild(dn); tools.appendChild(rm);
    head.appendChild(tools);
    head.onclick = function(ev){ if(ev.target.closest('.steptools')) return; st._open = !st._open; render(); };
    card.appendChild(head);
    if(st._open) card.appendChild(fieldsFor(st));
    card.appendChild(el('span','fl-handle t'));
    card.appendChild(el('span','fl-handle b'));
    stepsEl.appendChild(card);
    if(i < steps.length - 1) stepsEl.appendChild(edge(i + 1));
  });
  sync();
}
function addStep(type){ insertStep(steps.length, type); }
if(!window.__mtFlOut){ window.__mtFlOut = 1;
document.addEventListener('click', function(ev){
  document.querySelectorAll('.fl-insert-menu.on').forEach(function(mm){
    if(!mm.contains(ev.target)){ mm.classList.remove('on'); }
  });
  document.querySelectorAll('.fl-ins.on').forEach(function(bb){ bb.classList.remove('on'); });
});
}
var e0 = document.getElementById('fl-edge-0');
if(e0){
  var b0 = el('button','fl-ins','+'); b0.type = 'button'; b0.setAttribute('aria-label','Add a filter');
  var m0 = el('div','fl-insert-menu');
  [['field','+ match text'],['category','+ ✦ AI category'],['topic','+ ✦ about (topic)']].forEach(function(t){
    var x = el('button','btn small', t[1]); x.type = 'button';
    x.onclick = function(ev){ ev.stopPropagation(); m0.classList.remove('on'); b0.classList.remove('on'); condAdd(t[0]); };
    m0.appendChild(x);
  });
  b0.onclick = function(ev){ ev.stopPropagation(); m0.classList.toggle('on'); b0.classList.toggle('on', m0.classList.contains('on')); };
  e0.appendChild(b0); e0.appendChild(m0);
}
document.querySelectorAll('.cond-row .k-sel').forEach(condKind);
document.querySelectorAll('.cond-row .k-val, .cond-row .k-score').forEach(function(i){
  i.addEventListener('input', function(){ condEmpty(); renderSummary(); });
});
document.querySelectorAll('input[name=match_mode]').forEach(function(r){ r.addEventListener('change', renderSummary); });
CATS.forEach(function(c){ var o = document.createElement('option'); o.value = c; document.getElementById('fl-cats').appendChild(o); });
var enBox = document.getElementById('f-enabled');
var stPill = document.getElementById('fl-state');
if(enBox && stPill){
  var paintState = function(){
    var on = enBox.checked;
    stPill.textContent = on ? 'Enabled' : 'Disabled';
    stPill.className = 'fl-state ' + (on ? 'on' : 'off');
  };
  enBox.addEventListener('change', paintState); paintState();
}
render(); condEmpty();
function condReset(row){
  row.querySelector('.k-val').value = '';
  row.querySelector('.k-score').value = '';
  row.querySelector('.k-sel').value = 'field';
  condKind(row.querySelector('.k-sel'));
  row.classList.add('extra');
}
window.mtCtxState = function(){
  if(!document.getElementById('flowform')) return '';
  var conds = [];
  document.querySelectorAll('.cond-row').forEach(function(row){
    var kind = row.querySelector('.k-sel').value;
    var val = row.querySelector('.k-val').value.trim();
    if(!val) return;
    var o = {kind: kind, value: val};
    var sc = row.querySelector('.k-score').value;
    if(kind === 'field'){
      o.field = row.querySelector('[name^="cond_field_"]').value;
      o.op = row.querySelector('[name^="cond_op_"]').value;
    } else if(sc){
      if(kind === 'category') o.min_confidence = parseFloat(sc);
      else o.threshold = parseFloat(sc);
    }
    conds.push(o);
  });
  var mm = document.querySelector('input[name=match_mode]:checked');
  return JSON.stringify({
    name: document.getElementById('f-name').value,
    enabled: document.getElementById('f-enabled').checked,
    match_mode: mm ? mm.value : 'all',
    conditions: conds,
    steps: steps.map(function(s){ var o = {}; for(var k in s){ if(k.charAt(0) !== '_') o[k] = s[k]; } return o; })
  });
};
window.mtFlowFill = function(fields, note){
  fields = fields || {};
  if(typeof fields.name === 'string'){ document.getElementById('f-name').value = fields.name; }
  if(typeof fields.enabled === 'boolean'){
    var cb = document.getElementById('f-enabled');
    cb.checked = fields.enabled;
    cb.dispatchEvent(new Event('change'));
  }
  if(fields.match_mode === 'all' || fields.match_mode === 'any'){
    document.querySelectorAll('input[name=match_mode]').forEach(function(r){ r.checked = (r.value === fields.match_mode); });
    renderSummary();
  }
  if(Array.isArray(fields.conditions)){
    var rows = Array.prototype.slice.call(document.querySelectorAll('.cond-row'));
    fields.conditions.forEach(function(c, i){
      var row = rows[i]; if(!row) return;
      var kind = c.kind || 'field';
      if(kind !== 'field' && kind !== 'category' && kind !== 'topic') kind = 'field';
      if(kind === 'field'){
        row.querySelector('[name^="cond_field_"]').value = c.field || 'subject';
        row.querySelector('[name^="cond_op_"]').value = c.op || 'contains';
      }
      row.querySelector('.k-val').value = (c.value == null) ? '' : String(c.value);
      row.querySelector('.k-score').value = (c.min_confidence != null) ? c.min_confidence
                                             : ((c.threshold != null) ? c.threshold : '');
      row.classList.remove('extra');
      row.querySelector('.k-sel').value = kind;
      condKind(row.querySelector('.k-sel'));
    });
    rows.slice(fields.conditions.length).forEach(condReset);
    condEmpty(); renderSummary();
  }
  if(Array.isArray(fields.steps)){
    steps = fields.steps.map(function(s){
      var o = {}; for(var k in s){ if(k.charAt(0) !== '_') o[k] = s[k]; }
      o._open = false; return o;
    });
    render();
  }
  var canvas = document.querySelector('.flowcanvas');
  if(canvas){
    canvas.style.transition = 'box-shadow .35s';
    canvas.style.boxShadow = '0 0 0 2px var(--acc)';
    setTimeout(function(){ canvas.style.boxShadow = ''; }, 1800);
  }
  if(note && window.toast) toast(note, 'ok');
};
</script>
"""



def _cond_friendly(c):
    """Plain-language bit for one WHEN condition (deterministic or fuzzy)."""
    kind = (c.get("kind") or "field").lower()
    if kind == "category":
        s = "AI category is ‘%s’" % c.get("value")
        if c.get("min_confidence"):
            s += " (≥%d%%)" % round(float(c["min_confidence"]) * 100)
        return s
    if kind == "topic":
        try:
            th = float(c.get("threshold") or 0.45)
        except (TypeError, ValueError):
            th = 0.45
        return "is about ‘%s’ (≥%.2f)" % (c.get("value"), th)
    return "%s %s ‘%s’" % (c.get("field"), c.get("op"), c.get("value"))


def _flow_summary(flow, tpl_names):
    """One-line plain language: IF <conditions> -> <steps>."""
    try:
        conds = json.loads(flow.get("conditions") or "[]")
    except (TypeError, ValueError):
        conds = []
    joiner = " and " if (flow.get("match_mode") or "all") == "all" else " or "
    when = joiner.join(_cond_friendly(c) for c in conds) or "—"
    try:
        steps = json.loads(flow.get("actions") or "[]")
    except (TypeError, ValueError):
        steps = []
    acts = []
    for st in steps:
        t = st.get("type")
        if t == "move":
            acts.append("move to %s" % st.get("folder"))
        elif t == "mark_read":
            acts.append("mark as read")
        elif t == "flag":
            acts.append("star")
        elif t == "tag":
            acts.append("tag ‘%s’" % st.get("tag"))
        elif t == "draft":
            dmode = (st.get("mode") or "template").lower()
            if dmode == "plugin":
                name = tpl_names.get(int(st.get("template_id") or 0), "")
                acts.append("draft via plugin ‘%s’%s and save to Drafts"
                            % (st.get("plugin") or "?",
                               (" using ‘%s’" % name) if name else ""))
            elif dmode == "llm":
                name = tpl_names.get(int(st.get("template_id") or 0), "")
                ins = (st.get("instructions") or "").strip()
                acts.append("draft with the LLM%s%s and save to Drafts"
                            % ((" using ‘%s’" % name) if name else "",
                               (" guided by ‘%s…’" % ins[:40]) if ins else ""))
            elif dmode == "fixed":
                body = (st.get("body") or "").strip()
                acts.append("draft ‘%s%s’ and save to Drafts"
                            % (body[:50], "…" if len(body) > 50 else ""))
            else:
                name = tpl_names.get(int(st.get("template_id") or 0), "")
                acts.append("draft from ‘%s’ and save to Drafts" % name if name else "draft (no template)")
    return "IF %s → %s" % (when, ", then ".join(acts) or "—")


def _flow_from_form():
    name = (request.form.get("name") or "").strip() or "Untitled flow"
    match_mode = request.form.get("match_mode", "all")
    conditions = []
    for i in range(5):
        val = (request.form.get("cond_value_%d" % i) or "").strip()
        if not val:
            continue
        kind = (request.form.get("cond_kind_%d" % i) or "field").lower()
        raw_score = (request.form.get("cond_score_%d" % i) or "").strip()
        try:
            score = float(raw_score) if raw_score else None
        except (TypeError, ValueError):
            score = None
        if kind == "category":
            cond = {"kind": "category", "value": val[:60]}
            if score and score > 0:
                cond["min_confidence"] = max(0.0, min(1.0, round(score, 2)))
            conditions.append(cond)
        elif kind == "topic":
            cond = {"kind": "topic", "value": val[:300]}
            if score:
                cond["threshold"] = max(0.2, min(0.95, round(score, 2)))
            conditions.append(cond)
        else:
            conditions.append({"field": request.form.get("cond_field_%d" % i, "subject"),
                               "op": request.form.get("cond_op_%d" % i, "contains"),
                               "value": val})
    try:
        raw = json.loads(request.form.get("steps_json") or "[]")
    except (TypeError, ValueError):
        raw = []
    steps = []
    for st in (raw if isinstance(raw, list) else [])[:20]:
        if not isinstance(st, dict):
            continue
        t = (st.get("type") or "").lower()
        if t == "move" and (st.get("folder") or "").strip():
            steps.append({"type": "move", "folder": st["folder"].strip()[:80]})
        elif t == "draft":
            dmode = (st.get("mode") or "template").lower()
            tid = st.get("template_id") or None
            try:
                tid = int(tid) if tid not in (None, "", "0") else None
            except (TypeError, ValueError):
                tid = None
            if dmode == "plugin":
                st2 = {"type": "draft", "mode": "plugin",
                       "plugin": (st.get("plugin") or "").strip()[:80]}
                if tid:
                    st2["template_id"] = tid
                ins = (st.get("instructions") or "").strip()
                if ins:
                    st2["instructions"] = ins[:1000]
                steps.append(st2)
            elif dmode == "fixed":
                body = (st.get("body") or "").strip()
                if body:
                    steps.append({"type": "draft", "mode": "fixed", "body": body[:4000]})
            elif dmode == "llm":
                st2 = {"type": "draft", "mode": "llm", "template_id": tid}
                ins = (st.get("instructions") or "").strip()
                if ins:
                    st2["instructions"] = ins[:1000]
                steps.append(st2)
            elif tid:
                steps.append({"type": "draft", "mode": "template", "template_id": tid})
        elif t == "tag" and (st.get("tag") or "").strip():
            steps.append({"type": "tag", "tag": st["tag"].strip()[:40]})
        elif t in ("mark_read", "flag"):
            steps.append({"type": t})
    enabled = bool(request.form.get("enabled"))
    return name, match_mode, conditions, steps, enabled


def _flow_edit_context(error, name, mode, conds, steps, enabled, is_new=False, seed_note=""):
    flow = {"name": name, "match_mode": mode, "enabled": enabled}
    settings = store.all_settings()
    when = engine._flow_when_text({"conditions": json.dumps(conds or []),
                                   "match_mode": mode}) if conds else ""
    acts = engine._flow_steps_text(steps or []) if steps else ""
    return {"flow": flow, "conditions": conds, "steps": steps or [],
            "error": error, "is_new": is_new, "seed_note": seed_note,
            "categories": settings.get("categories") or [],
            "watch_text": ", ".join(settings.get("watch_folders") or ["INBOX"]),
            "poll_interval": int(settings.get("poll_interval", 90) or 90),
            "summary_text": "new mail · %s → %s"
                            % (when or "no filters (every message)",
                               acts or "no steps yet"),
            "templates": store.list_templates(),
            "templates_meta": [{"id": x["id"], "name": x["name"]}
                               for x in store.list_templates()],
            "plugins_meta": [{"id": r["id"], "name": r["manifest"].get("name") or r["id"]}
                             for r in plugins.enabled_of_kind("draft-provider")]}


@app.route("/flows")
def flows():
    tpl_names = {t["id"]: t["name"] for t in store.list_templates()}
    rows = []
    for f in store.list_flows():
        d = dict(f)
        d["summary"] = _flow_summary(f, tpl_names)
        rows.append(d)
    return render(_render_src(FLOWS_TMPL, flows=rows,
                              test_id=request.args.get("test", type=int) or 0))


_SEED_FLOW_NOTE = (
    "This draft was seeded from a category. A category flow that processes a message takes "
    "precedence over default filing — including preview-mode flows and flows without a move "
    "step. Leaving this flow disabled preserves your present routing, and the global flow "
    "processing switch is independent of this flow's Enabled state. Review the filters and "
    "steps, then enable it and press Save flow.")


def _resolve_seed_category(query, settings):
    """Exact configured category, or an unambiguous case-insensitive match; else None."""
    cats = settings.get("categories")
    if not isinstance(cats, list):
        return None
    q = (query or "").strip()
    if not q:
        return None
    if q in cats:
        return q
    matches = [c for c in cats if isinstance(c, str) and c.lower() == q.lower()]
    return matches[0] if len(matches) == 1 else None


def _seed_flow(category, settings):
    """Server-derived (name, conditions, steps) for a category; disabled by default."""
    folders = settings.get("category_folders")
    folder = folders.get(category) if isinstance(folders, dict) else None
    mapped = bool(isinstance(folder, str) and folder.strip())
    name = ("File %s" % category) if mapped else ("%s automation" % category)
    conds = [{"kind": "category", "value": category, "min_confidence": 0.0}]
    steps = [{"type": "move", "folder": folder.strip()}] if mapped else []
    return name, conds, steps


@app.route("/flows/new", methods=["GET", "POST"])
def flow_new():
    if request.method == "POST":
        name, mode, conds, steps, enabled = _flow_from_form()
        if not conds or not steps:
            return render(_render_src(FLOW_EDIT_TMPL, **_flow_edit_context(
                error=("Add at least one filter with a value." if not conds else "Add at least one step."),
                name=name, mode=mode, conds=conds, steps=steps, enabled=enabled, is_new=True)))
        nid = store.add_flow(name, mode, conds, steps, enabled)
        store.log_event("info", "flow '%s' added (%d step(s))" % (name, len(steps)))
        flash("Flow added.", "ok")
        return redirect(url_for("flows", test=nid))
    if "category" in request.args:
        settings = store.all_settings()
        canonical = _resolve_seed_category(request.args.get("category"), settings)
        if not canonical:
            flash("Pick a configured category to build a flow for.", "err")
            return redirect(url_for("automation_categories"), code=303)
        name, conds, steps = _seed_flow(canonical, settings)
        note = _SEED_FLOW_NOTE if steps else _SEED_FLOW_NOTE + " Add a step before saving."
        return render(_render_src(FLOW_EDIT_TMPL, **_flow_edit_context(
            None, name, "all", conds, steps, False, is_new=True, seed_note=note)))
    return render(_render_src(FLOW_EDIT_TMPL,
                              **_flow_edit_context(None, "", "all", [], [], True, is_new=True)))


@app.route("/flows/<int:flow_id>/edit", methods=["GET", "POST"])
def flow_edit(flow_id):
    flow = store.get_flow(flow_id)
    if not flow:
        flash("No such flow.", "err")
        return redirect(url_for("flows"))
    if request.method == "POST":
        name, mode, conds, steps, enabled = _flow_from_form()
        if not conds or not steps:
            return render(_render_src(FLOW_EDIT_TMPL, **_flow_edit_context(
                error=("Add at least one filter with a value." if not conds else "Add at least one step."),
                name=name, mode=mode, conds=conds, steps=steps, enabled=enabled)))
        store.update_flow(flow_id, name=name, match_mode=mode,
                          conditions=json.dumps(conds), actions=json.dumps(steps),
                          enabled=1 if enabled else 0)
        flash("Flow saved.", "ok")
        return redirect(url_for("flows", test=flow_id))
    try:
        conds = json.loads(flow.get("conditions") or "[]")
    except (TypeError, ValueError):
        conds = []
    try:
        steps = json.loads(flow.get("actions") or "[]")
    except (TypeError, ValueError):
        steps = []
    return render(_render_src(FLOW_EDIT_TMPL, **_flow_edit_context(
        None, flow.get("name") or "", flow.get("match_mode") or "all",
        conds, steps, bool(flow.get("enabled")))))


@app.route("/flows/<int:flow_id>/toggle", methods=["POST"])
def flow_toggle(flow_id):
    flow = store.get_flow(flow_id)
    if flow:
        new_state = 0 if flow["enabled"] else 1
        store.update_flow(flow_id, enabled=new_state)
        store.log_event("info", "flow #%d '%s' %s (by ui)"
                        % (flow_id, flow.get("name"),
                           "enabled" if new_state else "disabled"))
        flash("Flow '%s' %s." % (flow.get("name") or flow_id,
                                 "enabled" if new_state else "disabled"), "ok")
    return redirect(url_for("flows"))


@app.route("/flows/<int:flow_id>/delete", methods=["POST"])
def flow_delete(flow_id):
    flow = store.get_flow(flow_id)
    store.delete_flow(flow_id)
    store.log_event("info", "flow #%d '%s' deleted (by ui)"
                    % (flow_id, (flow or {}).get("name") or "?"))
    flash("Flow '%s' deleted." % ((flow or {}).get("name") or flow_id), "ok")
    return redirect(url_for("flows"))


@app.route("/flows/<int:flow_id>/move", methods=["POST"])
def flow_move(flow_id):
    d = request.form.get("dir")
    store.move_flow(flow_id, -1 if d == "up" else 1)
    return redirect(url_for("flows"))


# ---------------------------------------------------------------- templates

TEMPLATES_TMPL = """
<div class="page-head">
  <div>
    <h1 class="page-title">Drafting</h1>
    <div class="page-desc">Reply templates and where drafts are saved. Used as guidance when the LLM drafts a reply — placeholders: {sender} {subject} {date} {my_name}{% if infill %}; blocks tagged {llm-infill}...{/llm-infill} are filled by the LLM Draft Infill plugin{% endif %}</div>
  </div>
  <div class="row"><a class="btn primary" href="{{ url_for('template_new') }}">New template</a></div>
</div>
<div class="card" id="draft-destination">
  <div class="card-h"><h3>Draft destination</h3><span class="sub">shared by every draft step and the assistant</span></div>
  <p class="sub" style="margin-top:0">Where flow and assistant drafts are saved. Blank follows the server's Drafts folder automatically.</p>
  <form method="post" action="{{ url_for('settings') }}">
    <input type="hidden" name="section" value="behavior">
    <input type="hidden" name="scope" value="Drafting">
    <input type="hidden" name="next" value="{{ url_for('templates') }}#draft-destination">
    <div class="setrow"><div class="st-l"><b>Drafts folder override</b><span class="sub">Blank = auto-detect the server's Drafts special-use folder.</span></div>
      <div class="st-c"><input type="text" name="drafts_folder" value="{{ s.drafts_folder }}" placeholder="Auto-detect" aria-label="Drafts folder override"></div></div>
    <div class="savebar"><button class="btn primary" type="submit">Save destination</button><span class="sub">{{ 'Using the saved override.' if s.drafts_folder else 'Auto-detect.' }}</span></div>
  </form>
</div>
<div class="card" id="draft-flows">
  <div class="card-h"><h3>Flows with draft steps</h3><span class="sub">each draft step saves to the destination above</span></div>
  {% if drafting.invalid_flows %}
  <div class="msg err">Some flows have unreadable actions and cannot be listed here:
    {% for f in drafting.invalid_flows %}<div><a href="{{ url_for('flow_edit', flow_id=f.id) }}">#{{ f.id }} {{ f.name }}</a> — {{ f.reason }}</div>{% endfor %}
  </div>
  {% endif %}
  {% if drafting.flows %}
  <div class="tablewrap"><table class="tbl">
    <thead><tr><th>Flow</th><th>State</th><th class="r"></th></tr></thead>
    <tbody>
    {% for f in drafting.flows %}
    <tr><td><a href="{{ url_for('flow_edit', flow_id=f.id) }}">{{ f.name or ('Flow #%d' % f.id) }}</a></td>
      <td>{% if f.enabled %}<span class="badge ok">Enabled</span>{% else %}<span class="badge">Disabled</span>{% endif %}</td>
      <td class="r"><a class="btn small" href="{{ url_for('flow_edit', flow_id=f.id) }}">Edit</a></td></tr>
    {% endfor %}
    </tbody></table></div>
  <p class="sub" style="margin:10px 0 0"><a href="{{ url_for('templates') }}#draft-destination">Draft destination</a> applies to all of these and to assistant drafts.</p>
  {% else %}
  <div class="empty"><h4>No flows produce drafts</h4><p>Add a Draft step in a flow to save replies from your templates automatically.</p><a class="btn" href="{{ url_for('flows') }}">Open flows</a></div>
  {% endif %}
</div>
<div class="card flush" id="templates">
  {% if templates %}
  <div class="tablewrap"><table class="tbl mcards">
    <thead><tr><th>name</th><th>preview</th><th class="r"></th></tr></thead>
    <tbody>
    {% for t in templates %}
    <tr>
      <td><a href="{{ url_for('template_edit', tid=t.id) }}">{{ t.name }}</a></td>
      <td class="sub">{{ t.body[:120] }}</td>
      <td class="r"><span class="rowacts" style="justify-content:flex-end">
        <a class="btn small ra-inline" href="{{ url_for('template_edit', tid=t.id) }}">Edit</a>
        <form class="inline ra-inline" method="post" action="{{ url_for('template_delete', tid=t.id) }}">
          <button class="btn small danger arm-del" type="submit" data-arm-label="Press again to delete template {{ t.name }}" aria-label="Delete template {{ t.name }}">Delete</button></form>
        <details class="menu ra-menu">
          <summary class="btn small" aria-haspopup="menu" aria-label="More actions">⋯</summary>
          <div class="menu-pop" role="menu">
            <a class="menu-item" href="{{ url_for('template_edit', tid=t.id) }}">Edit</a>
            <form method="post" action="{{ url_for('template_delete', tid=t.id) }}">
              <button class="menu-item danger arm-del" type="submit" data-arm-label="Press again to delete template {{ t.name }}" aria-label="Delete template {{ t.name }}">Delete…</button></form>
          </div>
        </details>
      </span></td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% else %}
  <div class="empty">
    <h4>No templates yet</h4>
    <p>Start from one of your recurring replies, e.g. a short acknowledgment — the assistant can reuse it when drafting.</p>
    <a class="btn primary" href="{{ url_for('template_new') }}">New template</a>
  </div>
  {% endif %}
</div>
"""


TEMPLATE_EDIT_TMPL = """
<div class="page-head">
  <div>
    <div class="backlink"><a href="{{ url_for('templates') }}">← Templates</a></div>
    <h1 class="page-title">{{ 'Edit template' if template else 'New template' }}</h1>
    <div class="page-desc">Used as guidance when the LLM drafts a reply.</div>
  </div>
</div>
<form method="post">
  <div class="card">
    <div class="card-h"><h3>Template</h3></div>
    <div class="grid2">
      <div><label for="t-name">Name</label><input id="t-name" type="text" name="name" value="{{ template.name if template else '' }}" placeholder="e.g. Meeting ack"></div>
      <div><label for="t-subj">Subject <span class="sub">(optional)</span></label><input id="t-subj" type="text" name="subject" value="{{ template.subject if template else '' }}" placeholder="Re: {subject}"></div>
    </div>
  </div>
  <div class="card">
    <div class="card-h"><h3 id="t-body-h">Body</h3><span class="sub">keep it short — the LLM adapts it to the actual email</span></div>
    <div class="sub" id="t-body-help" style="margin-bottom:6px">Placeholders: <span class="mono">{sender}</span> <span class="mono">{subject}</span> <span class="mono">{date}</span> <span class="mono">{my_name}</span> are filled in from the message.{% if infill %}<br id="t-infill-help">With the <b>LLM Draft Infill</b> plugin (flow draft step &rarr; plugin), <span class="mono">{llm-infill}what the LLM should write here{/llm-infill}</span> blocks are written by the LLM and everything around them stays exactly as typed.{% endif %}</div>
    <textarea id="t-body" name="body" rows="10" aria-labelledby="t-body-h" aria-describedby="t-body-help">{{ template.body if template else '' }}</textarea>
  </div>
  <div class="savebar"><button class="btn primary" type="submit">Save template</button><a class="btn" href="{{ url_for('templates') }}">Cancel</a></div>
</form>
"""






def _infill_plugin_enabled():
    """True when the bundled LLM Draft Infill plugin is enabled; drives the
    {llm-infill} hint on the templates pages."""
    try:
        row = plugins.get("mt-llm-infill")
        return bool(row and row.get("enabled"))
    except Exception:
        return False


@app.route("/templates")
def templates():
    return render(_render_src(TEMPLATES_TMPL, templates=store.list_templates(),
                              infill=_infill_plugin_enabled(),
                              s=store.all_settings(),
                              drafting=ux.drafting_flows(store.list_flows())))


@app.route("/templates/new", methods=["GET", "POST"])
def template_new():
    if request.method == "POST":
        store.add_template((request.form.get("name") or "").strip() or "Untitled",
                           (request.form.get("subject") or "").strip(),
                           request.form.get("body") or "")
        flash("Template added.", "ok")
        return redirect(url_for("templates"))
    return render(_render_src(TEMPLATE_EDIT_TMPL, template=None,
                              infill=_infill_plugin_enabled()))


@app.route("/templates/<int:tid>/edit", methods=["GET", "POST"])
def template_edit(tid):
    t = store.get_template(tid)
    if not t:
        flash("No such template.", "err")
        return redirect(url_for("templates"))
    if request.method == "POST":
        store.update_template(tid, (request.form.get("name") or "").strip() or "Untitled",
                              (request.form.get("subject") or "").strip(),
                              request.form.get("body") or "")
        flash("Template saved.", "ok")
        return redirect(url_for("templates"))
    return render(_render_src(TEMPLATE_EDIT_TMPL, template=t,
                              infill=_infill_plugin_enabled()))


@app.route("/templates/<int:tid>/delete", methods=["POST"])
def template_delete(tid):
    tpl = store.get_template(tid)
    store.delete_template(tid)
    if tpl:
        store.log_event("info", "template '%s' deleted (by ui)" % (tpl.get("name") or tid))
    flash("Template '%s' deleted." % ((tpl or {}).get("name") or tid), "ok")
    return redirect(url_for("templates"))


# ---------------------------------------------------------------- messages

MESSAGES_TMPL = """
<style>
.mailtbl{table-layout:fixed;width:100%}.mailtbl th:nth-child(1){width:34px}.mailtbl th:nth-child(2){width:104px}.mailtbl th:nth-child(3){width:170px}.mailtbl th:nth-child(5){width:80px}.mailtbl th:nth-child(6){width:116px}.mailtbl th:nth-child(7){width:100px}
.mailtbl td{overflow-wrap:anywhere}.mailtbl td:nth-child(3){overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mailtbl td:nth-child(4) a{display:block;font-weight:600}.mailtbl td:nth-child(4) .sub{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-top:3px}
@media(min-width:768px) and (max-width:1399px){.mailtbl th:nth-child(3){width:140px}.mailtbl th:nth-child(5),.mailtbl td:nth-child(5){display:none}.mailtbl th:nth-child(6){width:108px}.mailtbl th:nth-child(7){width:90px}}
@media(max-width:767px){.mailtbl{table-layout:auto}}
</style>
<div class="page-head">
  <div>
    <h1 class="page-title">Messages</h1>
    <div class="page-desc">Newest first — select rows to tag or classify in bulk.</div>
  </div>
  <div class="row"><span class="sub">{{ total }} message{{ 's' if total != 1 else '' }} · page {{ page }} of {{ pages }}</span></div>
</div>

{% if proposals %}
<div class="card">
  <div class="card-h"><h3>Rules proposed from your tags <span class="sub">— review, then add with one click</span></h3></div>
  {% for p in proposals %}
  <div class="proposal" style="margin:8px 0">
    <div class="p-tag">✦ Proposed rule · from your tags</div>
    <div class="spread">
      <div><b>{{ p.rule_obj.name }}</b> <span class="sub">({{ p.rule_obj.match_mode }})</span>{% if p.rule_obj.placement == 'top' %} <span class="badge acc">added at top</span>{% endif %}{% if p.similar %} <span class="badge warn">overlaps #{{ p.similar.id }}</span>{% endif %}</div>
      <div class="row" style="white-space:nowrap">
        {% if p.similar %}<form class="inline" method="post" action="{{ url_for('proposal_apply', pid=p.id) }}"><input type="hidden" name="mode" value="update"><input type="hidden" name="rule_id" value="{{ p.similar.id }}"><button class="btn small primary" type="submit">Update rule #{{ p.similar.id }}</button></form>{% endif %}
        <form class="inline" method="post" action="{{ url_for('proposal_apply', pid=p.id) }}"><button class="btn small{{ '' if p.similar else ' primary' }}" type="submit">Add rule</button></form>
        <form class="inline" method="post" action="{{ url_for('proposal_apply', pid=p.id) }}"><input type="hidden" name="disabled" value="1"><button class="btn small" type="submit">Add (disabled)</button></form>
        <form class="inline" method="post" action="{{ url_for('proposal_dismiss', pid=p.id) }}"><button class="btn small" type="submit">Dismiss</button></form>
      </div>
    </div>
    {% if p.similar %}<div class="note" style="border-color:var(--warn);color:var(--warn);margin-top:8px">⚠ Similar rule exists: #{{ p.similar.id }} "{{ p.similar.name }}"{% if not p.similar.enabled %} (disabled){% endif %} — {{ p.similar_actions }}. Updating it avoids a duplicate.</div>{% endif %}
    <div class="mono" style="font-size:.83rem;margin-top:6px">{{ p.cond_text }}</div>
    <div class="sub">{{ p.act_text }}{% if p.rule_obj.rationale %} — {{ p.rule_obj.rationale }}{% endif %}</div>
  </div>
  {% endfor %}
</div>
{% endif %}

{% if classify_state.running %}
<div class="card">
  <div class="spread">
    <div class="row" style="gap:10px">
      <span class="badge acc">classifying…</span>
      <span class="sub">{{ classify_state.done }}/{{ classify_state.total }}{% if classify_state.failed %} · {{ classify_state.failed }} failed{% endif %}{% if classify_state.concurrency %} · {{ classify_state.concurrency }} at a time{% endif %}{% if classify_state.current %} · now: {{ classify_state.current }}{% endif %}</span>
    </div>
    <form class="inline" method="post" action="{{ url_for('classify_stop') }}"><button class="btn small danger" type="submit">Stop</button></form>
  </div>
  {% if classify_state.total %}
  <div class="progress" style="margin-top:10px" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{{ (classify_state.done * 100 / classify_state.total)|round|int }}" aria-label="Classification progress"><i style="width:{{ (classify_state.done * 100 / classify_state.total)|round|int }}%"></i></div>
  {% endif %}
</div>
<script>(function(){ var me=location.pathname; (function r(){ setTimeout(function(){ if(location.pathname!==me) return; if(document.hidden){ r(); } else { location.reload(); } }, 10000); })(); })();</script>
{% endif %}

<form method="get" class="card" aria-label="Search and filter mail">
  <input type="hidden" name="f" value="{{ filt }}">
  <input type="hidden" name="per" value="{{ per }}">
  <div class="ux-search"><label for="mail-q">Search mail<input id="mail-q" type="search" name="q" value="{{ search_form.q }}" placeholder="Sender, subject, text…"></label><button class="btn primary" type="submit">Search</button>{% if search_active %}<a class="btn" href="{{ url_for('messages', f=filt) }}">Clear filters</a>{% endif %}</div>
  <details class="ux-filters" {{ 'open' if search_advanced else '' }}><summary>More filters{% if search_active %} · applied{% endif %}</summary>
    <div class="grid3">
      <div><label for="mail-sender">Sender</label><input id="mail-sender" name="sender" value="{{ search_form.sender }}" placeholder="name@example.com"></div>
      <div><label for="mail-category">Category</label><select id="mail-category" name="category"><option value="">All categories</option>{% for c in categories %}<option {{ 'selected' if search_form.category==c else '' }}>{{ c }}</option>{% endfor %}</select></div>
      <div><label for="mail-folder">Folder</label><select id="mail-folder" name="folder"><option value="">All folders</option>{% for folder in folders %}<option {{ 'selected' if search_form.folder==folder else '' }}>{{ folder }}</option>{% endfor %}</select></div>
      <div><label for="mail-after">From date</label><input id="mail-after" type="date" name="after" value="{{ search_form.after }}"></div>
      <div><label for="mail-before">Through date</label><input id="mail-before" type="date" name="before" value="{{ search_form.before }}"></div>
    </div><button class="btn small" type="submit" style="margin-top:10px">Apply filters</button>
  </details>
</form>
<div class="card flush">
  <form id="bulk" method="post">
    <input type="hidden" name="f" value="{{ filt }}">
    <div class="toolbar">
      <span class="tchips">
      {% for key, label, n in filter_chips %}
      <a class="chip{{ ' active' if filt==key else '' }}" href="{{ url_for('messages', f=key, **search_form) }}">{{ label }} <span class="n">{{ n }}</span></a>
      {% endfor %}
      </span>
      <span class="tactions">
      {% if unclassified or classify_state.running %}<button class="btn small" type="submit" formaction="{{ url_for('messages_classify_all') }}" {{ 'disabled' if classify_state.running else '' }}>Classify all<span class="mhide"> unclassified</span> ({{ unclassified }})</button>{% endif %}
      {% if tagged_count %}
      <button class="btn small" type="submit" formaction="{{ url_for('learn_rules') }}" {{ 'disabled' if not tagged_count else '' }} title="{{ 'Nothing to learn from yet — tag some messages first' if not tagged_count else '' }}">Learn rules<span class="mhide"> from tags</span> ({{ tagged_count }})</button>
      {% endif %}
      </span>
    </div>
    <div class="bulkbar" id="bulkbar" role="region" aria-label="Bulk actions">
      <span class="n" id="bulkcount">0 selected</span>
      <button class="btn small" type="button" id="bulkclear">Clear</button>
      <input type="text" name="tag" list="taglist" placeholder="tag selected as…" aria-label="Tag"
             style="height:28px;width:190px;background:#141414;color:#fff;border-color:#3f3f46">
      <datalist id="taglist">{% for c in tag_options %}<option value="{{ c }}">{% endfor %}</datalist>
      <button class="btn small" type="submit" formaction="{{ url_for('messages_tag') }}">Tag</button>
      <button class="btn small" type="submit" formaction="{{ url_for('messages_untag') }}">Untag</button>
      <button class="btn small" type="submit" formaction="{{ url_for('messages_needs_reply') }}">No reply<span class="mhide"> needed</span></button>
      <button class="btn small primary" type="submit" formaction="{{ url_for('messages_classify') }}">Classify selected</button>
    </div>
    {% if msgs %}
    <div class="tablewrap"><table class="tbl mcards mailtbl">
      <thead><tr>
        <th class="sel"><input type="checkbox" id="selall" aria-label="Select all on this page"></th>
        <th>date</th><th>from</th><th>subject</th><th>tag</th><th>status</th><th>category</th>
      </tr></thead>
      <tbody>
      {% for m in msgs %}
      <tr>
        <td class="sel"><input type="checkbox" name="ids" value="{{ m.id }}" aria-label="Select message"></td>
        <td class="sub mono" style="background:none;border:0;font-size:.77rem">{{ m.when }}</td>
        <td class="sub" title="{{ m.from_addr }}">{{ m.from_addr|clip(34) }}</td>
        <td><a href="{{ url_for('message_detail', mid=m.id, f=filt, **search_form) }}" title="{{ m.subject }}">{{ m.subject|clip(84) or '(no subject)' }}</a>
          {% if m.llm_summary %}<div class="sub" style="font-size:.78rem" title="{{ m.llm_summary }}">{{ m.llm_summary|clip(150) }}</div>{% endif %}</td>
        <td>{% if m.user_tag %}<span class="badge warn">{{ m.user_tag }}</span>{% endif %}</td>
        <td><span class="badge {{ m.badge[0] }}">{{ m.badge[1] }}</span>{% if m.snoozed_active %} <span class="badge warn" title="until {{ m.snoozed_h }}">snoozed</span>{% endif %}{% if m.action %} <span class="sub">{{ m.action }}</span>{% endif %}</td>
        <td class="sub">{{ m.llm }}</td>
      </tr>
      {% endfor %}
      </tbody></table></div>
    {% else %}
    <div class="empty">
      <h4>No messages{% if filt != 'all' or search_active %} match these filters{% endif %}</h4>
      <p>{% if filt == 'all' and not search_active %}Mail shows up here after the watcher's first pass.{% else %}Try a different search or clear your filters.{% endif %}</p>
      {% if filt == 'all' and not search_active %}<button class="btn primary" type="submit" formaction="{{ url_for('check_now') }}">Check now</button>
      {% else %}<a class="btn" href="{{ url_for('messages') }}">Show all messages</a>{% endif %}
    </div>
    {% endif %}
    <div class="pager">
      {% if page > 1 %}<a class="btn small" href="{{ url_for('messages', f=filt, page=page-1, per=per, **search_form) }}">← Newer</a>{% endif %}
      {% if page < pages %}<a class="btn small primary" href="{{ url_for('messages', f=filt, page=page+1, per=per, **search_form) }}">Older →</a>{% endif %}
      {% if page < pages %}<a class="btn small plast" href="{{ url_for('messages', f=filt, page=pages, per=per, **search_form) }}">Last »</a>{% endif %}
      <span class="sub pageno">page {{ page }} of {{ pages }}</span>
      <span class="sub pp" style="margin-left:auto">per page:
        {% for n in [50, 100, 250, 500] %}<a class="chip{{ ' active' if per==n else '' }}" style="height:24px;padding:0 8px" href="{{ url_for('messages', f=filt, page=1, per=n, **search_form) }}">{{ n }}</a>{% endfor %}
      </span>
    </div>
  </form>
</div>
<script>
(function(){
  var form = document.getElementById('bulk');
  if(!form) return;
  var bar = document.getElementById('bulkbar');
  var count = document.getElementById('bulkcount');
  var all = document.getElementById('selall');
  function boxes(){ return Array.prototype.slice.call(form.querySelectorAll('input[name=ids]')); }
  function update(){
    var n = boxes().filter(function(b){ return b.checked; }).length;
    if(bar) bar.classList.toggle('on', n > 0);
    if(count) count.textContent = n + ' selected';
    if(all) all.checked = n > 0 && n === boxes().length;
  }
  if(all) all.addEventListener('change', function(){ boxes().forEach(function(b){ b.checked = all.checked; }); update(); });
  form.addEventListener('change', function(e){ if(e.target && e.target.name === 'ids') update(); });
  var clear = document.getElementById('bulkclear');
  if(clear) clear.addEventListener('click', function(){ boxes().forEach(function(b){ b.checked = false; }); update(); });
})();
</script>
"""




def _proposal_views():
    out = []
    for p in store.list_rule_proposals():
        ro = p["rule_obj"]
        sim = ro.get("similar_rule") or None
        out.append({
            "id": p["id"],
            "rule_obj": ro,
            "cond_text": summarize_conditions({"conditions": json.dumps(ro.get("conditions") or []),
                                                "match_mode": ro.get("match_mode") or "all"}),
            "act_text": summarize_actions({"actions": json.dumps(ro.get("actions") or {})}),
            "similar": sim,
            "similar_actions": summarize_actions({"actions": json.dumps((sim or {}).get("actions", {}))}) if sim else "",
        })
    return out


@app.route("/messages")
def messages():
    filt = request.args.get("f", "all")
    search_form, search = _mail_search()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        per = int(request.args.get("per", 100))
    except ValueError:
        per = 100
    per = max(10, min(500, per))
    total = store.count_messages(filt, search=search)
    pages = max(1, (total + per - 1) // per)
    page = min(page, pages)
    msgs = store.messages(limit=per, filt=filt, offset=(page - 1) * per, search=search)
    for m in msgs:
        m["when"] = fmt_ts(m.get("date_ts") or m.get("processed_at"))
        m["badge"] = STATUS_BADGES.get(m.get("status"), ("", m.get("status", "")))
        m["llm"] = _category_caption(m)
        su = m.get("snoozed_until") or 0
        m["snoozed_active"] = bool(su and su > time.time())
        m["snoozed_h"] = fmt_ts(su) if su else ""
    settings = store.all_settings()
    tag_options = sorted(set(
        list(settings.get("categories") or [])
        + list((settings.get("category_folders") or {}).values())
        + [json.loads(r.get("actions") or "{}").get("move_to", "")
           for r in store.list_rules() if r.get("enabled")]))
    tag_options = [t for t in tag_options if t]
    filter_chips = [(key, label, store.count_messages(key, search=search)) for key, label in (
        ("all", "All"), ("queued", "Awaiting LLM"), ("needs_reply", "Needs reply"),
        ("moved", "Sorted"), ("tagged", "Tagged"), ("snoozed", "Snoozed"),
        ("errors", "Errors"))]
    return render(_render_src(
        MESSAGES_TMPL, msgs=msgs, filt=filt, page=page, pages=pages, per=per, total=total,
        proposals=_proposal_views(),
        classify_state=dict(classifier.state),
        unclassified=store.unclassified_count(),
        tagged_count=len(store.tagged_examples(1000)),
        tag_options=tag_options, filter_chips=filter_chips,
        search_form=search_form, search_active=any(search_form.values()),
        search_advanced=any(v for k, v in search_form.items() if k != 'q'),
        categories=settings.get('categories') or [], folders=_mail_folders()))


def _mail_search():
    form = {k: (request.args.get(k) or '').strip()[:200]
            for k in ('q', 'sender', 'category', 'folder', 'after', 'before')}
    search = {k: form[k] for k in ('q', 'sender', 'category', 'folder')}
    from datetime import datetime, timezone
    offset = tz_offset_hours() * 3600
    for key in ('after', 'before'):
        if form[key]:
            try:
                search[key] = datetime.strptime(form[key], '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() - offset
                if key == 'before':
                    search[key] += 86400
            except ValueError:
                form[key] = ''
                flash('Invalid date filter ignored; use YYYY-MM-DD.', 'warn')
    return form, search


def _mail_folders():
    with store.db() as conn:
        return [r[0] for r in conn.execute('SELECT DISTINCT folder FROM messages ORDER BY folder') if r[0]]


def _category_caption(message):
    caption = message.get('llm_category') or ''
    if not caption:
        return ''
    if message.get('classified_by') == 'user':
        return caption + ' · your correction'
    if message.get('llm_confidence') is not None:
        caption += ' (%.0f%%)' % (message['llm_confidence'] * 100)
    return caption


@app.route("/messages/tag", methods=["POST"])
def messages_tag():
    ids = request.form.getlist("ids")
    tag = (request.form.get("tag") or "").strip()[:40]
    if not ids or not tag:
        flash("Select at least one message and type a tag.", "err")
    else:
        n = store.tag_messages(ids, tag)
        store.log_event("info", "tagged %d message(s) as '%s'" % (n, tag))
        flash("Tagged %d message(s) as '%s'." % (n, tag), "ok")
    return redirect(url_for("messages"))


@app.route("/messages/untag", methods=["POST"])
def messages_untag():
    ids = request.form.getlist("ids")
    if not ids:
        flash("Select at least one message first.", "err")
    else:
        n = store.untag_messages(ids)
        flash("Cleared tags on %d message(s)." % n, "ok")
    return redirect(url_for("messages"))


@app.route("/messages/needs-reply", methods=["POST"])
def messages_needs_reply():
    ids = request.form.getlist("ids")
    f = (request.form.get("f") or "all").strip()
    if f not in ("all", "queued", "needs_reply", "moved", "tagged", "snoozed", "errors"):
        f = "all"
    if not ids:
        flash("Select at least one message first.", "err")
        return redirect(url_for("messages", f=(None if f == "all" else f)))
    n, changed = store.clear_needs_reply(ids)
    for mid in changed:
        store.log_msg_event(mid, "needs_reply", "cleared (by ui)")
        learning.observe(mid, "needs_reply", "cleared", source="ui")
        store.record_label(mid, "needs_reply", "0", source="explicit_user_correction",
                           source_detail="cleared in the UI")
    if n:
        store.log_event("info", "cleared needs-reply on %d message(s) (by ui)" % n)
        flash("Cleared needs-reply on %d message%s." % (n, "" if n == 1 else "s"), "ok")
    else:
        flash("Nothing to clear - those messages were not flagged.", "warn")
    return redirect(url_for("messages", f=(None if f == "all" else f)))


@app.route("/messages/classify", methods=["POST"])
def messages_classify():
    ids = request.form.getlist("ids")
    if not ids:
        flash("Select at least one message first.", "err")
    else:
        classifier.trigger(ids=ids)
        flash("Classifying %d selected message(s) — progress shows above the list." % len(ids), "ok")
    return redirect(url_for("messages"))


@app.route("/messages/classify-all", methods=["POST"])
def messages_classify_all():
    classifier.trigger()
    flash("Classify-all started — it works newest-first and shows progress here.", "ok")
    return redirect(url_for("messages"))


@app.route("/classify/stop", methods=["POST"])
def classify_stop():
    classifier.request_stop()
    flash("Stopping after the current message…", "ok")
    return redirect(url_for("messages"))


@app.route("/classify/status")
def classify_status():
    return jsonify(classifier.state)


@app.route("/learn-rules", methods=["POST"])
def learn_rules():
    try:
        reply, rules = engine.propose_rules_from_tags()
        for r in rules:
            store.add_rule_proposal("tags", r, reply)
        if rules:
            flash("Proposed %d rule(s) from your tags%s"
                  % (len(rules), (" — " + reply) if reply else ""), "ok")
        else:
            flash("The model found no generalisable rules in your tags yet%s"
                  % ((": " + reply) if reply else "."), "err")
    except Exception as exc:
        flash("Learn failed: %r" % exc, "err")
    return redirect(url_for("messages"))


@app.route("/proposals/<int:pid>/apply", methods=["POST"])
def proposal_apply(pid):
    row = store.get_rule_proposal(pid)
    if not row or row.get("applied"):
        flash("That proposal is no longer available.", "err")
        return redirect(url_for("messages"))
    try:
        rule = json.loads(row.get("rule") or "{}")
    except (TypeError, ValueError):
        rule = {}
    norm = engine.normalize_rule(rule) or rule
    if not norm.get("conditions"):
        flash("That proposal is not valid any more.", "err")
        return redirect(url_for("messages"))
    disabled = bool(request.form.get("disabled"))
    mode = (request.form.get("mode") or "add").strip()
    if mode == "update":
        try:
            rid = int(request.form.get("rule_id") or 0)
        except ValueError:
            rid = 0
        target = store.get_rule(rid) if rid else None
        if target is None:
            flash("That rule no longer exists - nothing updated.", "err")
            return redirect(url_for("messages"))
        store.update_rule(rid, name=norm.get("name") or target["name"],
                          match_mode=norm.get("match_mode", "all"),
                          conditions=json.dumps(norm.get("conditions", [])),
                          actions=json.dumps(norm.get("actions", {})))
        store.mark_rule_proposal_applied(pid)
        store.log_event("info", "rule learned from tags updated #%d '%s'" % (rid, target["name"]))
        flash("Rule #%d '%s' updated from your tags - no duplicate added." % (rid, target["name"]), "ok")
        return redirect(url_for("messages"))
    store.add_rule(norm.get("name", "Learned rule"), norm.get("match_mode", "all"),
                   norm.get("conditions", []), norm.get("actions", {}),
                   enabled=not disabled, position=norm.get("placement") or "bottom")
    store.mark_rule_proposal_applied(pid)
    store.log_event("info", "rule '%s' added from tag learning (%s)"
                    % (norm.get("name"), "disabled" if disabled else "enabled"))
    flash("Rule '%s' added%s — check it on the Rules page."
          % (norm.get("name"), " (disabled)" if disabled else ""), "ok")
    return redirect(url_for("messages"))


@app.route("/proposals/<int:pid>/dismiss", methods=["POST"])
def proposal_dismiss(pid):
    store.mark_rule_proposal_applied(pid)
    return redirect(url_for("messages"))

MESSAGE_TMPL = """
<style>
.msgrid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:14px;align-items:start}
.msgrid .stickycol{position:sticky;top:14px;display:flex;flex-direction:column;gap:14px}
.msgrid .card{margin-top:0}
@media(max-width:1023px){.msgrid{grid-template-columns:1fr}.msgrid .stickycol{position:static}}
.msgbody{white-space:pre-wrap;margin:10px 0 2px;font-size:.94rem;line-height:1.65;overflow-wrap:anywhere;max-width:72ch}
.msgbody a{text-decoration:underline;text-underline-offset:2px}
.quote{margin-top:16px;border-left:2px solid var(--line);padding-left:12px;color:var(--dim);font-size:.88rem}
.quote summary{cursor:pointer}
.quote .qbody{white-space:pre-wrap;margin-top:8px}
.msgfrom{overflow-wrap:anywhere}
.emailtools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 0}
.emailtools .sp{flex:1}
.emailbody{margin:12px 0 2px;border:1px solid var(--line);background:#fff;padding:16px 18px;overflow-x:auto;font-size:.94rem;line-height:1.6;color:var(--ink)}
.emailbody img{max-width:100%;height:auto}
.emailbody table{max-width:100%;border-collapse:collapse}
.emailbody a{text-decoration:underline;text-underline-offset:2px}
.emailbody h1{font-size:1.35em;margin:.6em 0 .4em}.emailbody h2{font-size:1.2em;margin:.6em 0 .4em}.emailbody h3{font-size:1.05em;margin:.5em 0 .3em}
.emailbody blockquote{margin:10px 0;padding-left:12px;border-left:2px solid var(--line);color:var(--dim)}
.emailbody p{margin:0 0 .7em}
.message-workbench{position:sticky;top:0;z-index:20;background:var(--card);border:1px solid var(--line);padding:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.message-workbench .btn{min-height:38px}.message-workbench details{position:relative}.message-workbench details>div{position:absolute;top:100%;right:0;background:var(--card);border:1px solid var(--line);padding:10px;min-width:190px;z-index:21}
.reply-panel{margin:0 0 14px}.reply-panel>summary{font-weight:600;cursor:pointer}.reply-panel form{margin-top:12px}
.msg-disclosure>summary{cursor:pointer;font-weight:600;padding-bottom:8px}
.category-correction{margin:10px 0}.category-correction label{margin:0}.category-correction select{width:auto;min-width:140px}
@media(max-width:767px){.message-workbench .btn{min-height:44px}.message-workbench details>div{left:0;right:auto}.message-workbench{gap:6px}.msgrid{gap:10px}}
@media(max-width:767px){.message-workbench{top:calc(env(safe-area-inset-top) + 45px)}}
@media(min-width:768px) and (max-width:1023px){.message-workbench{top:calc(env(safe-area-inset-top) + 56px)}}
</style>
<div class="page-head">
  <div style="min-width:0">
    <div class="backlink"><a href="{{ url_for('messages', f=filt, **search_form) }}">← Messages{{ ' (' + filt.replace('_', ' ') + ')' if filt != 'all' else '' }}</a></div>
    <h1 class="page-title" style="font-size:1.12rem">{{ m.subject[:100] or '(no subject)' }}</h1>
    <div class="page-desc msgfrom">{{ m.from_addr }} · <span title="{{ m.date }}">{{ m.date_disp or m.date }}</span> · {{ m.folder }}</div>
  </div>
  <div class="row">
    <span class="badge {{ m.badge[0] }}">{{ m.badge[1] }}</span>
    {% if m.llm_needs_reply %}<span class="badge warn">needs reply</span>{% endif %}
    {% if m.snoozed_active %}<span class="badge warn">snoozed until {{ m.snoozed_h }}</span>{% endif %}
  </div>
</div>

{% set can_file = m.llm_suggested_folder and not (m.action_taken or '').startswith('move') %}
<div class="qbar">
  {% if prev_id %}<a class="btn small" href="{{ url_for('message_detail', mid=prev_id, f=filt, **search_form) }}">← Newer</a>
  {% else %}<span class="btn small qoff">← Newer</span>{% endif %}
  {% if next_id %}<a class="btn small" href="{{ url_for('message_detail', mid=next_id, f=filt, **search_form) }}">Older →</a>
  {% else %}<span class="btn small qoff">Older →</span>{% endif %}
  <span class="sp"></span>
  {% if can_file %}<form class="inline" method="post" action="{{ url_for('message_file', mid=m.id) }}"><input type="hidden" name="next" value="1"><input type="hidden" name="f" value="{{ filt }}"><button class="btn primary small" type="submit">File &amp; next</button></form>{% endif %}
  {% if m.llm_needs_reply %}<form class="inline" method="post" action="{{ url_for('message_needs_reply', mid=m.id, **search_form) }}"><input type="hidden" name="next" value="1"><input type="hidden" name="f" value="{{ filt }}"><button class="btn small" type="submit">No reply needed &amp; next</button></form>{% endif %}
</div>
<div class="message-workbench" aria-label="Message actions">
  <button class="btn primary" type="button" data-open-reply>Draft reply</button>
  {% if m.snoozed_active %}<form class="inline" method="post" action="{{ url_for('message_snooze', mid=m.id) }}"><input type="hidden" name="hours" value="0"><button class="btn" type="submit">Wake now</button></form>
  {% else %}<details><summary class="btn">Snooze</summary><div class="stack">{% for h,label in [(24,'1 day'),(72,'3 days'),(168,'1 week')] %}<form method="post" action="{{ url_for('message_snooze', mid=m.id) }}"><input type="hidden" name="hours" value="{{ h }}"><button class="btn" type="submit">{{ label }}</button></form>{% endfor %}</div></details>{% endif %}
  <form class="inline" method="post" action="{{ url_for('message_needs_reply', mid=m.id) }}"><input type="hidden" name="value" value="{{ 0 if m.llm_needs_reply else 1 }}"><button class="btn" type="submit">{{ 'No reply needed' if m.llm_needs_reply else 'Needs reply' }}</button></form>
</div>
<details class="card reply-panel" id="reply-composer" {{ 'open' if draft or draft_error else '' }}>
  <summary>Replies · draft and review</summary>
  <p class="sub">Drafts land in your Drafts folder — nothing is sent automatically.</p>
  <form method="post" action="{{ url_for('message_draft', mid=m.id, f=filt, **search_form) }}" class="row">
    <select name="template_id" aria-label="Reply template" style="width:auto;max-width:100%"><option value="">No template — freeform</option>{% for t in templates %}<option value="{{ t.id }}" {{ 'selected' if draft_template_id==t.id else '' }}>{{ t.name }}</option>{% endfor %}</select>
    <button class="btn primary" type="submit">Draft with LLM</button>
  </form>
  {% if draft %}<form method="post" action="{{ url_for('message_save', mid=m.id) }}"><label for="reply-body">Draft body</label><textarea id="reply-body" name="body" rows="8">{{ draft }}</textarea><div class="row" style="margin-top:8px"><button class="btn primary" type="submit">Save to Drafts</button><button class="btn" type="button" onclick="cp(document.getElementById('reply-body').value,this)">Copy</button><span class="sub">Review and send from your mail client.</span></div></form>
  {% elif draft_error %}<div class="msg err" role="alert">Draft failed: {{ draft_error }}</div>{% endif %}
</details>
{% if reply_state or m.llm_needs_reply or reply_targets %}
<div class="card" id="reply-status">
  <div class="card-h"><h3>Reply status</h3>{% if reply_state %}<span class="badge {{ 'ok' if reply_state.state == 'answered' else 'warn' if reply_state.state in ['partial','uncertain','matched'] else '' }}">{{ {'answered':'Answered','partial':'Partial reply','uncertain':'Needs review','matched':'Reply check pending','reopened':'Reopened by you','cleared':'Cleared by you'}.get(reply_state.state, reply_state.state) }}</span>{% elif m.llm_needs_reply %}<span class="badge warn">Awaiting your reply</span>{% endif %}</div>
  {% if reply_state %}<p>{{ reply_state.reason }}</p>{% if reply_state.sent %}<p class="sub"><a href="{{ url_for('message_detail', mid=reply_state.sent.id) }}">View sent reply →</a>{% if reply_state.state in ['answered','partial','uncertain'] %} · {{ '%.0f' % (reply_state.confidence*100) }}% assessment confidence{% endif %}</p>{% endif %}{% endif %}
  {% for target in reply_targets %}<p><a href="{{ url_for('message_detail', mid=target.id) }}">{{ target.subject }}</a> <span class="badge {{ 'ok' if target.state == 'answered' else 'warn' }}">{{ 'Answered' if target.state == 'answered' else 'Needs review' }}</span></p>{% endfor %}
  <div class="row"><form method="post" action="{{ url_for('message_check_replies', mid=m.id, f=filt, **search_form) }}"><button class="btn small" type="submit">Check sent replies</button></form><span class="sub">{% if reply_tracking %}Confirmed sent replies clear the flag; marking Needs reply reopens it.{% else %}Automatic checks are paused in AI settings.{% endif %}</span></div>
</div>
{% endif %}
<div class="msgrid">
  <div class="stack">
    <div class="card">
      <div class="row" style="margin-bottom:10px">
        {% if m.user_tag %}<span class="badge warn">tag: {{ m.user_tag }}</span>{% endif %}
        {% if m.action_taken %}<span class="badge">{{ m.action_taken|replace('move:', 'moved to ') }}</span>{% endif %}
        {% if m.llm_category %}<span class="badge acc">{{ 'Your correction' if m.classified_by == 'user' else 'LLM' }}: {{ m.llm_category }}{% if m.llm_confidence is not none and m.classified_by != 'user' %} ({{ '%.0f' % (m.llm_confidence*100) }}%){% endif %}</span>{% endif %}
        {% if m.classified_by and m.classified_by.startswith('heuristic') %}<span class="badge acc">⚙ {{ m.classified_by }}</span>{% endif %}
      </div>
      <form class="row category-correction" method="post" action="{{ url_for('message_category', mid=m.id) }}"><label for="correct-category">Correct category</label><select id="correct-category" name="category" required><option value="">Choose category…</option>{% for c in categories %}<option {{ 'selected' if m.llm_category==c else '' }}>{{ c }}</option>{% endfor %}</select><button class="btn small" type="submit">Save correction</button><span class="sub">Teaches the models; does not move mail.</span></form>
      {% set _mvt = (m.action_taken or '')[5:] if (m.action_taken or '').startswith('move') else '' %}
      {% if m.llm_summary %}<div class="note">LLM summary: {{ m.llm_summary }}{% if m.llm_reason %} · why: {{ m.llm_reason }}{% endif %}{% if m.llm_suggested_folder %} · {{ ('filed to ' + _mvt) if _mvt else ('suggested folder: ' + m.llm_suggested_folder) }}{% endif %}</div>{% endif %}
      {% if m.llm_thinking %}<details class="sub" style="margin:8px 0 0"><summary style="cursor:pointer">classifier thinking</summary><pre class="mono" style="white-space:pre-wrap;font-size:.8rem;color:var(--dim);margin:6px 0">{{ m.llm_thinking }}</pre></details>{% endif %}
      {% if classify_result %}<div class="note" style="margin-top:8px">LLM classified this as <b>{{ classify_result.category }}</b>
        ({{ '%.0f' % (classify_result.confidence*100) }}%) — {{ classify_result.summary }}{% if classify_result.reason %} · why: {{ classify_result.reason }}{% endif %}{% if classify_result.moved %} · filed to {{ classify_result.moved }}{% endif %}</div>{% endif %}
      {% if m.body_note %}<div class="note" style="margin-top:12px">{{ m.body_note }}</div>{% endif %}
      {% if m.email_html or (m.plain_view and (m.body_html or '').strip()) %}
      <div class="emailtools">
        {% if m.email_html and m.email_blocked %}<span class="badge warn">remote images blocked</span><a class="btn small" href="{{ url_for('message_detail', mid=m.id, imgs=1) }}">Load images</a>{% endif %}
        {% if m.email_html %}<span class="sp"></span><a class="sub" href="{{ url_for('message_detail', mid=m.id, view='plain') }}">View plain text</a>{% endif %}
        {% if m.plain_view %}<span class="sp"></span><a class="sub" href="{{ url_for('message_detail', mid=m.id) }}">View formatted</a>{% endif %}
      </div>
      {% endif %}
      {% if m.email_html %}<div class="emailbody">{{ m.email_html|safe }}</div>
      {% elif m.body_text_html %}<div class="msgbody">{{ m.body_text_html|safe }}</div>
      {% else %}<div class="empty" style="padding:26px 0 10px"><h4>Body unavailable</h4><p>Reload to retry the fetch, or open this message in your mail client.</p></div>{% endif %}
    </div>

  </div>

  <div class="stickycol">
    <details class="card msg-disclosure">
      <summary>Details</summary>
      <div class="kv">
        <div class="k">From</div><div class="msgfrom">{{ m.from_addr or '—' }}</div>
        <div class="k">To</div><div class="msgfrom">{{ m.to_addr or '—' }}</div>
        <div class="k">Date</div><div>{{ m.date or '—' }}</div>
        <div class="k">Folder</div><div>{{ m.folder }} <span class="sub">uid {{ m.uid }}</span></div>
        <div class="k">Message-ID</div><div class="mono" style="font-size:.77rem">{{ m.msgid or '—' }}</div>
      </div>
    </details>
    <details class="card msg-disclosure" id="audit">
      <summary>Audit trail · {{ m.audit|length }} event{{ 's' if m.audit|length != 1 else '' }}</summary>
      <div class="card-h"><h3>Audit trail</h3><span class="sub">How this email was triaged, oldest first{% if m.audit %} · {{ m.audit|length }} event{{ 's' if m.audit|length != 1 else '' }}{% endif %}</span></div>
      {% for ev in m.audit %}
      <div class="arow2">
        <div class="ahead">
          <span class="mono atime">{{ ev.when }}</span>
          <span class="badge {{ {'classify':'acc','rule':'ok','flow':'acc','file':'ok','move':'','undo':'warn','guard':'warn','snooze':'warn','needs_reply':'warn','wake':'ok','tag':'warn','draft':'acc','backfill':'warn'}.get(ev.kind,'') }}">{{ ev.kind }}</span>
          {% if ev.meta %}
          {% if ev.meta.needs_reply %}<span class="badge warn">needs reply</span>{% endif %}
          <span class="sub">{% if ev.meta.confidence is not none %}{{ '%.0f' % (ev.meta.confidence * 100) }}% · {% endif %}{{ ev.meta.by }}{% if ev.meta._backfilled %} · reconstructed{% endif %}</span>
          {% endif %}
        </div>
        <div class="adetail">
          {% if ev.meta %}
          <b>{{ ev.meta.category or '(no category)' }}</b>
          {% if ev.meta.reason %}<div class="sub">why: {{ ev.meta.reason }}</div>{% endif %}
          {% if ev.meta.thinking %}<details><summary class="sub" style="cursor:pointer">full reasoning</summary><pre class="mono audit-pre">{{ ev.meta.thinking }}</pre></details>{% endif %}
          {% else %}{{ (ev.detail or '')|replace('move:', 'moved to ') }}{% endif %}
        </div>
      </div>
      {% else %}<div class="sub">Nothing recorded yet — events appear as rules, flows, the classifier and you act on it.</div>
      <form class="inline" method="post" action="{{ url_for('message_sweep', mid=m.id) }}" style="margin-top:8px"><button class="btn small" type="submit">Rebuild this email's history</button></form>{% endfor %}
    </details>
    <div class="card">
      <div class="card-h"><h3>Actions</h3></div>
      <div class="row">
        {% if can_file %}
        <form class="inline" method="post" action="{{ url_for('message_file', mid=m.id) }}">
          <button class="btn primary" type="submit">File to {{ m.llm_suggested_folder }}</button></form>
        <form class="inline" method="post" action="{{ url_for('message_classify', mid=m.id) }}">
          <button class="btn small" type="submit">Re-classify with LLM</button></form>
        {% elif m.llm_category %}
        <form class="inline" method="post" action="{{ url_for('message_classify', mid=m.id) }}">
          <button class="btn small" type="submit">Re-classify with LLM</button></form>
        {% else %}
        <form class="inline" method="post" action="{{ url_for('message_classify', mid=m.id) }}">
          <button class="btn primary" type="submit">Classify with LLM</button></form>
        {% endif %}
      </div>
      <form method="post" action="{{ url_for('message_tag', mid=m.id) }}" style="margin-top:12px">
        <label for="m-tag">Tag this message</label>
        <div class="row" style="flex-wrap:nowrap">
          <input id="m-tag" type="text" name="tag" value="{{ m.user_tag }}" placeholder="e.g. Receipt">
          <button class="btn small" type="submit">Save</button>
        </div>
      </form>
      <div style="margin-top:12px">
        <label>Needs reply</label>
        <div class="row">
          {% if m.llm_needs_reply %}
            <span class="sub" style="margin-right:auto">{{ 'flagged by you' if m.nr_user == 1 else 'flagged by the LLM' }}</span>
          <form class="inline" method="post" action="{{ url_for('message_needs_reply', mid=m.id) }}"><button class="btn small" type="submit">No reply needed</button></form>
          {% elif m.nr_cleared %}
          <span class="sub">✓ cleared by you — a re-classify won't re-flag it</span>
          {% else %}
          <span class="sub">not flagged</span>
          {% endif %}
        </div>
      </div>
      <div style="margin-top:12px">
        <label>Snooze — hide it from the lists</label>
        <div class="row">
          {% if m.snoozed_active %}
          <span class="sub" style="margin-right:auto">Snoozed until {{ m.snoozed_h }}</span>
          <form class="inline" method="post" action="{{ url_for('message_snooze', mid=m.id) }}"><input type="hidden" name="hours" value="0"><button class="btn small" type="submit">Wake now</button></form>
          {% else %}
          <form class="inline" method="post" action="{{ url_for('message_snooze', mid=m.id) }}"><input type="hidden" name="hours" value="24"><button class="btn small" type="submit">1 day</button></form>
          <form class="inline" method="post" action="{{ url_for('message_snooze', mid=m.id) }}"><input type="hidden" name="hours" value="72"><button class="btn small" type="submit">3 days</button></form>
          <form class="inline" method="post" action="{{ url_for('message_snooze', mid=m.id) }}"><input type="hidden" name="hours" value="168"><button class="btn small" type="submit">1 week</button></form>
          {% endif %}
        </div>
      </div>
    </div>
    {% if m.decisions %}
    <details class="card msg-disclosure" id="decisions">
      <summary>Machine decisions</summary>
      <div class="card-h"><h3>Machine decisions</h3><span class="sub">learning loop · shadow rows never change behavior</span></div>
      {% for d in m.decisions %}
      <div class="arow2">
        <div class="ahead">
          <span class="mono atime">{{ d.when }}</span>
          <span class="badge {{ {'specialist':'acc','heuristic':'ok'}.get(d.source_type,'') }}">{{ d.source_type }}</span>
          {% if d.shadow %}<span class="badge">shadow</span>{% endif %}
          <span class="sub">{{ d.task }}{% if d.confidence %} · confidence {{ '%.2f' % d.confidence }}{% endif %}</span>
        </div>
        <div class="adetail">
          <b>{{ d.value }}</b> <span class="sub">· {{ d.source_id }}</span>
          {% if d.evidence %}<div class="sub">evidence: {% for e in d.evidence %}{{ e.feature_name }} {{ '%+.2f' % (e.contribution or 0) }}{{ ' · ' if not loop.last }}{% endfor %}</div>{% endif %}
        </div>
      </div>
      {% endfor %}
    </details>
    {% endif %}
  </div>
</div>
"""






def _find_message_location(mc, msgid, first_folder=None):
    """Locate a message by its Message-ID across folders: (folder, uid) or None.
    Used when a stored row points at a folder/uid the message no longer occupies
    (moved externally, uidvalidity bump)."""
    needle = (msgid or "").strip().strip("<>").strip()
    if not needle:
        return None
    tried = []
    for name in ([first_folder] if first_folder else []) + list(mc.folders()):
        if not name or name in tried:
            continue
        tried.append(name)
        try:
            mc.select(name)
            hits = mc.search("HEADER", "Message-ID", needle)
        except Exception:
            continue
        if hits:
            return name, hits[-1]
    return None


@app.template_filter("clip")
def clip(s, n):
    """Truncate a display value to n chars, adding an ellipsis only when it truncates."""
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def _display_date(raw):
    """Human date for the viewer, in the display timezone; falls back to the raw header value."""
    if not raw:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        off = int(round(tz_offset_hours() * 3600))
        shifted = parsedate_to_datetime(raw).timestamp() + off
        return time.strftime("%a %d %b %Y · %H:%M", time.gmtime(shifted)) + " " + tz_label()
    except Exception:
        return raw


def message_body_html(text):
    """Safe HTML for the message body: escaped, links clickable, long quoted
    tails collapsed into a details block."""
    if not text:
        return ""
    esc = html_escape(text)
    esc = re.sub(r"https?://[^\s<>\"']+[^\s<>\"'.,;:\])\]]",
                 lambda mo: '<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
                            % (mo.group(0), mo.group(0)), esc)
    lines = esc.split("\n")
    idx = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("&gt;") and sum(1 for l in lines[i:i + 12]
                                        if l.strip().startswith("&gt;")) >= 2:
            idx = i
            break
        if re.match(r"^On .{3,90} wrote:$", s) or s.startswith("-----Original Message-----") or (
                re.match(r'^(From:|发件人:)', s) and
                sum(bool(re.match(r'^(From:|Sent:|Date:|To:|Subject:|发件人:|日期:|收件人:|主题:)', l.strip()))
                    for l in lines[i:i + 12]) >= 3):
            idx = i
            break
    if idx is None:
        return esc
    main = "\n".join(lines[:idx]).strip()
    quoted = "\n".join(lines[idx:]).strip()
    return (main + '<details class="quote"><summary>··· show quoted text (%d lines)</summary><div class="qbody">%s</div></details>'
            % (len(lines) - idx, quoted))


def _message_body_payload(m):
    """Viewer payload {text, html, cids, note}: the stored copy when complete,
    else re-fetch from the mailbox (Message-ID rescue when the row went stale),
    cache text + sanitized html + cid map back onto the row."""
    stored = m.get("snippet") or ""
    text_ok = bool(stored) and not engine.looks_like_mime_junk(stored) and engine.looks_readable(stored)
    html_cached = bool((m.get("body_html") or "").strip())
    html_checked = html_cached or bool(m.get("body_html_at") or 0)
    if text_ok and html_checked:
        return {"text": stored[:4000], "html": m.get("body_html") or "",
                "cids": m.get("body_cids") or "", "note": None}

    fetched, moved = None, None
    try:
        mc = engine.MailClient().connect()
        try:
            try:
                mc.select(m["folder"])
                got = mc.fetch_body_payload(m["uid"], limit=6000)
                if got.get("text") and not engine.looks_like_mime_junk(got["text"]) \
                        and engine.looks_readable(got["text"]):
                    fetched = got
            except Exception:
                fetched = None
            if fetched is None:
                loc = _find_message_location(mc, m.get("msgid"))
                if loc:
                    try:
                        mc.select(loc[0])
                        got = mc.fetch_body_payload(loc[1], limit=6000)
                        if got.get("text") and not engine.looks_like_mime_junk(got["text"]) \
                                and engine.looks_readable(got["text"]):
                            fetched = got
                            moved = loc
                    except Exception:
                        pass
        finally:
            try:
                mc.close()
            except Exception:
                pass
    except Exception:
        fetched = None

    if fetched is not None:
        text = (fetched.get("text") or "")[:4000]
        fields = {"snippet": text, "body_html_at": int(time.time())}
        if fetched.get("html"):
            fields["body_html"] = fetched["html"][:400000]
            fields["body_cids"] = json.dumps(fetched.get("cids") or {})
        if moved:
            fields["folder"], fields["uid"] = moved
            m["folder"], m["uid"] = moved
        store.update_message(m["id"], **fields)
        m["body_html"] = fields.get("body_html", m.get("body_html") or "")
        m["body_cids"] = fields.get("body_cids", m.get("body_cids") or "")
        return {"text": text, "html": m["body_html"], "cids": m["body_cids"], "note": None}

    if text_ok:
        return {"text": stored[:4000], "html": m.get("body_html") or "",
                "cids": m.get("body_cids") or "", "note": None}
    salv = engine.readable_body(stored, limit=4000)
    if salv and not engine.looks_like_mime_junk(salv) and engine.looks_readable(salv):
        return {"text": salv[:4000], "html": "", "cids": "",
                "note": "Shown from a repaired stored copy — the mailbox re-fetch failed."}
    return {"text": "", "html": "", "cids": "",
            "note": ("This message could not be decoded: the stored copy is raw MIME and "
                     "the mailbox copy could not be read. Reload to retry.")}


_IMG_PLACEHOLDER = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
                    "width='132' height='40'><rect width='132' height='40' fill='%23f4f4f4' "
                    "stroke='%23e5e5e5'/><text x='66' y='24' font-size='10' fill='%23a3a3a3' "
                    "text-anchor='middle' font-family='sans-serif'>image blocked</text></svg>")


def _email_body_html(m, show_images):
    """Render-ready email HTML: cid: refs -> inline part route, remote refs ->
    the image proxy (or a placeholder while images are blocked for this view)."""
    html = (m.get("body_html") or "").strip()
    if not html:
        return "", False
    try:
        cids = json.loads(m.get("body_cids") or "{}")
    except Exception:
        cids = {}
    mid = m["id"]

    def cid_url(cid):
        sec = (cids.get(cid) or {}).get("section")
        return "/messages/%d/part/%s?v=2" % (mid, sec) if sec else ""

    def repl_cid_attr(mo):
        url = cid_url(mo.group(3))
        if not url:
            return mo.group(0)
        q = mo.group(2)
        if q:
            return '%s=%s%s' % (mo.group(1), q, url)  # original closing quote remains
        return '%s="%s"' % (mo.group(1), url)         # add both quotes

    html = re.sub(r'(?i)(src|background)\s*=\s*(["\']?)cid:([^"\'\s>)]+)', repl_cid_attr, html)

    def repl_cid_css(mo):
        url = cid_url(mo.group(2))
        return mo.group(0) if not url else 'url("%s")' % url

    html = re.sub(r'(?i)url\(\s*(["\']?)cid:([^"\')]+)\1\s*\)', repl_cid_css, html)

    def proxy(u):
        return "/messages/%d/img?u=%s" % (mid, quote(u, safe=""))

    def repl_remote_attr(mo):
        q = mo.group(2)
        if q:
            return '%s=%s%s' % (mo.group(1), mo.group(2), proxy(mo.group(3)))
        return '%s="%s"' % (mo.group(1), proxy(mo.group(3)))

    html = re.sub(r'(?i)(src|background)\s*=\s*(["\']?)(https?://[^"\'\s>]+)', repl_remote_attr, html)

    def repl_remote_css(mo):
        return 'url(%s)' % proxy(mo.group(1))

    html = re.sub(r'(?i)url\(\s*["\']?(https?://[^"\')]+)["\']?\s*\)', repl_remote_css, html)

    has_remote = "/img?u=" in html
    if has_remote and not show_images:
        prefix = re.escape("/messages/%d/img?u=" % mid)
        html = re.sub(r'(?i)(src|background)="' + prefix + r'[^"]*"',
                      lambda mo: '%s="%s"' % (mo.group(1), _IMG_PLACEHOLDER), html)
        html = re.sub(r'url\(' + prefix + r'[^)]*\)', 'none', html)
    return html, has_remote


def _message_decisions(mid, limit=14):
    """Learning-loop decision rows for one message: specialist (shadow), the
    system's own verdict, and the router intent - with evidence contributions."""
    out = []
    if not mid:
        return out
    try:
        rows = store.list_decisions(msg_id=mid, limit=limit)
    except Exception:
        return out
    for d in rows:
        try:
            val = json.loads(d["predicted_value"])
        except (TypeError, ValueError):
            val = d["predicted_value"]
        if d["task"] == "needs_reply" and isinstance(val, bool):
            val_h = "needs a reply" if val else "no reply needed"
        elif d["task"] == "route":
            val_h = "would route: %s" % val
        else:
            val_h = str(val)
        ev = store.decision_evidence(d["id"], limit=4) if d["source_type"] == "specialist" else []
        out.append({"when": fmt_ts(d["ts"]), "task": d["task"], "source_type": d["source_type"],
                    "source_id": d["source_id"], "value": val_h, "confidence": d["confidence"],
                    "shadow": bool(d["shadow"]), "evidence": ev})
    return out


_VIEW_RESULTS = {}


def _render_message(m, classify_result=None, draft=None, draft_error=None, draft_template_id=0,
                    show_images=False, plain=False, filt="all", prev_id=None, next_id=None):
    if request.method == 'POST' and 'text/vnd.turbo-stream.html' in request.headers.get('Accept', ''):
        # Turbo requires a redirect after successful forms. Hold the generated
        # preview briefly so it remains editable on the canonical viewer GET.
        now = time.time()
        for key in [k for k, v in _VIEW_RESULTS.items() if now - v[0] > 1800]:
            _VIEW_RESULTS.pop(key, None)
        key = os.urandom(16).hex()
        _VIEW_RESULTS[key] = (now, m['id'], {'classify_result': classify_result, 'draft': draft,
                                           'draft_error': draft_error, 'draft_template_id': draft_template_id})
        while len(_VIEW_RESULTS) > 30:
            _VIEW_RESULTS.pop(min(_VIEW_RESULTS, key=lambda k: _VIEW_RESULTS[k][0]))
        return redirect(url_for('message_detail', mid=m['id'], result=key,
                                f=request.args.get('f') or 'all', **_mail_search()[0]), code=303)
    if "body" not in m:
        payload = _message_body_payload(m)
        m["body"] = payload["text"]
        m["body_note"] = payload["note"]
        m["body_html"] = payload["html"]
        m["body_cids"] = payload["cids"]
        m["body_text_html"] = message_body_html(m["body"])
    if "date_disp" not in m:
        m["date_disp"] = _display_date(m.get("date"))
    if "decisions" not in m:
        m["decisions"] = _message_decisions(m.get("id"))
    m["email_html"] = ""
    has_remote = False
    if not plain and (m.get("body_html") or "").strip():
        m["email_html"], has_remote = _email_body_html(m, show_images)
    m["email_blocked"] = bool(has_remote and not show_images)
    m["email_has_remote"] = has_remote
    m["show_images"] = show_images
    m["plain_view"] = plain
    reply_state = replies.view_state(m)
    headers = store.thread_headers(m['id'])
    targets = store.reply_targets(m['id']) if headers.get('sent_folder') else []
    return render(_render_src(
        MESSAGE_TMPL, m=m, templates=store.list_templates(), draft=draft,
        draft_error=draft_error, draft_template_id=draft_template_id,
        classify_result=classify_result, llm_configured=bool(config.LLM_API_KEY),
        filt=filt, prev_id=prev_id, next_id=next_id,
        search_form=_mail_search()[0], categories=store.get_setting('categories') or [],
        reply_state=reply_state, reply_targets=targets,
        reply_tracking=store.get_setting('reply_tracking_enabled') and store.get_setting('llm_suggest')))


@app.route("/messages/<int:mid>")
def message_detail(mid):
    m = store.get_message(mid)
    if not m:
        flash("No such message.", "err")
        return redirect(url_for("messages"))
    m["badge"] = STATUS_BADGES.get(m.get("status"), ("", m.get("status", "")))
    su = m.get("snoozed_until") or 0
    m["snoozed_active"] = bool(su and su > time.time())
    m["snoozed_h"] = fmt_ts(su) if su else ""
    m['nr_user'] = store.user_needs_reply(mid)
    m["nr_cleared"] = m['nr_user'] == 0
    m["audit"] = []
    for ev in store.get_msg_events(mid, limit=200):
        if ev['kind'] == 'thread_headers':
            continue  # machine-readable evidence, not an actionable audit event
        d = {"when": fmt_ts(ev["ts"]), "kind": ev["kind"], "detail": ev["detail"], "meta": None}
        if ev["kind"] == "classify":
            try:
                d["meta"] = json.loads(ev["detail"])
            except (TypeError, ValueError):
                pass
        elif ev['kind'] == 'reply_state':
            evidence = json.loads(ev['detail'])
            d['detail'] = '%s · sent message #%s · %s' % (evidence['state'], evidence['sent_id'], evidence['reason'])
        m["audit"].append(d)
    show_images = request.args.get("imgs") == "1" or bool(store.get_setting("render_images"))
    plain = request.args.get("view") == "plain"
    filt = request.args.get("f") or "all"
    prev_id, next_id = store.neighbors(mid, filt, search=_mail_search()[1])
    options = {}
    key = request.args.get('result') or ''
    if key:
        hit = _VIEW_RESULTS.get(key)
        if hit and hit[1] == mid and time.time() - hit[0] <= 1800:
            options = hit[2]
        else:
            flash('That generated preview has expired. Generate it again from Draft reply.', 'warn')
    return _render_message(m, show_images=show_images, plain=plain, filt=filt,
                           prev_id=prev_id, next_id=next_id, **options)


def _data_dir():
    try:
        return config.DATA_DIR
    except Exception:
        return os.path.dirname(config.DB_PATH)


@app.route("/messages/<int:mid>/part/<section>")
def message_part(mid, section):
    """Inline MIME part (images referenced as cid:...) for the message viewer."""
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", section or ""):
        return ("bad section", 404)
    m = store.get_message(mid)
    if not m:
        return ("no such message", 404)
    cache_dir = os.path.join(_data_dir(), "partcache")
    key = "v2-m%d-u%d-%s" % (mid, m["uid"], section)
    bin_path = os.path.join(cache_dir, key + ".bin")
    meta_path = os.path.join(cache_dir, key + ".json")
    if os.path.exists(bin_path) and os.path.exists(meta_path):
        try:
            with open(bin_path, "rb") as fh:
                data = fh.read()
            with open(meta_path) as fh:
                ct = (json.load(fh) or {}).get("ct") or "application/octet-stream"
            return Response(data, mimetype=ct, headers=_SAFE_MEDIA_HEADERS)
        except Exception:
            pass
    try:
        mc = engine.MailClient().connect()
        try:
            mc.select(m["folder"])
            ct, data = mc.fetch_section(m["uid"], section)
        finally:
            mc.close()
    except Exception as exc:
        store.log_event("error", "inline part fetch failed for msg %d [%s]: %r" % (mid, section, exc))
        return ("part fetch failed", 502)
    if not (ct or "").lower().startswith("image/"):
        return ("not an image part", 404)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(bin_path, "wb") as fh:
            fh.write(data)
        with open(meta_path, "w") as fh:
            json.dump({"ct": ct}, fh)
    except Exception:
        pass
    return Response(data, mimetype=ct, headers=_SAFE_MEDIA_HEADERS)


def _fetch_remote_image(url, verify=True):
    import urllib.request
    import ssl
    ctx = None
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "mail-triage-viewer/1.0"})
    with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
        ct = str(resp.headers.get("Content-Type") or "").split(";")[0].strip()
        data = resp.read(8 * 1024 * 1024 + 1)
    return data, ct


_SAFE_MEDIA_HEADERS = {"Cache-Control": "public, max-age=2592000",
                       "X-Content-Type-Options": "nosniff",
                       "Content-Security-Policy": "default-src 'none'; sandbox"}


@app.route("/messages/<int:mid>/img")
def message_img(mid):
    """Remote image proxy + cache for the viewer (SSRF-guarded, images only)."""
    url = request.args.get("u") or ""
    if not engine.image_url_ok(url):
        return ("blocked", 404)
    cache_dir = os.path.join(_data_dir(), "imgcache")
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    bin_path = os.path.join(cache_dir, key + ".bin")
    meta_path = os.path.join(cache_dir, key + ".json")
    if os.path.exists(bin_path) and os.path.exists(meta_path):
        try:
            with open(bin_path, "rb") as fh:
                data = fh.read()
            with open(meta_path) as fh:
                ct = (json.load(fh) or {}).get("ct") or "application/octet-stream"
            return Response(data, mimetype=ct, headers=_SAFE_MEDIA_HEADERS)
        except Exception:
            pass
    data, ct = None, ""
    try:
        data, ct = _fetch_remote_image(url, verify=True)
    except Exception as exc:
        # Some senders' CDNs serve incomplete chains (leaf without intermediate) -
        # browsers mask this via AIA fetches. Retry once without verification for
        # the image bytes (sender-controlled media anyway) and log it.
        if "SSL" in repr(exc) or "CERTIFICATE" in repr(exc):
            try:
                data, ct = _fetch_remote_image(url, verify=False)
                store.log_event("warn", "unverified image fetch (bad chain): %s" % url[:110])
            except Exception as exc2:
                store.log_event("warn", "remote image fetch failed (%s): %r" % (url[:110], exc2))
                return ("fetch failed", 502)
        else:
            store.log_event("warn", "remote image fetch failed (%s): %r" % (url[:110], exc))
            return ("fetch failed", 502)
    if data is None or len(data) > 8 * 1024 * 1024:
        return ("too large", 502)
    sniffed = engine._sniff_image_type(data)
    if not ct.lower().startswith("image/") and not sniffed:
        return ("not an image", 404)
    if not ct.lower().startswith("image/"):
        ct = sniffed
    if sniffed and ct.lower().startswith("image/") and             sniffed.split("/")[1][:3] not in ct and ct.split("/")[1][:3] not in sniffed:
        ct = sniffed
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(bin_path, "wb") as fh:
            fh.write(data)
        with open(meta_path, "w") as fh:
            json.dump({"ct": ct}, fh)
    except Exception:
        pass
    return Response(data, mimetype=ct, headers=_SAFE_MEDIA_HEADERS)


@app.route("/messages/<int:mid>/classify", methods=["POST"])
def message_classify(mid):
    m = store.get_message(mid)
    if not m:
        flash("No such message.", "err")
        return redirect(url_for("messages"))
    try:
        res = engine.classify_and_store(m, store.all_settings())
        learning.observe(mid, "reclassify", str(res.get("category") or ""), source="ui")
        store.add_llm_log(mid, True)
        m = store.get_message(mid) or m
        m["badge"] = STATUS_BADGES.get(m.get("status"), ("", m.get("status", "")))
        return _render_message(m, classify_result={
            "category": res.get("category"),
            "confidence": float(res.get("confidence") or 0),
            "summary": str(res.get("summary") or ""),
            "reason": str(res.get("reason") or ""),
            "moved": res.get("_moved_to") or "",
        })
    except Exception as exc:
        flash("Classify failed: %r" % exc, "err")
        return redirect(url_for("message_detail", mid=mid))


@app.route("/messages/<int:mid>/tag", methods=["POST"])
def message_tag(mid):
    tag = (request.form.get("tag") or "").strip()[:40]
    store.tag_messages([mid], tag)
    store.log_msg_event(mid, "tag", ("tagged \u201c%s\u201d" % tag) if tag else "tag cleared")
    if tag:
        learning.observe(mid, "tag", tag, source="ui")
        store.record_label(mid, "category", tag.lower(), 1.0, "explicit_user_label", "user_tag")
    else:
        learning.observe(mid, "untag", "", source="ui")
    flash(("Tag saved: " + tag) if tag else "Tag cleared.", "ok")
    return redirect(url_for("message_detail", mid=mid))


@app.route("/messages/<int:mid>/needs-reply", methods=["POST"])
def message_needs_reply(mid):
    if request.form.get('value') == '1':
        if store.correct_needs_reply(mid, True):
            store.log_msg_event(mid, 'needs_reply', 'flagged by you')
            learning.observe(mid, 'needs_reply', '1', source='ui')
            flash('Marked as needing a reply — your correction survives reclassification.', 'ok')
        else:
            flash('No such message.', 'err')
        return redirect(url_for('message_detail', mid=mid))
    n, _ = store.clear_needs_reply([mid])
    if n:
        store.correct_needs_reply(mid, False)
        store.log_msg_event(mid, "needs_reply", "cleared (by ui)")
        learning.observe(mid, "needs_reply", "cleared", source="ui")
        store.record_label(mid, "needs_reply", "0", source="explicit_user_correction",
                           source_detail="cleared in the viewer")
        store.log_event("info", "cleared needs-reply on message %d (by ui)" % mid)
        flash("Needs-reply cleared - a re-classify will not re-flag it.", "ok")
    else:
        flash("This message was not flagged.", "warn")
    filt = request.form.get("f") or "all"
    if request.form.get("next") == "1":
        _p, next_id = store.neighbors(mid, filt, search=_mail_search()[1])
        if next_id and next_id != mid:
            return redirect(url_for("message_detail", mid=next_id, f=filt, **_mail_search()[0]))
        return redirect(url_for("messages", f=filt, **_mail_search()[0]))
    return redirect(url_for("message_detail", mid=mid))


@app.route('/messages/<int:mid>/category', methods=['POST'])
def message_category(mid):
    category = (request.form.get('category') or '').strip()
    if not store.get_message(mid):
        flash('No such message.', 'err')
        return redirect(url_for('messages'))
    if category not in (store.get_setting('categories') or []):
        flash('Choose one of your configured categories.', 'err')
        return redirect(url_for('message_detail', mid=mid))
    store.update_message(mid, llm_category=category, classified_by='user', llm_confidence=1.0,
                         llm_suggested_folder=(store.get_setting('category_folders') or {}).get(category, ''))
    store.record_category_correction(mid, category)
    learning.observe(mid, 'relabel', category, source='ui')
    store.log_msg_event(mid, 'category', 'corrected by you: ' + category)
    flash('Saved as your correction: %s. Mail was not moved.' % category, 'ok')
    return redirect(url_for('message_detail', mid=mid))


@app.route('/messages/<int:mid>/check-replies', methods=['POST'])
def message_check_replies(mid):
    if not store.get_message(mid):
        flash('No such message.', 'err')
        return redirect(url_for('messages'))
    if not store.get_setting('reply_tracking_enabled') or not store.get_setting('llm_suggest'):
        flash('Automatic reply checks are paused. Enable Detect answered mail and LLM classification in AI settings.', 'warn')
    else:
        worker.trigger()
        flash('Mailbox check queued, including sent replies. Refresh shortly to see the result.', 'ok')
    return redirect(url_for('message_detail', mid=mid, f=request.args.get('f') or 'all', **_mail_search()[0]))


@app.route("/messages/<int:mid>/file", methods=["POST"])
def message_file(mid):
    m = store.get_message(mid)
    target = (m or {}).get("llm_suggested_folder") or ""
    want_next = request.form.get("next") == "1"
    filt = request.form.get("f") or "all"
    next_id = None
    if want_next and m:
        _p, next_id = store.neighbors(mid, filt)
    if not m or not target:
        flash("No suggested folder for this message — classify it first.", "err")
    else:
        out = engine.run_act("move", mid, {"folder": target, "source": "manual"})
        if not out.get("ok"):
            flash("File failed: %s" % out.get("error"), "err")
        else:
            learning.observe(mid, "move", target, source="ui")
            store.update_message(mid, status="llm-moved", action_taken="move:" + target)
            store.clear_keep(m.get("msgid"))
            flash("Filed to '%s'%s" % (target, " — next up." if want_next else "."), "ok")
    if want_next and next_id and next_id != mid:
        return redirect(url_for("message_detail", mid=next_id, f=filt))
    if want_next:
        return redirect(url_for("messages", f=filt))
    return redirect(url_for("message_detail", mid=mid))


@app.route("/messages/<int:mid>/draft", methods=["POST"])
def message_draft(mid):
    m = store.get_message(mid)
    if not m:
        flash("No such message.", "err")
        return redirect(url_for("messages"))
    m["badge"] = STATUS_BADGES.get(m.get("status"), ("", m.get("status", "")))
    template_id = request.form.get("template_id") or None
    try:
        draft = engine.generate_draft(mid, int(template_id) if template_id else None)
        store.log_msg_event(mid, "draft", "reply draft generated%s"
                            % ((" with template %s" % template_id) if template_id else ""))
        return _render_message(m, draft=draft,
                               draft_template_id=int(template_id) if template_id else 0)
    except Exception as exc:
        return _render_message(m, draft_error=repr(exc))


@app.route("/messages/<int:mid>/save", methods=["POST"])
def message_save(mid):
    try:
        folder = engine.save_draft(mid, request.form.get("body") or "")
        flash("Draft saved to '%s' — review it in your mail client." % folder, "ok")
    except engine.ActPending as exc:
        flash("Draft is taking longer than expected — check Drafts before retrying.", "warn")
    except Exception as exc:
        flash("Could not save the draft: %r" % exc, "err")
    return redirect(url_for("message_detail", mid=mid))


# ---------------------------------------------------------------- assistant

CONVO_TMPL = r"""
{% if convo %}
  {% for m in convo %}
    {% if m.role == 'user' %}
    <div class="crow user">
      <div class="avatar you">You</div>
      <div class="bubble user">{{ m.content }}<div class="meta"><span>{{ m.when }}</span>{% if m.retry %}<button type="button" class="regen" data-mid="{{ m.id }}" title="Retry this message" aria-label="Retry this message">↻ retry</button>{% endif %}</div></div>
    </div>
    {% else %}
    <div class="crow ai">
      <div class="avatar ai">AI</div>
      <div class="bubble ai">
        {% if m.reasoning %}
        <details class="think"><summary>{{ m.reasoning_summary or 'Reasoning' }}</summary><pre>{{ m.reasoning }}</pre></details>
        {% endif %}
        {% if m.tool_steps %}
        {% set _tl = m.tool_steps|last %}
        <details class="tools">
          <summary>⚙ {{ m.tool_steps|length }} tool call{{ 's' if m.tool_steps|length != 1 else '' }}{% if _tl %} · {{ '✓' if _tl.ok else '✗' }} {{ _tl.name }} → {{ _tl.summary }}{% endif %}</summary>
          <div class="tools-list">
          {% for t in m.tool_steps %}<span class="tool-chip {{ 'ok' if t.ok else 'err' }}">{{ '✓' if t.ok else '✗' }} {{ t.name }}{% if t.dry_run %} · dry-run{% endif %}{% if t.pending %} · awaiting approval{% endif %} → {{ t.summary }}</span>{% endfor %}
          </div>
        </details>
        {% endif %}
        <div class="md">{{ md(m.content)|safe }}</div>
        <div class="meta"><button type="button" class="copy" data-copy="{{ m.content|e }}">copy</button><span>{{ m.when }}</span>{% if m.regen %}<button type="button" class="regen" data-mid="{{ m.id }}" title="Regenerate reply">↻</button>{% endif %}</div>
        {% for p in m.proposals_list %}
        <div class="proposal">
          <div class="p-tag">✦ Proposed {{ 'flow' if p.kind == 'flow' else 'rule' }}</div>
          <div class="spread">
            <div><b>{{ p.name }}</b> <span class="sub">({{ p.match_mode }})</span>{% if p.placement == 'top' %} <span class="badge acc">added at top</span>{% endif %}{% if p.updates %} <span class="badge warn">updates #{{ p.updates.id }} "{{ p.updates.name }}"</span>{% elif p.similar %} <span class="badge warn">overlaps #{{ p.similar.id }}</span>{% endif %}</div>
            <div class="row" style="white-space:nowrap">
              {% set tgt = p.updates if p.updates else p.similar %}
              {% if p.applied %}
              <span class="btn small" style="border-color:var(--line);color:var(--dim);pointer-events:none" aria-disabled="true">✓ Added</span>
              {% else %}
              {% if tgt %}
              <form class="inline" method="post" action="{{ url_for('assistant_apply') }}" onsubmit="return guardApply(this)">
                <input type="hidden" name="msg_id" value="{{ m.id }}">
                <input type="hidden" name="idx" value="{{ loop.index0 }}">
                <input type="hidden" name="mode" value="update">
                <input type="hidden" name="rule_id" value="{{ tgt.id }}">
                <input type="hidden" name="session" value="{{ sid }}">
                <input type="hidden" name="back" value="{{ (back or '')|e }}">
                <button class="btn small primary" type="submit">{{ 'Update flow #%d' % tgt.id if p.kind == 'flow' else 'Update rule #%d' % tgt.id }}</button>
              </form>
              {% endif %}
              <form class="inline" method="post" action="{{ url_for('assistant_apply') }}" onsubmit="return guardApply(this)">
                <input type="hidden" name="msg_id" value="{{ m.id }}">
                <input type="hidden" name="idx" value="{{ loop.index0 }}">
                <input type="hidden" name="session" value="{{ sid }}">
                <input type="hidden" name="back" value="{{ (back or '')|e }}">
                <button class="btn small{{ '' if tgt else ' primary' }}" type="submit">{{ 'Add flow' if p.kind == 'flow' else 'Add rule' }}</button>
              </form>
              <form class="inline" method="post" action="{{ url_for('assistant_apply') }}" onsubmit="return guardApply(this)">
                <input type="hidden" name="msg_id" value="{{ m.id }}">
                <input type="hidden" name="idx" value="{{ loop.index0 }}">
                <input type="hidden" name="disabled" value="1">
                <input type="hidden" name="session" value="{{ sid }}">
                <input type="hidden" name="back" value="{{ (back or '')|e }}">
                <button class="btn small" type="submit">Add (disabled)</button>
              </form>
              {% endif %}
            </div>
          </div>
          {% if p.updates %}
          <div class="note" style="border-color:var(--warn);color:var(--warn)">Updates {{ 'flow' if p.kind == 'flow' else 'rule' }} #{{ p.updates.id }} "{{ p.updates.name }}" — currently {{ p.updates_actions }}.</div>
          {% elif p.similar %}
          <div class="note" style="border-color:var(--warn);color:var(--warn)">⚠ Similar rule exists: #{{ p.similar.id }} "{{ p.similar.name }}"{% if not p.similar.enabled %} (disabled){% endif %} — {{ p.similar_actions }}. Updating it avoids a duplicate.</div>
          {% endif %}
          <div class="mono" style="font-size:.85rem">{{ p.summary }}</div>
          <div class="sub">{{ p.actions_summary }}{% if p.rationale %} - {{ p.rationale }}{% endif %}</div>
        </div>
        {% endfor %}
      </div>
    </div>
    {% endif %}
  {% endfor %}
{% else %}
  <div class="chat-empty">
    <div class="ce-icon">✉</div>
    <div class="ce-title">Ask about your mail</div>
    <div class="sub">Searches your archive by meaning, reads and files mail, and proposes rules you approve
    with one click. Every step shows as it happens.</div>
    <div class="chips">
      {% for s in suggest %}
      {% if s.href %}<a class="chip" href="{{ s.href }}">{{ s.label }}</a>
      {% else %}<button type="button" class="chip" data-fill="{{ s.prompt|e }}">{{ s.label }}</button>{% endif %}
      {% endfor %}
    </div>
  </div>
{% endif %}
{% if live_run %}<div class="live-run" data-run="{{ live_run }}" hidden></div>{% endif %}
"""

ASSISTANT_TMPL = r"""
<style>
.chip.sm{height:22px;padding:0 8px;font-size:.72rem;gap:4px}
:root{--plug:#6d4fc4;--plug-bg:#f7f4fd;--plug-line:#d9cdf3}
.chip.plug{color:var(--plug);border-color:var(--plug-line);background:var(--plug-bg)}
.chip.plug:hover{border-color:var(--plug);color:var(--plug)}
.aspecline{display:flex;align-items:flex-start;gap:12px;margin-top:10px}
.aspecline .aspec{flex:1;min-width:0}
.aspec summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;color:var(--dim);font-size:.78rem;user-select:none}
.aspec summary::-webkit-details-marker{display:none}
.aspec summary:before{content:'▸';font-size:.7rem}
.aspec[open] summary:before{content:'▾'}
.aspec-body{border:1px solid var(--line);background:#fff;padding:10px 12px;margin-top:8px}
.aspec-grp{display:flex;gap:8px;align-items:baseline;padding:4px 0;flex-wrap:wrap}
.aspec-grp b{flex:0 0 110px;font-size:.78rem}
.aspec-chips{display:flex;flex-wrap:wrap;gap:6px}
.aspec-body>a{display:inline-block;margin-top:8px}
</style>
<script>/* /assistant renders directly (no 302) so Turbo never double-renders;
  keep the address bar on the canonical session URL. */
(function(){ var p = location.pathname.replace(/\/$/, "");
  if (p === "/assistant") { try { history.replaceState(history.state, "", "/assistant/s/{{ sid }}"); } catch (e) {} } })();</script>
<div class="page-head am-head">
  <div>
    <h1 class="page-title">Assistant</h1>
    <div class="page-desc">Ask about your mail. Every step streams live; chats are saved - resume any of them from the list.</div>
  </div>
  <div class="row">
    <a class="btn primary" href="{{ url_for('assistant') }}">New chat</a>
  </div>
</div>
<div class="assistant-shell">
  <aside class="assistant-rail" aria-label="Chat history">
    <div class="rail-h"><input id="chatfilter" type="search" placeholder="Filter chats…" aria-label="Filter chats"></div>
    {% if sessions %}
    {% set ns = namespace(g='') %}
    {% for s in sessions %}
    {% if s.group != ns.g %}<div class="rgroup">{{ s.group }}</div>{% set ns.g = s.group %}{% endif %}
    <div class="arow{{ ' cur' if s.id == sid else '' }}" data-sid="{{ s.id }}">
      {% if s.active %}<span class="chat-spin" role="status" aria-label="Assistant is working in this chat" title="Assistant is working in this chat"></span>{% endif %}
      <a class="t" href="{{ url_for('assistant_session', sid=s.id) }}" title="{{ s.title or 'Untitled chat' }}">{{ s.title or 'Untitled chat' }}</a>
      <span class="when">{{ s.when }}</span>
      <form class="inline" method="post" action="{{ url_for('assistant_session_delete', sid=s.id) }}"><button class="btn small chat-del" type="submit" aria-label="Delete chat: {{ s.title or 'Untitled chat' }}">✕</button></form>
    </div>
    {% endfor %}
    {% else %}
    <div class="sub" style="padding:12px">No chats yet - say hello on the right.</div>
    {% endif %}
  </aside>
  <div class="assistant-main">
    <div class="chat-head">
      <button type="button" class="iconbtn" id="ahist" aria-label="Chat history"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/></svg></button>
      <div class="ch-title">Assistant{% if asst_fb %} <span class="badge" title="Assistant is answering from the fallback endpoint">fallback: {{ asst_fb_model }}</span>{% endif %}</div>
      <a class="iconbtn" href="{{ url_for('assistant') }}" aria-label="New chat"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></a>
    </div>
    <div class="assistant-flex">
      <div class="jumpwrap">
        <button type="button" class="btn small hidden" id="jump">↓ Jump to latest</button>
        <div class="chat" id="convo">{{ convo_html|safe }}</div>
      </div>
      {% if pending %}
      <div class="card" id="pending-panel" style="margin:10px 0 0">
        <div class="card-h"><h3>Awaiting your approval ({{ pending|length }})</h3>
          <span class="sub">The assistant queued these — nothing happens until you click.</span></div>
        {% for a in pending %}
        <div class="proposal" style="margin:8px 0">
          <div class="p-tag warn">{{ '✦ From a plugin' if a.card else '✦ Needs your approval' }}</div>
          <div class="spread">
            <div><b>{{ a.preview }}</b> <span class="sub">{{ a.capability }} · queued {{ a.when_h }}</span></div>
            <div class="row" style="white-space:nowrap">
              <form class="inline" method="post" action="{{ url_for('agent_action_apply', aid=a.id) }}"><input type="hidden" name="session" value="{{ sid }}"><button class="btn small primary" type="submit">{{ 'Acknowledge' if a.card else 'Approve' }}</button></form>
              <form class="inline" method="post" action="{{ url_for('agent_action_dismiss', aid=a.id) }}"><input type="hidden" name="session" value="{{ sid }}"><button class="btn small" type="submit">Dismiss</button></form>
            </div>
          </div>
          {% if a.card_html %}<div style="margin:6px 0 0">{{ a.card_html|safe }}</div>{% endif %}
          {% if a.card_fields %}<div style="margin:6px 0 0">{% for f in a.card_fields %}<div style="display:flex;gap:8px;font-size:.85rem"><span class="sub" style="flex:0 0 34%">{{ f.label }}</span><span>{{ f.value }}</span></div>{% endfor %}</div>{% endif %}
          <div class="note">{{ 'Nothing changes until you click.' if a.card else 'Nothing happens until you click Approve.' }}</div>
        </div>
        {% endfor %}
      </div>
      {% endif %}
      <form id="aform" class="composer" method="post" action="{{ url_for('assistant_send') }}">
        <input type="hidden" name="session" value="{{ sid }}">
        <div class="am-ctx" id="amctx" hidden></div>
        <textarea name="message" id="msg" rows="1" enterkeyhint="send" placeholder="Message the assistant…">{{ (request.args.get('prompt') or '')[:1000] }}</textarea>
        <div class="comp-row">
          <span class="sub" style="font-size:.78rem">Enter sends · Shift+Enter new line</span>
          <span class="row" style="margin-left:auto">
            <button class="btn danger" type="button" id="astop" style="display:none">Stop</button>
            <button class="btn primary" type="submit" id="asend">Send</button>
          </span>
        </div>
        <div class="aspecline">
          <details class="aspec">
            <summary><span class="chip sm">Model: {{ llm.model or 'none' }}</span> <span class="sub">assistant details</span></summary>
            <div class="aspec-body">
              <div class="aspec-grp"><b>Can do directly</b><span class="aspec-chips">{% for c in spec.auto %}<span class="chip sm{{ ' plug' if c.plug else '' }}">{{ c.label }}</span>{% else %}<span class="sub">nothing yet</span>{% endfor %}</span></div>
              <div class="aspec-grp"><b>Asks first</b><span class="aspec-chips">{% for c in spec.ask %}<span class="chip sm{{ ' plug' if c.plug else '' }}">{{ c.label }}</span>{% else %}<span class="sub">nothing yet</span>{% endfor %}</span></div>
              <div class="aspec-grp"><b>Off</b><span class="aspec-chips">{% for c in spec.off %}<span class="chip sm{{ ' plug' if c.plug else '' }}">{{ c.label }}</span>{% else %}<span class="sub">none</span>{% endfor %}</span></div>
              <a class="sub" href="{{ url_for('settings') }}#ai-perms">Edit permissions &#8599;</a>
            </div>
          </details>
          {% if convo %}<a href="#" id="aclear" class="sub">clear this chat</a>{% endif %}
        </div>
      </form>
      {% if convo %}<form id="clearform" method="post" action="{{ url_for('assistant_clear') }}"><input type="hidden" name="session" value="{{ sid }}"></form>{% endif %}
    </div>
  </div>
</div>
<div class="sheet-ov hidden" id="asheet-ov">
  <div class="sheet" role="dialog" aria-modal="true" aria-label="Chat history">
    <div class="sheet-h"><b>Chats</b><button type="button" class="iconbtn" id="asheet-x" aria-label="Close">✕</button></div>
    <div class="sheet-b" id="asheet-b"></div>
  </div>
</div>
<script>
(function(){
  var clearLink=document.getElementById('aclear'), clearForm=document.getElementById('clearform');
  if(clearLink && clearForm) clearLink.addEventListener('click', function(e){ e.preventDefault(); if(confirm('Clear this chat?')) clearForm.submit(); });
  function initAssistant(){
  var inst = window.assistantChat({
    root: document.getElementById('convo'),
    form: document.getElementById('aform'),
    ta: document.getElementById('msg'),
    sendBtn: document.getElementById('asend'),
    stopBtn: document.getElementById('astop'),
    jump: document.getElementById('jump'),
    sessionId: {{ sid }},
    onSession: function(ns){ try{ history.replaceState(null, '', '/assistant/s/' + ns); }catch(e){} }
  });
  if(inst && inst.attach) inst.attach();  /* a reloaded page re-joins an in-flight turn */
  var ov=document.getElementById('asheet-ov'), rail=document.querySelector('.assistant-rail'), hbtn=document.getElementById('ahist');
  if(ov && rail && window.matchMedia && window.matchMedia('(max-width:767px)').matches){
    document.getElementById('asheet-b').appendChild(rail);
    var closeSheet=function(){ ov.classList.add('hidden'); };
    hbtn.addEventListener('click', function(){ ov.classList.remove('hidden'); });
    document.getElementById('asheet-x').addEventListener('click', closeSheet);
    ov.addEventListener('click', function(e){ if(e.target===ov) closeSheet(); });
  } else if(ov){ ov.remove(); }
  var f=document.getElementById('chatfilter');
  if(f){ f.addEventListener('input', function(){
    var q=f.value.trim().toLowerCase();
    [].slice.call(document.querySelectorAll('.assistant-rail .arow')).forEach(function(a){
      var t=(a.querySelector('.t')||{}).textContent||'';
      a.style.display=(!q || t.toLowerCase().indexOf(q)>=0)?'':'none'; });
    [].slice.call(document.querySelectorAll('.assistant-rail .rgroup')).forEach(function(g){
      var n=g.nextElementSibling, any=false;
      while(n && !n.classList.contains('rgroup')){ if(n.classList.contains('arow') && n.style.display!=='none'){ any=true; break; } n=n.nextElementSibling; }
      g.style.display=any?'':'none'; });
  }); }
  }
  if(document.readyState === 'loading'){ document.addEventListener('DOMContentLoaded', initAssistant); }
  else { initAssistant(); }
})();
</script>
"""


def _proposal_view(p):
    if (p.get("kind") or "rule") == "flow":
        upd = p.get("updates_flow") or None
        return {
            "kind": "flow",
            "name": p.get("name", ""),
            "match_mode": p.get("match_mode", "all"),
            "placement": "",
            "summary": engine._flow_when_text({"conditions": json.dumps(p.get("conditions", [])),
                                               "match_mode": p.get("match_mode", "all")}),
            "actions_summary": engine._flow_steps_text(p.get("steps", [])),
            "rationale": p.get("rationale", ""),
            "similar": None,
            "similar_actions": "",
            "applied": bool(p.get("applied")),
            "updates": upd,
            "updates_actions": engine._flow_steps_text((upd or {}).get("steps", [])) if upd else "",
        }
    sim = p.get("similar_rule") or None
    upd = p.get("updates_rule") or None
    return {
        "name": p.get("name", ""),
        "match_mode": p.get("match_mode", "all"),
        "placement": p.get("placement", ""),
        "summary": summarize_conditions({"conditions": json.dumps(p.get("conditions", [])),
                                         "match_mode": p.get("match_mode", "all")}),
        "actions_summary": summarize_actions({"actions": json.dumps(p.get("actions", {}))}),
        "rationale": p.get("rationale", ""),
        "similar": sim,
        "similar_actions": summarize_actions({"actions": json.dumps((sim or {}).get("actions", {}))}) if sim else "",
        "applied": bool(p.get("applied")),
        "updates": upd,
        "updates_actions": summarize_actions({"actions": json.dumps((upd or {}).get("actions", {}))}) if upd else "",
    }


@app.route("/assistant")
def assistant():
    # clicking the Assistant tab starts a fresh chat (a recent empty chat is reused).
    # Rendered directly instead of 302-redirected: Turbo's followRedirect() would
    # render this response, then propose a second "replace" visit to the final URL -
    # two full page transitions for one tap. The template replaceState()s the URL.
    return _assistant_page(store.find_or_create_session())


ASSIST_SUGGESTIONS = {
    "assistant": [
        {"label": "What can you do?", "prompt": "What can you do? List your capabilities briefly."},
        {"label": "Summarize my inbox", "prompt": "Give me a quick overview of what is in my inbox right now."},
        {"label": "Suggest rules for me", "prompt": "What rules would you suggest for my inbox?"},
        {"label": "Find an old invoice", "prompt": "Find the last invoice a vendor sent me and summarise it."},
    ],
    "dashboard": [
        {"label": "What needs a reply?", "prompt": "Which recent emails need a reply from me? Rank them by importance."},
        {"label": "Problems to fix?", "prompt": "Are there any errors, parked mail, or connection problems I should fix?"},
        {"label": "Summarize today's mail", "prompt": "Summarize today's new mail for me."},
        {"label": "Clean up newsletters", "prompt": "Find newsletters older than two weeks and propose how to clean them up."},
    ],
    "messages": [
        {"label": "Which need replies?", "prompt": "Which recent emails still need a reply from me?"},
        {"label": "Oldest unread", "prompt": "Find my oldest unread email and tell me what it wants."},
        {"label": "Who mails me most?", "prompt": "Which senders email me the most, and is any of it worth muting?"},
    ],
    "rules": [
        {"label": "Explain my rules", "prompt": "Explain what my current rules do, in list order."},
        {"label": "Any overlaps?", "prompt": "Are any of my rules overlapping or duplicated? Suggest a cleanup."},
        {"label": "New rule for invoices", "prompt": "Propose a rule that files invoice emails into the right folder."},
    ],
    "flows": [
        {"label": "Explain my flows", "prompt": "Explain what my flows do and when they run."},
        {"label": "Suggest an AI flow", "prompt": "Suggest a flow that uses an AI category or topic filter to help my inbox."},
        {"label": "Check for conflicts", "prompt": "Do any of my flows or rules conflict with each other?"},
    ],
    "classifiers": [
        {"label": "How are they doing?", "prompt": "Evaluate my heuristic classifiers - which are strong, which are weak?"},
        {"label": "What should I train?", "prompt": "Based on my tags, which category should get a trained classifier next?"},
    ],
    "templates": [
        {"label": "Suggest a template", "prompt": "Suggest a reply template worth adding for my common mail."},
        {"label": "Improve my templates", "prompt": "Review my reply templates and suggest improvements."},
    ],
    "settings": [
        {"label": "What am I running?", "prompt": "What LLM, embedding, and reranker setup am I running right now?"},
        {"label": "Check for problems", "prompt": "Check the app status and tell me if anything is misconfigured."},
    ],
    "log": [
        {"label": "Any errors today?", "prompt": "Summarize any errors in the event log from the last day."},
        {"label": "What moved mail?", "prompt": "What has been moving my mail in the last day - rules, flows, or the LLM?"},
    ],
    "simulate": [
        {"label": "Fill a draft to test a flow", "prompt": "Help me test one of my flows: pick a suitable flow, fill the simulator draft with an example that would exercise it, then tell me what you filled in."},
        {"label": "Fill a draft to test a rule", "prompt": "Help me test one of my rules: pick a suitable rule, fill the simulator draft with an example that would exercise it, then tell me what you filled in."},
        {"label": "How does this work?", "prompt": "Explain what the simulator does and how the Prefill from / Generate an example draft options work."},
    ],
    "accounts": [
        {"label": "Is my connection healthy?", "prompt": "Check my mail connection and token status and report anything wrong."},
    ],
    "default": [
        {"label": "What needs a reply?", "prompt": "Which recent emails need a reply from me?"},
        {"label": "Suggest rules for me", "prompt": "What rules would you suggest for my inbox?"},
        {"label": "Summarize my inbox", "prompt": "Give me a quick overview of what is in my inbox right now."},
    ],
}


def _suggestions_for_path(path):
    """Contextual starter prompts for the assistant, keyed by the page path."""
    parts = [seg for seg in (path or "/").split("?")[0].split("/") if seg]
    key = "default"
    if not parts:
        key = "dashboard"
    else:
        seg = parts[0]
        if seg == "messages":
            key = "message" if len(parts) >= 2 else "messages"
        elif seg in ("rules", "flows", "classifiers", "templates", "settings",
                     "log", "accounts", "simulate"):
            key = seg
        elif seg == "proxy":
            key = "log"
        elif seg == "assistant":
            key = "assistant"
    if key == "message":
        try:
            mid = int(parts[1])
        except (ValueError, IndexError):
            mid = 0
        msg = store.get_message(mid) if mid else None
        if msg:
            subj = (msg.get("subject") or "").strip()
            tail = (" (" + subj[:70] + ")") if subj else ""
            return [
                {"label": "Summarize this email", "prompt": "Summarize message %d%s and tell me what it wants from me." % (mid, tail)},
                {"label": "Suggest a reply", "prompt": "Suggest a short reply I could send for message %d%s." % (mid, tail)},
                {"label": "Any deadline?", "prompt": "Does message %d mention a deadline or due date? Read it and tell me." % mid},
            ]
        key = "messages"
    if key == "flows" and len(parts) >= 2 and parts[1].isdigit():
        fid = int(parts[1])
        fl = store.get_flow(fid)
        if fl:
            nm = (fl.get("name") or "#%d" % fid)[:60]
            chips = [{"label": "Test it in the simulator \u2192", "href": "/simulate?flow=%d" % fid}]
            if len(parts) >= 3 and parts[2] == "edit":
                chips.append({"label": "Complete this form with AI",
                              "prompt": ("Look at the flow draft I have open and fill in the missing or weak "
                                         "parts of the form (filters and steps) with fill_flow, then tell me in "
                                         "one line what you changed.")})
            chips += [
                {"label": "Explain this flow", "prompt": "Explain what flow #%d (\u201c%s\u201d) does, in two lines." % (fid, nm)},
                {"label": "When does it fire?", "prompt": "Walk me through when flow #%d (\u201c%s\u201d) fires and what each step does." % (fid, nm)},
            ]
            return chips
    if key == "flows" and len(parts) >= 2 and parts[1] == "new":
        return [
            {"label": "Fill an example flow", "prompt": ("Fill this new-flow form with a simple example flow - a filter plus "
                                                          "two steps - using fill_flow, then tell me in one line what you filled.")},
            {"label": "Explain the canvas", "prompt": "Explain the parts of the flow editor I am looking at: filters, match all/any, and the step types."},
        ]
    if key == "rules" and len(parts) >= 2 and parts[1].isdigit():
        rid = int(parts[1])
        ru = store.get_rule(rid)
        if ru:
            nm = (ru.get("name") or "#%d" % rid)[:60]
            return [
                {"label": "Test it in the simulator \u2192", "href": "/simulate?rule=%d" % rid},
                {"label": "Explain this rule", "prompt": "Explain what rule #%d (\u201c%s\u201d) matches and does, in two lines." % (rid, nm)},
                {"label": "Any conflicts?", "prompt": "Would rule #%d (\u201c%s\u201d) conflict with my other rules? Check match order too." % (rid, nm)},
            ]
    chips = list(ASSIST_SUGGESTIONS.get(key) or ASSIST_SUGGESTIONS["default"])
    if key in ('default', 'dashboard', 'assistant'):
        for pid, label, prompt in [('mt-invoice-finder', 'Find invoices', 'Find recent invoices using the Invoice finder plugin.'),
                                   ('mt-daily-digest', 'Daily digest', 'Give me a daily digest using the Daily digest plugin.')]:
            row = plugins.get(pid)
            if row and row.get('enabled') and engine.agent_permissions().get('plugin:' + pid) != 'off':
                chips.append({'label': label, 'prompt': prompt})
        # the invoice-finder plugin chip supersedes the built-in "Find an old invoice" prompt
        if any(c.get('label') == 'Find invoices' for c in chips):
            chips = [c for c in chips if c.get('label') != 'Find an old invoice']
    return chips


def _assistant_prep(convo, active=False):
    for m in convo:
        m["proposals_list"] = []
        if m.get("role") == "assistant" and m.get("proposals"):
            try:
                m["proposals_list"] = [_proposal_view(p) for p in json.loads(m["proposals"])]
            except (TypeError, ValueError):
                pass
        m["reasoning"] = ""
        m["reasoning_summary"] = ""
        m["tool_steps"] = []
        m["when"] = fmt_ts(m.get("ts"))
        if m.get("role") == "assistant" and m.get("meta"):
            try:
                meta = json.loads(m["meta"])
                m["reasoning"] = meta.get("reasoning") or ""
                m["reasoning_summary"] = meta.get("reasoning_summary") or ""
                m["tool_steps"] = meta.get("tools") or []
            except (TypeError, ValueError):
                pass
    if convo and convo[-1].get("role") == "assistant":
        convo[-1]["regen"] = True
    elif convo and convo[-1].get("role") == "user" and not active:
        # no reply followed (stopped, failed, or the server restarted): offer retry
        convo[-1]["retry"] = True
    return convo


def _assistant_fragment(sid, ctx_path="", back=""):
    live = _assistant_active_run(sid)
    convo = _assistant_prep(store.session_messages(sid), active=live is not None)
    return convo, _render_src(CONVO_TMPL, convo=convo, sid=sid,
                              suggest=_suggestions_for_path(ctx_path), back=back,
                              live_run=(live.id if live else 0))


def _assistant_page(sid):
    if not store.get_session(sid):
        flash("That chat no longer exists.", "err")
        return redirect(url_for("assistant"))
    sessions = store.list_sessions()
    off = tz_offset_hours() * 3600.0
    today = int((time.time() + off) // 86400)
    for s in sessions:
        ts = s.get("last_ts") or s.get("created") or 0
        d = int((ts + off) // 86400) if ts else -1
        s["group"] = "Today" if d == today else ("Yesterday" if d == today - 1 else "Earlier")
        s["when"] = time.strftime("%H:%M" if d == today else "%m-%d", time.gmtime(ts + off)) if ts else ""
        s["active"] = _assistant_active_run(s["id"]) is not None
    convo, convo_html = _assistant_fragment(sid, "/assistant",
                                            url_for("assistant_session", sid=sid))
    pending = store.pending_agent_actions()
    for pa in pending:
        pa["when_h"] = fmt_ts(pa.get("created_ts"))
        try:
            pobj = json.loads(pa.get("payload") or "{}")
        except (TypeError, ValueError):
            pobj = {}
        card = pobj.get("card") if isinstance(pobj, dict) else None
        pa["card"] = card if isinstance(card, dict) else None
        pa["card_html"] = md_to_html(card.get("markdown") or "") if pa["card"] else ""
        pa["card_fields"] = (card.get("fields") or [])[:16] if pa["card"] else []
    try:
        perms = engine.agent_permissions()
    except Exception:
        perms = {}
    spec = {"auto": [], "ask": [], "off": []}
    for cap, label, _risk, _tools, _desc in engine.AGENT_CAPS:
        spec.setdefault(perms.get(cap, "off"), []).append({"label": label, "plug": False})
    for cap, lvl in perms.items():
        if cap.startswith("plugin:") and lvl in spec:
            pid = cap.split(":", 1)[1]
            row = plugins.get(pid)
            label = plugins.action_label(row) if row else pid
            spec[lvl].append({"label": label or pid, "plug": True})
    _lc = engine.llm_config()
    asst_fb = bool(_lc.get("assistant_use_fallback") and _lc.get("fallback"))
    asst_fb_model = (_lc["fallback"] or {}).get("model") if asst_fb else ""
    return render(_render_src(
        ASSISTANT_TMPL, sid=sid, sessions=sessions, convo=convo, convo_html=convo_html,
        llm=_lc, pending=pending, spec=spec, asst_fb=asst_fb, asst_fb_model=asst_fb_model))


@app.route("/assistant/s/<int:sid>")
def assistant_session(sid):
    return _assistant_page(sid)


@app.route("/assistant/panel")
def assistant_panel():
    try:
        sid = int(request.args.get("sid") or 0)
    except (TypeError, ValueError):
        sid = 0
    if not sid or not store.get_session(sid):
        return ("no such chat", 404)
    pth = request.args.get("path") or ""
    _, convo_html = _assistant_fragment(sid, pth, pth)
    return Response(convo_html, mimetype="text/html")


@app.route("/assistant/sessions.json")
def assistant_sessions_json():
    out = []
    for s in store.list_sessions():
        out.append({"id": s["id"], "title": s["title"] or "Untitled chat",
                    "when": fmt_ts(s["last_ts"] or s["created"]), "n": s["n"],
                    "active": _assistant_active_run(s["id"]) is not None})
    return Response(json.dumps({"sessions": out}), mimetype="application/json")


@app.route("/assistant/new.json", methods=["POST"])
def assistant_new_json():
    return Response(json.dumps({"sid": store.find_or_create_session()}),
                    mimetype="application/json")


@app.route("/assistant/session/<int:sid>/delete", methods=["POST"])
def assistant_session_delete(sid):
    store.delete_session(sid)
    if request.form.get("json"):
        return Response(json.dumps({"ok": True}), mimetype="application/json")
    flash("Chat deleted.", "ok")
    return redirect(url_for("assistant"))


def _assistant_sid_from_form():
    try:
        sid = int(request.form.get("session") or 0)
    except (TypeError, ValueError):
        sid = 0
    if not sid or not store.get_session(sid):
        sid = store.find_or_create_session()
    return sid


def _sse(event, data):
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))


# ------------------------------------------------- assistant background runs
#
# A turn used to live inside the HTTP response: closing the tab (or the phone
# sleeping) killed the SSE connection and with it the generator, losing the
# query. Runs now execute in a per-session background worker. The browser tails
# the worker's events; a disconnect only drops a subscriber, and the reply is
# persisted exactly as before. /assistant/live re-attaches a reloaded page to a
# run still in flight, and a trailing user message with no reply (stopped,
# failed, or the server restarted mid-turn) renders a retry button.

_ASSISTANT_RUN_GRACE = 45     # seconds a finished run stays replayable
_ASSISTANT_RUNS = {}          # session id -> [AssistantRun, ...] in start order
_ASSISTANT_RUNS_LOCK = threading.Lock()
_ASSISTANT_RUN_ID = 0


class AssistantRun:
    """One user turn executing off the request thread.

    Events (UI-shaped, proposals already view-converted) accumulate under
    ``cond`` so any number of SSE subscribers can replay from index 0 and then
    follow live; the DB keeps only the final transcript as before."""

    __slots__ = ("id", "sid", "text", "store_user", "page_path", "page_state",
                 "cond", "events", "done", "finished", "msg_id", "user_msg_id",
                 "error", "cancelled", "terminal", "prev", "finished_event", "thread")

    def __init__(self, run_id, sid, text, store_user, page_path, page_state, prev):
        self.id = run_id
        self.sid = int(sid or 0)
        self.text = text
        self.store_user = bool(store_user)
        self.page_path = page_path
        self.page_state = page_state
        self.cond = threading.Condition()
        self.events = []
        self.done = False
        self.finished = None
        self.msg_id = None
        self.user_msg_id = None
        self.error = None
        self.cancelled = False
        self.terminal = False
        self.prev = prev
        self.finished_event = threading.Event()
        self.thread = None


def _assistant_emit(run, etype, data=None):
    with run.cond:
        run.events.append({"event": etype, "data": data or {}})
        run.cond.notify_all()


def _assistant_run_worker(run):
    if run.prev is not None:
        run.prev.finished_event.wait()
    agent = None
    gen = None
    try:
        if run.store_user:
            run.user_msg_id = store.add_assistant_message(
                "user", run.text[:4000], session_id=run.sid)
            _assistant_emit(run, "user_saved", {"message_id": run.user_msg_id})
        agent = engine.AssistantAgent(session_id=run.sid, page_path=run.page_path,
                                      page_state=run.page_state)
        gen = agent.stream(run.text, store_user=False)
        for ev in gen:
            if run.cancelled:
                break
            etype = ev.pop("type")
            if etype == "proposals":
                try:
                    ev["proposals"] = [_proposal_view(pp)
                                       for pp in ev.get("proposals") or []]
                except Exception:
                    pass
            if etype == "done":
                run.msg_id = ev.get("message_id")
            if etype in ("done", "error"):
                run.terminal = True
            _assistant_emit(run, etype, ev)
    except Exception as exc:
        if not run.terminal:
            run.terminal = True
            run.error = repr(exc)
            try:
                store.log_event("error", "assistant run failed: %r" % exc)
            except Exception:
                pass
            _assistant_emit(run, "error", {"message": "assistant failed: %r" % exc})
    finally:
        if gen is not None:
            try:
                gen.close()
            except Exception:
                pass
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass
        if run.cancelled and not run.terminal:
            _assistant_emit(run, "stopped", {})
        with run.cond:
            run.done = True
            run.finished = time.time()
            run.cond.notify_all()
        run.finished_event.set()


def _assistant_prune(sid=None):
    now = time.time()
    with _ASSISTANT_RUNS_LOCK:
        for s in ([int(sid)] if sid else list(_ASSISTANT_RUNS)):
            runs = [r for r in _ASSISTANT_RUNS.get(s) or []
                    if not r.done or (now - (r.finished or now)) < _ASSISTANT_RUN_GRACE]
            if runs:
                _ASSISTANT_RUNS[s] = runs
            else:
                _ASSISTANT_RUNS.pop(s, None)


def _assistant_start_run(sid, text, store_user=True, page_path="", page_state=""):
    global _ASSISTANT_RUN_ID
    _assistant_prune(sid)
    with _ASSISTANT_RUNS_LOCK:
        runs = _ASSISTANT_RUNS.setdefault(int(sid or 0), [])
        _ASSISTANT_RUN_ID += 1
        run = AssistantRun(_ASSISTANT_RUN_ID, sid, text, store_user,
                           page_path, page_state, runs[-1] if runs else None)
        runs.append(run)
    t = threading.Thread(target=_assistant_run_worker, args=(run,), daemon=True,
                         name="assistant-turn-%d" % run.id)
    run.thread = t
    t.start()
    return run


def _assistant_active_run(sid):
    """The oldest run for this chat still executing (None when idle)."""
    with _ASSISTANT_RUNS_LOCK:
        for r in _ASSISTANT_RUNS.get(int(sid or 0)) or []:
            if not r.done:
                return r
    return None


def _assistant_live_run(sid):
    """The run a reloaded page should attach to: the active one, else the most
    recently finished one still inside the replay grace window."""
    _assistant_prune(sid)
    with _ASSISTANT_RUNS_LOCK:
        runs = list(_ASSISTANT_RUNS.get(int(sid or 0)) or [])
    for r in runs:
        if not r.done:
            return r
    return runs[-1] if runs else None


def _assistant_tail(run, cursor=0):
    """Yield SSE frames for run events from ``cursor``, then follow live.

    Never holds the run lock across a yield: subscribers are independent."""
    while True:
        with run.cond:
            if cursor >= len(run.events) and not run.done:
                run.cond.wait(10)
            if cursor < len(run.events):
                ev = run.events[cursor]
                cursor += 1
            else:
                ev = None
        if ev is not None:
            yield _sse(ev["event"], ev["data"])
        elif run.done:
            return
        else:
            yield ": keep-alive\n\n"


def _sse_response(gen):
    resp = Response(gen, mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


def _assistant_run_response(run, sid):
    def gen():
        yield _sse("session", {"sid": sid, "run": run.id})
        for chunk in _assistant_tail(run):
            yield chunk
    return _sse_response(gen())


@app.route("/assistant/context.json")
def assistant_context_json():
    path = (request.args.get("path") or "")[:300]
    _kind, desc, _block, key = engine.assistant_page_context(path)
    return Response(json.dumps({"desc": desc, "key": key}), mimetype="application/json")


@app.route("/assistant/stream", methods=["POST"])
def assistant_stream():
    """Start (or queue) one assistant turn and SSE-tail its background run.
    Disconnecting only detaches this browser; the run keeps going and its reply
    is persisted, so the chat can be reopened or re-attached later."""
    text = (request.form.get("message") or "").strip()
    sid = _assistant_sid_from_form()
    page_path = request.form.get("path") or ""
    page_state = (request.form.get("state") or "")[:60000]
    if not text:
        def gen_empty():
            yield _sse("session", {"sid": sid})
            yield _sse("error", {"message": "empty message"})
        return _sse_response(gen_empty())
    run = _assistant_start_run(sid, text, page_path=page_path, page_state=page_state)
    return _assistant_run_response(run, sid)


@app.route("/assistant/live")
def assistant_live():
    """Re-attach a reloaded page to a chat's in-flight (or just-finished) run,
    replaying every event from the start so the transcript rebuilds."""
    try:
        sid = int(request.args.get("sid") or 0)
    except (TypeError, ValueError):
        sid = 0
    run = _assistant_live_run(sid) if sid else None
    if run is None:
        return Response("no live run for this chat", status=404, mimetype="text/plain")
    return _assistant_run_response(run, sid)


@app.route("/assistant/stop", methods=["POST"])
def assistant_stop():
    """Cooperative cancel: the worker stops before its next event and persists
    no reply, leaving the trailing user message retry-able."""
    try:
        sid = int(request.form.get("session") or 0)
    except (TypeError, ValueError):
        sid = 0
    try:
        rid = int(request.form.get("run") or 0)
    except (TypeError, ValueError):
        rid = 0
    stopped = 0
    with _ASSISTANT_RUNS_LOCK:
        for r in _ASSISTANT_RUNS.get(sid) or []:
            if r.done or (rid and r.id != rid):
                continue
            r.cancelled = True
            stopped += 1
            if rid:
                break
    return jsonify({"stopped": stopped})


@app.route("/assistant/regenerate", methods=["POST"])
def assistant_regenerate():
    """SSE stream that re-runs a turn without duplicating the user row: a
    trailing assistant reply is replaced, a trailing user message (no reply
    followed) is retried as-is."""
    sid = _assistant_sid_from_form()
    page_path = request.form.get("path") or ""
    page_state = (request.form.get("state") or "")[:60000]
    try:
        mid = int(request.form.get("mid") or 0)
    except (TypeError, ValueError):
        mid = 0
    msgs = store.session_messages(sid)
    last = msgs[-1] if msgs else None
    prev = msgs[-2] if len(msgs) > 1 else None
    text = None
    if (last and last.get("role") == "assistant"
            and (not mid or mid == last.get("id"))
            and prev and prev.get("role") == "user"):
        store.delete_assistant_message(last["id"])
        text = prev["content"]
    elif last and last.get("role") == "user" and (not mid or mid == last.get("id")):
        text = last["content"]
    if text is None:
        def gen_err():
            yield _sse("session", {"sid": sid})
            yield _sse("error", {"message": "nothing to regenerate"})
        return _sse_response(gen_err())
    run = _assistant_start_run(sid, text, store_user=False,
                               page_path=page_path, page_state=page_state)
    return _assistant_run_response(run, sid)


@app.route("/assistant/send", methods=["POST"])
def assistant_send():
    text = (request.form.get("message") or "").strip()
    sid = _assistant_sid_from_form()
    if not text:
        flash("Type a message first.", "err")
        return redirect(url_for("assistant_session", sid=sid))
    # no-JS fallback: run the same background turn and block for it. Closing the
    # tab does not cancel the run - it finishes and the reply is in the chat.
    run = _assistant_start_run(sid, text)
    run.finished_event.wait(1800)
    if run.error:
        flash("Assistant error: %s" % run.error, "err")
    return redirect(url_for("assistant_session", sid=sid))


def _safe_back(v, fallback):
    """Local-only redirect target (no open redirects, no scheme-relative URLs)."""
    v = (v or "").strip()
    if v.startswith("/") and not v.startswith("//") and "\\" not in v and "\r" not in v and "\n" not in v:
        return v
    return fallback


def _mark_proposal_applied(mid, proposals, idx, mode):
    proposals[idx]["applied"] = {"ts": time.time(), "mode": mode}
    store.set_assistant_proposals(mid, json.dumps(proposals))


@app.route("/assistant/apply", methods=["POST"])
def assistant_apply():
    try:
        mid = int(request.form.get("msg_id") or 0)
        idx = int(request.form.get("idx") or 0)
    except ValueError:
        mid, idx = 0, 0
    try:
        sid = int(request.form.get("session") or 0)
    except (TypeError, ValueError):
        sid = 0
    back_default = (url_for("assistant_session", sid=sid)
                    if sid and store.get_session(sid) else url_for("assistant"))
    back = _safe_back(request.form.get("back"), back_default)
    disabled = bool(request.form.get("disabled"))
    row = store.get_assistant_message(mid)
    proposals = []
    if row:
        try:
            proposals = json.loads(row.get("proposals") or "[]")
        except (TypeError, ValueError):
            proposals = []
    if not (0 <= idx < len(proposals)):
        flash("That proposal is no longer available.", "err")
        return redirect(back)
    prop = proposals[idx]
    if prop.get("applied"):
        flash("That proposal was already added - nothing to do. Ask the assistant if you want another version.", "warn")
        return redirect(back)
    mode = (request.form.get("mode") or "add").strip()
    if (prop.get("kind") or "rule") == "flow":
        norm = engine.normalize_flow(prop) or prop
        if mode == "update":
            try:
                fid = int(request.form.get("rule_id") or 0)
            except ValueError:
                fid = 0
            target = store.get_flow(fid) if fid else None
            if target is None:
                flash("That flow no longer exists - nothing updated.", "err")
                return redirect(back)
            store.update_flow(fid, name=norm.get("name") or target["name"],
                              match_mode=norm.get("match_mode", "all"),
                              conditions=json.dumps(norm.get("conditions", [])),
                              actions=json.dumps(norm.get("steps", [])))
            _mark_proposal_applied(mid, proposals, idx, "update")
            store.log_event("info", "assistant updated flow #%d '%s'" % (fid, target["name"]))
            flash("Flow #%d '%s' updated." % (fid, target["name"]), "ok")
            return redirect(back)
        store.add_flow(norm.get("name", "Assistant flow"), norm.get("match_mode", "all"),
                       norm.get("conditions", []), norm.get("steps", []), enabled=not disabled)
        _mark_proposal_applied(mid, proposals, idx, mode or "add")
        store.log_event("info", "assistant flow '%s' added (%s)"
                        % (norm.get("name"), "disabled" if disabled else "enabled"))
        flash("Flow '%s' added%s - see it on the Flows page; the dry-run toggle lives in Settings."
              % (norm.get("name"), " (disabled)" if disabled else ""), "ok")
        return redirect(back)
    norm = engine.normalize_rule(prop) or prop
    if mode == "update":
        try:
            rid = int(request.form.get("rule_id") or 0)
        except ValueError:
            rid = 0
        target = store.get_rule(rid) if rid else None
        if target is None:
            flash("That rule no longer exists - nothing updated.", "err")
            return redirect(back)
        store.update_rule(rid, name=norm.get("name") or target["name"],
                          match_mode=norm.get("match_mode", "all"),
                          conditions=json.dumps(norm.get("conditions", [])),
                          actions=json.dumps(norm.get("actions", {})))
        _mark_proposal_applied(mid, proposals, idx, "update")
        store.log_event("info", "assistant updated rule #%d '%s'" % (rid, target["name"]))
        flash("Rule #%d '%s' updated - no duplicate added." % (rid, target["name"]), "ok")
        return redirect(back)
    store.add_rule(norm.get("name", "Assistant rule"), norm.get("match_mode", "all"),
                   norm.get("conditions", []), norm.get("actions", {}), enabled=not disabled,
                   position=norm.get("placement") or "bottom")
    _mark_proposal_applied(mid, proposals, idx, mode or "add")
    store.log_event("info", "assistant rule '%s' added (%s)"
                    % (norm.get("name"), "disabled" if disabled else "enabled"))
    flash("Rule '%s' added%s - check it on the Rules page (the Test button dry-runs it against recent mail)."
          % (norm.get("name"), " (disabled)" if disabled else ""), "ok")
    return redirect(back)


@app.route("/agent/actions/<int:aid>/apply", methods=["POST"])
def agent_action_apply(aid):
    row = store.get_agent_action(aid)
    sid = 0
    try:
        sid = int(request.form.get("session") or (row or {}).get("session_id") or 0)
    except (TypeError, ValueError):
        sid = 0
    back = (url_for("assistant_session", sid=sid)
            if sid and store.get_session(sid) else url_for("assistant"))
    if not row or row.get("status") != "pending":
        flash("That approval is no longer pending.", "warn")
        return redirect(back)
    payload = {}
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    if isinstance(payload, dict) and payload.get("card") and not payload.get("tool"):
        # informational card from a plugin's ctx.action.propose() - there is no
        # tool to execute; acknowledging just files it.
        title = str((payload.get("card") or {}).get("title") or row.get("preview")
                    or "plugin card")[:160]
        store.set_agent_action(aid, "applied",
                               json.dumps({"summary": title, "ok": True, "card": True},
                                          ensure_ascii=False))
        store.log_event("info", "agent action #%s (plugin card) acknowledged by user: %s"
                        % (aid, title))
        flash("Acknowledged: " + title, "ok")
        return redirect(back)
    agent = engine.AssistantAgent(session_id=sid)
    try:
        res = agent.call_tool(payload.get("tool") or row.get("tool"),
                              payload.get("args") or {}, approved=True)
    finally:
        agent.close()
    ok = bool(res.get("ok"))
    summ = res.get("summary") or ("done" if ok else "failed")
    store.set_agent_action(aid, "applied" if ok else "failed",
                           json.dumps({"summary": summ, "ok": ok}, ensure_ascii=False))
    store.log_event("info" if ok else "error",
                    "agent action #%s (%s) %s by user: %s"
                    % (aid, row.get("tool"), "applied" if ok else "failed", summ))
    flash(("Approved: " if ok else "Failed: ") + summ, "ok" if ok else "err")
    return redirect(back)


@app.route("/agent/actions/<int:aid>/dismiss", methods=["POST"])
def agent_action_dismiss(aid):
    row = store.get_agent_action(aid)
    sid = 0
    try:
        sid = int(request.form.get("session") or (row or {}).get("session_id") or 0)
    except (TypeError, ValueError):
        sid = 0
    back = (url_for("assistant_session", sid=sid)
            if sid and store.get_session(sid) else url_for("assistant"))
    if row and row.get("status") == "pending":
        store.set_agent_action(aid, "dismissed")
        store.log_event("info", "agent action #%s (%s) dismissed by user" % (aid, row.get("tool")))
        flash("Dismissed.", "ok")
    return redirect(back)


@app.route("/assistant/clear", methods=["POST"])
def assistant_clear():
    try:
        sid = int(request.form.get("session") or 0)
    except (TypeError, ValueError):
        sid = 0
    if sid and store.get_session(sid):
        store.clear_assistant(session_id=sid)
        store.log_event("info", "assistant chat #%d cleared" % sid)
        flash("Chat cleared.", "ok")
        return redirect(url_for("assistant_session", sid=sid))
    store.clear_assistant()
    flash("Chat cleared.", "ok")
    return redirect(url_for("assistant"))

def _form_int(name, default, lo=None, hi=None):
    raw = (request.form.get(name) or "").strip()
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v



def _form_float(name, default):
    raw = (request.form.get(name) or "").strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default



def _save_secret(name):
    """Password-style field: blank keeps the stored value; the paired <name>_clear checkbox clears it."""
    if name + "_clear" in request.form:
        store.set_setting(name, "")
        return
    if name in request.form:
        val = (request.form.get(name) or "").strip()
        if val:
            store.set_setting(name, val)



def _save_behavior_settings():
    f = request.form

    def has(k):
        return k in f

    if has("poll_interval"):
        store.set_setting("poll_interval", _form_int("poll_interval", 90, lo=15))
    if has("classify_concurrency"):
        store.set_setting("classify_concurrency", _form_int("classify_concurrency", 8, lo=1, hi=16))
    if has("lookback_hours"):
        store.set_setting("lookback_hours", _form_int("lookback_hours", 48, lo=1))
    if has("display_tz_offset"):
        store.set_setting("display_tz_offset", max(-14.0, min(14.0, _form_float("display_tz_offset", 8))))
    if has("watch_folders"):
        store.set_setting("watch_folders",
                          [x.strip() for x in (f.get("watch_folders") or "INBOX").split(",") if x.strip()])
    if has("my_name"):
        store.set_setting("my_name", (f.get("my_name") or "").strip())
    if has('reply_sent_folder'):
        store.set_setting('reply_sent_folder', (f.get('reply_sent_folder') or '').strip())
    if has('reply_identity_addresses'):
        aliases = [a.strip().lower() for a in (f.get('reply_identity_addresses') or '').split(',') if a.strip()]
        if any(not re.fullmatch(r'[^@\s,<>]+@[^@\s,<>]+', a) for a in aliases):
            flash('Sending aliases were not saved. Enter comma-separated email addresses.', 'err')
        else:
            store.set_setting('reply_identity_addresses', aliases)
    for k in ("rules_apply", "heuristics_enabled", "heuristic_autorefine", "llm_suggest",
              "llm_apply", "index_enabled", "rerank_enabled",
              "render_images", "flows_apply", "reply_tracking_enabled"):
        if has(k):
            store.set_setting(k, f.get(k) not in (None, "", "0"))
    for cap, _l, _r, _t, _d in engine.AGENT_CAPS:
        k = "perm_" + cap
        if has(k):
            v = (f.get(k) or "").strip().lower()
            store.set_setting(k, v if v in ("off", "ask", "auto") else "off")
    if has("sends_per_hour"):
        store.set_setting("sends_per_hour", _form_int("sends_per_hour", 5, lo=0))
    if has("index_folders"):
        store.set_setting("index_folders",
                          [x.strip() for x in (f.get("index_folders") or "").split(",") if x.strip()])
    if has("max_llm_per_hour"):
        store.set_setting("max_llm_per_hour", _form_int("max_llm_per_hour", 40, lo=0))
    if has("llm_batch_per_cycle"):
        store.set_setting("llm_batch_per_cycle", _form_int("llm_batch_per_cycle", 5, lo=1))
    if has("categories"):
        store.set_setting("categories",
                          [c.strip() for c in (f.get("categories") or "").split(",") if c.strip()])
    if has("category_folders"):
        mapping = {}
        for line in (f.get("category_folders") or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    mapping[k.strip()] = v.strip()
        store.set_setting("category_folders", mapping)
    if has("drafts_folder"):
        store.set_setting("drafts_folder", (f.get("drafts_folder") or "").strip())



def _save_llm_settings():
    f = request.form
    for k in ("llm_base_url", "llm_model", "llm_fallback_base_url", "llm_fallback_model"):
        if k in f:
            store.set_setting(k, (f.get(k) or "").strip())
    if "llm_thinking" in f:
        store.set_setting("llm_thinking", "off" if f.get("llm_thinking") == "off" else "auto")
    if "llm_timeout" in f:
        store.set_setting("llm_timeout", _form_int("llm_timeout", 0, lo=0))
    _save_secret("llm_api_key")
    _save_secret("llm_fallback_api_key")
    llm_health.trigger()  # re-probe now instead of waiting for the next poll


def _save_rag_settings():
    f = request.form
    for k in ("embed_base_url", "embed_model", "rerank_base_url", "rerank_model"):
        if k in f:
            store.set_setting(k, (f.get(k) or "").strip())
    if "embed_protocol" in f and f.get("embed_protocol") in ("tei", "openai", "local"):
        store.set_setting("embed_protocol", f.get("embed_protocol"))
    if "rerank_protocol" in f and f.get("rerank_protocol") in ("tei", "cohere", "local"):
        store.set_setting("rerank_protocol", f.get("rerank_protocol"))
    if "rag_backend" in f and f.get("rag_backend") in ("lite", "legacy"):
        store.set_setting("rag_backend", f.get("rag_backend"))
    if "local_embed_threads" in f:
        store.set_setting("local_embed_threads", _form_int("local_embed_threads", 8, lo=1))
    if "embed_timeout" in f:
        store.set_setting("embed_timeout", _form_int("embed_timeout", 0, lo=0))
    if "rerank_timeout" in f:
        store.set_setting("rerank_timeout", _form_int("rerank_timeout", 0, lo=0))
    if "embed_query_prefix" in f:
        store.set_setting("embed_query_prefix", f.get("embed_query_prefix") or "")
    if "index_refresh_minutes" in f:
        store.set_setting("index_refresh_minutes", _form_int("index_refresh_minutes", 10, lo=1))
    if "rag_exclude_folders" in f:
        store.set_setting("rag_exclude_folders",
                          [x.strip() for x in (f.get("rag_exclude_folders") or "").split(",") if x.strip()])
    _save_secret("embed_api_key")
    _save_secret("rerank_api_key")



def _save_connection_settings():
    f = request.form
    if "proxy_mode" in f:
        store.set_setting("proxy_mode",
                          "external" if f.get("proxy_mode") == "external" else "embedded")
    for k in ("imap_host", "imap_port", "imap_user", "proxy_tailnet_host"):
        if k in f:
            store.set_setting(k, (f.get(k) or "").strip())
    if "imap_tls" in f:
        v = (f.get("imap_tls") or "").strip()
        store.set_setting("imap_tls", v if v in ("0", "1") else "")
    _save_secret("imap_password")


# ---------------------------------------------------------------- endpoint test gate
# Saving CHANGED LLM / RAG endpoint values requires a passing test of those exact
# values first (suite section T63). Every Test button probes the values currently
# in the form (unsaved edits included) and records a per-side fingerprint; a save
# whose endpoint values differ from the stored ones is refused unless it matches
# the last successful test. The marker is per-process: after a restart unchanged
# values still save, changed values need a fresh test. The client mirrors this as
# a disabled Save button + inline test results; the server check stays authoritative.

_ENDPOINT_TESTED = {}  # side -> fingerprint of the last successful test

_EP_SIDES = {
    "llm:primary": {"label": "LLM endpoint", "base": "llm_base_url",
                    "key": "llm_api_key",
                    "fields": ("llm_base_url", "llm_model", "llm_api_key",
                               "llm_api_key_clear")},
    "llm:fallback": {"label": "fallback endpoint", "base": "llm_fallback_base_url",
                     "key": "llm_fallback_api_key",
                     "fields": ("llm_fallback_base_url", "llm_fallback_model",
                                "llm_fallback_api_key", "llm_fallback_api_key_clear")},
    "embed": {"label": "embedding endpoint", "base": "embed_base_url",
              "key": "embed_api_key", "protocol": "embed_protocol",
              "fields": ("embed_protocol", "embed_base_url", "embed_model",
                         "embed_api_key", "embed_api_key_clear")},
    "rerank": {"label": "reranker endpoint", "base": "rerank_base_url",
               "key": "rerank_api_key", "protocol": "rerank_protocol",
               "fields": ("rerank_protocol", "rerank_base_url", "rerank_model",
                          "rerank_api_key", "rerank_api_key_clear")},
}


def _ep_keytoken(v):
    return hashlib.sha1(("k:" + (v or "")).encode("utf-8")).hexdigest()[:12]


def _ep_fp(*parts):
    return hashlib.sha1(json.dumps([str(p) for p in parts])
                        .encode("utf-8")).hexdigest()[:16]


def _note_endpoint_test(side, fp):
    if fp:
        _ENDPOINT_TESTED[side] = fp


def _llm_overlay(f):
    """Form values for an unsaved LLM edit: present keys replace the stored setting
    (blank clears, same as a save; an armed clear checkbox wins like _save_secret)."""
    ov = {}
    for k in ("llm_base_url", "llm_model", "llm_timeout", "llm_thinking",
              "llm_fallback_base_url", "llm_fallback_model"):
        if k in f:
            ov[k] = (f.get(k) or "").strip()
    for k, clr in (("llm_api_key", "llm_api_key_clear"),
                   ("llm_fallback_api_key", "llm_fallback_api_key_clear")):
        if (f.get(clr) or "") not in ("", "0"):
            ov[k] = ""
        elif k in f:
            ov[k] = (f.get(k) or "").strip()
    return ov


def _rag_overlay(f):
    ov = {}
    for k in ("embed_protocol", "embed_base_url", "embed_model", "embed_timeout",
              "rerank_protocol", "rerank_base_url", "rerank_model", "rerank_timeout"):
        if k in f:
            ov[k] = (f.get(k) or "").strip()
    if "embed_query_prefix" in f:
        ov["embed_query_prefix"] = f.get("embed_query_prefix") or ""  # raw: never stripped
    for k, clr in (("embed_api_key", "embed_api_key_clear"),
                   ("rerank_api_key", "rerank_api_key_clear")):
        if (f.get(clr) or "") not in ("", "0"):
            ov[k] = ""
        elif k in f:
            ov[k] = (f.get(k) or "").strip()
    return ov


def _ep_changed(f, spec):
    """True when the posted endpoint-defining values differ from the stored ones.
    A blank secret field means keep-the-stored-key and never counts as a change."""
    for k in spec["fields"]:
        if k not in f:
            continue
        v = (f.get(k) or "").strip()
        if k == spec["key"]:
            if v:
                return True
            continue
        stored = store.get_setting(k)
        stored = "" if stored is None else str(stored).strip()
        if v != stored:
            return True
    return False


def _fp_llm_cfg(cfg, which):
    if which == "fallback":
        fb = cfg.get("fallback")
        if not fb:
            return None
        return _ep_fp("llm:fallback", fb.get("base"), fb.get("model"),
                      _ep_keytoken(fb.get("key")))
    return _ep_fp("llm:primary", cfg.get("base"), cfg.get("model"),
                  _ep_keytoken(cfg.get("key")))


def _fp_embed_cfg(cfg):
    return _ep_fp("embed", cfg.get("protocol"), cfg.get("base"), cfg.get("model"),
                  _ep_keytoken(cfg.get("key")))


def _fp_rerank_cfg(cfg):
    return _ep_fp("rerank", cfg.get("protocol"), cfg.get("base"), cfg.get("model"),
                  _ep_keytoken(cfg.get("key")))


def _endpoint_gate_blocked(section, f):
    """Labels of sides whose changed endpoint values were never tested ([] = ok)."""
    blocked = []
    if section == "llm":
        cfg = engine.llm_config(overlay=_llm_overlay(f))
        for side, which in (("llm:primary", "primary"), ("llm:fallback", "fallback")):
            spec = _EP_SIDES[side]
            if not _ep_changed(f, spec):
                continue
            if spec["base"] in f:
                required = bool((f.get(spec["base"]) or "").strip())
            else:
                resolved = (cfg.get("base") if which == "primary"
                            else (cfg.get("fallback") or {}).get("base"))
                required = bool((resolved or "").strip())
            if not required:
                continue  # clearing to "no endpoint" needs no test
            fp = _fp_llm_cfg(cfg, which)
            if not fp or _ENDPOINT_TESTED.get(side) != fp:
                blocked.append(spec["label"])
    elif section == "rag":
        ov = _rag_overlay(f)
        for side, cfg, fpfn in (("embed", rag.embed_config(overlay=ov), _fp_embed_cfg),
                                ("rerank", rag.rerank_config(overlay=ov), _fp_rerank_cfg)):
            spec = _EP_SIDES[side]
            if not _ep_changed(f, spec):
                continue
            if (cfg.get("protocol") or "") == "local":
                continue  # nothing remote to reach
            fp = fpfn(cfg)
            if not fp or _ENDPOINT_TESTED.get(side) != fp:
                blocked.append(spec["label"])
    return blocked


def _endpoint_gate_spec():
    """JS mirror of the save gate for the Settings page (keep in sync with _EP_SIDES)."""
    def mk(key, status, extra=None):
        sp = _EP_SIDES[key]
        d = {"key": key, "label": sp["label"], "base": sp["base"],
             "fields": list(sp["fields"]), "status": status}
        if extra:
            d.update(extra)
        return d
    return [
        {"form": "llm-form", "save": "llm-save", "gate": "llm-gate",
         "sides": [mk("llm:primary", "llm-status-primary"),
                   mk("llm:fallback", "llm-status-fallback")]},
        {"form": "rag-form", "save": "rag-save", "gate": "rag-gate",
         "sides": [mk("embed", "embed-status", {"protocol": "embed_protocol"}),
                   mk("rerank", "rerank-status", {"protocol": "rerank_protocol"})]},
    ]


SETTINGS_TMPL = """
<style>
.settings-grid{display:grid;grid-template-columns:216px minmax(0,1fr);gap:26px;align-items:start;margin-top:14px}
.setnav{position:sticky;top:14px;display:flex;flex-direction:column;gap:1px}
.setnav a{padding:6px 10px;color:var(--dim);font-size:.86rem;font-weight:500;display:block}
.setnav a:hover{background:var(--hover);color:var(--ink)}
.setnav a.on{background:var(--hover);color:var(--ink);font-weight:600}
.setnav a.sn-sub{padding-left:24px;font-size:.79rem}
.setnav .sn-h{font-size:.72rem;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--dim);padding:0 10px 6px}
.setbody{min-width:0}
.setbody section{margin-bottom:28px;scroll-margin-top:14px}
.setbody section:last-child{margin-bottom:0}
.model-picker{display:flex;flex-direction:column;gap:5px}
.model-picker .model-custom[hidden]{display:none}
.model-picker .model-status{font-size:.74rem;color:var(--dim)}
@media(max-width:900px){.settings-grid{grid-template-columns:1fr}.setnav{flex-direction:row;flex-wrap:wrap;position:static;gap:4px;margin-bottom:6px}.setnav .sn-h{display:none}}
@media(max-width:767px){.setnav{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;padding-bottom:2px;-webkit-mask-image:linear-gradient(to right,#000 calc(100% - 22px),transparent);mask-image:linear-gradient(to right,#000 calc(100% - 22px),transparent)}.setnav::-webkit-scrollbar{display:none}.setnav a{flex:none;border:1px solid var(--line);white-space:nowrap;padding:5px 10px}}
</style>
<div class="page-head">
  <div>
    <h1 class="page-title">Settings</h1>
    <div class="page-desc">Everything is stored in SQLite; blank fields fall back to the container env file.</div>
  </div>
</div>

<div class="settings-grid">
<nav class="setnav" aria-label="Settings sections">
  <span class="sn-h">Settings</span>
  <a href="#general">General</a>
  <a href="#ai">AI</a>
  <a href="#mail">Mail</a>
  <a href="#sorting">Sorting &amp; filing</a>
  <a href="#searchidx">Search</a>
</nav>
<div class="setbody">

<section id="general">
  <h2 class="sec-h">General</h2>
  <div class="sec-desc">About you and how the interface reads for you.</div>
  <div class="card">
    <div class="card-h"><h3>Profile &amp; display</h3></div>
    <form method="post">
      <input type="hidden" name="section" value="behavior">
      <input type="hidden" name="scope" value="General">
      <div class="setrow"><div class="st-l"><b>Your name</b><span class="sub">Used when drafting replies and filling template placeholders.</span></div>
        <div class="st-c"><input type="text" name="my_name" value="{{ s.my_name }}" aria-label="Your name"></div></div>
      <div class="setrow"><div class="st-l"><b>Time display offset (hours)</b><span class="sub">Hours from UTC; timestamps show as {{ tz }}.</span></div>
        <div class="st-c"><input type="number" step="0.5" name="display_tz_offset" value="{{ '%g'|format(s.display_tz_offset|float) }}" min="-14" max="14" aria-label="Time display offset"></div></div>
      <div class="setrow"><div class="st-l"><b>Remote images in the viewer</b><span class="sub">When off, every message keeps a &ldquo;Load images&rdquo; button instead.</span></div>
        <div class="st-c"><label class="check"><input type="checkbox" name="render_images" value="1" {{ 'checked' if s.render_images else '' }}><input type="hidden" name="render_images" value="0"> <span>Always load</span></label></div></div>
      <div class="savebar"><button class="btn primary" type="submit">Save general</button></div>
    </form>
  </div>
</section>

<section id="ai">
  <h2 class="sec-h">AI Settings</h2>
  <div class="sec-desc">Endpoints, models and the classification pipeline. Anything OpenAI-compatible plugs in.</div>

  <div class="card" id="ai-model">
    <div class="card-h"><h3>LLM endpoint</h3><span class="sub">any OpenAI-compatible /chat/completions server</span></div>
    <form method="post" id="llm-form">
      <input type="hidden" name="section" value="llm">
      <input type="hidden" name="scope" value="LLM endpoint">
      <div class="setrow"><div class="st-l"><b>Base URL</b><span class="sub">e.g. http://host:8000/v1</span></div>
        <div class="st-c"><input id="llm-base-url" type="text" name="llm_base_url" value="{{ s.llm_base_url }}" placeholder="{{ llm.base or 'http://host:8000/v1' }}" aria-label="LLM base URL"></div></div>
      <div class="setrow"><div class="st-l"><b>Model</b><span class="sub">Choices come from the endpoint; Custom lets you type one.</span></div>
        <div class="st-c">
          <div class="model-picker" data-url="{{ url_for('settings_llm_models') }}" data-which="primary" data-base="llm_base_url" data-effective="{{ llm.model }}">
            <select class="model-select" aria-label="LLM model">
              {% if s.llm_model %}<option value="{{ s.llm_model }}" selected>{{ s.llm_model }}</option>{% endif %}
              <option value="__custom__">Custom&hellip;</option>
            </select>
            <input type="text" class="model-custom" name="llm_model" value="{{ s.llm_model }}" placeholder="{{ llm.model or 'model name' }}" aria-label="LLM model"{% if s.llm_model %} hidden{% endif %}>
            <span class="model-status sub"></span>
          </div>
        </div></div>
      <div class="setrow"><div class="st-l"><b>API key</b><span class="sub">Blank keeps the stored key.</span></div>
        <div class="st-c"><input type="password" name="llm_api_key" value="" autocomplete="new-password" placeholder="{{ 'set - type to replace' if llm.key else 'not set' }}" aria-label="LLM API key"></div></div>
      <div class="setrow"><div class="st-l"><b>Timeout</b><span class="sub">Seconds; blank = default.</span></div>
        <div class="st-c"><input type="number" name="llm_timeout" min="0" value="{{ s.llm_timeout or '' }}" placeholder="90" aria-label="LLM timeout"></div></div>
      <div class="setrow"><div class="st-l"><b>Thinking / reasoning channel</b><span class="sub">auto sends it and drops it if the endpoint rejects it; off never sends it.</span></div>
        <div class="st-c"><select name="llm_thinking" aria-label="Thinking mode">
          <option value="auto" {{ 'selected' if s.llm_thinking != 'off' else '' }}>auto</option>
          <option value="off" {{ 'selected' if s.llm_thinking == 'off' else '' }}>off (strict OpenAI servers)</option>
        </select></div></div>
      <div class="setrow"><div class="st-l"><b>Clear the stored API key</b></div>
        <div class="st-c"><label class="check"><input type="checkbox" name="llm_api_key_clear" value="1"> <span>Clear key</span></label></div></div>
      <div class="hr"></div>
      <details class="fbx">
        <summary style="cursor:pointer;font-weight:600;list-style:none">Fallback endpoint <span class="sub">(optional; used automatically when the primary fails)</span></summary>
        <div style="margin-top:6px">
      <div class="setrow"><div class="st-l"><b>Base URL</b></div>
        <div class="st-c"><input id="llm-fallback-base-url" type="text" name="llm_fallback_base_url" value="{{ s.llm_fallback_base_url }}" placeholder="{{ (llm.fallback.base if llm.fallback else '') or 'none' }}" aria-label="Fallback base URL"></div></div>
      <div class="setrow"><div class="st-l"><b>Model</b><span class="sub">Blank = same as primary.</span></div>
        <div class="st-c">
          <div class="model-picker" data-url="{{ url_for('settings_llm_models') }}" data-which="fallback" data-base="llm_fallback_base_url" data-effective="{{ (llm.fallback.model if llm.fallback else '') or '' }}">
            <select class="model-select" aria-label="Fallback model">
              {% if s.llm_fallback_model %}<option value="{{ s.llm_fallback_model }}" selected>{{ s.llm_fallback_model }}</option>{% endif %}
              <option value="__custom__">Custom&hellip;</option>
            </select>
            <input type="text" class="model-custom" name="llm_fallback_model" value="{{ s.llm_fallback_model }}" placeholder="{{ (llm.fallback.model if llm.fallback else '') or 'same as primary' }}" aria-label="Fallback model"{% if s.llm_fallback_model %} hidden{% endif %}>
            <span class="model-status sub"></span>
          </div>
        </div></div>
      <div class="setrow"><div class="st-l"><b>API key</b><span class="sub">Blank keeps the stored key.</span></div>
        <div class="st-c"><input type="password" name="llm_fallback_api_key" value="" autocomplete="new-password" placeholder="{{ 'set - type to replace' if (llm.fallback and llm.fallback.key) else 'not set' }}" aria-label="Fallback API key"></div></div>
      <div class="setrow"><div class="st-l"><b>Clear the stored fallback key</b></div>
        <div class="st-c"><label class="check"><input type="checkbox" name="llm_fallback_api_key_clear" value="1"> <span>Clear fallback key</span></label></div></div>
        </div>
      </details>
      <div class="savebar"><button class="btn primary" type="submit" id="llm-save">Save LLM endpoint</button><span class="sub" id="llm-gate" style="color:var(--warn)" hidden></span><span class="sub">Blank fields fall back to the env file.</span></div>
    </form>
    <div class="row" style="margin-top:10px">
      <form class="inline" method="post" action="{{ url_for('settings_test_llm') }}"><button class="btn" type="submit" data-side="llm:primary">Test primary</button></form>
      <span class="sub" id="llm-status-primary" aria-live="polite"></span>
      <form class="inline" method="post" action="{{ url_for('settings_test_llm', which='fallback') }}"><button class="btn" type="submit" data-side="llm:fallback">Test fallback</button></form>
      <span class="sub" id="llm-status-fallback" aria-live="polite"></span>
      <span class="sub">tests the values in the form; saving changed endpoint values needs a passing test</span>
    </div>
  </div>

  <div class="card" id="ai-assistant-model">
    <div class="card-h"><h3>Assistant model</h3><span class="sub">which endpoint answers in the assistant chat</span></div>
    <form method="post" action="{{ url_for('settings_assistant_model') }}">
      <input type="hidden" name="next" value="{{ url_for('settings') }}#ai">
      <div class="setrow"><div class="st-l"><b>Use the fallback model for the assistant</b><span class="sub">Test the assistant against the fallback endpoint; the primary stays the safety net. Classification and drafting keep using the primary.</span></div>
        <div class="st-c"><label class="px-sw" title="Use the fallback model for the assistant"><input type="checkbox" name="assistant_use_fallback" value="1" {{ 'checked' if s.assistant_use_fallback else '' }} onchange="this.form.requestSubmit()" aria-label="Use the fallback model for the assistant"><span class="px-tr"></span></label></div></div>
      {% if not llm.fallback %}<div class="sub" style="padding:0 14px 12px">No fallback endpoint configured - add one in the LLM endpoint card above.</div>{% endif %}
    </form>
  </div>

  <div class="card" id="ai-classify">
    <div class="card-h"><h3>Classification</h3><span class="sub">rules first, then classifiers, then the LLM</span></div>
    <p class="sub" style="margin-top:0">Classification switches (LLM suggestions, budgets and concurrency) moved into the Automation workspace, next to the rest of the mail-handling controls.</p>
    <div class="row"><a class="btn primary" href="{{ url_for('automation_controls') }}">Open Controls</a><a class="btn" href="{{ url_for('automation') }}">Automation overview</a></div>
  </div>

  <div class="card" id="ai-reply">
    <div class="card-h"><h3>Reply detection</h3><span class="sub">detects whether incoming mail was answered</span></div>
    <form method="post">
      <input type="hidden" name="section" value="behavior">
      <input type="hidden" name="scope" value="Reply detection">
      <div class="setrow"><div class="st-l"><b>Detect answered mail</b><span class="sub">Check Sent replies for thread membership and whether they address the request. Sufficient replies clear Needs reply at 90% or higher assessment confidence. Uses the automatic-call budget; LLM classification must be on.</span></div>
        <div class="st-c"><label class="px-sw" title="Detect answered mail"><input type="checkbox" name="reply_tracking_enabled" value="1" {{ 'checked' if s.reply_tracking_enabled else '' }} aria-label="Detect answered mail"><span class="px-tr"></span></label><input type="hidden" name="reply_tracking_enabled" value="0"></div></div>
      <details class="ux-advanced"><summary>Advanced · sent-reply matching</summary>
        <div class="setrow"><div class="st-l"><b>Sent folder</b><span class="sub">Blank discovers the server's Sent folder, including Sent Items.</span></div><div class="st-c"><input type="text" name="reply_sent_folder" value="{{ s.reply_sent_folder }}" placeholder="Auto-detect" aria-label="Sent folder override"></div></div>
        <div class="setrow"><div class="st-l"><b>Other sending addresses</b><span class="sub">Comma-separated aliases you send as. Your primary mailbox address is recognized automatically. These are matching preferences, not account credentials.</span></div><div class="st-c"><input type="text" name="reply_identity_addresses" value="{{ s.reply_identity_addresses|join(', ') }}" placeholder="alias@example.com" aria-label="Sending aliases"></div></div>
      </details>
      <div class="savebar"><button class="btn primary" type="submit">Save reply detection</button><span class="sub">Applies to future checks.</span></div>
    </form>
  </div>

  <div class="card" id="ai-plugins">
    <div class="card-h"><h3>Plugins</h3><a class="sub" href="{{ url_for('plugins_page') }}">Manage plugins →</a></div>
    <div class="setrow"><div class="st-l"><b>Plugin tools for the assistant</b><span class="sub">Enabled plugins can contribute assistant tools — each gated by its own off / ask / auto permission on the Plugins page. Budget: {{ s.plugin_tools_budget }} plugin tool schemas per turn.</span></div></div>
  </div>

  <div class="card" id="ai-classifiers">
    <div class="card-h"><h3>Classifiers</h3><span class="row"><a class="sub" href="{{ url_for('automation_controls') }}">Switches in Automation →</a><a class="sub" href="{{ url_for('classifiers') }}">Manage classifiers →</a></span></div>
    <p class="sub" style="margin-top:0">The fast-path switches (run classifiers before the LLM, auto-retrain) moved into Automation → Controls. Review, retrain and edit datasets here.</p>
    <div class="row"><a class="btn" href="{{ url_for('automation_controls') }}">Classifier switches</a><a class="btn" href="{{ url_for('classifiers') }}">Manage classifiers</a><a class="btn" href="{{ url_for('learning_page') }}">Learning</a></div>
  </div>

  <div class="card" id="ai-search">
    <div class="card-h"><h3>Embeddings &amp; reranker</h3><span class="sub">the semantic search stack</span></div>
    <form method="post" id="rag-form">
      <input type="hidden" name="section" value="rag">
      <input type="hidden" name="scope" value="Embeddings &amp; reranker">
      <h4>Search architecture</h4>
      <div class="setrow"><div class="st-l"><b>Backend</b><span class="sub">lite = hybrid FTS5 + sqlite-vec + optional local CPU models (recommended). legacy = the original chunks/_fts/vec_chunks pipeline. Both indexes coexist; switching is instant and reversible.</span></div>
        <div class="st-c"><select name="rag_backend" aria-label="RAG backend">
          <option value="lite" {{ 'selected' if s.rag_backend != 'legacy' else '' }}>lite — hybrid + local models</option>
          <option value="legacy" {{ 'selected' if s.rag_backend == 'legacy' else '' }}>legacy — original pipeline</option>
        </select></div></div>
      <div class="setrow"><div class="st-l"><b>Local model threads</b><span class="sub">ONNX Runtime threads for the local (CPU) embedder/reranker. 8 keeps a busy host responsive; raise for faster backfills.</span></div>
        <div class="st-c"><input type="number" name="local_embed_threads" min="1" value="{{ s.local_embed_threads or '' }}" placeholder="8" aria-label="Local model threads"></div></div>
      <div class="hr"></div>
      <h4>Embeddings</h4>
      <div class="setrow"><div class="st-l"><b>Base URL</b><span class="sub">Not used by the local protocol.</span></div>
        <div class="st-c"><input id="embed-base-url" type="text" name="embed_base_url" value="{{ s.embed_base_url }}" placeholder="{{ ecfg.base or 'http://host:8080' }}" aria-label="Embed base URL"></div></div>
      <div class="setrow"><div class="st-l"><b>Model</b><span class="sub">Local lists FastEmbed ids; other protocols list the endpoint's models.</span></div>
        <div class="st-c">
          <div class="model-picker" data-url="{{ url_for('settings_rag_models') }}" data-which="embed" data-protocol="embed_protocol" data-base="embed_base_url" data-effective="{{ s.embed_model or (lembed if ecfg.protocol == 'local' else ecfg.model) }}">
            <select class="model-select" aria-label="Embed model">
              {% if s.embed_model %}<option value="{{ s.embed_model }}" selected>{{ s.embed_model }}</option>{% endif %}
              <option value="__custom__">Custom&hellip;</option>
            </select>
            <input type="text" class="model-custom" name="embed_model" value="{{ s.embed_model }}" placeholder="{{ ecfg.model or 'Qwen/Qwen3-Embedding-0.6B' }}" aria-label="Embed model"{% if s.embed_model %} hidden{% endif %}>
            <span class="model-status sub"></span>
          </div>
        </div></div>
      <div class="setrow"><div class="st-l"><b>Protocol</b><span class="sub">TEI /embed vs OpenAI /embeddings (OpenAI, Ollama, LM Studio, TEI /v1).</span></div>
        <div class="st-c"><select name="embed_protocol" aria-label="Embed protocol">
          <option value="tei" {{ 'selected' if s.embed_protocol not in ('openai', 'local') else '' }}>TEI — POST /embed</option>
          <option value="openai" {{ 'selected' if s.embed_protocol == 'openai' else '' }}>OpenAI — POST /embeddings</option>
          <option value="local" {{ 'selected' if s.embed_protocol == 'local' else '' }}>Local — CPU (ONNX, no server)</option>
        </select></div></div>
      <div class="setrow"><div class="st-l"><b>API key</b><span class="sub">Blank keeps the stored key; only for gated endpoints.</span></div>
        <div class="st-c"><input type="password" name="embed_api_key" value="" autocomplete="new-password" placeholder="{{ 'set' if ecfg.key else 'not set' }}" aria-label="Embed API key"></div></div>
      <div class="setrow"><div class="st-l"><b>Timeout</b><span class="sub">Seconds; blank = default.</span></div>
        <div class="st-c"><input type="number" name="embed_timeout" min="0" value="{{ s.embed_timeout or '' }}" placeholder="180" aria-label="Embed timeout"></div></div>
      <div class="setrow"><div class="st-l"><b>Clear the stored key</b></div>
        <div class="st-c"><label class="check"><input type="checkbox" name="embed_api_key_clear" value="1"> <span>Clear key</span></label></div></div>
      <div class="setrow"><div class="st-l"><b>Query instruction prefix</b><span class="sub">Prepended to search queries only, never to documents. Qwen3-Embedding needs one; most models want it blank.</span></div>
        <div class="st-c"><textarea name="embed_query_prefix" rows="3" aria-label="Query prefix">{{ s.embed_query_prefix }}</textarea></div></div>
      <div class="hr"></div>
      <h4>Reranker</h4>
      <div class="setrow"><div class="st-l"><b>Base URL</b><span class="sub">Not used by the local protocol.</span></div>
        <div class="st-c"><input id="rerank-base-url" type="text" name="rerank_base_url" value="{{ s.rerank_base_url }}" placeholder="{{ rcfg.base or 'http://host:8081' }}" aria-label="Rerank base URL"></div></div>
      <div class="setrow"><div class="st-l"><b>Model</b><span class="sub">Local lists FastEmbed cross-encoders; other protocols list the endpoint's models.</span></div>
        <div class="st-c">
          <div class="model-picker" data-url="{{ url_for('settings_rag_models') }}" data-which="rerank" data-protocol="rerank_protocol" data-base="rerank_base_url" data-effective="{{ s.rerank_model or (lrerank if rcfg.protocol == 'local' else rcfg.model) }}">
            <select class="model-select" aria-label="Rerank model">
              {% if s.rerank_model %}<option value="{{ s.rerank_model }}" selected>{{ s.rerank_model }}</option>{% endif %}
              <option value="__custom__">Custom&hellip;</option>
            </select>
            <input type="text" class="model-custom" name="rerank_model" value="{{ s.rerank_model }}" placeholder="{{ rcfg.model or 'jinaai/jina-reranker-v1-turbo-en' }}" aria-label="Rerank model"{% if s.rerank_model %} hidden{% endif %}>
            <span class="model-status sub"></span>
          </div>
        </div></div>
      <div class="setrow"><div class="st-l"><b>Protocol</b></div>
        <div class="st-c"><select name="rerank_protocol" aria-label="Rerank protocol">
          <option value="tei" {{ 'selected' if s.rerank_protocol not in ('cohere', 'local') else '' }}>TEI — {"query", "texts"}</option>
          <option value="cohere" {{ 'selected' if s.rerank_protocol == 'cohere' else '' }}>Cohere-style — {"query", "documents"}</option>
          <option value="local" {{ 'selected' if s.rerank_protocol == 'local' else '' }}>Local — CPU (ONNX, no server)</option>
        </select></div></div>
      <div class="setrow"><div class="st-l"><b>API key</b><span class="sub">Blank keeps the stored key.</span></div>
        <div class="st-c"><input type="password" name="rerank_api_key" value="" autocomplete="new-password" placeholder="{{ 'set' if rcfg.key else 'not set' }}" aria-label="Rerank API key"></div></div>
      <div class="setrow"><div class="st-l"><b>Timeout</b><span class="sub">Seconds; blank = default.</span></div>
        <div class="st-c"><input type="number" name="rerank_timeout" min="0" value="{{ s.rerank_timeout or '' }}" placeholder="90" aria-label="Rerank timeout"></div></div>
      <div class="setrow"><div class="st-l"><b>Clear the stored key</b></div>
        <div class="st-c"><label class="check"><input type="checkbox" name="rerank_api_key_clear" value="1"> <span>Clear key</span></label></div></div>
      <div class="savebar"><button class="btn primary" type="submit" id="rag-save">Save endpoints</button><span class="sub" id="rag-gate" style="color:var(--warn)" hidden></span><span class="sub">Changing the embedding model or dimension needs an index rebuild (Dashboard).</span></div>
    </form>
    <div class="row" style="margin-top:10px">
      <form class="inline" method="post" action="{{ url_for('settings_test_embed') }}"><button class="btn" type="submit" data-side="embed">Test embeddings</button></form>
      <span class="sub" id="embed-status" aria-live="polite"></span>
      <form class="inline" method="post" action="{{ url_for('settings_test_rerank') }}"><button class="btn" type="submit" data-side="rerank">Test reranker</button></form>
      <span class="sub" id="rerank-status" aria-live="polite"></span>
      <span class="sub">tests the values in the form; saving changed endpoint values needs a passing test</span>
    </div>
  </div>

  <div class="card" id="ai-perms">
    <div class="card-h"><h3>Agent permissions</h3><a class="sub" href="{{ url_for('assistant') }}">Open assistant →</a></div>
    <p class="sub" style="margin:0 2px 8px">What the assistant may do on its own. Every capability is enforced server-side — <b>Ask me first</b> queues the action as an approval card in the chat; <b>Off</b> refuses it. Dangerous capabilities are off by default.</p>
    <form method="post">
      <input type="hidden" name="section" value="behavior">
      <input type="hidden" name="scope" value="Agent permissions">
      {% set riskbadge = {'safe': 'ok', 'caution': 'warn', 'dangerous': 'err'} %}
      {% for cap, label, risk, tools, desc in agcaps %}
      <div class="setrow">
        <div class="st-l"><b>{{ label }}</b> <span class="badge {{ riskbadge.get(risk, 'warn') }}">{{ risk }}</span>
          <span class="sub">{{ desc }} <span class="mono" style="font-size:.72rem">{{ tools|join(', ') }}</span></span>
          {% if risk == 'dangerous' %}<span class="sub" style="color:var(--err)">⚠ Dangerous — {{ 'sends mail as you to real recipients' if cap == 'send' else 'moves mail to Trash' }}. Only enable if you are sure.</span>{% endif %}
        </div>
        <div class="st-c">
          <select name="perm_{{ cap }}" data-risk="{{ risk }}" aria-label="{{ label }} permission">
            <option value="off" {{ 'selected' if s['perm_' ~ cap] == 'off' else '' }}>Off</option>
            <option value="ask" {{ 'selected' if s['perm_' ~ cap] == 'ask' else '' }}>Ask me first</option>
            <option value="auto" {{ 'selected' if s['perm_' ~ cap] == 'auto' else '' }}>Auto</option>
          </select>
        </div>
      </div>
      {% endfor %}
      <div class="setrow"><div class="st-l"><b>Send cap</b><span class="sub">Maximum assistant sends per hour (0 = unlimited).</span></div>
        <div class="st-c"><input type="number" name="sends_per_hour" min="0" value="{{ s.sends_per_hour }}" aria-label="Assistant sends per hour"></div></div>
      <div class="savebar"><button class="btn primary" type="submit">Save permissions</button><span class="sub">Applies to the next assistant turn.</span></div>
    </form>
    <script>
    (function(){
      document.querySelectorAll('#ai-perms select[data-risk]').forEach(function(sel){
        var orig=sel.value;
        sel.addEventListener('change', function(){
          if(sel.dataset.risk==='dangerous' && sel.value!=='off'){
            var what = sel.name==='perm_send' ? 'SEND mail as you' : 'DELETE mail (move to Trash)';
            if(!confirm('DANGER: this lets the assistant '+what+' on its own. Are you sure?')){ sel.value=orig; return; }
          }
          orig=sel.value;
        });
      });
    })();
    </script>
  </div>
</section>

<section id="mail">
  <h2 class="sec-h">Mail &amp; connection</h2>
  <div class="sec-desc">Where mail comes from, how often it is checked, and the account behind it.</div>

  <div class="card" id="mail-src">
    <div class="card-h"><h3>Mail source</h3>
      <span class="badge {{ 'acc' if s.proxy_mode != 'external' else 'warn' }}">{{ 'embedded proxy' if s.proxy_mode != 'external' else 'external server' }}</span>
    </div>
    <form method="post">
      <input type="hidden" name="section" value="connection">
      <input type="hidden" name="scope" value="Mail source">
      <div class="setrow"><div class="st-l"><b>Mode</b><span class="sub">Embedded keeps OAuth sign-in and tokens inside this app (Accounts page). External points the app at another IMAP server below.</span></div>
        <div class="st-c"><select id="c-mode" name="proxy_mode" aria-label="Mail source mode">
          <option value="embedded" {{ 'selected' if s.proxy_mode != 'external' else '' }}>Embedded proxy (recommended)</option>
          <option value="external" {{ 'selected' if s.proxy_mode == 'external' else '' }}>External server</option>
        </select></div></div>
      <div class="setrow"><div class="st-l"><b>Tailnet host</b><span class="sub">Builds redirect URIs like <span class="mono">https://node.tailnet.ts.net:41810</span> for accounts in tailnet mode.</span></div>
        <div class="st-c"><input type="text" name="proxy_tailnet_host" value="{{ s.proxy_tailnet_host }}" placeholder="node.tailnet.ts.net" aria-label="Tailnet host"></div></div>
      <div id="extf"{% if s.proxy_mode != 'external' %} class="hidden"{% endif %}>
        <div class="hr"></div>
        <h4>External server <span class="sub">(only used in external mode; blank fields fall back to the env file)</span></h4>
        <div class="setrow"><div class="st-l"><b>IMAP host</b></div>
          <div class="st-c"><input type="text" name="imap_host" value="{{ s.imap_host }}" placeholder="{{ cfg.IMAP_HOST }}" aria-label="IMAP host"></div></div>
        <div class="setrow"><div class="st-l"><b>Port</b></div>
          <div class="st-c"><input type="text" name="imap_port" value="{{ s.imap_port }}" placeholder="{{ cfg.IMAP_PORT }}" aria-label="IMAP port"></div></div>
        <div class="setrow"><div class="st-l"><b>Username</b></div>
          <div class="st-c"><input type="text" name="imap_user" value="{{ s.imap_user }}" placeholder="{{ cfg.IMAP_USER }}" aria-label="IMAP user"></div></div>
        <div class="setrow"><div class="st-l"><b>Password</b><span class="sub">Blank keeps the stored value.</span></div>
          <div class="st-c"><input type="password" name="imap_password" value="" autocomplete="new-password" placeholder="{{ 'set' if icfg.password else 'not set' }}" aria-label="IMAP password"></div></div>
        <div class="setrow"><div class="st-l"><b>TLS</b></div>
          <div class="st-c"><select name="imap_tls" aria-label="IMAP TLS">
            <option value="" {{ 'selected' if s.imap_tls in ('', none) else '' }}>(env)</option>
            <option value="0" {{ 'selected' if s.imap_tls == '0' else '' }}>0 — plain (proxy)</option>
            <option value="1" {{ 'selected' if s.imap_tls == '1' else '' }}>1 — TLS</option>
          </select></div></div>
        <div class="setrow"><div class="st-l"><b>Clear the stored password</b></div>
          <div class="st-c"><label class="check"><input type="checkbox" name="imap_password_clear" value="1"> <span>Clear password</span></label></div></div>
      </div>
      <div class="savebar"><button class="btn primary" type="submit">Save mail source</button><span class="sub">Switching mode stops or starts the embedded proxy automatically.</span></div>
    </form>
  </div>

  <div class="card" id="mail-check">
    <div class="card-h"><h3>Checking</h3></div>
    <form method="post">
      <input type="hidden" name="section" value="behavior">
      <input type="hidden" name="scope" value="Checking">
      <div class="setrow"><div class="st-l"><b>Check every</b><span class="sub">Seconds between mailbox checks.</span></div>
        <div class="st-c"><input type="number" name="poll_interval" value="{{ s.poll_interval }}" min="15" aria-label="Poll interval"></div></div>
      <div class="setrow"><div class="st-l"><b>First-run lookback</b><span class="sub">Hours; how far back the first scan (and re-scans) reach.</span></div>
        <div class="st-c"><input type="number" name="lookback_hours" value="{{ s.lookback_hours }}" min="1" aria-label="Lookback hours"></div></div>
      <div class="setrow"><div class="st-l"><b>Watched folders</b><span class="sub">Comma separated; folders scanned for new mail — usually just INBOX.</span></div>
        <div class="st-c"><input type="text" name="watch_folders" value="{{ s.watch_folders|join(', ') }}" aria-label="Watched folders"></div></div>
      <div class="savebar"><button class="btn primary" type="submit">Save checking</button></div>
    </form>
  </div>
</section>

<section id="sorting">
  <h2 class="sec-h">Sorting &amp; filing</h2>
  <div class="sec-desc">What happens to arriving mail and where it ends up.</div>

  <div class="card" id="sort-rules">
    <div class="card-h"><h3>Rules &amp; flows</h3><span class="sub">live / preview switches and editors</span></div>
    <p class="sub" style="margin-top:0">The rule-live and flow-live switches live in Automation → Controls; the editors keep their existing pages.</p>
    <div class="row"><a class="btn" href="{{ url_for('automation_controls') }}">Live switches</a><a class="btn" href="{{ url_for('rules') }}">Rules</a><a class="btn" href="{{ url_for('flows') }}">Flows</a></div>
  </div>

  <div class="card" id="sort-filing">
    <div class="card-h"><h3>Filing &amp; drafts</h3><span class="sub">moved to Automation</span></div>
    <p class="sub" style="margin-top:0">Category vocabulary, the category destination map, the default-filing switch and the draft destination moved into the Automation workspace.</p>
    <div class="row"><a class="btn primary" href="{{ url_for('automation_categories') }}">Categories &amp; filing</a><a class="btn" href="{{ url_for('templates') }}#draft-destination">Drafting</a></div>
  </div>
</section>

<section id="searchidx">
  <h2 class="sec-h">Search index</h2>
  <div class="sec-desc">A local embedding index over the whole archive, fused with keyword search.</div>
  <div class="card">
    <div class="card-h"><h3>Index</h3><a class="sub" href="{{ url_for('dashboard') }}">Run or rebuild from the Dashboard →</a></div>
    <form method="post">
      <input type="hidden" name="section" value="behavior">
      <input type="hidden" name="scope" value="Search index">
      <div class="setrow"><div class="st-l"><b>Build and maintain the search index</b></div>
        <div class="st-c"><label class="check"><input type="checkbox" name="index_enabled" value="1" {{ 'checked' if s.index_enabled else '' }}><input type="hidden" name="index_enabled" value="0"> <span>Enabled</span></label></div></div>
      <div class="setrow"><div class="st-l"><b>Rerank results</b><span class="sub">Cross-encoder rerank — better precision, slightly slower.</span></div>
        <div class="st-c"><label class="check"><input type="checkbox" name="rerank_enabled" value="1" {{ 'checked' if s.rerank_enabled else '' }}><input type="hidden" name="rerank_enabled" value="0"> <span>Enabled</span></label></div></div>
      <div class="setrow"><div class="st-l"><b>Indexed folders</b><span class="sub">Comma separated; blank = all except the excluded list.</span></div>
        <div class="st-c"><input type="text" name="index_folders" value="{{ s.index_folders|join(', ') }}" aria-label="Indexed folders"></div></div>
      <div class="setrow"><div class="st-l"><b>Idle refresh interval</b><span class="sub">Minutes between index passes for newly arrived mail.</span></div>
        <div class="st-c"><input type="number" name="index_refresh_minutes" value="{{ s.index_refresh_minutes }}" min="1" aria-label="Refresh minutes"></div></div>
      <div class="setrow"><div class="st-l"><b>Excluded folders</b><span class="sub">Substrings, comma separated.</span></div>
        <div class="st-c"><input type="text" name="rag_exclude_folders" value="{{ s.rag_exclude_folders|join(', ') }}" aria-label="Excluded folders"></div></div>
      <div class="savebar"><button class="btn primary" type="submit">Save index</button></div>
    </form>
  </div>
</section>

<section id="status">
  <h2 class="sec-h">System status</h2>
  <div class="sec-desc">Effective values right now.</div>
  <div class="card">
    <div class="sub mono" style="line-height:1.9">
      IMAP: {{ icfg.user or '?' }} @ {{ icfg.host }}:{{ icfg.port }} ({{ icfg.mode }})<br>
      LLM: {{ llm.base }} · model {{ llm.model }} · key {{ 'set' if llm.key else 'MISSING' }}{% if llm.fallback %} · fallback: {{ llm.fallback.model }}{% endif %}<br>
      Embed: {{ ecfg.base or '— not configured —' }} · {{ ecfg.model }} · Rerank: {{ rcfg.base or '— not configured —' }} · {{ rcfg.model }}<br>
      state: {{ engine_state }} · db: {{ cfg.DB_PATH }}
    </div>
    <p class="sub" style="margin-bottom:0">Keys are stored in SQLite and shown masked only; blank fields fall back to the container env file, so an existing .env keeps working.</p>
  </div>
</section>

</div>
</div>

<script>
(function(){
  var sel = document.getElementById('c-mode'), ext = document.getElementById('extf');
  if(!sel || !ext) return;
  function upd(){ ext.classList.toggle('hidden', sel.value !== 'external'); }
  sel.addEventListener('change', upd);
})();
</script>

<script>
(function(){
  function init(picker){
    var select = picker.querySelector('.model-select');
    var input = picker.querySelector('.model-custom');
    var status = picker.querySelector('.model-status');
    var endpoint = picker.getAttribute('data-url');
    var which = picker.getAttribute('data-which') || 'primary';
    var protocolName = picker.getAttribute('data-protocol');
    var protocolInput = protocolName ? document.querySelector('select[name="' + protocolName + '"]') : null;
    var baseName = picker.getAttribute('data-base');
    var baseInput = baseName ? document.querySelector('input[name="' + baseName + '"]') : null;
    var effective = picker.getAttribute('data-effective') || '';
    function custom(focus){
      select.value = '__custom__';
      input.hidden = false;
      if(focus){ input.focus(); }
    }
    function refresh(){
      if(!endpoint){ return; }
      var url = endpoint + '?which=' + encodeURIComponent(which);
      if(protocolInput && protocolInput.value){ url += '&protocol=' + encodeURIComponent(protocolInput.value); }
      if(baseInput && baseInput.value.trim()){ url += '&base_url=' + encodeURIComponent(baseInput.value.trim()); }
      fetch(url, {headers:{'Accept':'application/json'}})
        .then(function(r){ return r.json(); })
        .then(function(d){
          var models = (d && d.ok && d.models) ? d.models : [];
          if(!models.length){
            status.textContent = (d && d.error) ? 'Could not list models - type below.' : '';
            if(d && d.error){ custom(false); }
            return;
          }
          var customOpt = select.querySelector('option[value="__custom__"]');
          Array.prototype.slice.call(select.options).forEach(function(o){
            if(o !== customOpt){ select.removeChild(o); }
          });
          var have = {};
          models.forEach(function(m){
            if(have[m]){ return; }
            have[m] = true;
            var o = document.createElement('option');
            o.value = m; o.textContent = m;
            select.insertBefore(o, customOpt);
          });
          var current = input.value || effective;
          if(models.indexOf(current) >= 0){ select.value = current; input.hidden = true; }
          else { custom(false); }
          status.textContent = models.length + ' models';
        })
        .catch(function(){ status.textContent = 'Could not list models - type below.'; });
    }
    picker._reload = refresh;
    select.addEventListener('change', function(){
      if(select.value === '__custom__'){ input.hidden = false; input.focus(); }
      else { input.value = select.value; input.hidden = true; }
    });
    if(baseInput){ baseInput.addEventListener('change', refresh); }
    refresh();
  }
  Array.prototype.forEach.call(document.querySelectorAll('.model-picker'), init);

  [['embed_protocol','embed-base-url'],['rerank_protocol','rerank-base-url']].forEach(function(pair){
    var sel = document.querySelector('select[name="' + pair[0] + '"]');
    var inp = document.getElementById(pair[1]);
    if(!sel){ return; }
    var orig = inp ? inp.placeholder : '';
    function upd(reload){
      var local = sel.value === 'local';
      if(inp){ inp.disabled = local; inp.placeholder = local ? 'not used for local models' : orig; }
      if(reload){
        var picker = document.querySelector('.model-picker[data-protocol="' + pair[0] + '"]');
        if(picker && picker._reload){ picker._reload(); }
      }
    }
    sel.addEventListener('change', function(){ upd(true); });
    upd(false);
  });

  // ---- endpoint test gate (T63): changed endpoint values need a passing test ----
  var EPGATE = {{ epgate|tojson }};
  if (EPGATE) {
    EPGATE.forEach(function(card){
      var form = document.getElementById(card.form);
      if (!form) { return; }
      var saveBtn = document.getElementById(card.save);
      var gateEl = document.getElementById(card.gate);
      function sideVals(s){
        var vals = [];
        s.fields.forEach(function(n){
          var el = form.querySelector('[name="' + n + '"]');
          if (!el) { vals.push(""); }
          else if (el.type === "checkbox") { vals.push(el.checked ? "1" : "0"); }
          else { vals.push(el.value); }
        });
        return JSON.stringify(vals);
      }
      var initial = {}, tested = {};
      card.sides.forEach(function(s){ initial[s.key] = sideVals(s); });
      function required(s){
        if (s.protocol) {
          var p = form.querySelector('[name="' + s.protocol + '"]');
          return !!(p && p.value !== "local");
        }
        var b = form.querySelector('[name="' + s.base + '"]');
        return !!(b && b.value.trim());
      }
      function refresh(){
        var blocked = [];
        card.sides.forEach(function(s){
          var v = sideVals(s);
          if (!required(s)) { return; }
          if (v === initial[s.key]) { return; }
          if (tested[s.key] !== undefined && v === tested[s.key]) { return; }
          blocked.push(s.label);
        });
        if (saveBtn) {
          saveBtn.disabled = blocked.length > 0;
          saveBtn.title = blocked.length ? "Test " + blocked.join(" / ") + " first" : "";
        }
        if (gateEl) {
          gateEl.hidden = blocked.length === 0;
          if (blocked.length) {
            gateEl.textContent = "Test " + blocked.join(" / ") + " first - the new values are not verified yet.";
          }
        }
      }
      Array.prototype.forEach.call(form.querySelectorAll("input,select,textarea"), function(el){
        el.addEventListener("input", refresh);
        el.addEventListener("change", refresh);
      });
      form.addEventListener("submit", function(ev){
        refresh();
        if (saveBtn && saveBtn.disabled) { ev.preventDefault(); }
      });
      var testBtns = [];
      card.sides.forEach(function(s){
        var b = document.querySelector('button[data-side="' + s.key + '"]');
        if (b) { testBtns.push(b); }
      });
      Array.prototype.forEach.call(testBtns, function(btn){
        btn.addEventListener("click", function(ev){
          ev.preventDefault();
          var key = btn.getAttribute("data-side"), s = null;
          card.sides.forEach(function(x){ if (x.key === key) { s = x; } });
          if (!s || !btn.form) { return; }
          var st = document.getElementById(s.status);
          var snap = sideVals(s);
          if (st) { st.textContent = "testing..."; st.style.color = "var(--dim)"; }
          fetch(btn.form.getAttribute("action"), {
            method: "POST",
            body: new FormData(form),
            headers: {"X-Requested-With": "fetch", "Accept": "application/json"}
          }).then(function(r){ return r.json(); }).then(function(d){
            if (d && d.ok) {
              tested[key] = snap;
              if (st) { st.textContent = "verified - " + (d.msg || "OK"); st.style.color = "var(--ok)"; }
            } else if (st) {
              st.textContent = (d && d.msg) || "test failed";
              st.style.color = "var(--err)";
            }
            refresh();
          }).catch(function(){
            if (st) { st.textContent = "test request failed"; st.style.color = "var(--err)"; }
          });
        });
      });
      refresh();
    });
  }
})();
</script>"""


_SETTINGS_ANCHORS = {"llm": "ai-model", "rag": "ai-search", "connection": "mail-src", "behavior": "general"}


def _settings_anchor(section, scope):
    s = (scope or "").lower()
    for key, val in (("sorting & filing", "sort-filing"), ("filing & drafts", "sort-filing"),
                     ("drafting", "sort-filing"), ("sorting", "sorting"),
                     ("rules", "sort-rules"), ("classif", "ai-classify"),
                     ("reply detection", "ai-reply"),
                     ("index", "searchidx"), ("checking", "mail-check"),
                     ("mail source", "mail-src"), ("search", "ai-search"),
                     ("llm", "ai-model"), ("permission", "ai-perms"),
                     ("time", "general"), ("general", "general")):
        if key in s:
            return val
    return _SETTINGS_ANCHORS.get(section, "")


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        section = request.form.get("section") or "behavior"
        scope = (request.form.get("scope") or "").strip()
        nxt = (request.values.get("next") or "").strip()
        if section in ("llm", "rag"):
            blocked = _endpoint_gate_blocked(section, request.form)
            if blocked:
                flash("Not saved - test the %s first: the values changed since the "
                      "last successful test." % " and ".join(blocked), "err")
                anchor = _settings_anchor(section, scope)
                return redirect(url_for("settings") + ("#" + anchor if anchor else ""))
        if section == "llm":
            _save_llm_settings()
            flash(("%s saved." % scope) if scope else "LLM endpoint settings saved.", "ok")
        elif section == "rag":
            _save_rag_settings()
            flash(("%s saved." % scope) if scope else "RAG settings saved.", "ok")
        elif section == "connection":
            _save_connection_settings()
            flash(("%s saved." % scope) if scope else "Connection settings saved.", "ok")
            try:
                if (store.get_setting("proxy_mode") or "embedded") == "external":
                    proxy.manager.stop()
                elif proxy.list_accounts():
                    ok, merr = proxy.manager.start()
                    if not ok:
                        flash("The embedded proxy did not start: %s" % merr, "warn")
            except Exception as exc:
                flash("Proxy apply failed: %r" % exc, "warn")
        else:
            _save_behavior_settings()
            flash(("%s saved." % scope) if scope else "Settings saved.", "ok")
        _TZ_CACHE["at"] = 0  # re-read the display timezone on the next render
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        anchor = _settings_anchor(section, scope)
        return redirect(url_for("settings") + ("#" + anchor if anchor else ""))
    return render(_render_src(
        SETTINGS_TMPL, s=store.all_settings(), engine_state=worker.state, cfg=config,
        tz=tz_label(), agcaps=engine.AGENT_CAPS,
        llm=engine.llm_config(), ecfg=rag.embed_config(), rcfg=rag.rerank_config(),
        icfg=engine.imap_config(), lembed=rag.LOCAL_EMBED_DEFAULT,
        lrerank=rag.LOCAL_RERANK_DEFAULT, epgate=_endpoint_gate_spec()))


@app.route("/settings/llm-models")
def settings_llm_models():
    """Model ids reported by the configured endpoint, for the Settings dropdowns.
    An empty/failed list is not an error: the UI keeps the Custom text field."""
    which = request.args.get("which") or "primary"
    client = engine.LLMClient()
    if which == "fallback":
        base, key = (client.fallback[0], client.fallback[1]) if client.fallback else ("", "")
    else:
        base, key = client.base, client.key
    override = (request.args.get("base_url") or "").strip()
    if override:
        base = override.rstrip("/")
    if not base:
        return jsonify({"ok": False, "error": "no endpoint configured", "models": []})
    try:
        models = client.list_models(base, key)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "models": []})
    return jsonify({"ok": True, "models": models})


@app.route("/settings/rag-models")
def settings_rag_models():
    """Model ids for the embed/rerank dropdowns: FastEmbed's list for the local
    protocol, otherwise the endpoint's OpenAI-compatible /models."""
    which = (request.args.get("which") or "embed").lower()
    kind = "rerank" if which == "rerank" else "embed"
    if (request.args.get("protocol") or "").lower() == "local":
        try:
            return jsonify({"ok": True, "models": rag.local_model_ids(kind), "local": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "models": []})
    cfg = rag.rerank_config() if kind == "rerank" else rag.embed_config()
    base = (request.args.get("base_url") or "").strip().rstrip("/") or cfg["base"]
    if not base:
        return jsonify({"ok": False, "error": "no endpoint configured", "models": []})
    try:
        models = engine.fetch_model_ids(base, cfg["key"])
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "models": []})
    return jsonify({"ok": True, "models": models})


@app.route("/settings/assistant-model", methods=["POST"])
def settings_assistant_model():
    on = (request.form.get("assistant_use_fallback") or "0") == "1"
    store.set_setting("assistant_use_fallback", 1 if on else 0)
    if on and not engine.llm_config().get("fallback"):
        flash("Assistant set to the fallback model, but no fallback endpoint is "
              "configured - the primary will answer.", "warn")
    else:
        flash("Assistant now answers with the %s model." % ("fallback" if on else "primary"),
              "ok")
    nxt = (request.form.get("next") or "").strip()
    dest = nxt if (nxt.startswith("/") and not nxt.startswith("//")) else url_for("settings")
    if "#" not in dest:
        dest += "#ai"
    return redirect(dest)


def _wants_json():
    return (request.headers.get("X-Requested-With") == "fetch"
            or "application/json" in (request.headers.get("Accept") or ""))


@app.route("/settings/test-llm", methods=["POST"])
def settings_test_llm():
    which = request.args.get("which") or "primary"
    wants_json = _wants_json()
    nxt = (request.values.get("next") or "").strip()
    dest = nxt if (nxt.startswith("/") and not nxt.startswith("//")) else url_for("settings")
    started = time.time()
    # probe the VALUES IN THE FORM (unsaved edits included); an empty form probes
    # the saved config, exactly like before
    cfg = engine.llm_config(overlay=_llm_overlay(request.form))
    client = engine.LLMClient(cfg)
    if which == "fallback":
        if not client.fallback:
            msg = "No fallback endpoint configured (see the LLM endpoint card)."
            if wants_json:
                return jsonify({"ok": False, "msg": msg})
            flash(msg, "err")
            return redirect(dest)
        base, key, model = client.fallback
    else:
        base, key, model = client.base, client.key, client.model
    if not base:
        msg = "No LLM endpoint configured - set one in Settings."
        if wants_json:
            return jsonify({"ok": False, "msg": msg})
        flash(msg, "err")
        return redirect(dest)
    side = "llm:fallback" if which == "fallback" else "llm:primary"
    try:
        out = client._chat_once(base, key, model,
                                "You are a connectivity test. Reply with the single word ok.",
                                [{"role": "user", "content": "Reply with the single word ok."}],
                                json_mode=False)
        msg = ("LLM %s OK in %.1fs - %s @ %s - replied: %s"
               % (which, time.time() - started, model, base, (out or "").strip()[:60]))
        _note_endpoint_test(side, _fp_llm_cfg(cfg, which))
        if wants_json:
            return jsonify({"ok": True, "msg": msg})
        flash(msg, "ok")
    except Exception as exc:
        msg = ("LLM %s FAILED after %.1fs: %r - check the endpoint on this page "
               "(local model server: cd gemma && docker compose ps)"
               % (which, time.time() - started, exc))
        if wants_json:
            return jsonify({"ok": False, "msg": msg})
        flash(msg, "err")
    return redirect(dest)


@app.route("/settings/test-embed", methods=["POST"])
def settings_test_embed():
    started = time.time()
    wants_json = _wants_json()
    cfg = rag.embed_config(overlay=_rag_overlay(request.form))
    try:
        vec = rag.embed(["connectivity test"], kind="query", cfg=cfg)[0]
        msg = ("Embeddings OK in %.1fs - %s @ %s - %d dimensions - protocol %s"
               % (time.time() - started, cfg["model"], cfg["base"], len(vec),
                  cfg["protocol"]))
        _note_endpoint_test("embed", _fp_embed_cfg(cfg))
        if wants_json:
            return jsonify({"ok": True, "msg": msg})
        flash(msg, "ok")
    except Exception as exc:
        msg = "Embeddings FAILED after %.1fs: %r" % (time.time() - started, exc)
        if wants_json:
            return jsonify({"ok": False, "msg": msg})
        flash(msg, "err")
    return redirect(url_for("settings"))


@app.route("/settings/test-rerank", methods=["POST"])
def settings_test_rerank():
    started = time.time()
    wants_json = _wants_json()
    cfg = rag.rerank_config(overlay=_rag_overlay(request.form))
    try:
        rr = rag.rerank("budget review", ["the Q4 budget was reviewed and approved",
                                          "lunch tomorrow near campus"], cfg=cfg)
        if not rr:
            msg = "Reranker not configured (Settings -> RAG) or returned no results."
            if wants_json:
                return jsonify({"ok": False, "msg": msg})
            flash(msg, "err")
        else:
            top = max(rr, key=lambda d: d.get("score") or 0)
            msg = ("Reranker OK in %.1fs - %d result(s), top score %.3f"
                   % (time.time() - started, len(rr), top.get("score") or 0))
            _note_endpoint_test("rerank", _fp_rerank_cfg(cfg))
            if wants_json:
                return jsonify({"ok": True, "msg": msg})
            flash(msg, "ok")
    except Exception as exc:
        msg = "Reranker FAILED after %.1fs: %r" % (time.time() - started, exc)
        if wants_json:
            return jsonify({"ok": False, "msg": msg})
        flash(msg, "err")
    return redirect(url_for("settings"))



# ---------------------------------------------------------------- accounts (embedded proxy)

def _accounts_view():
    accounts = []
    try:
        ic = engine.imap_config()
        reading_user = (ic.get("user") or "") if ic.get("mode") == "embedded" else ""
    except Exception:
        reading_user = ""
    for a in proxy.list_accounts():
        cs = proxy.client_settings(a)
        tok = proxy.token_status(a["email"])
        if tok.get("expires_at"):
            tok["expires_h"] = fmt_ts(tok["expires_at"])
        accounts.append({
            "email": a["email"],
            "sid": re.sub(r"[^A-Za-z0-9]", "-", a["email"]),
            "reading": bool(reading_user) and a["email"].lower() == reading_user.lower(),
            "provider": a.get("provider"),
            "provider_label": cs["provider_label"],
            "redirect_uri": cs["redirect_uri"],
            "mode": a.get("redirect_mode") or "tailnet",
            "mode_note": cs["mode_note"],
            "password": a.get("password", ""),
            "imap_local_port": a.get("imap_local_port"),
            "smtp_local_port": a.get("smtp_local_port"),
            "token": tok,
            "auth": proxy.auth_state(a["email"]),
        })
    return accounts


def _proxy_status_view():
    st = proxy.manager.status()
    st["listener_rows"] = [{"port": port, "up": up}
                           for port, up in sorted((st.get("ports") or {}).items())]
    if st.get("started_at"):
        st["started_h"] = fmt_ts(st["started_at"])
    return st

ACCOUNTS_TMPL = """
<div class="page-head">
  <div>
    <h1 class="page-title">Accounts</h1>
    <div class="page-desc">Mail accounts this app reads, and the embedded OAuth proxy that signs in to them.</div>
  </div>
  <div class="row">
    <a class="btn primary" href="{{ url_for('account_new') }}">Add account</a>
  </div>
</div>

{% if not p.installed %}
<div class="msg err">The emailproxy package is not installed in this image — accounts cannot run.</div>
{% endif %}
{% if ext_mode %}
<div class="note">Mail source is set to <b>external</b> in Settings — the accounts below are idle; the app connects to the server configured there instead.</div>
{% endif %}

<div class="card">
  <div class="spread">
    <div class="row" style="gap:10px">
      {% if p.running %}<span class="badge ok">proxy running</span>{% else %}<span class="badge warn">proxy stopped</span>{% endif %}
      <span class="sub">
        {% for l in p.listener_rows %}<span class="mono">127.0.0.1:{{ l.port }}</span> <span class="dot {{ 'ok' if l.up else 'err' }}"></span>{% if not loop.last %} · {% endif %}{% else %}no listeners yet{% endfor %}
        {% if p.pid %} · pid {{ p.pid }}{% endif %}{% if p.started_h %} · up since {{ p.started_h }}{% endif %}
        {% if p.restarts %} · restarted {{ p.restarts }}×{% endif %}
      </span>
    </div>
    <div class="row">
      <form class="inline" method="post" action="{{ url_for('proxy_restart') }}"><button class="btn small" type="submit">Restart proxy</button></form>
      <a class="btn small" href="{{ url_for('proxy_log') }}">Proxy log</a>
    </div>
  </div>
  {% if p.last_error %}<div class="msg err" style="margin-bottom:0"><span class="mono">{{ p.last_error }}</span></div>{% endif %}
</div>

{% for a in accounts %}
<div class="card" data-email="{{ a.email }}">
  <div class="spread">
    <div>
      <h3>{{ a.email }} <span class="sub">· {{ a.provider_label }}</span>{% if a.reading %} <span class="badge acc">reading</span>{% endif %}</h3>
      <div class="sub" style="margin-top:4px">
        {% if a.auth.status in ['starting','triggering','triggered','url_ready'] %}<span class="badge acc">authorising…</span>
        {% elif a.token.authorized %}<span class="badge ok">authorised</span>{% if a.token.expires_h %} · access token until {{ a.token.expires_h }}{% endif %}
        {% else %}<span class="badge warn">not authorised</span> — sign in once to start reading mail{% endif %}
        &nbsp;· {{ a.mode }} mode
      </div>
    </div>
    <div class="row">
      <button class="btn primary" onclick="startAuth('{{ a.email }}', this)">{{ 'Re-authorise' if a.token.authorized else 'Authorise' }}</button>
      <a class="btn" href="{{ url_for('account_edit', email=a.email) }}">Edit</a>
      <details class="menu">
        <summary class="btn" aria-haspopup="menu">More</summary>
        <div class="menu-pop" role="menu">
          <button class="menu-item" type="button" onclick="cp('{{ a.password }}', this); this.closest('details').removeAttribute('open')">Copy local password</button>
          <button class="menu-item" type="button" onclick="cp('{{ a.redirect_uri }}', this); this.closest('details').removeAttribute('open')">Copy redirect URI</button>
          <form method="post" action="{{ url_for('account_reset_tokens', email=a.email) }}"
                onsubmit="return confirm('Forget the cached OAuth tokens for {{ a.email }}? You will need to authorise again.');">
            <button class="menu-item" type="submit">Reset tokens</button></form>
          <form method="post" action="{{ url_for('account_delete', email=a.email) }}"
                onsubmit="return confirm('Remove account {{ a.email }}? Its tokens and config entry are deleted.');">
            <button class="menu-item danger" type="submit">Remove account…</button></form>
        </div>
      </details>
    </div>
  </div>

  <details class="sub" style="margin-top:8px">
    <summary style="cursor:pointer">Setup details</summary>
    <div class="kv">
      <div class="k">Redirect URI</div><div><code>{{ a.redirect_uri }}</code> <span class="sub">— register exactly this at your provider (when using your own OAuth app)</span></div>
      <div class="k">Local password</div><div><code>{{ a.password }}</code> <span class="sub">— the app signs in with it automatically</span></div>
      <div class="k">Listener</div><div><code>127.0.0.1:{{ a.imap_local_port }}</code> IMAP{% if a.smtp_local_port %} · <code>127.0.0.1:{{ a.smtp_local_port }}</code> SMTP{% endif %}</div>
      <div class="k">Mode</div><div class="sub">{{ a.mode_note }}</div>
    </div>
  </details>

  <div class="auth-panel hidden" id="panel-{{ a.sid }}">
    <div class="auth-msg sub">…</div>
    <div class="auth-url hidden" style="margin-top:8px">
      <div class="note">Open this link in a browser and sign in as <b>{{ a.email }}</b>.
      {% if a.mode == 'tailnet' %}You should then see a “successfully authenticated” page
      from the proxy — if the browser cannot reach it, copy the URL it ended on and paste it below.
      {% else %}The browser will end on an address starting with <code>{{ a.redirect_uri }}</code>
      that fails to load — that is expected. Copy the <b>whole address</b> from the address bar and
      paste it below.{% endif %}</div>
      <p><a class="btn primary" id="url-{{ a.sid }}" href="#" target="_blank" rel="noopener">Open login page →</a>
      <span class="copy" onclick="cp(document.getElementById('url-{{ a.sid }}').href, this)">copy link</span></p>
    </div>
    <div class="auth-paste" style="margin-top:10px">
      <div class="sub" style="margin-bottom:6px">Paste the URL the browser ended on:</div>
      <div class="row" style="gap:8px;flex-wrap:nowrap">
        <input type="text" id="paste-{{ a.sid }}" placeholder="https://localhost:.../?code=..." autocomplete="off">
        <button class="btn" onclick="submitPaste('{{ a.email }}')">Submit</button>
      </div>
    </div>
  </div>
</div>
{% else %}
<div class="card">
  <div class="empty">
    <h4>No accounts yet</h4>
    <p>Add your mail account and sign in once — the watcher and the assistant pick it up automatically.</p>
    <a class="btn primary" href="{{ url_for('account_new') }}">Add account</a>
  </div>
</div>
{% endfor %}

<details class="card">
  <summary style="cursor:pointer;font-weight:600;list-style:none">How the proxy works <span class="sub">(click to read)</span></summary>
  <div class="sub" style="margin-top:10px">The embedded <b>email-oauth2-proxy</b> signs in to your provider with OAuth 2.0 and
  exposes a PLAIN local IMAP listener; Mail Triage reads mail through that listener and never stores a
  provider password. OAuth tokens live in <code>{{ p.cache_file }}</code> and the generated config in
  <code>{{ p.config_file }}</code> (log: <code>{{ p.log_file }}</code>). Account changes restart the proxy automatically.</div>
</details>

<script>
function startAuth(email, btn){
  if(!btn.dataset.label){ btn.dataset.label = btn.textContent.trim(); }
  btn.disabled = true; btn.textContent = 'Starting…';
  fetch('/api/proxy/auth/' + encodeURIComponent(email), {method:'POST'})
    .then(r => r.json())
    .then(j => {
      if(!j.ok){ toast(j.message || 'Could not start authorisation', 'err'); btn.disabled=false; btn.textContent=btn.dataset.label; return; }
      pollAuth(email, btn);
    })
    .catch(e => { toast('Request failed: ' + e, 'err'); btn.disabled=false; btn.textContent=btn.dataset.label; });
}
function pollAuth(email, btn){
  const sid = email.replace(/[^A-Za-z0-9]/g, '-');
  const panel = document.getElementById('panel-' + sid);
  const msg = panel.querySelector('.auth-msg');
  const urlBox = panel.querySelector('.auth-url');
  const urlLink = document.getElementById('url-' + sid);
  panel.classList.remove('hidden');
  btn.textContent = 'Authorising…';
  const timer = setInterval(function(){
    if(!document.body.contains(panel)){ clearInterval(timer); return; }
    fetch('/api/proxy/auth/' + encodeURIComponent(email) + '/status')
      .then(r => r.json())
      .then(j => {
        msg.textContent = j.message || j.status;
        if(j.url){
          urlBox.classList.remove('hidden'); urlLink.href = j.url;
        }
        if(['success','failed','timeout'].indexOf(j.status) !== -1){
          clearInterval(timer);
          btn.disabled = false; btn.textContent = btn.dataset.label || 'Authorise';
          if(j.status === 'success'){
            msg.innerHTML = '<span class="badge ok">authorised</span> ' + (j.message||'');
            setTimeout(function(){ location.reload(); }, 1800);
          } else {
            msg.innerHTML = '<span class="badge err">' + j.status + '</span> ' + (j.message||'');
          }
        } else if(j.status === 'url_ready'){
          msg.innerHTML = '<span class="badge acc">waiting for login</span> ' + (j.message||'');
        }
      })
      .catch(function(){});
  }, 2000);
}
function submitPaste(email){
  const sid = email.replace(/[^A-Za-z0-9]/g, '-');
  const input = document.getElementById('paste-' + sid);
  const panel = document.getElementById('panel-' + sid);
  const msg = panel.querySelector('.auth-msg');
  const val = (input.value || '').trim();
  if(!val){ toast('Paste the URL from the browser first.', 'err'); return; }
  const fd = new URLSearchParams(); fd.append('url', val);
  fetch('/api/proxy/auth/' + encodeURIComponent(email) + '/complete', {method:'POST', body: fd})
    .then(r => r.json())
    .then(j => {
      msg.innerHTML = '<span class="badge ' + (j.ok ? 'ok' : 'err') + '">' +
        (j.ok ? 'submitted' : 'error') + '</span> ' + (j.message || '');
      if(!j.ok){ toast(j.message || 'Could not submit the URL.', 'err'); }
    })
    .catch(e => toast('Request failed: ' + e, 'err'));
}
</script>
"""





ACCOUNT_NEW_TMPL = """
<div class="page-head">
  <div>
    <div class="backlink"><a href="{{ url_for('accounts') }}">← Accounts</a></div>
    <h1 class="page-title">Add account</h1>
    <div class="page-desc">One sign-in per provider. After adding, register the redirect URI shown on the account card, then press Authorise.</div>
  </div>
</div>
<form method="post">
  <div class="card">
    <div class="card-h"><h3>Account</h3></div>
    <label for="provider">Provider</label>
    <select id="provider" name="provider" required onchange="toggleProvider()">
      {% for key, pr in presets.items() %}<option value="{{ key }}" {{ 'selected' if key=='gmail' else '' }}>{{ pr.label }}</option>{% endfor %}
    </select>
    {% for key, pr in presets.items() %}
    <div class="note" id="note-{{ key }}" style="margin-top:8px;display:none">{{ pr.register_notes }}</div>
    {% endfor %}
    <div class="grid2" style="margin-top:4px">
      <div><label for="email">Email address</label><input type="text" id="email" name="email" placeholder="you@example.com" required oninput="autoProvider()"></div>
      <div><label for="password">Local password <span class="sub">(between the app and the proxy)</span></label>
        <div class="row" style="flex-wrap:nowrap"><input type="text" id="password" name="password" value="{{ default_password }}">
        <span class="copy" onclick="document.getElementById('password').value='{{ default_password }}'">reset</span></div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-h"><h3>OAuth app</h3><span class="sub">your own app, or a reused public client ID</span></div>
    <div id="reuse-row" class="note" style="display:none">
      <label class="check" style="margin:0">
        <input type="checkbox" id="reuse_tb" onchange="applyReuse(this.checked)">
        <span>No Entra app of your own? Use <b>Thunderbird's public client ID</b> (personal Outlook/Hotmail,
        and tenants where you cannot register an app). No client secret needed; loopback mode is used and the
        login finishes via the paste box.</span>
      </label>
    </div>
    <div class="grid2">
      <div><label for="client_id">Client ID</label><input type="text" id="client_id" name="client_id"></div>
      <div><label for="client_secret">Client secret <span class="sub">(if required)</span></label><input type="password" id="client_secret" name="client_secret" autocomplete="new-password"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-h"><h3>Login flow</h3></div>
    <label for="redirect_mode">How will you open the login page?</label>
    <select id="redirect_mode" name="redirect_mode">
      <option value="tailnet" selected>From any tailnet device (recommended — needs your own OAuth app with the redirect URI registered)</option>
      <option value="loopback">Loopback + paste-back (needed with reused client IDs, e.g. Thunderbird's)</option>
    </select>
    <div class="sub" style="margin-top:4px">The redirect URI to register at your provider appears on the account card after adding.</div>
  </div>

  <div id="custom-fields" style="display:none">
    <div class="card">
      <div class="card-h"><h3>Custom provider details</h3></div>
      <div class="grid2">
        <div><label for="permission_url">Permission (authorize) URL</label><input type="text" id="permission_url" name="permission_url"></div>
        <div><label for="token_url">Token URL</label><input type="text" id="token_url" name="token_url"></div>
      </div>
      <label for="scope">Scope</label><input type="text" id="scope" name="scope">
      <div class="grid2">
        <div><label for="imap_host">IMAP server</label><input type="text" id="imap_host" name="imap_host" placeholder="imap.example.com"></div>
        <div><label for="imap_port">IMAP port</label><input type="number" id="imap_port" name="imap_port" value="993"></div>
      </div>
      <div class="grid2">
        <div><label for="smtp_host">SMTP server <span class="sub">(optional)</span></label><input type="text" id="smtp_host" name="smtp_host" placeholder="smtp.example.com"></div>
        <div><label for="smtp_port">SMTP port</label><input type="number" id="smtp_port" name="smtp_port" value="465"></div>
      </div>
      <label class="check"><input type="checkbox" name="use_pkce" value="1"> <span>Use PKCE (no client secret)</span></label>
    </div>
  </div>

  <div class="savebar"><button class="btn primary" type="submit">Add account</button><a class="btn" href="{{ url_for('accounts') }}">Cancel</a></div>
</form>
<script>
var REUSE_CLIENT_ID = "{{ reuse_client_id }}";
function applyReuse(on){
  const cid = document.getElementById('client_id');
  const secret = document.getElementById('client_secret');
  const mode = document.getElementById('redirect_mode');
  if(on){
    cid.value = REUSE_CLIENT_ID;
    cid.readOnly = true;
    secret.value = '';
    secret.readOnly = true;
    mode.value = 'loopback';
  } else {
    if(cid.value === REUSE_CLIENT_ID){ cid.value = ''; }
    cid.readOnly = false;
    secret.readOnly = false;
  }
}
function toggleProvider(){
  const v = document.getElementById('provider').value;
  document.getElementById('custom-fields').style.display = (v === 'custom') ? 'block' : 'none';
  document.querySelectorAll('[id^=note-]').forEach(function(el){
    el.style.display = (el.id === 'note-' + v) ? 'block' : 'none';
  });
  const reuseRow = document.getElementById('reuse-row');
  const reuseBox = document.getElementById('reuse_tb');
  if(v === 'outlook'){
    reuseRow.style.display = 'block';
  } else {
    reuseRow.style.display = 'none';
    if(reuseBox.checked){ reuseBox.checked = false; applyReuse(false); }
  }
  if(reuseBox.checked){ applyReuse(true); }
}
var PROVIDER_EMAIL_DOMAINS = {
  gmail: ["gmail.com", "googlemail.com", "google.com"],
  outlook: ["outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "live.co.uk", "msn.com", "outlook.co.uk", "outlook.de", "office365.com"],
  fastmail: ["fastmail.com", "fastmail.fm", "fastmail.net", "fastmail.us"],
  yahoo: ["yahoo.com", "yahoo.co.uk", "yahoo.ca", "yahoo.com.au", "ymail.com", "rocketmail.com"]
};
function providerFromEmail(value){
  var at = value.indexOf('@');
  if(at < 0) return null;
  var domain = value.slice(at + 1).trim().toLowerCase();
  if(!domain) return null;
  var match = null;
  for(var key in PROVIDER_EMAIL_DOMAINS){
    var domains = PROVIDER_EMAIL_DOMAINS[key];
    for(var i = 0; i < domains.length; i++){
      var d = domains[i];
      if(d === domain || (domain.length >= 3 && d.indexOf(domain) === 0)){
        if(match && match !== key) return null;
        match = key;
        break;
      }
    }
  }
  return match;
}
function autoProvider(){
  var key = providerFromEmail(document.getElementById('email').value);
  if(!key) return;
  var sel = document.getElementById('provider');
  if(sel.value !== key){ sel.value = key; toggleProvider(); }
}
toggleProvider();
</script>
"""





ACCOUNT_EDIT_TMPL = """
<div class="page-head">
  <div>
    <div class="backlink"><a href="{{ url_for('accounts') }}">← Accounts</a></div>
    <h1 class="page-title">Edit {{ a.email }}</h1>
    <div class="page-desc">Saving restarts the proxy. Redirect URI: <code>{{ client_settings.redirect_uri }}</code></div>
  </div>
</div>
<form method="post">
  <div class="card">
    <div class="card-h"><h3>Account</h3></div>
    <div class="grid2">
      <div><label>Email</label><div class="sub" style="padding-top:8px"><b>{{ a.email }}</b> <span class="sub">· cannot be changed</span></div></div>
      <div><label for="provider">Provider</label>
        <select id="provider" name="provider">
          {% for key, pr in presets.items() %}<option value="{{ key }}" {{ 'selected' if key==a.provider else '' }}>{{ pr.label }}</option>{% endfor %}
        </select></div>
    </div>
    <div class="grid2">
      <div><label for="password">Local password <span class="sub">(local to this app — blank = keep current)</span></label><input type="text" id="password" name="password" value="{{ a.password }}" autocomplete="off" spellcheck="false" autocapitalize="none"></div>
      <div><label for="redirect_mode">Login mode</label>
        <select id="redirect_mode" name="redirect_mode">
          <option value="tailnet" {{ 'selected' if a.redirect_mode != 'loopback' else '' }}>Tailnet (browser on any tailnet device)</option>
          <option value="loopback" {{ 'selected' if a.redirect_mode == 'loopback' else '' }}>Loopback + paste-back</option>
        </select>
        <div class="sub" style="margin-top:4px">{{ client_settings.mode_note }}</div></div>
    </div>
  </div>
  <div class="card">
    <div class="card-h"><h3>OAuth app</h3></div>
    <div class="grid2">
      <div><label for="client_id">Client ID</label><input type="text" id="client_id" name="client_id" value="{{ a.client_id }}"></div>
      <div><label for="client_secret">Client secret <span class="sub">({{ 'currently set' if a.client_secret else 'not set' }} — blank = keep current)</span></label><input type="password" id="client_secret" name="client_secret" autocomplete="new-password"></div>
    </div>
    <div class="grid2">
      <div><label for="permission_url">Permission URL</label><input type="text" id="permission_url" name="permission_url" value="{{ a.auth_url }}"></div>
      <div><label for="token_url">Token URL</label><input type="text" id="token_url" name="token_url" value="{{ a.token_url }}"></div>
    </div>
    <label for="scope">Scope</label><input type="text" id="scope" name="scope" value="{{ a.scopes }}">
    <label class="check" style="margin-top:10px"><input type="checkbox" name="use_pkce" value="1" {{ 'checked' if a.use_pkce else '' }}> <span>Use PKCE (no client secret)</span></label>
  </div>
  <div class="card">
    <div class="card-h"><h3>Server</h3><span class="sub">proxied upstream servers</span></div>
    <div class="grid2">
      <div><label for="imap_host">IMAP server</label><input type="text" id="imap_host" name="imap_host" value="{{ a.imap_host }}"></div>
      <div><label for="imap_port">IMAP port</label><input type="number" id="imap_port" name="imap_port" value="{{ a.imap_port }}"></div>
    </div>
    <div class="grid2">
      <div><label for="smtp_host">SMTP server</label><input type="text" id="smtp_host" name="smtp_host" value="{{ a.smtp_host }}"></div>
      <div><label for="smtp_port">SMTP port</label><input type="number" id="smtp_port" name="smtp_port" value="{{ a.smtp_port }}"></div>
    </div>
  </div>
  <div class="savebar"><button class="btn primary" type="submit">Save account</button><a class="btn" href="{{ url_for('accounts') }}">Cancel</a><span class="sub">Listener: 127.0.0.1:{{ a.imap_local_port }}</span></div>
</form>
"""






PROXY_LOG_TMPL = """
<div class="page-head">
  <div>
    <h1 class="page-title">Proxy log</h1>
    <div class="page-desc">The embedded email-oauth2-proxy — log file <span class="mono">{{ log_file }}</span></div>
  </div>
  <div class="row chiprow">
    <a class="chip{{ ' active' if n == 200 else '' }}" href="{{ url_for('proxy_log') }}?n=200">200</a>
    <a class="chip{{ ' active' if n == 500 else '' }}" href="{{ url_for('proxy_log') }}?n=500">500</a>
    <a class="chip{{ ' active' if n == 2000 else '' }}" href="{{ url_for('proxy_log') }}?n=2000">2000</a>
    <a class="btn small" href="{{ url_for('accounts') }}">Accounts</a>
  </div>
</div>
<div class="card flush">
  <pre class="log" style="border:0;margin:0">{{ content }}</pre>
</div>
"""






@app.route("/accounts")
def accounts():
    ext_mode = (store.get_setting("proxy_mode") or "embedded") == "external"
    return render(_render_src(ACCOUNTS_TMPL, accounts=_accounts_view(),
                                         p=_proxy_status_view(), ext_mode=ext_mode))


@app.route("/accounts/new", methods=["GET", "POST"])
def account_new():
    if request.method == "POST":
        rec, err = proxy.account_from_form(request.form)
        if err:
            flash(err, "err")
            return redirect(url_for("account_new"))
        if proxy.get_account(rec["email"]):
            flash("An account for %s already exists." % rec["email"], "err")
            return redirect(url_for("account_new"))
        proxy.upsert_account(rec)
        ok, merr = proxy.manager.apply()
        store.log_event("info", "emailproxy: account %s added" % rec["email"])
        if ok:
            flash("Account %s added — press Authorise on the Accounts page, then log in."
                  % rec["email"], "ok")
        else:
            flash("Account %s added, but the embedded proxy did not start: %s"
                  % (rec["email"], merr), "warn")
        return redirect(url_for("accounts"))
    return render(_render_src(
        ACCOUNT_NEW_TMPL, presets=proxy.PRESETS,
        default_password=proxy.default_password(),
        reuse_client_id=proxy.PRESETS["outlook"]["reuse_client_id"]))


@app.route("/accounts/<path:email>/edit", methods=["GET", "POST"])
def account_edit(email):
    acct = proxy.get_account(email)
    if not acct:
        flash("Unknown account %s." % email, "err")
        return redirect(url_for("accounts"))
    if request.method == "POST":
        data = dict(request.form)
        data["email"] = acct["email"]
        if not (data.get("password") or "").strip():
            data["password"] = acct.get("password") or ""
        if not (data.get("client_secret") or "").strip():
            data["client_secret"] = acct.get("client_secret") or ""
        same_provider = (data.get("provider") or acct.get("provider")) == acct.get("provider")
        rec, err = proxy.account_from_form(data, existing=acct if same_provider else None)
        if err:
            flash(err, "err")
            return redirect(url_for("account_edit", email=acct["email"]))
        proxy.upsert_account(rec)
        ok, merr = proxy.manager.apply()
        store.log_event("info", "emailproxy: account %s updated" % acct["email"])
        if ok:
            flash("Saved %s." % acct["email"], "ok")
        else:
            flash("Saved, but the embedded proxy did not restart: %s" % merr, "warn")
        return redirect(url_for("accounts"))
    return render(_render_src(ACCOUNT_EDIT_TMPL, a=acct, presets=proxy.PRESETS,
                                         client_settings=proxy.client_settings(acct)))


@app.route("/accounts/<path:email>/reset-tokens", methods=["POST"])
def account_reset_tokens(email):
    ok, err = proxy.reset_tokens(email)
    flash(("Cached tokens cleared for %s." % email) if ok else ("Reset failed: %s" % err),
          "ok" if ok else "warn")
    return redirect(url_for("accounts"))


@app.route("/accounts/<path:email>/delete", methods=["POST"])
def account_delete(email):
    ok, err = proxy.remove_account(email)
    flash(("Account %s removed." % email) if ok else ("Removal failed: %s" % err),
          "ok" if ok else "warn")
    return redirect(url_for("accounts"))


@app.route("/proxy/restart", methods=["POST"])
def proxy_restart():
    ok, err = proxy.manager.restart()
    store.log_event("info", "emailproxy: manual restart (%s)" % ("ok" if ok else err))
    flash("Proxy restarted." if ok else "Proxy restart failed: %s" % err, "ok" if ok else "err")
    return redirect(url_for("accounts"))


@app.route("/proxy/log")
def proxy_log():
    n = request.args.get("n", type=int) or 300
    return render(_render_src(PROXY_LOG_TMPL, content=proxy.tail_log(n), n=n,
                                         log_file=proxy.log_path()))


@app.route("/api/proxy/auth/<path:email>", methods=["POST"])
def proxy_auth_start(email):
    if not proxy.get_account(email):
        return jsonify({"ok": False, "message": "Unknown account."}), 404
    ok, err = proxy.start_auth(email)
    if not ok:
        return jsonify({"ok": False, "message": err}), 409
    return jsonify({"ok": True})


@app.route("/api/proxy/auth/<path:email>/status")
def proxy_auth_status(email):
    state = proxy.auth_state(email)
    state["token"] = proxy.token_status(email)
    return jsonify(state)


@app.route("/api/proxy/auth/<path:email>/complete", methods=["POST"])
def proxy_auth_complete(email):
    ok, message = proxy.complete_auth(email, request.form.get("url") or "")
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


# ---------------------------------------------------------------- log

LOG_TMPL = """
<div class="page-head">
  <div>
    <h1 class="page-title">Activity log</h1>
    <div class="page-desc logdesc">Everything Mail Triage did, newest first · times in {{ tz }}{% if not show_debug %} (debug lines hidden){% endif %}.</div>
  </div>
  <div class="row chiprow">
    <form class="inline" method="get" action="{{ url_for('log') }}">
      {% if lvl != 'all' %}<input type="hidden" name="lvl" value="{{ lvl }}">{% endif %}
      {% if show_debug %}<input type="hidden" name="debug" value="1">{% endif %}
      {% if mins %}<input type="hidden" name="mins" value="{{ mins }}">{% endif %}
      <input type="search" name="q" value="{{ q }}" placeholder="Filter lines&hellip;" aria-label="Filter log lines" style="width:170px">
      <button class="btn small" type="submit">Find</button>
    </form>
    <a class="chip{{ ' active' if lvl == 'all' and not mins else '' }}" href="{{ url_for('log', debug=('1' if show_debug else none), q=(q or none), mins=(mins or none)) }}">All</a>
    <a class="chip{{ ' active' if lvl == 'error' else '' }}" href="{{ url_for('log', lvl='error', debug=('1' if show_debug else none), q=(q or none), mins=(mins or none)) }}">Errors{% if errors %} <span class="n">{{ errors }}</span>{% endif %}</a>
    <a class="chip{{ ' active' if lvl == 'warn' else '' }}" href="{{ url_for('log', lvl='warn', debug=('1' if show_debug else none), q=(q or none), mins=(mins or none)) }}">Warnings{% if warns %} <span class="n">{{ warns }}</span>{% endif %}</a>
    <a class="chip{{ ' active' if lvl == 'info' else '' }}" href="{{ url_for('log', lvl='info', debug=('1' if show_debug else none), q=(q or none), mins=(mins or none)) }}">Info</a>
    {% if show_debug %}<a class="chip" href="{{ url_for('log', lvl=lvl, q=(q or none), mins=(mins or none)) }}">hide debug lines</a>
    {% else %}<a class="chip" href="{{ url_for('log', lvl=lvl, debug='1', q=(q or none), mins=(mins or none)) }}">show debug lines</a>{% endif %}
    <a class="chip{{ ' active' if mins == '15' else '' }}" href="{{ url_for('log', lvl=lvl, debug=('1' if show_debug else none), q=(q or none), mins='15') }}">15m</a>
    <a class="chip{{ ' active' if mins == '60' else '' }}" href="{{ url_for('log', lvl=lvl, debug=('1' if show_debug else none), q=(q or none), mins='60') }}">1h</a>
    <a class="chip{{ ' active' if mins == '1440' else '' }}" href="{{ url_for('log', lvl=lvl, debug=('1' if show_debug else none), q=(q or none), mins='1440') }}">24h</a>
    <button class="btn small" id="logpause" type="button">Pause</button>
    <a class="btn small" href="{{ url_for('log', lvl=lvl, debug=('1' if show_debug else none), q=(q or none), mins=(mins or none)) }}">Refresh</a>
  </div>
</div>
<div class="card logpanel">
  {% for e in events %}
  <div class="logrow"><span class="mono">{{ e.when }}</span> <span class="badge {{ e.cls }}">{{ e.level }}</span> <span class="lmsg">{{ e.message }}</span></div>
  {% else %}<div class="sub">Nothing logged at this level yet.</div>{% endfor %}
</div>
<script>
/* log live mode: refreshes every 10s unless paused (localStorage) or the user
   is typing; re-arms itself and no-ops when the page was swapped away. */
(function(){
  var btn = document.getElementById('logpause');
  if(!btn) return;
  var KEY = 'logPaused';
  var paused = false;
  try { paused = localStorage.getItem(KEY) === '1'; } catch(e) {}
  function paint(){
    btn.textContent = paused ? 'Resume' : 'Pause';
    btn.title = paused ? 'Auto-refresh is paused' : 'Pauses the 10s auto-refresh';
  }
  paint();
  btn.addEventListener('click', function(){
    paused = !paused;
    try { localStorage.setItem(KEY, paused ? '1' : '0'); } catch(e) {}
    paint(); arm();
  });
  function arm(){
    if(window.__logTimer) clearTimeout(window.__logTimer);
    if(paused) return;
    window.__logTimer = setTimeout(function(){
      if(document.getElementById('logpause') !== btn) return;
      if(document.hidden || (document.activeElement && document.activeElement.tagName === 'INPUT')) { arm(); return; }
      location.reload();
    }, 10000);
  }
  arm();
  document.addEventListener('visibilitychange', function(){ if(!document.hidden) arm(); });
})();
</script>
"""




@app.route("/log")
def log():
    show_debug = request.args.get("debug") == "1"
    lvl = request.args.get("lvl", "all")
    if lvl not in ("all", "error", "warn", "info"):
        lvl = "all"
    q = (request.args.get("q") or "").strip()[:80]
    mins = request.args.get("mins") or ""
    if mins not in ("15", "60", "1440"):
        mins = ""
    events = store.recent_events(1000)
    if not show_debug:
        events = [e for e in events if e.get("level") != "debug"]
    errors = sum(1 for e in events if e.get("level") == "error")
    warns = sum(1 for e in events if e.get("level") == "warn")
    if lvl != "all":
        events = [e for e in events if e.get("level") == lvl]
    if q:
        ql = q.lower()
        events = [e for e in events if ql in (e.get("message") or "").lower()]
    if mins:
        since = time.time() - int(mins) * 60
        events = [e for e in events if (e.get("ts") or 0) >= since]
    events = events[:300]
    for e in events:
        e["when"] = fmt_ts(e["ts"])
        e["cls"] = {"error": "err", "info": "ok", "warn": "warn", "debug": ""}.get(e.get("level"), "")
    return render(_render_src(LOG_TMPL, events=events, show_debug=show_debug,
                                         errors=errors, warns=warns, lvl=lvl, q=q, mins=mins,
                                         tz=tz_label()))


SIMULATE_TMPL = """
<style>
/* simulator (dry-run report): draft left, report right from 1024px up */
.simgrid{display:grid;grid-template-columns:minmax(340px,5fr) minmax(0,7fr);gap:14px;align-items:start}
.simgrid .card{margin:0}
@media(max-width:1023px){.simgrid{grid-template-columns:1fr}}
.simcheck{display:flex;gap:10px;align-items:flex-start;margin-top:14px;cursor:pointer}
.simcheck input{width:15px;height:15px;margin:2px 0 0;flex:none}
.simcheck>span{min-width:0}
.simcheck .t{display:block;font-size:.86rem;font-weight:500;color:var(--fg);line-height:1.4}
.simcheck .d{display:block;font-size:.78rem;color:var(--dim);margin-top:2px;line-height:1.45}
.simrun{margin-top:14px}
.simrun .btn{width:100%}
.simhero{display:grid;grid-template-columns:auto 1fr;gap:0 12px;padding:2px 0 14px;border-bottom:1px solid var(--line)}
.simhero .dot{margin-top:8px}
.simhero-t{font-size:1.01rem;font-weight:600;letter-spacing:-.01em;line-height:1.5}
.simchips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.simchips:empty{display:none}
.simrow{display:grid;grid-template-columns:auto 1fr;gap:0 12px;padding:12px 0;position:relative}
.simrow+.simrow{border-top:1px solid var(--line)}
.simrow .dot{margin-top:6px}
.simrow:not(:last-child)::before{content:'';position:absolute;left:3px;top:26px;bottom:-1px;width:1px;background:var(--line)}
.simrow-t{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.simrow-t b{font-size:.9rem;font-weight:600;letter-spacing:-.01em}
.simrow-d{font-size:.85rem;color:var(--dim);margin-top:3px;line-height:1.5}
.simrow.cur .simrow-d{color:var(--fg)}
.simnest{margin-top:10px;padding-left:13px;border-left:2px solid var(--line)}
.simdraft{margin-top:10px;border:1px solid var(--line);background:#fff;padding:10px 12px}
.simdraft-b{white-space:pre-wrap;font-family:var(--mono);font-size:.82rem;line-height:1.5;margin:8px 0 0;max-height:260px;overflow:auto}
.simacts{display:flex;flex-wrap:wrap;gap:6px}
.simact{font-size:.8rem;border:1px solid var(--line);padding:2px 9px;background:#fff;color:#3f3f46}
.simverdict{font-size:.92rem;line-height:1.5}
.simnotes{margin-top:14px;display:flex;flex-direction:column;gap:8px}
.simnotes .note{margin:0}
@media(max-width:767px){
  .simhero-t{font-size:.95rem}
  .simrow{padding:11px 0}
}
</style>
<div class="page-head">
  <div>
    <h1 class="page-title">Simulator</h1>
    <div class="page-desc">Draft an email and see how the pipeline would treat it — rules, flows, classifier. Nothing is changed.</div>
  </div>
</div>
{% if prefill_note %}<div class="card" style="border-left:3px solid var(--acc)"><span class="sub">{{ prefill_note }}</span></div>{% endif %}
<div class="simgrid">
  <form method="post">
    <div class="card">
      <div class="card-h"><h3>The draft</h3><span class="sub">Nothing here touches your mailbox.</span></div>
      <label for="s-from">From</label>
      <input id="s-from" type="text" name="from_addr" value="{{ form.from_addr }}" placeholder="sender@example.com" autocomplete="off">
      <label for="s-to">To <span class="sub">· optional</span></label>
      <input id="s-to" type="text" name="to_addr" value="{{ form.to_addr }}" autocomplete="off">
      <label for="s-subj">Subject</label>
      <input id="s-subj" type="text" name="subject" value="{{ form.subject }}">
      <label for="s-body">Body</label>
      <textarea id="s-body" name="body" rows="7">{{ form.body }}</textarea>
      <div class="row" style="margin-top:12px;align-items:center;gap:8px">
        <label for="s-prefill" style="margin:0">Prefill from</label>
        <select id="s-prefill" name="t" style="max-width:280px">
          <option value="">Pick a flow or rule&hellip;</option>
          {% for tg in targets %}<option value="{{ tg.value }}">{{ tg.label }}</option>{% endfor %}
        </select>
        <button class="btn small" type="submit" formmethod="get" formaction="{{ url_for('simulate') }}">Generate an example draft</button>
      </div>
      <label class="simcheck" for="s-llm">
        <input id="s-llm" type="checkbox" name="use_llm" value="1" {{ 'checked' if form.use_llm else '' }}>
        <span><span class="t">Ask the classifier</span>
        <span class="d">Runs the model too — needed to test AI category / topic flows, preview an LLM draft and see its reasoning.</span></span>
      </label>
      <div class="simrun">
        <button class="btn primary" type="submit">Run simulation</button>
      </div>
    </div>
  </form>
  {% if result %}
  {% set v = result.verdict %}{% set llm = result.use_llm %}
  <section class="card" id="sim-report">
    <div class="card-h"><h3>What would happen</h3><span class="sub">Nothing was changed — this is a dry run.</span></div>
    <div class="simhero">
      <span class="dot {% if result.guard %}warn{% elif result.rule or result.flow %}ok{% elif v %}acc{% endif %}"></span>
      <div>
        <div class="simhero-t">{% if result.guard %}Blocked by guard “{{ result.guard }}” — this mail would stay put.
          {% elif result.rule %}Rule “{{ result.rule.name }}” matches — {{ result.rule_actions|join(', ') }}.
          {% elif result.flow %}Flow “{{ result.flow.name }}” matches — {{ result.flow_taken|length }} step{{ 's' if result.flow_taken|length != 1 else '' }} would run.
          {% elif v and v.category %}No rule or flow matches — the classifier suggests “{{ v.category }}”.
          {% elif v %}No rule or flow matches — the classifier found no category.
          {% else %}No rule or flow matches.{% endif %}</div>
        <div class="simchips">
          {% if v and v.needs_reply %}<span class="badge warn">needs reply</span>{% endif %}
          {% if result.rule and not result.rules_apply %}<span class="badge">rules in dry-run — suggest only</span>{% endif %}
          {% if result.flow and not result.flows_apply %}<span class="badge">flows in dry-run — record only</span>{% endif %}
          {% if v and not result.guard and not result.rule and not result.flow and result.suggested_folder %}
            {% if result.llm_apply %}<span class="badge ok">auto-file → “{{ result.suggested_folder }}”</span>
            {% else %}<span class="badge">suggested folder “{{ result.suggested_folder }}” — auto-filing off</span>{% endif %}
          {% endif %}
        </div>
      </div>
    </div>
    <div class="simrows">
      <div class="simrow{% if result.guard or result.rule %} cur{% endif %}">
        <span class="dot {% if result.guard %}warn{% elif result.rule %}ok{% endif %}"></span>
        <div>
          <div class="simrow-t"><b>Rules</b>
            {% if result.guard %}<span class="badge warn">blocked</span>
            {% elif result.rule %}<span class="badge ok">matched</span>
            {% else %}<span class="badge">no match</span>{% endif %}
          </div>
          <div class="simrow-d">{% if result.guard %}Guard “{{ result.guard }}” is a keep guard — it stops all automated filing for this mail.
            {% elif result.rule %}“{{ result.rule.name }}” — first match in list order.
            {% else %}No enabled rule matched.{% endif %}</div>
          {% if result.rule %}
          <div class="simnest">
            <div class="simacts">{% for a in result.rule_actions %}<span class="simact">→ {{ a }}</span>{% endfor %}</div>
          </div>
          {% endif %}
        </div>
      </div>
      <div class="simrow{% if result.flow %} cur{% endif %}">
        <span class="dot {% if result.guard %}warn{% elif result.flow %}ok{% endif %}"></span>
        <div>
          <div class="simrow-t"><b>Flows</b>
            {% if result.guard or result.rule %}<span class="badge">skipped</span>
            {% elif result.flow %}<span class="badge ok">matched</span>
            {% else %}<span class="badge">no match</span>{% endif %}
          </div>
          <div class="simrow-d">
            {% if result.guard %}Not evaluated — the guard stops the pipeline here.
            {% elif result.rule %}Not evaluated — a rule matched first (flows run after rules).
            {% elif result.flow %}“{{ result.flow.name }}” matched.
            {% else %}No rule or deterministic flow matches.{% if not llm %} Tick “Ask the classifier” to also test AI category / topic flows.{% endif %}{% endif %}
          </div>
          {% if result.flow %}
          <div class="simnest">
            <ol class="simul">{% for s in result.flow_taken %}<li>{{ s }}</li>{% endfor %}</ol>
            {% if result.draft_preview %}
            {% set dp = result.draft_preview %}
            <div class="simdraft">
              <div class="simrow-t"><b>Draft preview</b>
                {% if dp.error %}<span class="badge err">could not build</span>
                {% elif dp.needs_llm %}<span class="badge warn">model not asked</span>
                {% else %}<span class="badge ok">{{ 'written by the model' if dp.by == 'model' else ('written by plugin' if dp.by == 'plugin' else ('rendered from the template' if dp.by == 'template' else 'fixed text')) }}</span>
                {% endif %}
              </div>
              {% if dp.error %}<div class="simrow-d">The draft could not be built: {{ dp.error }}</div>
              {% elif dp.needs_llm %}<div class="simrow-d">This step writes an LLM draft{% if dp.instructions %} guided by “{{ dp.instructions }}”{% endif %}. Tick “Ask the classifier” above to preview the actual text here.</div>
              {% else %}
              <div class="simrow-d">Would be saved to Drafts — To: {{ dp.to or '(none)' }} · Subject: {{ dp.subject or '(none)' }}</div>
              <pre class="simdraft-b">{{ dp.body }}</pre>
              {% endif %}
            </div>
            {% endif %}
          </div>
          {% endif %}
        </div>
      </div>
      <div class="simrow{% if v and not result.guard and not result.rule and not result.flow %} cur{% endif %}">
        <span class="dot {% if v %}acc{% endif %}"></span>
        <div>
          <div class="simrow-t"><b>Classifier</b>
            {% if v %}<span class="badge acc">ran</span>{% else %}<span class="badge">skipped</span>{% endif %}
          </div>
          <div class="simrow-d">
            {% if v %}“{{ v.category or '(no category)' }}”{% if v.confidence %} — {{ '%.0f' % (v.confidence * 100) }}%{% endif %} · {{ v.by }}
            {% else %}Skipped — “Ask the classifier” is off.{% endif %}
          </div>
          {% if v %}
          <div class="simnest">
            <div class="simverdict"><b>Classifier:</b> “{{ v.category or '(no category)' }}”{% if v.confidence %} — {{ '%.0f' % (v.confidence * 100) }}%{% endif %} <span class="sub">· {{ v.by }}</span></div>
            {% if v.reason %}<div class="simrow-d">why: {{ v.reason }}</div>{% endif %}
            {% if v.summary %}<div class="simrow-d">{{ v.summary }}</div>{% endif %}
            {% if v.thinking %}<details class="think" style="margin-top:9px"><summary>Model reasoning <span class="sub">(raw chain of thought)</span></summary><pre class="mono audit-pre">{{ v.thinking }}</pre></details>{% endif %}
          </div>
          {% endif %}
        </div>
      </div>
    </div>
    {% if result.notes %}
    <div class="simnotes">{% for n in result.notes %}<div class="note">Note: {{ n }}</div>{% endfor %}</div>
    {% endif %}
  </section>
  <script>setTimeout(function(){try{var c=document.getElementById('sim-report');if(c){c.scrollIntoView({block:'start'});window.scrollBy(0,-70);}}catch(e){}},60);</script>
  {% else %}
  <div class="card">
    <div class="empty">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3v6l-5 8a2 2 0 0 0 1.7 3h12.6a2 2 0 0 0 1.7-3l-5-8V3"/><path d="M7 3h10"/></svg>
      <h4>Test a draft against the whole pipeline</h4>
      <p>Write a plausible email and run it. The report shows the decision path — <b>rules</b> first, then <b>flows</b>, then the <b>classifier</b> — which stage would act, and why. Nothing is sent, moved or changed.</p>
    </div>
  </div>
  {% endif %}
</div>
<script>
window.mtSimFill = function(fields, note){
  fields = fields || {};
  function set(id, v){ if(typeof v !== 'string') return; var el=document.getElementById(id); if(el) el.value=v; }
  set('s-from', fields.from_addr); set('s-to', fields.to_addr);
  set('s-subj', fields.subject); set('s-body', fields.body);
  var llm=document.getElementById('s-llm');
  if(llm && typeof fields.use_llm === 'boolean') llm.checked = fields.use_llm;
  var card=document.querySelector('.simgrid form .card');
  if(card){
    card.style.transition='box-shadow .35s';
    card.style.boxShadow='0 0 0 2px var(--acc)';
    setTimeout(function(){ card.style.boxShadow=''; }, 1800);
  }
  if(note && window.toast) toast(note, 'ok');
};
</script>
"""


_SIM_RESULTS = {}


def _sim_store(form, result):
    """Short-lived store for simulator results. POST must redirect (Turbo skips
    visits to the same URL unless action=replace, which a redirect provides)."""
    now = time.time()
    for k in [k for k, v in list(_SIM_RESULTS.items()) if now - v[0] > 1800]:
        _SIM_RESULTS.pop(k, None)
    key = os.urandom(8).hex()
    _SIM_RESULTS[key] = (now, form, result)
    while len(_SIM_RESULTS) > 30:
        _SIM_RESULTS.pop(min(_SIM_RESULTS, key=lambda k: _SIM_RESULTS[k][0]))
    return key


@app.route("/simulate", methods=["GET", "POST"])
def simulate():
    form = {"from_addr": "", "to_addr": "", "subject": "", "body": "", "use_llm": True}
    result = None
    if request.method == "POST":
        form["from_addr"] = (request.form.get("from_addr") or "").strip()[:200]
        form["to_addr"] = (request.form.get("to_addr") or "").strip()[:200]
        form["subject"] = (request.form.get("subject") or "").strip()[:300]
        form["body"] = (request.form.get("body") or "").strip()[:8000]
        form["use_llm"] = request.form.get("use_llm") == "1"
        if not (form["subject"] or form["body"] or form["from_addr"]):
            flash("Give the draft at least a sender, a subject or a body.", "err")
            return redirect(url_for("simulate"))
        result = engine.simulate_email(form["from_addr"], form["subject"], form["body"],
                                       to_addr=form["to_addr"], use_llm=form["use_llm"])
        return redirect(url_for("simulate", s=_sim_store(form, result)))
    key = request.args.get("s") or ""
    if key:
        hit = _SIM_RESULTS.get(key)
        if hit:
            _ts, form, result = hit
    prefill_note = ""
    if not key:
        target = (request.args.get("t") or "").strip()
        if not target:
            if request.args.get("flow"):
                target = "flow:" + (request.args.get("flow") or "")
            elif request.args.get("rule"):
                target = "rule:" + (request.args.get("rule") or "")
        if target and ":" in target:
            k, _, g = target.partition(":")
            try:
                gid = int(g)
            except ValueError:
                gid = 0
            ex = engine.example_draft_for(k, gid) if gid else None
            if ex:
                form = {"from_addr": ex["draft"].get("from_addr") or "", "to_addr": "",
                        "subject": ex["draft"].get("subject") or "",
                        "body": ex["draft"].get("body") or "", "use_llm": True}
                prefill_note = ("Draft %s to exercise %s \u201c%s\u201d \u2014 edit anything, then Run. "
                                "Nothing runs until you do."
                                % ("written by the model" if ex["draft"].get("by") == "llm"
                                   else "built from its conditions", ex["kind"], ex["name"]))
    targets = ([{"value": "flow:%d" % f["id"], "label": "Flow: %s" % (f.get("name") or f["id"])}
                for f in store.list_flows()]
               + [{"value": "rule:%d" % r["id"], "label": "Rule: %s" % (r.get("name") or r["id"])}
                  for r in store.list_rules()])
    return render(_render_src(SIMULATE_TMPL, form=form, result=result,
                              prefill_note=prefill_note, targets=targets))


EVAL_TMPL = """<style>
.evmsgsub{font-size:.85rem;color:var(--dim);margin-top:2px}
.evbody{white-space:pre-wrap;font-size:.88rem;line-height:1.55;margin-top:10px;max-height:340px;overflow:auto}
.evbtns{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}
@media(max-width:767px){.evbody{max-height:150px}}
</style>
<div class="page-head">
  <div>
    <h1>Label the test set</h1>
    <div class="page-desc">Answer from your own judgment. Nothing shows what any model thinks here - that is the point. Your answers become the test set everything gets scored against.</div>
  </div>
</div>
<div class="card">
  <div class="card-h"><h3>Progress</h3><span class="sub">{{ done }} of {{ total }} messages labeled{% if skipped %} - {{ skipped }} marked not sure{% endif %}</span></div>
  <span class="eprog" style="min-width:220px"><i style="width:{{ (100 * done / total)|round|int if total else 0 }}%"></i></span>
</div>
{% if msg %}
<div class="card">
  <div class="card-h"><h3>{{ msg.subject or '(no subject)' }}</h3><span class="sub">{{ fmt_ts(msg.date_ts) }}</span></div>
  <div class="evmsgsub">From: {{ msg.from_addr }}{% if msg.to_addr %} - To: {{ msg.to_addr }}{% endif %}</div>
  <div class="evbody">{{ msg.snippet or '(no preview)' }}</div>
  <div class="sub" style="margin-top:8px"><a href="/messages/{{ msg.id }}" target="_blank">Open the full message</a></div>
</div>
{% if 'needs_reply' in need %}
<div class="card">
  <div class="card-h"><h3>Does it need a reply from you?</h3></div>
  <div class="evbtns">
    <form method="post" action="{{ url_for('learning_eval_label') }}"><input type="hidden" name="msg" value="{{ msg.id }}"><input type="hidden" name="task" value="needs_reply"><input type="hidden" name="label" value="1"><button class="btn small" type="submit">Yes - needs a reply</button></form>
    <form method="post" action="{{ url_for('learning_eval_label') }}"><input type="hidden" name="msg" value="{{ msg.id }}"><input type="hidden" name="task" value="needs_reply"><input type="hidden" name="label" value="0"><button class="btn small" type="submit">No reply needed</button></form>
  </div>
</div>
{% endif %}
{% if 'category' in need %}
<div class="card">
  <div class="card-h"><h3>Which category?</h3></div>
  <div class="evbtns">
    <form method="post" action="{{ url_for('learning_eval_label') }}"><input type="hidden" name="msg" value="{{ msg.id }}"><input type="hidden" name="task" value="category"><select class="evsel" name="label">{% for c in cats %}<option value="{{ c }}">{{ c }}</option>{% endfor %}</select><button class="btn small" type="submit">Save</button></form>
    <form method="post" action="{{ url_for('learning_eval_label') }}"><input type="hidden" name="msg" value="{{ msg.id }}"><input type="hidden" name="task" value="category"><input type="hidden" name="label" value="__skip__"><button class="btn small" type="submit">Not sure</button></form>
  </div>
</div>
{% endif %}
{% else %}
<div class="card"><div class="card-h"><h3>All done</h3></div><div class="sub">Every sampled message has your answers. The scores are on the <a href="{{ url_for('learning_page') }}">Learning page</a>.</div></div>
{% endif %}
<div class="sub" style="margin-top:10px"><a href="{{ url_for('learning_page') }}">Back to Learning</a></div>
"""


LEARN_TMPL = """<style>
.lsteps{margin-top:6px}
.lstep{display:grid;grid-template-columns:auto 1fr;gap:0 12px;padding:10px 0;position:relative}
.lstep:not(:last-child)::before{content:'';position:absolute;left:3px;top:25px;bottom:-1px;width:1px;background:var(--line)}
.lstep .dot{margin-top:5px}
.lstep.now .dot{background:var(--acc)}
.lstep b{font-size:.9rem;letter-spacing:-.01em}
.lstep .sub{font-size:.82rem;margin-top:2px;line-height:1.5}
.lstep.todo b,.lstep.todo .sub{color:var(--dim)}
.lead{font-size:.98rem;font-weight:600;letter-spacing:-.01em;line-height:1.5;margin-bottom:2px}
.lfold{border-top:1px solid var(--line)}
.lfold>summary{cursor:pointer;padding:13px 2px;font-weight:600;font-size:.9rem;list-style:none;display:flex;align-items:center;gap:8px}
.lfold>summary::-webkit-details-marker{display:none}
.lfold>summary::before{content:'▸';font-size:.7rem;color:var(--dim)}
.lfold[open]>summary::before{content:'▾'}
.lfold .lfold-i{padding:0 0 14px}
.ltbl td,.ltbl th{vertical-align:top}
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px;align-items:start}
.mgrid .card{margin:0}
.eprog{display:inline-block;min-width:110px;height:6px;border-radius:3px;background:var(--line);vertical-align:middle;margin-right:8px;overflow:hidden}
.eprog i{display:block;height:100%;border-radius:3px;background:var(--acc)}
.evsel{padding:5px 8px;border-radius:8px;border:1px solid var(--line);background:var(--bg,#fff);color:inherit;font:inherit;font-size:.85rem}
@media(max-width:767px){.ltbl .hide-m{display:none}.lstep{padding:9px 0}}
</style>
{% set s = rep.current %}
{% set titles = {'needs_reply': 'Reply detector', 'category': 'Category sorter'} %}
{% set m = (s.metrics_parsed.val or {}) if s else {} %}
{% set ds = (s.stats_parsed.dataset or {}) if s else {} %}
{% set live = s.live if s else {} %}
{% set stat_word = {'validated': 'Trained · not watching yet', 'shadow': 'Watching quietly', 'active': 'Taking over confident calls', 'degraded': 'Needs attention', 'retired': 'Retired', 'rejected': 'Rejected', 'proposed': 'Prepared'}.get(s.status, s.status) if s else '' %}
<div class="page-head">
  <div>
    <h1 class="page-title">Learning</h1>
    <div class="page-desc">Review the models helping with triage, correct their mistakes and decide which may act.</div>
  </div>
  {% if s %}<form method="post" action="{{ url_for('learning_train') }}"><input type="hidden" name="task" value="{{ s.task }}"><button class="btn primary" type="submit">Retrain {{ titles.get(s.task, s.task)|lower }}</button></form>{% endif %}
</div>
{% if not s %}
<div class="card">
  <div class="empty">
    <h4>Nothing is being learned yet</h4>
    <p>Training takes about five seconds. The model studies every classification the AI has already made on your mail, learns to imitate its “needs a reply” call, and then waits. You decide what it may do, step by step.</p>
    <form method="post" action="{{ url_for('learning_train') }}" style="margin-top:12px"><button class="btn primary" type="submit">Train the first model</button></form>
  </div>
</div>
{% else %}
{% macro spec_actions(sp) %}{% if sp.status == 'validated' %}<form method="post" action="{{ url_for('learning_transition', sid=sp.id) }}"><input type="hidden" name="to" value="shadow"><button class="btn small" type="submit">Start watching new mail</button></form>
    <span class="sub">Changes nothing — it only records what it would decide.</span>
    {% elif sp.status == 'shadow' %}<div class="sub">{% if rep.route_mode != 'enforce' %}<b>Live routing is off.</b> Promotion changes this model's state; it cannot bypass AI calls until live routing is configured.{% elif not rep.enabled %}<b>Learning is disabled.</b> Enable learning before this model can take over.{% else %}Live routing is on. Review disagreements before promoting.{% endif %}</div><a class="btn small" href="#learning-disagreements" data-open-disagreements>Review disagreements</a><form method="post" action="{{ url_for('learning_transition', sid=sp.id) }}"><input type="hidden" name="to" value="active"><button class="btn small" type="submit">Promote model</button></form>
    <form method="post" action="{{ url_for('learning_transition', sid=sp.id) }}"><input type="hidden" name="to" value="retired"><button class="btn small" type="submit">Retire</button></form>
    {% elif sp.status == 'active' %}<form method="post" action="{{ url_for('learning_transition', sid=sp.id) }}"><input type="hidden" name="to" value="retired"><button class="btn small" type="submit">Retire</button></form>
    <span class="sub">Retiring keeps every version and all history.</span>
    {% endif %}{% endmacro %}
{% macro spec_stats(sp, spm, sp_live) %}{% if sp.task == 'category' %}<div class="dsrow"><span class="dsk">Sorts the newest emails like the AI</span><span class="dsv">{{ '%.0f' % (spm.accuracy * 100) if spm.accuracy else '—' }}%</span></div>
  <div class="dsrow"><span class="dsk">Categories it chooses from</span><span class="dsv">{{ spm.classes|length if spm.classes else '—' }}</span></div>
  {% else %}<div class="dsrow"><span class="dsk">Catches the AI's “needs a reply” flags</span><span class="dsv">{{ '%.0f' % (spm.recall * 100) if spm.recall else '—' }}%</span></div>
  <div class="dsrow"><span class="dsk">Right when it raises a flag</span><span class="dsv">{{ '%.0f' % (spm.precision * 100) if spm.precision else '—' }}%</span></div>
  {% endif %}<div class="dsrow"><span class="dsk">Agrees with the AI on new mail</span><span class="dsv">{% if sp_live.n %}{{ sp_live.agree }} of {{ sp_live.n }}{% else %}no checks yet{% endif %}</span></div>{% endmacro %}
<div class="card">
  <div class="card-h"><h3>Working on your mail</h3><span class="sub">{{ rep.counts.classifiers_live }} fast-path{{ 's' if rep.counts.classifiers_live != 1 else '' }} deciding · {{ rep.counts.learners_running }} learner{{ 's' if rep.counts.learners_running != 1 else '' }} watching — nothing acts without you</span></div>
  <div class="sub">Fast-paths answer before the AI; newer learners watch until promoted. Training data may include both AI labels and your corrections. <a href="{{ url_for('classifiers') }}">Manage all classifiers →</a></div>
</div>
<div class="mgrid">
{% for c in rep.classifiers %}
<div class="card">
  <div class="card-h"><h3>{{ c.name }}</h3><span class="badge {{ 'ok' if c.status == 'live' else '' }}">{{ 'deciding live' if c.status == 'live' else 'paused' }}</span></div>
  <div class="sub" style="margin-bottom:8px">Sorts “{{ c.job }}” mail before the AI sees it.</div>
  <div class="dsrow"><span class="dsk">Training source</span><span class="dsv">{{ 'AI labels + corrections' if c.weak else 'Your labels' }}</span></div>
  <div class="dsrow"><span class="dsk">Held-out label agreement</span><span class="dsv">{{ '%.0f' % (c.accuracy * 100) if c.accuracy is not none else '—' }}%</span></div>
  <div class="dsrow"><span class="dsk">Learned from</span><span class="dsv">{{ "{:,}".format(c.samples) if c.samples else '—' }} examples</span></div>
  <div class="row" style="margin-top:12px;align-items:center;gap:8px">
    <form class="px-swf" method="post" action="{{ url_for('classifier_toggle', hid=c.id) }}">
      <label class="px-sw" title="{{ 'Disable' if c.status == 'live' else 'Enable' }} {{ c.name }}"><input type="checkbox" {{ 'checked' if c.status == 'live' }} onchange="this.form.requestSubmit()" aria-label="{{ 'Disable' if c.status == 'live' else 'Enable' }} {{ c.name }}"><span class="px-tr"></span></label>
    </form>
    <form method="post" action="{{ url_for('classifier_retrain', hid=c.id) }}"><button class="btn small" type="submit">Retrain</button></form>
    <a class="btn small" href="{{ url_for('classifier_dataset', hid=c.id) }}">Review dataset</a>
  </div>
</div>
{% endfor %}
<div class="card">
  <div class="card-h"><h3>{{ titles.get(s.task, s.task) }}</h3><span class="badge {{ {'validated':'acc','shadow':'warn','active':'ok','degraded':'warn','rejected':'err'}.get(s.status, '') }}">{{ {'validated': 'ready to watch', 'shadow': 'watching quietly', 'active': 'taking over', 'degraded': 'needs attention', 'retired': 'retired'}.get(s.status, s.status) }}</span></div>
  <div class="sub" style="margin-bottom:8px">{{ 'Predicts the category.' if s.task == 'category' else 'Predicts whether you need to reply.' }} Correct categories and reply flags directly on any message.</div>
  <div class="dsrow"><span class="dsk">Training source</span><span class="dsv">{% if ds.by_source %}{% for source,n in ds.by_source.items() %}{{ 'AI labels' if source == 'llm_annotation' else 'Human / other labels' }}: {{ n }}{{ ' · ' if not loop.last }}{% endfor %}{% else %}AI labels; corrections used when available{% endif %}</span></div>
  {{ spec_stats(s, m, live) }}
  <div class="sub" style="margin-top:8px">These compare it to the AI's answers on your newest 20% of mail — a ceiling, not the truth: the AI is not always right. Corrections from you weigh several times more than the AI's own labels when retraining.</div>
  <div class="row" style="margin-top:12px;align-items:center;gap:8px">{{ spec_actions(s) }}</div>
</div>
{% for sp in rep.specialists if s and sp.id != s.id and sp.task != s.task and sp.status in ('shadow', 'active', 'degraded') %}
{% set spm = sp.metrics_parsed.val or {} %}
<div class="card">
  <div class="card-h"><h3>{{ titles.get(sp.task, sp.task) }}</h3><span class="badge warn">watching quietly</span></div>
  <div class="sub" style="margin-bottom:8px">v{{ sp.version }}{% if sp.live.n %} · agrees with the AI on {{ sp.live.agree }} of {{ sp.live.n }}{% endif %}</div>
  {{ spec_stats(sp, spm, sp.live) }}
  <div class="row" style="margin-top:12px;align-items:center;gap:8px">{{ spec_actions(sp) }}</div>
</div>
{% endfor %}
</div>
<div class="card">
  <div class="card-h"><h3>Test sets</h3><span class="sub">hand-labeled by you - frozen - kept out of training</span></div>
  {% if rep.eval.any %}
    <div style="margin-bottom:10px"><span class="eprog"><i style="width:{{ (100 * rep.eval.progress.done / rep.eval.progress.total)|round|int }}%"></i></span><span class="sub">{{ rep.eval.progress.done }} of {{ rep.eval.progress.total }} messages labeled</span></div>
    {% for t, g in rep.eval.tasks.items() %}
      {% if g.n %}
      <div class="dsrow" style="margin-top:6px"><span class="dsk">{{ g.title }}</span><span class="dsv">{{ g.labeled }} of {{ g.n }}{% if g.skipped %} - {{ g.skipped }} not sure{% endif %}</span></div>
      {% if g.model_text %}<div class="sub" style="margin:2px 0 6px">{{ g.model_text }}</div>{% endif %}
      {% endif %}
    {% endfor %}
    {% if rep.eval.progress.done < rep.eval.progress.total %}
    <div class="row" style="margin-top:12px"><a class="btn small" href="{{ url_for('learning_eval') }}">Label now ({{ rep.eval.progress.total - rep.eval.progress.done }} left)</a></div>
    {% endif %}
    {% if rep.eval.stale_models %}
    <div class="sub" style="margin-top:10px">Models were trained before this set existed. <b>Retrain</b> them so your test messages never leak into training.</div>
    {% endif %}
  {% else %}
    <div class="sub" style="margin-bottom:10px">Scores against AI labels measure imitation - the AI taught the models. This samples ~50 real emails for <b>you</b> to answer by hand (about 10 minutes, once). From then on every model is scored against your answers: the only truth-based score here. The set is frozen, and the messages are kept out of all training.</div>
    <form method="post" action="{{ url_for('learning_eval_sample') }}"><button class="btn small" type="submit">Build the test set</button></form>
  {% endif %}
</div>
<details class="card"><summary style="cursor:pointer;font-weight:600">Training and observation history</summary>
  <div class="card-h"><h3>The newest learner</h3><span class="badge {{ {'validated':'acc','shadow':'warn','active':'ok','degraded':'warn','rejected':'err'}.get(s.status, '') }}">{{ stat_word }}</span></div>
  <div class="lead">{% if s.status == 'shadow' %}Watching quietly — it sees every classified email, records what it would decide, and changes nothing.
    {% elif s.status == 'active' %}Taking over confident calls — everything it is unsure about still goes to the AI.
    {% elif s.status == 'validated' %}Trained and scored — ready to start watching. It still changes nothing until it watches for a while and you promote it.
    {% elif s.status == 'degraded' %}Quality dropped below its bar — it is back to watching until retrained.
    {% elif s.status == 'retired' %}Retired — kept for the record. Retrain to bring it back.
    {% else %}Prepared but not running.{% endif %}</div>
  <div class="sub" style="margin:2px 0 4px">{{ titles.get(s.task, s.task) }} · version {{ s.version }}{% if live.last_at %} · last check {{ fmt_ts(live.last_at) }}{% elif s.created %} · trained {{ fmt_ts(s.created) }}{% endif %}</div>
  <div class="lsteps">
    <div class="lstep done"><span class="dot ok"></span><div><b>1 · Learned from your past mail</b>
      <div class="sub">{{ "{:,}".format(ds.n or 0) }} classifications to study — every label was written by the AI itself, not by you.</div></div></div>
    <div class="lstep done"><span class="dot ok"></span><div><b>2 · Scored against history</b>
      <div class="sub">{% if s.task == 'category' %}Checked on the newest {{ m.n or 0 }} emails: it sorts {{ '%.0f' % (m.accuracy * 100) if m.accuracy else '—' }}% the same way the AI does.{% else %}Checked on the newest {{ m.n or 0 }} emails: it catches {{ '%.0f' % (m.recall * 100) if m.recall else '—' }}% of the AI's “needs a reply” flags, and is right {{ '%.0f' % (m.precision * 100) if m.precision else '—' }}% of the times it raises one.{% endif %}</div></div></div>
    {% if s.status in ('shadow', 'active') %}
    <div class="lstep now"><span class="dot acc"></span><div><b>3 · Watching new mail</b> <span class="badge acc">now</span>
      <div class="sub">{% if live.n %}Checked {{ live.n }} so far, agrees with the AI on {{ live.agree }} of them ({{ '%.0f' % (live.agreement * 100) }}%). Every check is recorded below.{% else %}Running — the first check appears with the next classified email.{% endif %}</div></div></div>
    {% else %}
    <div class="lstep todo"><span class="dot"></span><div><b>3 · Watch new mail</b>
      <div class="sub">Not started — press “Start watching new mail” below.</div></div></div>
    {% endif %}
    <div class="lstep {{ 'now' if s.status == 'active' else 'todo' }}"><span class="dot {{ 'acc' if s.status == 'active' else '' }}"></span><div><b>4 · Take over confident calls</b>{% if s.status == 'active' %} <span class="badge ok">on</span>{% endif %}
      <div class="sub">{{ 'Emails it is confident about stop going to the AI. The rest still escalate.' if s.status == 'active' else 'The end goal: emails it is confident about stop going to the AI. Needs more watching time and your go-ahead.' }}</div></div></div>
  </div>
</details>
<div class="card">
  <div class="card-h"><h3>What can be trained next</h3><span class="sub">candidates found in your data — nothing trains without you</span></div>
  {% for p in rep.proposals %}
  <div class="dsrow">
    <span class="dsk"><b>{{ p.title }}</b><div class="sub">{{ p.evidence }} · {{ p.why }}</div></span>
    <span class="dsv">
      {% if p.status == 'ready' and p.trainable %}<form class="inline" method="post" action="{{ url_for('learning_train') }}"><input type="hidden" name="task" value="{{ p.task }}"><button class="btn small primary" type="submit">Train</button></form>
      {% elif p.status == 'ready' %}<span class="badge">low gain</span>
      {% elif p.status == 'watching' %}<span class="badge warn">watching</span>
      {% else %}<span class="badge">blocked</span>{% endif %}
    </span>
  </div>
  {% endfor %}
</div>
<div class="card">
  <details class="lfold" id="learning-disagreements">
    <summary>Where it disagrees with the AI{% if rep.disagreements %} · {{ rep.disagreements|length }} recent{% endif %}</summary>
    <div class="lfold-i">
      <div class="sub" style="margin-bottom:8px">Shadow checks — the AI's decision still ran; these are the moments the model would have said something different. Open one to judge for yourself.</div>
      {% if rep.disagreements %}
      <div class="tablewrap"><table class="tbl ltbl">
        <tr><th>Email</th><th>This model said</th><th>The AI said</th><th class="hide-m">When</th></tr>
        {% for d in rep.disagreements %}
        <tr>
          <td><a href="{{ url_for('message_detail', mid=d.msg_id) }}">{{ d.subject or ('#' ~ d.msg_id) }}</a><div class="sub">{{ titles.get(d.task, d.task) }} · {{ d.from_addr }}</div></td>
          <td><span class="badge warn">{{ d.specialist if d.task == 'category' else ('needs reply' if d.specialist else 'no reply') }}</span> <span class="sub">{{ '%.2f' % d.specialist_conf }}</span></td>
          <td><span class="badge">{{ d.system if d.task == 'category' else ('needs reply' if d.system else 'no reply') }}</span> <span class="sub">{{ d.system_source }}</span></td>
          <td class="sub hide-m">{{ fmt_ts(d.ts) }}</td>
        </tr>
        {% endfor %}
      </table></div>
      {% else %}<div class="sub">Nothing yet — disagreements appear once it has watched some new mail.</div>{% endif %}
    </div>
  </details>
  <details class="lfold">
    <summary>Raw numbers</summary>
    <div class="lfold-i">
      <div class="dsrow"><span class="dsk">Emails still sent to the AI</span><span class="dsv">{{ '%.0f' % (rep.routing.escalation_rate * 100) if rep.routing.escalation_rate is not none else '—' }}%<span class="dsp"> of last {{ rep.routing.n }}</span></span></div>
      <div class="dsrow"><span class="dsk">Could have skipped the AI</span><span class="dsv">{{ '%.0f' % (rep.routing.would_skip_rate * 100) if rep.routing.would_skip_rate is not none else '—' }}%</span></div>
      <div class="dsrow"><span class="dsk">Sent for a second look</span><span class="dsv">{{ rep.routing.counts['verify'] }}</span></div>
      <div class="dsrow"><span class="dsk">Decisions recorded</span><span class="dsv">{{ "{:,}".format(rep.library.decisions) }}</span></div>
      <div class="dsrow"><span class="dsk">Your actions recorded (tags, moves, undos)</span><span class="dsv">{{ "{:,}".format(rep.library.observations) }}</span></div>
      <div class="dsrow"><span class="dsk">Confirmed labels (from your corrections)</span><span class="dsv">{{ "{:,}".format(rep.library.labels) }}</span></div>
      <div class="sub" style="margin-top:8px">“Emails still sent to the AI” is the number that should fall as models take over. Labels written by you are worth several AI labels each{% if rep.library.labels_by_source %} — so far: {% for src, n in rep.library.labels_by_source.items() %}{{ src }} {{ n }}{{ ' · ' if not loop.last }}{% endfor %}{% endif %}.</div>
    </div>
  </details>
  <details class="lfold">
    <summary>All models &amp; versions · {{ rep.specialists|length }}</summary>
    <div class="lfold-i">
      <div class="tablewrap"><table class="tbl ltbl">
        <tr><th>Model</th><th>Status</th><th>Catches / right</th><th class="hide-m">Agrees on new mail</th><th></th></tr>
        {% for sp in rep.specialists %}
        {% set spm = sp.metrics_parsed.val or {} %}
        <tr>
          <td><b>{{ sp.name }}</b> <span class="sub">v{{ sp.version }} · {{ sp.kind }} · #{{ sp.id }}</span><div class="sub">{{ sp.task }}{% set n = (sp.stats_parsed.dataset or {}).get('n') %}{% if n %} · {{ n }} samples{% endif %}</div></td>
          <td><span class="badge {{ {'validated':'acc','shadow':'warn','active':'ok','degraded':'warn','rejected':'err'}.get(sp.status, '') }}">{{ sp.status }}</span></td>
          <td class="sub">{{ '%.0f' % (spm.recall * 100) if spm.recall else '—' }}% / {{ '%.0f' % (spm.precision * 100) if spm.precision else '—' }}%</td>
          <td class="sub hide-m">{% if sp.live.n %}{{ sp.live.agree }} of {{ sp.live.n }}{% else %}—{% endif %}</td>
          <td class="r">{% if sp.status == 'validated' and (not s or sp.id != s.id) %}<form class="inline" method="post" action="{{ url_for('learning_transition', sid=sp.id) }}"><input type="hidden" name="to" value="shadow"><button class="btn small" type="submit">Start watching</button></form>{% endif %}</td>
        </tr>
        {% endfor %}
      </table></div>
    </div>
  </details>
  <details class="lfold">
    <summary>How this works</summary>
    <div class="lfold-i">
      <div class="sub" style="line-height:1.6">The goal is simple: <b>use the AI for novel mail, use small models for the repetitive parts.</b> Two families do that here — <b>fast-paths</b> (taught by your labels; they decide before the AI when sure) and <b>learners</b> (taught by the AI's answers; they watch, then take over when you promote them). The loop in plain words: the system watches what the AI decides → when a pattern repeats, a small model is trained to imitate that one decision → it is scored against history → it watches real mail without acting → if it holds up, you can let it take over → anything it is unsure about still goes to the AI. “Emails still sent to the AI” (in Raw numbers) is the number that should fall as this works.</div>
      <ul class="simul" style="margin-top:6px">
        <li>“Watching quietly” really means quiet — shadow decisions are recorded, never acted on, and marked as such everywhere.</li>
        <li>A model's own predictions never become training data — only the AI's answers and <b>your</b> corrections teach it.</li>
        <li>Every version is kept forever; promoting and retiring are reversible; deleting is not a thing here.</li>
        <li>Small print: “the AI said” is not ground truth either — until you correct enough emails, all scores measure imitation, not truth.</li>
      </ul>
    </div>
  </details>
</div>
{% endif %}
"""


@app.route("/learning")
def learning_page():
    rep = learning.status_report()
    return render(_render_src(LEARN_TMPL, rep=rep, fmt_ts=fmt_ts))


@app.route("/learning/train", methods=["POST"])
def learning_train():
    task = (request.form.get("task") or "needs_reply").strip()
    try:
        res = learning.train_specialist(task, created_by="ui")
        v = res["val"]
        score = v.get("f1") if v.get("f1") is not None else v.get("accuracy")
        flash("Trained %s v%d - validation score %s on %d held-out sample(s) of %d. "
              "Deploy to shadow when ready." % (res["name"], res["version"], score,
                                                v.get("n") or 0, res["dataset"]["n"]), "ok")
    except Exception as exc:
        flash("Training failed: %s" % exc, "err")
    return redirect(url_for("learning_page"))


@app.route("/learning/specialists/<int:sid>/transition", methods=["POST"])
def learning_transition(sid):
    to = (request.form.get("to") or "").strip()
    try:
        learning.transition(sid, to, reason="by ui", by="ui")
        flash("Specialist #%d is now %s." % (sid, to.upper()), "ok")
    except Exception as exc:
        flash("Transition failed: %s" % exc, "err")
    return redirect(url_for("learning_page"))


@app.route("/learning/eval")
def learning_eval():
    ctx = learning.next_eval_context()
    prog = learning.eval_progress()
    cats = list(store.get_setting("categories") or []) or learning.known_categories()
    msg = ctx["msg"] if ctx else None
    need = ctx["need"] if ctx else []
    return render(_render_src(EVAL_TMPL, msg=msg, need=need, cats=cats,
                              done=prog["done"], total=prog["total"],
                              skipped=prog["skipped"], fmt_ts=fmt_ts))


@app.route("/learning/eval/sample", methods=["POST"])
def learning_eval_sample():
    try:
        res = learning.sample_eval_set()
        flash("Test set built: %d messages picked (%d questions). Label them from the Learning page."
              % (res["messages"], res["items"]), "ok")
    except Exception as exc:
        flash("Could not build the test set: %s" % exc, "err")
    return redirect(url_for("learning_page"))


@app.route("/learning/eval/label", methods=["POST"])
def learning_eval_label():
    msg_id = int(request.form.get("msg") or 0)
    task = (request.form.get("task") or "").strip()
    label = (request.form.get("label") or "").strip()
    try:
        learning.label_eval(msg_id, task, label)
    except Exception as exc:
        flash("Could not save that answer: %s" % exc, "err")
    return redirect(url_for("learning_eval"))


@app.route("/fonts/<name>")
def fonts(name):
    if name not in ("geist.woff2", "geist-mono.woff2"):
        return Response("not found", status=404)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", name)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return Response("not found", status=404)
    return Response(data, mimetype="font/woff2",
                    headers={"Cache-Control": "public, max-age=2592000"})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "worker": worker.state.get("last_ok"), "error": worker.state.get("last_error")})


@app.route("/llm/health.json")
def llm_health_json():
    """Cached result of the background LLM reachability poll - the dashboard
    polls this to keep the LLM dot current without a page reload."""
    st = dict(llm_health.state)
    st["checked_r"] = rel_time(st.get("checked_at"))
    return jsonify(st)


def _editor_preview(kind, editor_id=0):
    return _render_src(ux.EDITOR_PREVIEW_TMPL, kind=kind, editor_id=editor_id,
                       recent=store.messages(limit=20))


app.jinja_env.globals['editor_preview'] = _editor_preview


@app.route('/messages/<int:mid>/preview.json')
def message_preview(mid):
    row = store.get_message(mid)
    if not row:
        return jsonify(error='No such message.'), 404
    return jsonify(from_addr=row.get('from_addr') or '', to_addr=row.get('to_addr') or '',
                   subject=row.get('subject') or '', body=(row.get('snippet') or '')[:8000])


# ---------------------------------------------------------------- automation workspace

AUTOMATION_OVERVIEW_TMPL = """
<style>
.status-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 28px}
.status-item{padding:10px 0;border-top:1px solid var(--line)}
.status-item .st-row{display:flex;align-items:center;gap:8px}
.status-item .st-row b{font-size:.9rem}
.status-item .st-sub{font-size:.78rem;color:var(--dim);margin-top:3px}
.steps{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:16px;counter-reset:step}
.steps li{position:relative;padding-left:34px;font-size:.86rem;color:var(--dim);line-height:1.6}
.steps li b{display:block;color:var(--fg);font-size:.9rem;margin-bottom:2px}
.steps li::before{counter-increment:step;content:counter(step);position:absolute;left:0;top:0;width:22px;height:22px;
 border:1px solid var(--line);background:#fff;display:flex;align-items:center;justify-content:center;
 font-size:.72rem;font-weight:600;color:var(--fg);font-variant-numeric:tabular-nums}
.know{margin:14px 0 0;background:var(--card);border:1px solid var(--line)}
.know summary{cursor:pointer;padding:12px 18px;font-size:.9rem;font-weight:600;display:flex;align-items:center;gap:8px;list-style:none}
.know summary::-webkit-details-marker{display:none}
.know summary::after{content:'+';margin-left:auto;color:var(--dim);font-size:1rem}
.know[open] summary::after{content:'−'}
.know ul{margin:0;padding:2px 18px 14px 36px;font-size:.82rem;color:var(--dim);line-height:1.7}
@media(max-width:767px){.status-grid,.steps{grid-template-columns:1fr}.know ul{padding-left:32px}}
</style>
<div class="page-head">
  <div>
    <h1 class="page-title">Automation</h1>
    <div class="page-desc">What happens to your mail, why, and where to change it.</div>
  </div>
  <div class="row"><a class="btn primary" href="{{ url_for('simulate') }}">Test on a message</a></div>
</div>
<div class="card">
  <div class="card-h"><h3>Status</h3><span class="sub">enabled entities and live modes — change any switch on Controls</span></div>
  <div class="status-grid">
    <div class="status-item">
      <div class="st-row"><b>Rules</b>{% if s.rules_apply %}<span class="badge ok">On</span>{% else %}<span class="badge warn">Preview only</span>{% endif %}</div>
      <div class="st-sub">{{ rules_on }} of {{ rules_total }} rules enabled · <a href="{{ url_for('automation_controls') }}#ctl-rules">Change</a></div>
    </div>
    <div class="status-item">
      <div class="st-row"><b>Flows</b>{% if s.flows_apply %}<span class="badge ok">On</span>{% else %}<span class="badge warn">Dry-run</span>{% endif %}</div>
      <div class="st-sub">{{ flows_on }} of {{ flows_total }} flows enabled · <a href="{{ url_for('automation_controls') }}#ctl-flows">Change</a></div>
    </div>
    <div class="status-item">
      <div class="st-row"><b>Classification</b>{% if s.llm_suggest %}<span class="badge ok">On</span>{% else %}<span class="badge">Off</span>{% endif %}</div>
      <div class="st-sub">{{ heur_on }} of {{ heur_total }} classifiers enabled · <a href="{{ url_for('automation_controls') }}#ctl-classify">Change</a> · <a href="{{ url_for('learning_page') }}">Review classifiers</a></div>
    </div>
    <div class="status-item">
      <div class="st-row"><b>Default filing</b>{% if s.llm_apply %}<span class="badge ok">On</span>{% else %}<span class="badge">Off</span>{% endif %}</div>
      <div class="st-sub">Moves classified mail to its category destination · <a href="{{ url_for('automation_categories') }}#filing-switch">Change</a></div>
    </div>
  </div>
</div>
<div class="card">
  <div class="card-h"><h3>How mail is handled</h3><span class="sub">two stages</span></div>
  <ol class="steps">
    <li><b>Mailbox scan</b>Rules run first. Remaining eligible mail may match non-category flows (text or topic). Matching guard rules protect mail; topic filters use embeddings.</li>
    <li><b>After classification</b>A user correction, a fast-path classifier or the LLM supplies a verdict; eligible category-bearing flows are evaluated in order. Default filing is the fallback when it is enabled and no flow has been recorded as handling the message. Kept, already-filed and guarded mail is protected by the existing checks.</li>
  </ol>
</div>
<details class="know">
  <summary>Mode interactions — what Preview and Off change</summary>
  <ul>
    <li>A preview (dry-run) category flow can still suppress default filing.</li>
    <li>A flow action failure may leave fallback filing eligible.</li>
    <li>Default filing being off does not prevent a live flow from moving mail.</li>
  </ul>
</details>
"""


AUTOMATION_CATEGORIES_TMPL = """
<style>
.cat-row{padding:12px 0;border-top:1px solid var(--line)}
.cat-row:first-of-type{border-top:0}
.cat-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.cat-num{font-size:.72rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--dim)}
.cat-map{display:grid;grid-template-columns:minmax(160px,1.1fr) auto minmax(150px,1fr) auto;gap:10px 12px;align-items:start}
.cat-map>div{min-width:0}
.cat-arrow{display:flex;align-items:center;justify-content:center;height:34px;color:var(--dim);font-size:1rem}
.cat-row .cat-actions{display:flex;flex-direction:row;align-items:center;gap:8px;flex-wrap:wrap}
.cat-row .cat-actions .check{margin:0}
.cat-row.locked,.cat-row.removed{background:var(--card2)}
.cat-row.removed .cat-map{opacity:.5}
.cat-add{margin-top:14px}
.cat-add .btn{width:100%;border-style:dashed;color:var(--dim)}
.cat-add .btn:hover{color:var(--fg)}
@media(max-width:767px){.cat-map{grid-template-columns:1fr}.cat-arrow{height:auto;transform:rotate(90deg)}}
</style>
<div class="page-head">
  <div>
    <h1 class="page-title">Categories &amp; filing</h1>
    <div class="page-desc">The classification vocabulary and where each category is filed. Blank destination keeps the message in its current folder.</div>
  </div>
</div>
{% if error %}
<div class="msg err" id="form-err" tabindex="-1" role="alert">{{ error }}</div>
{% endif %}
{% if conflict %}
<div class="msg err" id="form-err" tabindex="-1" role="alert">Nothing was saved — the categories changed since this form was loaded. <a href="{{ url_for('automation_categories') }}">Reload current categories</a> before editing.</div>
{% endif %}
<form method="post" id="cat-form" action="{{ url_for('automation_categories') }}">
  <input type="hidden" name="settings_version" value="{{ version }}">
  <input type="hidden" name="row_count" value="{{ rows|length }}">
  <div class="card">
    <div class="card-h"><h3>Classification vocabulary &amp; destinations</h3><span class="sub">names are read-only; add or remove whole categories</span></div>
    <p class="sub" style="margin-top:0">Removing a category never rewrites historical labels, trained classifiers or plugin settings. Categories already used by a flow or fast-path classifier must be edited there first. Suggestion maps still apply when automatic default filing is off.</p>
    {% for r in rows %}
    {% set i = loop.index0 %}
    <div class="cat-row{{ ' locked' if r.duplicate or r.readonly else '' }}{{ ' removed' if r.remove else '' }}" data-row-index="{{ i }}">
      <input type="hidden" name="original_{{ i }}" value="{{ r.original }}">
      <input type="hidden" name="configured_{{ i }}" value="{{ '1' if r.configured else '0' }}">
      <input type="hidden" name="mapping_present_{{ i }}" value="{{ '1' if r.mapping_present else '0' }}">
      <div class="cat-head">
        <span class="cat-num">{% if r.existing %}Mapping #{{ i + 1 }}{% else %}New mapping{% endif %}</span>
        {% if r.flow_refs or r.classifier_refs %}
        <span class="cat-refs">
          {% for f in r.flow_refs %}<a class="chip" href="{{ url_for('flow_edit', flow_id=f.id) }}">flow #{{ f.id }} · {{ f.name }}{{ ' · disabled' if not f.enabled else '' }}</a>{% endfor %}
          {% for c in r.classifier_refs %}<a class="chip" href="{{ url_for('classifier_dataset', hid=c.id) }}">classifier #{{ c.id }} · {{ c.name }}{{ ' · disabled' if not c.enabled else '' }}</a>{% endfor %}
        </span>
        {% endif %}
      </div>
      <div class="cat-map">
        <div>
          <input type="text" name="name_{{ i }}" value="{{ r.name }}" placeholder="New category" aria-label="Category name"{{ ' readonly'|safe if (r.existing or r.readonly or conflict) else '' }}>
          {% if r.legacy %}<span class="sub">Legacy mapping · not offered for classification</span>{% endif %}
          {% if r.duplicate %}<span class="sub">Exact duplicate — cleanup needs a separate repair; this row is read-only.</span>{% endif %}
        </div>
        <span class="cat-arrow" aria-hidden="true">&#8594;</span>
        <div>
          <input type="text" name="folder_{{ i }}" value="{{ r.folder }}" placeholder="Keep in current folder" aria-label="Destination folder"{{ ' readonly'|safe if (r.duplicate or r.readonly or conflict) else '' }}>
        </div>
        <div class="cat-actions">
          {% if r.configured and r.existing and not r.duplicate and not conflict %}<a class="btn small" href="{{ url_for('flow_new', category=r.name) }}">Create flow</a>{% endif %}
          {% if r.legacy %}<label class="check"><input type="checkbox" name="restore_{{ i }}" value="1"{{ ' checked' if r.restore else '' }}{{ ' disabled'|safe if (r.readonly or conflict) else '' }}> <span>Restore to vocabulary</span></label>{% endif %}
          {% if r.existing and not r.duplicate and not conflict %}
          <input type="hidden" name="remove_{{ i }}" value="{{ '1' if r.remove else '0' }}" data-remove-input>
          <button type="button" class="btn small" data-row-remove aria-label="{{ 'Undo removal of' if r.remove else 'Remove' }} mapping {{ r.name }}">{{ 'Undo' if r.remove else 'Remove' }}</button>
          {% endif %}
        </div>
      </div>
    </div>
    {% endfor %}
    <div class="cat-row" id="cat-blank-row" hidden>
      <input type="hidden" data-tpl="original" value="">
      <input type="hidden" data-tpl="configured" value="1">
      <input type="hidden" data-tpl="mapping_present" value="0">
      <div class="cat-head"><span class="cat-num">New mapping</span></div>
      <div class="cat-map">
        <div><input type="text" data-tpl="name" value="" placeholder="New category" aria-label="Category name"></div>
        <span class="cat-arrow" aria-hidden="true">&#8594;</span>
        <div><input type="text" data-tpl="folder" value="" placeholder="Keep in current folder" aria-label="Destination folder"></div>
      </div>
    </div>
    {% if not conflict %}
    <div class="cat-add"><button type="button" class="btn" data-add-category-row>+ Add mapping</button></div>
    <div class="savebar"><button class="btn primary" type="submit">Save categories</button><span class="sub">All changes save together.</span></div>
    {% endif %}
  </div>
</form>
<div class="card" id="filing-switch">
  <div class="card-h"><h3>Automatic default filing</h3>{% if s.llm_apply %}<span class="badge ok">On</span>{% else %}<span class="badge">Off</span>{% endif %}</div>
  <p class="sub" style="margin-top:0">When on, a classified message with no category flow recorded as handling it is moved to its category destination. Kept, guarded and already-filed mail is protected by the existing checks. Off = suggestions only.</p>
  <form method="post" action="{{ url_for('settings') }}" id="filing-form" data-stored="{{ '1' if s.llm_apply else '0' }}">
    <input type="hidden" name="section" value="behavior">
    <input type="hidden" name="scope" value="Filing &amp; drafts">
    <input type="hidden" name="next" value="{{ url_for('automation_categories') }}">
    <div class="setrow"><div class="st-l"><b>Enable automatic default filing</b><span class="sub">Rules and flows still apply first. A blank destination means no fallback move — not a guard.</span></div>
      <div class="st-c"><label class="px-sw" title="Enable automatic default filing"><input type="checkbox" name="llm_apply" value="1" {{ 'checked' if s.llm_apply else '' }} aria-label="Enable automatic default filing"><span class="px-tr"></span></label><input type="hidden" name="llm_apply" value="0"></div></div>
    <div class="savebar"><button class="btn primary" type="submit">Save filing switch</button></div>
  </form>
</div>
<noscript><div class="msg warn">JavaScript is off. Use each form's Save button; the add-row button needs JavaScript, but existing rows can still be edited and saved.</div></noscript>
{% if error or conflict %}<script>(function(){var e=document.getElementById('form-err');if(e)e.focus();})();</script>{% endif %}
"""


AUTOMATION_CONTROLS_TMPL = """
<div class="page-head">
  <div>
    <h1 class="page-title">Controls</h1>
    <div class="page-desc">Explicit switches. Saving preferences does not process mail immediately; the worker reads them on its next check.</div>
  </div>
</div>
<noscript><div class="msg warn">JavaScript is off. Each switch is submitted with its own Save button.</div></noscript>
<div class="card" id="ctl-rules">
  <div class="card-h"><h3>Rules</h3><span class="sub">first-match sorting and guards</span></div>
  <form method="post" action="{{ url_for('settings') }}">
    <input type="hidden" name="section" value="behavior">
    <input type="hidden" name="scope" value="Rules live">
    <input type="hidden" name="next" value="{{ url_for('automation_controls') }}">
    <div class="setrow"><div class="st-l"><b>Apply rule actions for real</b><span class="sub">Off = preview only (suggest, never move) — not the same as a disabled rule.</span></div>
      <div class="st-c"><label class="px-sw" title="Apply rule actions for real"><input type="checkbox" name="rules_apply" value="1" {{ 'checked' if s.rules_apply else '' }} aria-label="Apply rule actions for real"><span class="px-tr"></span></label><input type="hidden" name="rules_apply" value="0"></div></div>
    <div class="savebar"><button class="btn primary" type="submit">Save rule mode</button><span class="sub">{{ 'Live' if s.rules_apply else 'Preview only' }}</span></div>
  </form>
  <div class="row" style="margin-top:8px"><a class="btn" href="{{ url_for('rules') }}">Edit rules</a></div>
</div>
<div class="card" id="ctl-flows">
  <div class="card-h"><h3>Flows</h3><span class="sub">multi-step automations</span></div>
  <form method="post" action="{{ url_for('settings') }}">
    <input type="hidden" name="section" value="behavior">
    <input type="hidden" name="scope" value="Flows live">
    <input type="hidden" name="next" value="{{ url_for('automation_controls') }}">
    <div class="setrow"><div class="st-l"><b>Apply flow actions for real</b><span class="sub">Off = dry-run. Independent of each flow's Enabled state; a matching dry-run can still suppress default filing.</span></div>
      <div class="st-c"><label class="px-sw" title="Apply flow actions for real"><input type="checkbox" name="flows_apply" value="1" {{ 'checked' if s.flows_apply else '' }} aria-label="Apply flow actions for real"><span class="px-tr"></span></label><input type="hidden" name="flows_apply" value="0"></div></div>
    <div class="savebar"><button class="btn primary" type="submit">Save flow mode</button><span class="sub">{{ 'Live' if s.flows_apply else 'Dry-run' }}</span></div>
  </form>
  <div class="row" style="margin-top:8px"><a class="btn" href="{{ url_for('flows') }}">Edit flows</a></div>
</div>
<div class="card" id="ctl-classify">
  <div class="card-h"><h3>Classification</h3><span class="sub">rules first, then classifiers, then the LLM</span></div>
  <form method="post" action="{{ url_for('settings') }}">
    <input type="hidden" name="section" value="behavior">
    <input type="hidden" name="scope" value="Classification">
    <input type="hidden" name="next" value="{{ url_for('automation_controls') }}">
    <div class="setrow"><div class="st-l"><b>Classify unmatched mail with the LLM</b><span class="sub">Anything no rule or classifier claimed gets a category, summary and confidence.</span></div>
      <div class="st-c"><label class="px-sw" title="Classify unmatched mail with the LLM"><input type="checkbox" name="llm_suggest" value="1" {{ 'checked' if s.llm_suggest else '' }} aria-label="Classify unmatched mail with the LLM"><span class="px-tr"></span></label><input type="hidden" name="llm_suggest" value="0"></div></div>
    <div class="setrow"><div class="st-l"><b>Max automatic calls per hour</b><span class="sub">Hourly cap for background classification.</span></div>
      <div class="st-c"><input type="number" name="max_llm_per_hour" value="{{ s.max_llm_per_hour }}" min="0" aria-label="Max LLM calls per hour"></div></div>
    <div class="setrow"><div class="st-l"><b>Classifications per check</b><span class="sub">How many queued messages each cycle picks up.</span></div>
      <div class="st-c"><input type="number" name="llm_batch_per_cycle" value="{{ s.llm_batch_per_cycle }}" min="1" aria-label="Batch size"></div></div>
    <div class="setrow"><div class="st-l"><b>Classify concurrency</b><span class="sub">Parallel requests for “Classify all” (1–16; 16 measured best on this GPU).</span></div>
      <div class="st-c"><input type="number" name="classify_concurrency" value="{{ s.classify_concurrency }}" min="1" max="16" aria-label="Concurrency"></div></div>
    <div class="savebar"><button class="btn primary" type="submit">Save classification</button></div>
  </form>
</div>
<div class="card" id="ctl-fastpath">
  <div class="card-h"><h3>Fast-path classifiers</h3><span class="row"><a class="sub" href="{{ url_for('classifiers') }}">Manage classifiers →</a><a class="sub" href="{{ url_for('learning_page') }}">Learning →</a></span></div>
  <form method="post" action="{{ url_for('settings') }}">
    <input type="hidden" name="section" value="behavior">
    <input type="hidden" name="scope" value="Classifiers">
    <input type="hidden" name="next" value="{{ url_for('automation_controls') }}">
    <div class="setrow"><div class="st-l"><b>Run trained classifiers before the LLM</b><span class="sub">Deterministic verdicts from your own labels — faster and immune to prompt injection.</span></div>
      <div class="st-c"><label class="px-sw" title="Run trained classifiers before the LLM"><input type="checkbox" name="heuristics_enabled" value="1" {{ 'checked' if s.heuristics_enabled else '' }} aria-label="Run trained classifiers before the LLM"><span class="px-tr"></span></label><input type="hidden" name="heuristics_enabled" value="0"></div></div>
    <div class="setrow"><div class="st-l"><b>Auto-retrain classifiers</b><span class="sub">Retrains tag-sourced classifiers as new labels arrive.</span></div>
      <div class="st-c"><label class="px-sw" title="Auto-retrain classifiers"><input type="checkbox" name="heuristic_autorefine" value="1" {{ 'checked' if s.heuristic_autorefine else '' }} aria-label="Auto-retrain classifiers"><span class="px-tr"></span></label><input type="hidden" name="heuristic_autorefine" value="0"></div></div>
    <div class="savebar"><button class="btn primary" type="submit">Save classifiers</button></div>
  </form>
</div>
<div class="card" id="ctl-filing">
  <div class="card-h"><h3>Default filing</h3><span class="sub">read-only here</span></div>
  <div class="kv"><div class="k">Automatic default filing</div><div>{% if s.llm_apply %}<span class="badge ok">On</span>{% else %}<span class="badge">Off</span>{% endif %}</div></div>
  <p class="sub" style="margin:0">Edit this switch (and the category destinations) on the Categories &amp; filing page so the confirmation sits with the consequence.</p>
  <div class="row" style="margin-top:8px"><a class="btn" href="{{ url_for('automation_categories') }}">Categories &amp; filing</a></div>
</div>
"""


def _category_display_rows(settings, flows, heuristics):
    rows = ux.category_rows(settings, flows, heuristics)
    counts = {}
    for r in rows:
        if r["configured"]:
            counts[r["name"]] = counts.get(r["name"], 0) + 1
    out = []
    for i, r in enumerate(rows):
        out.append({
            "original": str(i), "existing": True, "name": r["name"], "folder": r["folder"],
            "configured": r["configured"], "mapping_present": r["mapping_present"],
            "legacy": not r["configured"],
            "duplicate": r["configured"] and counts.get(r["name"], 0) > 1,
            "remove": False, "restore": False,
            "flow_refs": r["flow_refs"], "classifier_refs": r["classifier_refs"],
            "readonly": False,
        })
    for _ in range(1):
        out.append({"original": "", "existing": False, "name": "", "folder": "",
                    "configured": True, "mapping_present": False, "legacy": False,
                    "duplicate": False, "remove": False, "restore": False,
                    "flow_refs": [], "classifier_refs": [], "readonly": False})
    return out


def _posted_category_rows(form, settings, flows, heuristics):
    """Rebuild display rows from a posted form for 422/409 renders."""
    try:
        current = ux.category_rows(settings, flows, heuristics)
    except ValueError:
        current = []
    counts = {}
    for r in current:
        if r["configured"]:
            counts[r["name"]] = counts.get(r["name"], 0) + 1
    try:
        row_count = int((form.get("row_count") or "0").strip() or 0)
    except (TypeError, ValueError):
        row_count = 0
    rows = []
    for i in range(max(0, min(row_count, 500))):
        original = (form.get("original_%d" % i) or "").strip()
        server = None
        if original.isdigit() and 0 <= int(original) < len(current):
            server = current[int(original)]
        configured = (form.get("configured_%d" % i) or "0") == "1"
        mapping_present = (form.get("mapping_present_%d" % i) or "0") == "1"
        if server is not None:
            configured = server["configured"]
            mapping_present = server["mapping_present"]
        name = server["name"] if server is not None else (form.get("name_%d" % i) or "")
        rows.append({
            "original": original, "existing": server is not None, "name": name,
            "folder": form.get("folder_%d" % i) or "",
            "configured": configured, "mapping_present": mapping_present,
            "legacy": not configured,
            "duplicate": configured and counts.get(name, 0) > 1,
            "remove": (form.get("remove_%d" % i) or "") == "1",
            "restore": (form.get("restore_%d" % i) or "") == "1",
            "flow_refs": server["flow_refs"] if server else [],
            "classifier_refs": server["classifier_refs"] if server else [],
            "readonly": False,
        })
    return rows


@app.route("/automation")
def automation():
    s = store.all_settings()
    rules = store.list_rules()
    flows = store.list_flows()
    heur = store.list_heuristics()
    return render(_render_src(
        AUTOMATION_OVERVIEW_TMPL, s=s,
        rules_on=sum(1 for r in rules if r.get("enabled")), rules_total=len(rules),
        flows_on=sum(1 for f in flows if f.get("enabled")), flows_total=len(flows),
        heur_on=sum(1 for h in heur if h.get("enabled")), heur_total=len(heur)))


@app.route("/automation/categories", methods=["GET", "POST"])
def automation_categories():
    s = store.all_settings()
    flows = store.list_flows()
    heuristics = store.list_heuristics()
    if request.method == "POST":
        posted_version = (request.form.get("settings_version") or "").strip()
        current_version = store.settings_version(s)
        if posted_version != current_version:
            return render(_render_src(
                AUTOMATION_CATEGORIES_TMPL,
                rows=_posted_category_rows(request.form, s, flows, heuristics),
                version=current_version, error=None, conflict=True, s=s)), 409
        try:
            references = ux.automation_references(s, flows, heuristics)
        except ValueError as exc:
            return render(_render_src(
                AUTOMATION_CATEGORIES_TMPL, rows=[], version=current_version,
                error=str(exc), conflict=False, s=s)), 422
        try:
            categories, mapping = ux.parse_category_rows(request.form, s, references)
        except ValueError as exc:
            return render(_render_src(
                AUTOMATION_CATEGORIES_TMPL,
                rows=_posted_category_rows(request.form, s, flows, heuristics),
                version=current_version, error=str(exc), conflict=False, s=s)), 422
        if not store.save_category_settings(categories, mapping, posted_version):
            return render(_render_src(
                AUTOMATION_CATEGORIES_TMPL,
                rows=_posted_category_rows(request.form, s, flows, heuristics),
                version=store.settings_version(store.all_settings()), error=None, conflict=True, s=s)), 409
        flash("Categories and filing saved.", "ok")
        return redirect(url_for("automation_categories"), code=303)
    try:
        rows = _category_display_rows(s, flows, heuristics)
        error = None
    except ValueError as exc:
        rows, error = [], str(exc)
    return render(_render_src(
        AUTOMATION_CATEGORIES_TMPL, rows=rows, version=store.settings_version(s),
        error=error, conflict=False, s=s))


@app.route("/automation/controls")
def automation_controls():
    return render(_render_src(AUTOMATION_CONTROLS_TMPL, s=store.all_settings()))


@app.route('/automation/preview', methods=['POST'])
def automation_preview():
    kind = request.form.get('preview_kind')
    if kind not in ('rule', 'flow'):
        return jsonify(error='Choose a rule or flow editor.'), 400
    try:
        editor_id = int(request.form.get('preview_id') or 0)
    except ValueError:
        return jsonify(error='Invalid editor id.'), 400
    if editor_id and not (store.get_rule(editor_id) if kind == 'rule' else store.get_flow(editor_id)):
        return jsonify(error='This automation no longer exists.'), 404
    name, mode, conds, actions, enabled = _rule_from_form() if kind == 'rule' else _flow_from_form()
    if not conds:
        return jsonify(error='Add at least one condition before testing.'), 400
    if kind == 'flow' and not actions:
        return jsonify(error='Add at least one complete step before testing.'), 400
    for c in conds:
        if c.get('op') == 'regex':
            try:
                re.compile(c.get('value') or '')
            except re.error:
                return jsonify(error='Fix the invalid regular expression before testing.'), 400
    draft = {'id': editor_id or -1, 'name': name, 'match_mode': mode, 'enabled': 1,
             'conditions': json.dumps(conds), 'actions': json.dumps(actions)}
    rules = store.list_rules(enabled_only=True)
    flows = store.list_flows(enabled_only=True)
    # Replace in original position, including a disabled editor's position.
    rows = store.list_rules() if kind == 'rule' else store.list_flows()
    overlaid = []
    for row in rows:
        if row['id'] == editor_id:
            overlaid.append(draft)
        elif row.get('enabled'):
            overlaid.append(row)
    if not editor_id:
        overlaid.append(draft)
    if kind == 'rule':
        rules = overlaid
    else:
        flows = overlaid
    fields = {k: (request.form.get('preview_' + k) or '')[:8000 if k == 'body' else 300]
              for k in ('from', 'to', 'subject', 'body')}
    if not any(fields.values()):
        return jsonify(error='Choose a recent message or enter an example.'), 400
    try:
        result = engine.simulate_email(fields['from'], fields['subject'], fields['body'],
                                       to_addr=fields['to'], use_llm=request.form.get('preview_llm') == '1',
                                       rules=rules, flows=flows)
        condition_results = []
        ctx = {'text': fields['subject'] + '\n' + fields['body'], 'verdict': result.get('verdict') or {}}
        matched = engine.rule_matches(draft, fields) if kind == 'rule' else engine.flow_matches(draft, fields, ctx)
        for c in conds:
            if (c.get('kind') or 'field') == 'field':
                ok = engine.rule_matches({'conditions': json.dumps([c]), 'match_mode': 'all'}, fields)
                condition_results.append({'text': summarize_conditions({'conditions': json.dumps([c])}),
                                          'result': 'matches' if ok else 'does not match'})
            else:
                condition_results.append({'text': c.get('kind') + ': ' + c.get('value', ''),
                                          'result': 'evaluated in the pipeline' if result.get('use_llm') else 'enable classifier to evaluate'})
        outcome = (('Guard “%s” keeps this mail in place.' % result['guard']) if result.get('guard') else
                   ('Rule “%s” wins before flows.' % result['rule']['name']) if result.get('rule') else
                   ('Flow “%s” wins.' % result['flow']['name']) if result.get('flow') else
                   'No automation matches this example.')
        proposed = [summarize_actions(draft)] if kind == 'rule' else engine._sim_flow_steps(draft, store.all_settings())
        report = _render_src(ux.PREVIEW_REPORT_TMPL, result=result, outcome=outcome, name=name, proposed=proposed,
                             matched=matched, conditions=condition_results,
                             placement='the end of the list' if not editor_id else 'its existing list position')
        return jsonify(html=report, matched=matched, result=result)
    except Exception as exc:
        return jsonify(error='Preview could not run: %s' % exc), 422


if __name__ == "__main__":
    store.init_db()
    try:
        plugins.scan()  # discover built-in + user plugins (inert until enabled)
    except Exception as exc:  # a broken plugins dir must never block boot
        print("plugin scan failed at boot: %r" % exc, flush=True)
    if "--doctor" in sys.argv:
        rep = engine.doctor()
        hw = rep.get("hardware") or {}
        ll = rep.get("llm") or {}
        mb = rep.get("mailbox") or {}
        print("Mail Triage doctor")
        gpus = ", ".join("%s %sGB" % (g.get("name"), g.get("gb", "?"))
                         for g in (hw.get("gpus") or []))
        print("  hardware: %s%s" % (hw.get("tier_label"),
                                    (" - " + gpus) if gpus else ""))
        print("            %s" % hw.get("advice"))
        print("  mailbox:  %s" % (("%s @ %s:%s" % (mb.get("user"), mb.get("host"), mb.get("port")))
                                  if mb.get("configured") else
                                  "not configured yet - add it on the Accounts page"))
        if ll.get("configured"):
            if ll.get("reachable"):
                print("  llm:      OK in %sms - %s @ %s (replied %r)"
                      % (ll.get("latency_ms") or 0, ll.get("model"), ll.get("base_url"),
                         (ll.get("reply") or "")[:30]))
            else:
                print("  llm:      configured (%s @ %s) but UNREACHABLE: %s"
                      % (ll.get("model"), ll.get("base_url"), ll.get("error")))
        else:
            print("  llm:      not configured - rules and search work; classification and "
                  "drafting need an OpenAI-compatible endpoint")
        print("  next:     open the Get started checklist at /welcome (UI on port %s)"
              % config.UI_PORT)
        sys.exit(0)
    if "--check" in sys.argv:
        out = engine.connectivity_check()
        try:
            out["proxy"] = proxy.manager.status()
        except Exception as exc:
            out["proxy"] = {"error": repr(exc)}
        print(json.dumps(out, indent=1))
        sys.exit(0)
    if "--plugins" in sys.argv:
        # plugin kernel: list | validate <dir> | rescan | enable|disable <id> | grant <id> [caps...]
        print(json.dumps(plugins.cli(sys.argv[sys.argv.index("--plugins") + 1:]), indent=1))
        sys.exit(0)
    if "--import-proxy" in sys.argv:
        # one-time migration: import accounts from a standalone emailproxy ui_state.json
        args = sys.argv[sys.argv.index("--import-proxy") + 1:]
        if not args:
            print("usage: python app.py --import-proxy /path/to/ui_state.json")
            sys.exit(1)
        print(json.dumps(proxy.import_legacy_state(args[0]), indent=1))
        sys.exit(0)
    if "--extract-html" in sys.argv:
        print(json.dumps(engine.extract_rendered(workers=6)))
        sys.exit(0)
    if "--heal-snippets" in sys.argv:
        print(json.dumps(engine.heal_snippets(workers=6)))
        sys.exit(0)
    if "--heal-locations" in sys.argv:
        print(json.dumps(engine.heal_locations(), indent=1))
        sys.exit(0)
    if "--index" in sys.argv or "--reindex" in sys.argv:
        if "--reindex" in sys.argv:
            rag.rebuild()
            print("index cleared (rebuild)", flush=True)
        tries, last_remaining, stall = 0, None, 0
        while True:
            try:
                # CLI mode is its own fetch stage: pull mail + bodies first,
                # then the (cache-only) index pass can make progress
                mc = engine.MailClient().connect()
                try:
                    scanned = engine.scan_index_folders_batch(mc, limit=200)
                    fetched = engine.drain_body_jobs(mc, limit=200)
                finally:
                    mc.close()
                res = rag.index_pass_active(limit=40)
                if scanned or fetched:
                    print("fetch stage: %d scanned, %d body fetched" % (scanned, fetched),
                          flush=True)
            except Exception as exc:
                tries += 1
                print("index pass error (%d): %r" % (tries, exc), flush=True)
                if tries > 8:
                    print("index failed: too many errors", flush=True)
                    sys.exit(1)
                time.sleep(15)
                continue
            tries = 0
            print(res["summary"], flush=True)
            if res["remaining"] == 0:
                print("index complete", flush=True)
                break
            if res["processed"] == 0 and res["remaining"] == last_remaining:
                stall += 1
                if stall >= 3:
                    print("index stalled (check the Log page)", flush=True)
                    sys.exit(1)
            else:
                stall = 0
            last_remaining = res["remaining"]
        sys.exit(0)
    store.requeue_running_jobs()  # orphaned act/classify/index jobs after a crash
    worker.start()
    body_fetcher.start()  # fetch stage: caches bodies the index stage consumes
    act_runner.start()  # the single mailbox writer every mutation goes through
    if stage_worker.enabled():
        # CPU-heavy stages (index + learning) live in a supervised child process
        # so they never make the web app unresponsive (docs/pipeline-queue.md)
        engine.set_external_stages(True)
        _stage_sup = stage_worker.Supervisor()
        _stage_sup.start()
        if hasattr(signal, "SIGTERM"):
            def _term(_signum, _frame):
                try:
                    _stage_sup.stop()
                except Exception:
                    pass
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTERM)
            signal.signal(signal.SIGTERM, _term)
    else:
        indexer.start()
    classifier.start()
    llm_health.start()
    proxy.supervisor.start()  # keeps the embedded emailproxy running (embedded mode only)
    plugin_rt.scheduler.start()  # fires due plugin onSchedule() entrypoints
    app.run(host=config.UI_HOST, port=config.UI_PORT, threaded=True)
