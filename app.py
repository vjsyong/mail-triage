#!/usr/bin/env python3
"""
Mail Triage — smart email management on top of the email-oauth2-proxy.

Container-friendly web app: quick filter rules (move/flag/read), LLM escalation for
classification of the rest, reply templates with LLM-drafted replies saved to Drafts.

Run:  python app.py            (serves the UI and starts the background worker)
      python app.py --check    (read-only connectivity check, prints JSON)
"""
import json
import os
import re
import sys
import time
from html import escape as html_escape

from flask import Flask, Response, flash, jsonify, redirect, render_template_string, request, url_for

import config
import engine
import rag
import store

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "mail-triage-local")

worker = engine.Worker()
indexer = rag.Indexer()
classifier = engine.ClassifyJob()

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
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                   r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
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

HKT = 8 * 3600


def fmt_ts(ts):
    if not ts:
        return "—"
    return time.strftime("%m-%d %H:%M", time.gmtime(int(ts) + HKT))


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
        return {
            "total": one("SELECT COUNT(*) FROM messages"),
            "queued": one("SELECT COUNT(*) FROM messages WHERE status='queued'"),
            "classified": one("SELECT COUNT(*) FROM messages WHERE status IN ('classified','llm-moved')"),
            "moved": one("SELECT COUNT(*) FROM messages WHERE action_taken LIKE 'move%'"),
            "needs_reply": one("SELECT COUNT(*) FROM messages WHERE llm_needs_reply=1"),
            "errors": one("SELECT COUNT(*) FROM messages WHERE status='error'"),
            "rules": one("SELECT COUNT(*) FROM rules WHERE enabled=1"),
        }


def index_status():
    st = dict(indexer.state)
    st["last_ok_r"] = rel_time(st.get("last_ok"))
    try:
        s = rag.index_stats()
        st["messages"] = s["messages"]
        st["chunks"] = s["chunks"]
        ov = store.index_overview()
        st["folders_done"] = sum(1 for r in ov if r.get("status") == "done")
        st["folders_total"] = len(ov)
    except Exception:
        st.setdefault("messages", 0)
        st.setdefault("chunks", 0)
        st.setdefault("folders_done", 0)
        st.setdefault("folders_total", 0)
    return st


def summarize_conditions(rule):
    try:
        conds = json.loads(rule.get("conditions") or "[]")
    except (TypeError, ValueError):
        conds = []
    if not conds:
        return "(no conditions — never matches)"
    joiner = " AND " if (rule.get("match_mode") or "all") == "all" else " OR "
    return joiner.join('%s %s "%s"' % (c.get("field", "?"), c.get("op", "?"), c.get("value", ""))
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
    "classified": ("acc", "classified"),
    "queued": ("warn", "queued"),
    "error": ("err", "error"),
    "new": ("", "new"),
}


BASE_TMPL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mail Triage</title>
<style>
:root{--bg:#10151b;--card:#181f28;--card2:#1e2732;--line:#2a3646;--fg:#dde6ef;--dim:#8ea2b8;
--acc:#57a6ff;--ok:#41d392;--warn:#ffb454;--err:#ff6b6b;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
.wrap{max-width:1060px;margin:0 auto;padding:18px 16px 80px}
h1{font-size:1.25rem;margin:0} h2{font-size:1.05rem;margin:20px 0 8px}
h3{font-size:1rem;margin:0 0 6px}
.top{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;margin-bottom:14px}
nav a{margin-left:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0}
.sub{color:var(--dim);font-size:.9rem}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.spread{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}
.badge{display:inline-block;font-size:.78rem;padding:2px 9px;border-radius:999px;border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.badge.ok{color:var(--ok);border-color:var(--ok)} .badge.err{color:var(--err);border-color:var(--err)}
.badge.warn{color:var(--warn);border-color:var(--warn)} .badge.acc{color:var(--acc);border-color:var(--acc)}
.btn{display:inline-block;border:1px solid var(--line);background:var(--card2);color:var(--fg);border-radius:9px;
padding:8px 13px;font-size:.92rem;cursor:pointer}
.btn:hover{border-color:var(--acc)}
.btn.primary{background:var(--acc);border-color:var(--acc);color:#08111c;font-weight:600}
.btn.danger{color:var(--err)}
.btn.small{padding:4px 9px;font-size:.82rem}
form.inline{display:inline}
input[type=text],input[type=number],select,textarea{background:#0d1319;border:1px solid var(--line);
color:var(--fg);border-radius:8px;padding:8px 10px;font-size:.93rem;width:100%}
textarea{font-family:var(--mono);font-size:.87rem;min-height:140px}
label{display:block;font-size:.85rem;color:var(--dim);margin:10px 0 4px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
.grid3{display:grid;grid-template-columns:150px 130px 1fr;gap:8px}
code,.mono{font-family:var(--mono);font-size:.85rem;background:#0d1319;border:1px solid var(--line);
border-radius:6px;padding:1px 5px;word-break:break-all}
.note{background:#12202e;border:1px solid #234a6b;border-radius:9px;padding:10px 12px;font-size:.88rem;color:#bcd4ee}
.msg{border-radius:9px;padding:10px 12px;margin:10px 0;font-size:.92rem}
.msg.ok{background:#10241a;border:1px solid var(--ok)}
.msg.warn{background:#26200f;border:1px solid var(--warn)}
.msg.err{background:#2a1414;border:1px solid var(--err)}
table.tbl{width:100%;border-collapse:collapse}
.tbl th,.tbl td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);font-size:.88rem;vertical-align:top}
.tbl th{color:var(--dim);font-weight:500;white-space:nowrap}
.hidden{display:none}
pre.log{background:#0d1319;border:1px solid var(--line);border-radius:9px;padding:12px;font-size:.78rem;
line-height:1.4;overflow:auto;max-height:70vh;white-space:pre-wrap}
.stat{display:inline-block;background:var(--card2);border:1px solid var(--line);border-radius:10px;
padding:8px 14px;margin:0 8px 8px 0;text-align:center}
.stat b{display:block;font-size:1.35rem}
.stat span{color:var(--dim);font-size:.8rem}
.foot{margin-top:26px;color:var(--dim);font-size:.8rem}
.md p{margin:6px 0}
.md .md-h{font-weight:600;margin:10px 0 4px}
.md ul,.md ol{margin:6px 0 6px 22px;padding:0}
.md blockquote{border-left:3px solid var(--line);margin:6px 0;padding:2px 10px;color:var(--dim)}
.md pre.md-pre{background:#0d1319;border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;white-space:pre-wrap;font-family:var(--mono);font-size:.85rem}
.md code{font-family:var(--mono);font-size:.85rem;background:#0d1319;border:1px solid var(--line);border-radius:6px;padding:1px 5px}
</style>
</head><body><div class="wrap">
<div class="top">
  <div><h1>Mail Triage</h1><div class="sub">{{ cfg.IMAP_USER }} · via proxy {{ cfg.IMAP_HOST }}:{{ cfg.IMAP_PORT }} · LLM: {{ cfg.LLM_MODEL }}</div></div>
  <nav class="sub"><a href="{{ url_for('dashboard') }}">Dashboard</a><a href="{{ url_for('assistant') }}">Assistant</a><a href="{{ url_for('rules') }}">Rules</a><a href="{{ url_for('templates') }}">Templates</a><a href="{{ url_for('messages') }}">Messages</a><a href="{{ url_for('settings') }}">Settings</a><a href="{{ url_for('log') }}">Log</a></nav>
</div>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% for cat, msg in messages %}<div class="msg {{ cat }}">{{ msg }}</div>{% endfor %}
{% endwith %}
{{ body|safe }}
<div class="foot">Times shown in HKT · app data in {{ cfg.DATA_DIR }} · never deletes mail (worst case: files it into a folder)</div>
</div></body></html>
"""


def render(body):
    return render_template_string(BASE_TMPL, body=body, cfg=config)


# ---------------------------------------------------------------- dashboard

DASH_TMPL = """
{% set ws = worker_state %}
<div class="card">
  <div class="spread">
    <div>
      <h3>Status</h3>
      <div class="sub">
        {% if ws.running %}<span class="badge acc">checking now…</span>
        {% elif ws.last_error %}<span class="badge err">last check failed</span>
          <span class="mono">{{ ws.last_error }}</span>
        {% elif ws.last_ok %}<span class="badge ok">connected</span>
        {% else %}<span class="badge warn">starting up…</span>{% endif %}
        &nbsp;last check: {{ ws.last_ok_r }} · next in ~{{ ws.next_in }}s · every {{ ws.interval }}s
        {% if ws.last_summary %}<br>last pass: {{ ws.last_summary }}{% endif %}
      </div>
    </div>
    <form class="inline" method="post" action="{{ url_for('check_now') }}">{% if false %}{% endif %}
      <button class="btn primary" type="submit" {{ 'disabled' if ws.running else '' }}>Check now</button></form>
  </div>
  {% if ws.last_error %}<div class="msg err">The last pass failed: {{ ws.last_error }} — see the Log page.</div>{% endif %}
</div>

<div class="card">
  <div class="row">
    <div class="stat"><b>{{ st.total }}</b><span>seen</span></div>
    <div class="stat"><b>{{ st.moved }}</b><span>sorted by rules</span></div>
    <div class="stat"><b>{{ st.classified }}</b><span>LLM classified</span></div>
    <div class="stat"><b>{{ st.queued }}</b><span>waiting for LLM</span></div>
    <div class="stat"><b>{{ st.needs_reply }}</b><span>need a reply</span></div>
    <div class="stat"><b>{{ st.rules }}</b><span>rules enabled</span></div>
  </div>
  <div class="sub">
    Rules act {{ 'live' if settings.rules_apply else 'in dry-run (suggest only)' }} ·
    LLM classification {{ 'on' if settings.llm_suggest else 'off' }} ·
    LLM auto-filing {{ 'ON' if settings.llm_apply else 'off (suggests only)' }} ·
    <a href="{{ url_for('settings') }}">change</a>
  </div>
</div>

<h2>Search index <span class="sub">— semantic search over the whole archive</span></h2>
<div class="card">
  <div class="spread">
    <div class="sub">
      {% if ix.running %}<span class="badge acc">indexing…</span> {{ ix.progress }}
      {% elif ix.last_error %}<span class="badge err">indexer error</span> <span class="mono">{{ ix.last_error }}</span>
      {% elif ix.chunks %}<span class="badge ok">ready</span>
      {% else %}<span class="badge warn">not built yet</span>{% endif %}
      <br>{{ ix.messages }} messages · {{ ix.chunks }} chunks indexed · folders {{ ix.folders_done }}/{{ ix.folders_total }} complete{% if ix.last_ok %} · last run {{ ix.last_ok_r }}{% endif %}
    </div>
    <div class="row" style="white-space:nowrap">
      <form class="inline" method="post" action="{{ url_for('index_run') }}"><button class="btn" type="submit" {{ 'disabled' if ix.running else '' }}>Index now</button></form>
      <form class="inline" method="post" action="{{ url_for('index_rebuild') }}" onsubmit="return confirm('Rebuild the search index from scratch? Mail is untouched.');"><button class="btn small" type="submit" {{ 'disabled' if ix.running else '' }}>Rebuild</button></form>
    </div>
  </div>
</div>
{% if ix.running %}<script>setTimeout(function(){location.reload();}, 8000);</script>{% endif %}
{% if st.errors %}
<div class="card">
  <div class="spread">
    <div class="sub">{{ st.errors }} message(s) parked after repeated LLM failures - fix the LLM endpoint, then retry.</div>
    <form class="inline" method="post" action="{{ url_for('retry_errors') }}"><button class="btn" type="submit">Retry parked</button></form>
  </div>
</div>
{% endif %}

<h2>Recent messages</h2>
<div class="card">
  {% if messages %}
  <table class="tbl"><tr><th>when</th><th>from</th><th>subject</th><th>status</th><th>LLM</th></tr>
  {% for m in messages %}
  <tr>
    <td class="sub">{{ m.when }}</td>
    <td class="sub">{{ m.from_addr[:40] }}</td>
    <td><a href="{{ url_for('message_detail', mid=m.id) }}">{{ m.subject[:80] or '(no subject)' }}</a></td>
    <td><span class="badge {{ m.badge[0] }}">{{ m.badge[1] }}</span>{% if m.action %} <span class="sub">{{ m.action }}</span>{% endif %}</td>
    <td class="sub">{{ m.llm }}</td>
  </tr>
  {% endfor %}</table>
  {% else %}<div class="sub">Nothing processed yet — the first pass will pick up your recent inbox ({% if true %}{{ settings.lookback_hours }}h lookback{% endif %}).</div>{% endif %}
  <p><a class="btn" href="{{ url_for('messages') }}">All messages →</a></p>
</div>

<h2>Recent activity</h2>
<div class="card">
  {% for e in events %}
  <div class="sub"><span class="mono">{{ e.when }}</span> <span class="badge {{ e.cls }}">{{ e.level }}</span> {{ e.message }}</div>
  {% else %}<div class="sub">No events yet.</div>{% endfor %}
</div>
"""


@app.route("/")
def dashboard():
    ws = dict(worker.state)
    ws["last_ok_r"] = rel_time(ws.get("last_ok"))
    interval = int(store.get_setting("poll_interval", 90))
    ws["interval"] = interval
    ws["next_in"] = max(0, int(interval - (time.time() - ws.get("last_cycle", 0))))
    msgs = store.messages(limit=15)
    for m in msgs:
        m["when"] = fmt_ts(m.get("processed_at"))
        m["badge"] = STATUS_BADGES.get(m.get("status"), ("", m.get("status", "")))
        llm = m.get("llm_category") or ""
        if llm and m.get("llm_confidence") is not None:
            llm += " (%.0f%%)" % (m["llm_confidence"] * 100)
        if m.get("llm_needs_reply"):
            llm += " · needs reply"
        m["llm"] = llm
    events = [e for e in store.recent_events(40) if e.get("level") != "debug"][:14]
    for e in events:
        e["when"] = fmt_ts(e["ts"])
        e["cls"] = {"error": "err", "info": "ok", "debug": ""}.get(e.get("level"), "")
    return render(render_template_string(
        DASH_TMPL, worker_state=ws, st=stats(), messages=msgs, events=events,
        settings=store.all_settings(), ix=index_status()))


@app.route("/check", methods=["POST"])
def check_now():
    worker.trigger()
    flash("Check triggered — give it a few seconds and reload.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/retry-errors", methods=["POST"])
def retry_errors():
    n = store.retry_parked_errors()
    worker.trigger()
    flash("Re-queued %d message(s) for classification." % n, "ok")
    return redirect(url_for("dashboard"))


@app.route("/index/run", methods=["POST"])
def index_run():
    indexer.trigger()
    flash("Indexing started — progress shows on the dashboard and the Log page.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/index/rebuild", methods=["POST"])
def index_rebuild():
    indexer.trigger(rebuild=True)
    flash("Rebuilding the search index from scratch — mail itself is untouched.", "ok")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- rules

RULES_TMPL = """
<h2>Filter rules <span class="sub">— evaluated top to bottom, first match wins; most mail should be sorted by these</span></h2>
<div class="card">
  <div class="spread">
    <div class="sub">Rules act {{ 'live' if settings.rules_apply else 'in dry-run (suggest only)' }} — see Settings.</div>
    <div class="row">
      <form class="inline" method="post" action="{{ url_for('rules_test') }}">
        <button class="btn" type="submit">Test against last {{ test_limit }} messages</button></form>
      <a class="btn primary" href="{{ url_for('rule_new') }}">New rule</a>
    </div>
  </div>
</div>
{% if test_results %}
<div class="card">
  <h3>Dry-run test <span class="sub">(nothing was changed)</span></h3>
  <table class="tbl"><tr><th>rule</th><th>matches</th></tr>
  {% for t in test_results %}<tr><td>{{ t.name }}</td><td>{{ t.count }}</td></tr>{% endfor %}
  <tr><td class="sub">unmatched (would go to LLM)</td><td>{{ test_unmatched }}</td></tr>
  </table>
</div>
{% endif %}
<div class="card">
  {% if rules %}
  <table class="tbl"><tr><th>#</th><th>name</th><th>matches</th><th>actions</th><th></th></tr>
  {% for r in rules %}
  <tr>
    <td class="sub">{{ loop.index }}</td>
    <td>
      {% if r.enabled %}{{ r.name }}{% else %}<span class="sub">{{ r.name }} (disabled)</span>{% endif %}
    </td>
    <td class="mono">{{ r.summary }}</td>
    <td class="sub">{{ r.actions }}</td>
    <td class="row" style="white-space:nowrap">
      <form class="inline" method="post" action="{{ url_for('rule_move', rule_id=r.id) }}"><input type="hidden" name="dir" value="top"><button class="btn small" type="submit" title="move to top">⤒</button></form>
      <form class="inline" method="post" action="{{ url_for('rule_move', rule_id=r.id) }}"><input type="hidden" name="dir" value="up"><button class="btn small" type="submit">↑</button></form>
      <form class="inline" method="post" action="{{ url_for('rule_move', rule_id=r.id) }}"><input type="hidden" name="dir" value="down"><button class="btn small" type="submit">↓</button></form>
      <form class="inline" method="post" action="{{ url_for('rule_toggle', rule_id=r.id) }}"><button class="btn small" type="submit">{{ 'disable' if r.enabled else 'enable' }}</button></form>
      <a class="btn small" href="{{ url_for('rule_edit', rule_id=r.id) }}">edit</a>
      <form class="inline" method="post" action="{{ url_for('rule_delete', rule_id=r.id) }}"
            onsubmit="return confirm('Delete rule {{ r.name }}?')"><button class="btn small danger" type="submit">delete</button></form>
    </td>
  </tr>
  {% endfor %}</table>
  {% else %}<div class="sub">No rules yet. Create one, e.g. “from contains newsletter@ → move to Newsletters”.</div>{% endif %}
</div>
"""


@app.route("/rules")
def rules():
    rules_list = store.list_rules()
    for r in rules_list:
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
    return render(render_template_string(
        RULES_TMPL, rules=rules_list, settings=store.all_settings(),
        test_results=tr, test_unmatched=tu, test_limit=200))


@app.route("/rules/test", methods=["POST"])
def rules_test():
    return redirect(url_for("rules", tested="1"))


RULE_EDIT_TMPL = """
<h2>{{ 'Edit rule' if rule else 'New rule' }}</h2>
<form method="post" class="card">
  <div class="grid2">
    <div><label>Name</label><input type="text" name="name" value="{{ rule.name if rule else '' }}" placeholder="e.g. Boss → Work"></div>
    <div><label>Match mode</label>
      <select name="match_mode">
        <option value="all" {{ 'selected' if (rule.match_mode if rule else 'all')=='all' else '' }}>ALL conditions must match</option>
        <option value="any" {{ 'selected' if rule and rule.match_mode=='any' else '' }}>ANY condition matches</option>
      </select></div>
  </div>
  <label>Conditions <span class="sub">(empty rows are ignored)</span></label>
  <div id="conds">
    {% for i in range(5) %}
    {% set c = conditions[i] if conditions|length > i else {} %}
    <div class="grid3" style="margin-bottom:6px">
      <select name="cond_field_{{ i }}">
        {% for f in ['from','to','subject','body'] %}
        <option value="{{ f }}" {{ 'selected' if c.get('field')==f else '' }}>{{ f }}</option>{% endfor %}
      </select>
      <select name="cond_op_{{ i }}">
        {% for o in ['contains','equals','regex'] %}
        <option value="{{ o }}" {{ 'selected' if c.get('op')==o else '' }}>{{ o }}</option>{% endfor %}
      </select>
      <input type="text" name="cond_value_{{ i }}" value="{{ c.get('value','') }}">
    </div>
    {% endfor %}
  </div>
  <label>Actions <span class="sub">— leave all blank to keep matching mail in place (a guard rule: no later rule or LLM filing can move it)</span></label>
  <div class="grid2">
    <div><label>Move to folder <span class="sub">(blank = don't move; created if missing)</span></label>
      <input type="text" name="move_to" value="{{ actions.get('move_to','') }}" placeholder="e.g. Work"></div>
    <div>
      <label class="row" style="color:var(--fg)"><input type="checkbox" name="mark_read" value="1" style="width:auto;margin-right:8px"
        {{ 'checked' if actions.get('mark_read') else '' }}> Mark as read</label>
      <label class="row" style="color:var(--fg)"><input type="checkbox" name="flag" value="1" style="width:auto;margin-right:8px"
        {{ 'checked' if actions.get('flag') else '' }}> Flag / star</label>
      <label class="row" style="color:var(--fg)"><input type="checkbox" name="enabled" value="1" style="width:auto;margin-right:8px"
        {{ 'checked' if (rule.enabled if rule else True) else '' }}> Enabled</label>
    </div>
  </div>
  <p style="margin-top:14px"><button class="btn primary" type="submit">Save rule</button>
  <a class="btn" href="{{ url_for('rules') }}">Back</a></p>
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
        conditions.append({"field": request.form.get("cond_field_%d" % i, "subject"),
                           "op": request.form.get("cond_op_%d" % i, "contains"),
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
            flash("Add at least one condition with a value.", "err")
            return redirect(url_for("rule_new"))
        store.add_rule(name, mode, conds, actions, enabled)
        store.log_event("info", "rule '%s' added" % name)
        flash("Rule added.", "ok")
        return redirect(url_for("rules"))
    return render(render_template_string(RULE_EDIT_TMPL, **_rule_form_context()))


@app.route("/rules/<int:rule_id>/edit", methods=["GET", "POST"])
def rule_edit(rule_id):
    rule = store.get_rule(rule_id)
    if not rule:
        flash("No such rule.", "err")
        return redirect(url_for("rules"))
    if request.method == "POST":
        name, mode, conds, actions, enabled = _rule_from_form()
        if not conds:
            flash("Add at least one condition with a value.", "err")
            return redirect(url_for("rule_edit", rule_id=rule_id))
        store.update_rule(rule_id, name=name, match_mode=mode,
                          conditions=json.dumps(conds), actions=json.dumps(actions),
                          enabled=1 if enabled else 0)
        flash("Rule saved.", "ok")
        return redirect(url_for("rules"))
    return render(render_template_string(RULE_EDIT_TMPL, **_rule_form_context(rule)))


@app.route("/rules/<int:rule_id>/toggle", methods=["POST"])
def rule_toggle(rule_id):
    rule = store.get_rule(rule_id)
    if rule:
        store.update_rule(rule_id, enabled=0 if rule["enabled"] else 1)
    return redirect(url_for("rules"))


@app.route("/rules/<int:rule_id>/delete", methods=["POST"])
def rule_delete(rule_id):
    store.delete_rule(rule_id)
    return redirect(url_for("rules"))


@app.route("/rules/<int:rule_id>/move", methods=["POST"])
def rule_move(rule_id):
    d = request.form.get("dir")
    if d == "top":
        store.move_rule_top(rule_id)
    else:
        store.move_rule(rule_id, -1 if d == "up" else 1)
    return redirect(url_for("rules"))


# ---------------------------------------------------------------- templates

TEMPLATES_TMPL = """
<h2>Reply templates <span class="sub">— placeholders: {sender} {subject} {date} {my_name}</span></h2>
<div class="card">
  <div class="spread"><div class="sub">Used as guidance when the LLM drafts a reply, or fill them in yourself.</div>
  <a class="btn primary" href="{{ url_for('template_new') }}">New template</a></div>
</div>
<div class="card">
  {% if templates %}
  <table class="tbl"><tr><th>name</th><th>body preview</th><th></th></tr>
  {% for t in templates %}
  <tr><td>{{ t.name }}</td><td class="sub">{{ t.body[:120] }}</td>
  <td class="row" style="white-space:nowrap">
    <a class="btn small" href="{{ url_for('template_edit', tid=t.id) }}">edit</a>
    <form class="inline" method="post" action="{{ url_for('template_delete', tid=t.id) }}"
          onsubmit="return confirm('Delete template {{ t.name }}?')"><button class="btn small danger" type="submit">delete</button></form>
  </td></tr>
  {% endfor %}</table>
  {% else %}<div class="sub">No templates yet.</div>{% endif %}
</div>
"""

TEMPLATE_EDIT_TMPL = """
<h2>{{ 'Edit template' if template else 'New template' }}</h2>
<form method="post" class="card">
  <div class="grid2">
    <div><label>Name</label><input type="text" name="name" value="{{ template.name if template else '' }}" placeholder="e.g. Meeting ack"></div>
    <div><label>Subject (optional; {subject} works)</label><input type="text" name="subject" value="{{ template.subject if template else '' }}" placeholder="Re: {subject}"></div>
  </div>
  <label>Body</label>
  <textarea name="body" rows="10">{{ template.body if template else '' }}</textarea>
  <p><button class="btn primary" type="submit">Save template</button>
  <a class="btn" href="{{ url_for('templates') }}">Back</a></p>
  <div class="sub">Tip: keep templates short — the LLM adapts them to the actual email.</div>
</form>
"""


@app.route("/templates")
def templates():
    return render(render_template_string(TEMPLATES_TMPL, templates=store.list_templates()))


@app.route("/templates/new", methods=["GET", "POST"])
def template_new():
    if request.method == "POST":
        store.add_template((request.form.get("name") or "").strip() or "Untitled",
                           (request.form.get("subject") or "").strip(),
                           request.form.get("body") or "")
        flash("Template added.", "ok")
        return redirect(url_for("templates"))
    return render(render_template_string(TEMPLATE_EDIT_TMPL, template=None))


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
    return render(render_template_string(TEMPLATE_EDIT_TMPL, template=t))


@app.route("/templates/<int:tid>/delete", methods=["POST"])
def template_delete(tid):
    store.delete_template(tid)
    return redirect(url_for("templates"))


# ---------------------------------------------------------------- messages

MESSAGES_TMPL = """
<h2>Messages <span class="sub">— newest mail first · tick rows to tag or classify in bulk</span></h2>

{% if proposals %}
<div class="card">
  <h3>Rules proposed from your tags <span class="sub">— review, then add with one click</span></h3>
  {% for p in proposals %}
  <div style="background:#0d1319;border:1px solid var(--line);border-radius:9px;padding:10px 12px;margin:8px 0">
    <div class="spread">
      <div><b>{{ p.rule_obj.name }}</b> <span class="sub">({{ p.rule_obj.match_mode }})</span>{% if p.rule_obj.placement == 'top' %} <span class="badge acc">added at top</span>{% endif %}</div>
      <div class="row" style="white-space:nowrap">
        <form class="inline" method="post" action="{{ url_for('proposal_apply', pid=p.id) }}"><button class="btn small primary" type="submit">Add rule</button></form>
        <form class="inline" method="post" action="{{ url_for('proposal_apply', pid=p.id) }}"><input type="hidden" name="disabled" value="1"><button class="btn small" type="submit">Add (disabled)</button></form>
        <form class="inline" method="post" action="{{ url_for('proposal_dismiss', pid=p.id) }}"><button class="btn small danger" type="submit">Dismiss</button></form>
      </div>
    </div>
    <div class="mono" style="font-size:.85rem">{{ p.cond_text }}</div>
    <div class="sub">{{ p.act_text }}{% if p.rule_obj.rationale %} — {{ p.rule_obj.rationale }}{% endif %}</div>
  </div>
  {% endfor %}
</div>
{% endif %}

{% if classify_state.running %}
<div class="card">
  <div class="row">
    <span class="badge acc">classifying…</span>
    <span class="sub">{{ classify_state.done }}/{{ classify_state.total }}{% if classify_state.failed %} · {{ classify_state.failed }} failed{% endif %}{% if classify_state.concurrency %} · {{ classify_state.concurrency }} at a time{% endif %}{% if classify_state.current %} · now: {{ classify_state.current }}{% endif %}</span>
    <form class="inline" method="post" action="{{ url_for('classify_stop') }}"><button class="btn small danger" type="submit">Stop</button></form>
  </div>
</div>
<script>setTimeout(function(){ location.reload(); }, 10000);</script>
{% endif %}

<div class="card">
  <form id="bulk" method="post">
    <div class="row">
      <input type="text" name="tag" list="taglist" placeholder="tag selected as…" style="max-width:210px">
      <datalist id="taglist">{% for c in tag_options %}<option value="{{ c }}">{% endfor %}</datalist>
      <button class="btn" type="submit" formaction="{{ url_for('messages_tag') }}">Tag</button>
      <button class="btn" type="submit" formaction="{{ url_for('messages_untag') }}">Untag</button>
      <button class="btn" type="submit" formaction="{{ url_for('messages_classify') }}">Classify selected</button>
      <button class="btn small" type="submit" formaction="{{ url_for('messages_classify_all') }}" {{ 'disabled' if classify_state.running else '' }}>Classify all unclassified ({{ unclassified }})</button>
      <button class="btn small" type="submit" formaction="{{ url_for('learn_rules') }}" {{ 'disabled' if not tagged_count else '' }}>Learn rules from tags ({{ tagged_count }})</button>
    </div>
    <div class="row" style="margin-top:8px">
      {% for key, label in [('all','All'),('queued','Awaiting LLM'),('needs_reply','Needs reply'),('moved','Sorted'),('tagged','Tagged'),('errors','Errors')] %}
        <a class="btn small {{ 'primary' if filt==key else '' }}" href="{{ url_for('messages', f=key) }}">{{ label }}</a>
      {% endfor %}
    </div>
    <table class="tbl" style="margin-top:8px">
      <tr>
        <th><input type="checkbox" style="width:auto" onclick="for (var b of document.querySelectorAll('#bulk input[name=ids]')) b.checked = this.checked;"></th>
        <th>date</th><th>from</th><th>subject</th><th>tag</th><th>status</th><th>LLM</th>
      </tr>
      {% for m in msgs %}
      <tr>
        <td><input type="checkbox" name="ids" value="{{ m.id }}" style="width:auto"></td>
        <td class="sub">{{ m.when }}</td>
        <td class="sub">{{ m.from_addr[:34] }}</td>
        <td><a href="{{ url_for('message_detail', mid=m.id) }}">{{ m.subject[:84] or '(no subject)' }}</a></td>
        <td>{% if m.user_tag %}<span class="badge warn">{{ m.user_tag }}</span>{% endif %}</td>
        <td><span class="badge {{ m.badge[0] }}">{{ m.badge[1] }}</span>{% if m.action %} <span class="sub">{{ m.action }}</span>{% endif %}</td>
        <td class="sub">{{ m.llm }}</td>
      </tr>
      {% endfor %}
    </table>
    {% if not msgs %}<div class="sub">No messages{% if filt != 'all' %} in this filter{% endif %} yet.</div>{% endif %}
  </form>
</div>
"""


def _proposal_views():
    out = []
    for p in store.list_rule_proposals():
        ro = p["rule_obj"]
        out.append({
            "id": p["id"],
            "rule_obj": ro,
            "cond_text": summarize_conditions({"conditions": json.dumps(ro.get("conditions") or []),
                                                "match_mode": ro.get("match_mode") or "all"}),
            "act_text": summarize_actions({"actions": json.dumps(ro.get("actions") or {})}),
        })
    return out


@app.route("/messages")
def messages():
    filt = request.args.get("f", "all")
    msgs = store.messages(limit=100, filt=filt)
    for m in msgs:
        m["when"] = fmt_ts(m.get("date_ts") or m.get("processed_at"))
        m["badge"] = STATUS_BADGES.get(m.get("status"), ("", m.get("status", "")))
        llm = m.get("llm_category") or ""
        if llm and m.get("llm_confidence") is not None:
            llm += " (%.0f%%)" % (m["llm_confidence"] * 100)
        m["llm"] = llm
    settings = store.all_settings()
    tag_options = sorted(set(
        list(settings.get("categories") or [])
        + list((settings.get("category_folders") or {}).values())
        + [json.loads(r.get("actions") or "{}").get("move_to", "")
           for r in store.list_rules() if r.get("enabled")]))
    tag_options = [t for t in tag_options if t]
    return render(render_template_string(
        MESSAGES_TMPL, msgs=msgs, filt=filt,
        proposals=_proposal_views(),
        classify_state=dict(classifier.state),
        unclassified=store.unclassified_count(),
        tagged_count=len(store.tagged_examples(1000)),
        tag_options=tag_options))


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
    if not norm.get("conditions") or not norm.get("actions"):
        flash("That proposal is not valid any more.", "err")
        return redirect(url_for("messages"))
    disabled = bool(request.form.get("disabled"))
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
<h2>{{ m.subject[:100] or '(no subject)' }}</h2>
<div class="card">
  <div class="kv sub">From <b>{{ m.from_addr }}</b> · {{ m.date }} · folder {{ m.folder }} · uid {{ m.uid }}</div>
  <div class="row" style="margin:8px 0">
    <span class="badge {{ m.badge[0] }}">{{ m.badge[1] }}</span>
    {% if m.user_tag %}<span class="badge warn">tag: {{ m.user_tag }}</span>{% endif %}
    {% if m.action_taken %}<span class="badge">{{ m.action_taken }}</span>{% endif %}
    {% if m.llm_category %}<span class="badge acc">LLM: {{ m.llm_category }}
      {% if m.llm_confidence is not none %}({{ '%.0f' % (m.llm_confidence*100) }}%){% endif %}</span>{% endif %}
    {% if m.llm_needs_reply %}<span class="badge warn">needs reply</span>{% endif %}
  </div>
  {% if m.llm_summary %}<div class="note">LLM summary: {{ m.llm_summary }}{% if m.llm_reason %} · why: {{ m.llm_reason }}{% endif %}{% if m.llm_suggested_folder %} · suggested folder: {{ m.llm_suggested_folder }}{% endif %}</div>{% endif %}
  {% if m.llm_thinking %}<details class="sub" style="margin:4px 0"><summary style="cursor:pointer">classifier thinking</summary><pre class="mono" style="white-space:pre-wrap;font-size:.8rem;color:var(--dim);margin:6px 0">{{ m.llm_thinking }}</pre></details>{% endif %}
  <div class="row" style="margin:10px 0 2px">
    <form class="inline" method="post" action="{{ url_for('message_classify', mid=m.id) }}">
      <button class="btn small primary" type="submit">{{ 'Re-classify with LLM' if m.llm_category else 'Classify with LLM' }}</button></form>
    {% if m.llm_suggested_folder %}
    <form class="inline" method="post" action="{{ url_for('message_file', mid=m.id) }}">
      <button class="btn small" type="submit">File to {{ m.llm_suggested_folder }}</button></form>
    {% endif %}
    <form class="inline row" method="post" action="{{ url_for('message_tag', mid=m.id) }}">
      <input type="text" name="tag" value="{{ m.user_tag }}" placeholder="tag…" style="max-width:170px;width:auto">
      <button class="btn small" type="submit">Save tag</button>
    </form>
  </div>
  {% if classify_result %}<div class="note" style="margin-top:8px">LLM classified this as <b>{{ classify_result.category }}</b>
    ({{ '%.0f' % (classify_result.confidence*100) }}%) — {{ classify_result.summary }}{% if classify_result.reason %} · why: {{ classify_result.reason }}{% endif %}{% if classify_result.moved %} · filed to {{ classify_result.moved }}{% endif %}</div>{% endif %}
  <p class="mono" style="font-size:.85rem;white-space:pre-wrap">{{ m.snippet[:900] }}</p>
</div>

<h2>Reply</h2>
<div class="card">
  <div class="row">
    <form class="inline" method="post" action="{{ url_for('message_draft', mid=m.id) }}">
      <select name="template_id" style="width:auto;min-width:220px">
        <option value="">(no template — freeform)</option>
        {% for t in templates %}<option value="{{ t.id }}" {{ 'selected' if draft_template_id==t.id else '' }}>{{ t.name }}</option>{% endfor %}
      </select>
      <button class="btn primary" type="submit">Draft with LLM</button>
    </form>
    <span class="sub">{% if not llm_configured %}LLM key not configured — see Settings.{% endif %}</span>
  </div>
  {% if draft %}
  <form method="post" action="{{ url_for('message_save', mid=m.id) }}" style="margin-top:10px">
    <textarea name="body" rows="12">{{ draft }}</textarea>
    <p class="row" style="margin-top:8px">
      <button class="btn primary" type="submit">Save to Drafts</button>
      <button class="btn" type="button" onclick="navigator.clipboard.writeText(document.querySelector('textarea[name=body]').value);this.textContent='copied'">Copy</button>
      <span class="sub">Saving puts it in your Drafts folder — nothing is sent automatically; review & send from your mail client.</span>
    </p>
  </form>
  {% elif draft_error %}
  <div class="msg err" style="margin-top:10px">Draft failed: {{ draft_error }}</div>
  {% endif %}
</div>
"""


def _render_message(m, classify_result=None, draft=None, draft_error=None, draft_template_id=0):
    return render(render_template_string(
        MESSAGE_TMPL, m=m, templates=store.list_templates(), draft=draft,
        draft_error=draft_error, draft_template_id=draft_template_id,
        classify_result=classify_result, llm_configured=bool(config.LLM_API_KEY)))


@app.route("/messages/<int:mid>")
def message_detail(mid):
    m = store.get_message(mid)
    if not m:
        flash("No such message.", "err")
        return redirect(url_for("messages"))
    m["badge"] = STATUS_BADGES.get(m.get("status"), ("", m.get("status", "")))
    return _render_message(m)


@app.route("/messages/<int:mid>/classify", methods=["POST"])
def message_classify(mid):
    m = store.get_message(mid)
    if not m:
        flash("No such message.", "err")
        return redirect(url_for("messages"))
    try:
        res = engine.classify_and_store(m, store.all_settings())
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
    flash(("Tag saved: " + tag) if tag else "Tag cleared.", "ok")
    return redirect(url_for("message_detail", mid=mid))


@app.route("/messages/<int:mid>/file", methods=["POST"])
def message_file(mid):
    m = store.get_message(mid)
    target = (m or {}).get("llm_suggested_folder") or ""
    if not m or not target:
        flash("No suggested folder for this message — classify it first.", "err")
    else:
        try:
            mc = engine.MailClient().connect()
            try:
                mc.ensure_selected(m["folder"])
                mc.ensure_folder(target)
                mc.move(m["uid"], target)
            finally:
                mc.close()
            store.update_message(mid, status="llm-moved", action_taken="move:" + target)
            store.log_event("info", "filed message %d ('%s') → %s"
                            % (mid, (m.get("subject") or "")[:50], target))
            flash("Filed to '%s'." % target, "ok")
        except Exception as exc:
            flash("File failed: %r" % exc, "err")
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
        return _render_message(m, draft=draft,
                               draft_template_id=int(template_id) if template_id else 0)
    except Exception as exc:
        return _render_message(m, draft_error=repr(exc))


@app.route("/messages/<int:mid>/save", methods=["POST"])
def message_save(mid):
    try:
        folder = engine.save_draft(mid, request.form.get("body") or "")
        flash("Draft saved to '%s' — review it in your mail client." % folder, "ok")
    except Exception as exc:
        flash("Could not save the draft: %r" % exc, "err")
    return redirect(url_for("message_detail", mid=mid))


# ---------------------------------------------------------------- assistant

ASSISTANT_TMPL = r"""
<h2>Mail assistant <span class="sub">streams tokens + thinking · tools: search mail, read, move, flag, folders, rules</span></h2>
<div class="card" id="convo">
{% if convo %}
  {% for m in convo %}
    {% if m.role == 'user' %}
      <div class="msg ok" style="margin-left:10%"><b>You:</b> {{ m.content }}<div class="sub" style="font-size:.72rem;text-align:right">{{ m.when }}</div></div>
    {% else %}
      <div class="msg" style="background:var(--card2);border:1px solid var(--line)">
        <b>Assistant:</b>
        {% if m.reasoning %}
        <details class="sub" style="margin:6px 0"><summary style="cursor:pointer">thinking</summary>
          <pre class="mono" style="white-space:pre-wrap;font-size:.8rem;color:var(--dim);margin:6px 0">{{ m.reasoning }}</pre>
        </details>
        {% endif %}
        {% if m.tool_steps %}
        <div style="margin:6px 0">
          {% for t in m.tool_steps %}
          <div class="sub mono" style="font-size:.8rem">{{ '✓' if t.ok else '✗' }} {{ t.name }}({{ t.args }}){% if t.dry_run %} [dry-run]{% endif %} → {{ t.summary }}</div>
          {% endfor %}
        </div>
        {% endif %}
        <div class="md">{{ md(m.content)|safe }}</div>
        <div class="sub" style="font-size:.72rem;text-align:right">{{ m.when }}</div>
        {% for p in m.proposals_list %}
        <div style="background:#0d1319;border:1px solid var(--line);border-radius:9px;padding:10px 12px;margin:8px 0">
          <div class="spread">
            <div><b>{{ p.name }}</b> <span class="sub">({{ p.match_mode }})</span>{% if p.placement == 'top' %} <span class="badge acc">added at top</span>{% endif %}</div>
            <div class="row" style="white-space:nowrap">
              <form class="inline" method="post" action="{{ url_for('assistant_apply') }}">
                <input type="hidden" name="msg_id" value="{{ m.id }}">
                <input type="hidden" name="idx" value="{{ loop.index0 }}">
                <button class="btn small primary" type="submit">Add rule</button>
              </form>
              <form class="inline" method="post" action="{{ url_for('assistant_apply') }}">
                <input type="hidden" name="msg_id" value="{{ m.id }}">
                <input type="hidden" name="idx" value="{{ loop.index0 }}">
                <input type="hidden" name="disabled" value="1">
                <button class="btn small" type="submit">Add (disabled)</button>
              </form>
            </div>
          </div>
          <div class="mono" style="font-size:.85rem">{{ p.summary }}</div>
          <div class="sub">{{ p.actions_summary }}{% if p.rationale %} - {{ p.rationale }}{% endif %}</div>
        </div>
        {% endfor %}
      </div>
    {% endif %}
  {% endfor %}
{% else %}
  <div class="sub">Ask anything about your mail — the assistant searches your whole archive by <b>meaning</b> (not just keywords), reads messages, can create folders and move or flag mail, and proposes rules you approve with one click. Its thinking and every tool step stream live below. Examples:</div>
  <div class="row" style="margin-top:8px">
    <a class="btn small" href="#" onclick="document.getElementById('msg').value='What did my landlord last email me about?';return false">What did the landlord want?</a>
    <a class="btn small" href="#" onclick="document.getElementById('msg').value='Find the last invoice a vendor sent me and summarise it';return false">Find an old invoice</a>
    <a class="btn small" href="#" onclick="document.getElementById('msg').value='Search my mail for anything from the library';return false">Library mail</a>
    <a class="btn small" href="#" onclick="document.getElementById('msg').value='What rules would you suggest for my inbox?';return false">Suggest rules for me</a>
  </div>
{% endif %}
<div id="live"></div>
</div>
<div class="card">
  <form id="aform" method="post" action="{{ url_for('assistant_send') }}">
    <textarea name="message" id="msg" rows="3" placeholder="e.g. Find the invoice the landlord sent in August and tell me what it says"></textarea>
    <p class="row" style="margin-top:8px">
      <button class="btn primary" type="submit" id="asend">Send</button>
      <span class="sub">runs on {{ cfg.LLM_MODEL }} · Ctrl+Enter sends · actions {{ 'live' if actions_live else 'in dry-run (set it in Settings)' }}</span>
    </p>
  </form>
  {% if convo %}<form class="inline" method="post" action="{{ url_for('assistant_clear') }}" onsubmit="return confirm('Clear the conversation?');"><button class="btn small danger" type="submit">Clear conversation</button></form>{% endif %}
</div>
<script>
(function(){
var form=document.getElementById('aform'), ta=document.getElementById('msg'),
    btn=document.getElementById('asend'), live=document.getElementById('live');
if(!(window.fetch && window.ReadableStream && window.TextDecoder)) return;

function esc(s){return (s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function linkifyText(s){ return esc(s).replace(/\[msg:(\d+)\]/g, '<a href="/messages/$1">[msg:$1]</a>'); }
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
    s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
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
function mk(tag, cls, text){var d=document.createElement(tag); if(cls) d.className=cls; if(text!=null) d.textContent=text; return d;}
function scrollDown(){ window.scrollTo(0, document.body.scrollHeight); }

form.addEventListener('submit', function(e){
  var text = ta.value.trim();
  if(!text){ e.preventDefault(); return; }
  e.preventDefault();
  run(text);
});
ta.addEventListener('keydown', function(e){
  if(e.key==='Enter' && (e.ctrlKey||e.metaKey)){ e.preventDefault(); form.requestSubmit(); }
});

function run(text){
  btn.disabled = true; ta.value = '';
  live.innerHTML = '';
  var ub = mk('div','msg ok'); ub.style.marginLeft='10%';
  ub.innerHTML = '<b>You:</b> ' + esc(text).replace(/\n/g,'<br>');
  live.appendChild(ub);

  var box = mk('div','msg'); box.style.background='var(--card2)'; box.style.border='1px solid var(--line)';
  var head = mk('div'); head.innerHTML = '<b>Assistant:</b> ';
  var phase = mk('span','sub'); head.appendChild(phase);
  var det = document.createElement('details'); det.className='sub'; det.open=true; det.style.display='none';
  var detSum = mk('summary','','thinking'); detSum.style.cursor='pointer'; det.appendChild(detSum);
  var pre = mk('pre','mono'); pre.style.whiteSpace='pre-wrap'; pre.style.fontSize='.8rem';
  pre.style.color='var(--dim)'; pre.style.margin='6px 0'; det.appendChild(pre);
  var toolsBox = mk('div'); var content = mk('div'); content.style.whiteSpace='pre-wrap';
  var propsBox = mk('div');
  box.appendChild(head); box.appendChild(det); box.appendChild(toolsBox);
  box.appendChild(content); box.appendChild(propsBox);
  live.appendChild(box);

  var t0 = Date.now(); var timer = null;
  function setPhase(label){
    phase.dataset.label = label;
    phase.textContent = ' ' + label + ' ' + Math.round((Date.now()-t0)/1000) + 's';
  }
  function stopTimer(){ if(timer){ clearInterval(timer); timer = null; } }
  timer = setInterval(function(){ if(phase.dataset.label && !phase.dataset.final) setPhase(phase.dataset.label); }, 500);
  setPhase('starting…');
  scrollDown();

  var cards = [];
  function toolCard(id, name, args){
    var c = mk('div','sub mono');
    c.style.fontSize='.82rem'; c.style.margin='4px 0'; c.style.padding='6px 8px';
    c.style.border='1px solid var(--line)'; c.style.borderRadius='8px';
    var a = '';
    try { a = JSON.stringify(args||{}); } catch(err) { a = ''; }
    if(a.length > 160) a = a.slice(0,160) + '…';
    c.textContent = '⏳ ' + name + '(' + a + ')';
    cards[id] = c;
    toolsBox.appendChild(c);
  }
  function toolDone(id, ok, summary, dry){
    var c = cards[id]; if(!c) return;
    var t = c.textContent.replace(/^[⏳✓✗]\s*/, '');
    c.textContent = (ok ? '✓ ' : '✗ ') + t + ' → ' + (dry ? '[dry-run] ' : '') + summary;
    c.style.color = ok ? 'var(--ok)' : 'var(--err)';
  }
  function addProposal(p, idx, msgId){
    var w = mk('div'); w.style.background='#0d1319'; w.style.border='1px solid var(--line)';
    w.style.borderRadius='9px'; w.style.padding='10px 12px'; w.style.margin='8px 0';
    var h = mk('div');
    h.appendChild(mk('b','',p.name));
    h.appendChild(document.createTextNode(' (' + (p.match_mode||'all') + ')'));
    w.appendChild(h);
    var conds = (p.conditions||[]).map(function(c){ return c.field + ' ' + c.op + ' "' + c.value + '"'; }).join((p.match_mode==='any') ? ' OR ' : ' AND ');
    w.appendChild(mk('div','mono', conds));
    var acts = [];
    if(p.actions && p.actions.move_to) acts.push('move → ' + p.actions.move_to);
    if(p.actions && p.actions.mark_read) acts.push('mark read');
    if(p.actions && p.actions.flag) acts.push('flag');
    w.appendChild(mk('div','sub', acts.join(', ') || '(none)'));
    if(p.rationale) w.appendChild(mk('div','sub', p.rationale));
    var row = mk('div','row'); row.style.marginTop='6px';
    var pair;
    for(pair of [[true,'Add rule'],[false,'Add (disabled)']]){
      var f = mk('form'); f.method='post'; f.action='/assistant/apply'; f.className='inline';
      var i1 = mk('input'); i1.type='hidden'; i1.name='msg_id'; i1.value=msgId; f.appendChild(i1);
      var i2 = mk('input'); i2.type='hidden'; i2.name='idx'; i2.value=idx; f.appendChild(i2);
      if(!pair[0]){ var i3 = mk('input'); i3.type='hidden'; i3.name='disabled'; i3.value='1'; f.appendChild(i3); }
      var bt = mk('button','',pair[1]); bt.type='submit'; bt.className='btn small' + (pair[0] ? ' primary' : '');
      f.appendChild(bt); row.appendChild(f);
    }
    w.appendChild(row);
    propsBox.appendChild(w);
  }
  var proposals = [], doneMsgId = null, finished = false;
  function finish(){
    if(finished) return; finished = true;
    stopTimer(); phase.dataset.final='1';
    phase.textContent = ' done in ' + Math.round((Date.now()-t0)/1000) + 's';
    if(content.textContent) content.innerHTML = mdRender(content.textContent);
    if(proposals.length && doneMsgId != null) proposals.forEach(function(p,i){ addProposal(p, i, doneMsgId); });
    btn.disabled = false; scrollDown();
  }
  function fail(msg){
    if(finished) return; finished = true;
    stopTimer(); phase.dataset.final='1'; phase.textContent = ' failed';
    box.appendChild(mk('div','msg err','Assistant failed: ' + msg));
    btn.disabled = false; scrollDown();
  }
  function handle(raw){
    var ev = null, data = '';
    raw.split('\n').forEach(function(line){
      if(line.indexOf('event:') === 0) ev = line.slice(6).trim();
      else if(line.indexOf('data:') === 0) data += line.slice(5).trim();
    });
    if(!ev) return;
    var d = {};
    if(data){ try { d = JSON.parse(data); } catch(err) { return; } }
    if(ev === 'reasoning'){ det.style.display=''; pre.textContent += (d.text||''); setPhase('thinking…'); }
    else if(ev === 'content'){ content.textContent += (d.text||''); setPhase('writing…'); }
    else if(ev === 'content_break'){ if(content.textContent) content.textContent += '\n\n'; }
    else if(ev === 'tool_start'){ setPhase('tool: ' + d.name + '…'); toolCard(d.id, d.name, d.args); }
    else if(ev === 'tool_end'){ toolDone(d.id, d.ok, d.summary, d.dry_run); setPhase('thinking…'); }
    else if(ev === 'proposals'){ proposals = d.proposals || []; }
    else if(ev === 'done'){
      doneMsgId = d.message_id;
      if(d.reply && !content.textContent) content.textContent = d.reply;
      finish();
    }
    else if(ev === 'error'){ fail(d.message || 'unknown error'); }
    scrollDown();
  }
  fetch('/assistant/stream', { method:'POST',
      headers: {'Content-Type':'application/x-www-form-urlencoded'},
      body: 'message=' + encodeURIComponent(text)
  }).then(function(resp){
    if(!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
    var rd = resp.body.getReader(); var dec = new TextDecoder(); var buf = '';
    function pump(){
      return rd.read().then(function(r){
        if(r.done){ if(!finished) fail('stream ended unexpectedly'); return; }
        buf += dec.decode(r.value, {stream:true});
        var i;
        while((i = buf.indexOf('\n\n')) >= 0){
          var raw = buf.slice(0, i);
          buf = buf.slice(i + 2);
          handle(raw);
        }
        return pump();
      });
    }
    return pump();
  }).catch(function(err){ fail('' + err); });
}
})();
</script>
"""


def _proposal_view(p):
    return {
        "name": p.get("name", ""),
        "match_mode": p.get("match_mode", "all"),
        "placement": p.get("placement", ""),
        "summary": summarize_conditions({"conditions": json.dumps(p.get("conditions", [])),
                                         "match_mode": p.get("match_mode", "all")}),
        "actions_summary": summarize_actions({"actions": json.dumps(p.get("actions", {}))}),
        "rationale": p.get("rationale", ""),
    }


@app.route("/assistant")
def assistant():
    convo = store.assistant_messages(limit=60)
    for m in convo:
        m["proposals_list"] = []
        if m.get("role") == "assistant" and m.get("proposals"):
            try:
                m["proposals_list"] = [_proposal_view(p) for p in json.loads(m["proposals"])]
            except (TypeError, ValueError):
                pass
        m["reasoning"] = ""
        m["tool_steps"] = []
        m["when"] = fmt_ts(m.get("ts"))
        if m.get("role") == "assistant" and m.get("meta"):
            try:
                meta = json.loads(m["meta"])
                m["reasoning"] = meta.get("reasoning") or ""
                m["tool_steps"] = meta.get("tools") or []
            except (TypeError, ValueError):
                pass
    return render(render_template_string(
        ASSISTANT_TMPL, convo=convo, cfg=config,
        actions_live=bool(store.get_setting("assistant_actions_apply", True))))


def _sse(event, data):
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))


@app.route("/assistant/stream", methods=["POST"])
def assistant_stream():
    """SSE stream of one assistant turn: reasoning/content deltas, tool
    start/end events, rule proposals, done/error."""
    text = (request.form.get("message") or "").strip()

    def gen():
        if not text:
            yield _sse("error", {"message": "empty message"})
            return
        agent = engine.AssistantAgent()
        try:
            for ev in agent.stream(text):
                etype = ev.pop("type")
                yield _sse(etype, ev)
        except Exception as exc:  # agent.stream handles its own errors; belt & braces
            try:
                store.log_event("error", "assistant stream failed: %r" % exc)
            except Exception:
                pass
            yield _sse("error", {"message": "assistant failed: %r" % exc})
        finally:
            agent.close()

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/assistant/send", methods=["POST"])
def assistant_send():
    text = (request.form.get("message") or "").strip()
    if not text:
        flash("Type a message first.", "err")
        return redirect(url_for("assistant"))
    try:
        engine.assistant_respond(text)
    except Exception as exc:
        flash("Assistant error: %r" % exc, "err")
    return redirect(url_for("assistant"))


@app.route("/assistant/apply", methods=["POST"])
def assistant_apply():
    try:
        mid = int(request.form.get("msg_id") or 0)
        idx = int(request.form.get("idx") or 0)
    except ValueError:
        mid, idx = 0, 0
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
        return redirect(url_for("assistant"))
    norm = engine.normalize_rule(proposals[idx]) or proposals[idx]
    store.add_rule(norm.get("name", "Assistant rule"), norm.get("match_mode", "all"),
                   norm.get("conditions", []), norm.get("actions", {}), enabled=not disabled,
                   position=norm.get("placement") or "bottom")
    store.log_event("info", "assistant rule '%s' added (%s)"
                    % (norm.get("name"), "disabled" if disabled else "enabled"))
    flash("Rule '%s' added%s - check it on the Rules page (the Test button dry-runs it against recent mail)."
          % (norm.get("name"), " (disabled)" if disabled else ""), "ok")
    return redirect(url_for("assistant"))


@app.route("/assistant/clear", methods=["POST"])
def assistant_clear():
    store.clear_assistant()
    return redirect(url_for("assistant"))


# ---------------------------------------------------------------- settings

SETTINGS_TMPL = """
<h2>Settings</h2>
<form method="post" class="card">
  <div class="grid2">
    <div><label>Check interval (seconds)</label><input type="number" name="poll_interval" value="{{ s.poll_interval }}" min="15"></div>
    <div><label>First-run lookback (hours)</label><input type="number" name="lookback_hours" value="{{ s.lookback_hours }}" min="1"></div>
  </div>
  <div class="grid2">
    <div><label>Watched folders (comma separated)</label><input type="text" name="watch_folders" value="{{ s.watch_folders|join(', ') }}"></div>
    <div><label>Your name (for drafts)</label><input type="text" name="my_name" value="{{ s.my_name }}"></div>
  </div>
  <div class="grid2">
    <div><label>Classify concurrency <span class="sub">(parallel LLM requests, 1-12)</span></label><input type="number" name="classify_concurrency" value="{{ s.classify_concurrency }}" min="1" max="12"></div>
  </div>
  <label class="row" style="color:var(--fg)"><input type="checkbox" name="rules_apply" value="1" style="width:auto;margin-right:8px"
    {{ 'checked' if s.rules_apply else '' }}> Apply rule actions for real (uncheck = dry-run, suggests only)</label>
  <label class="row" style="color:var(--fg)"><input type="checkbox" name="llm_suggest" value="1" style="width:auto;margin-right:8px"
    {{ 'checked' if s.llm_suggest else '' }}> Classify unmatched mail with the LLM</label>
  <label class="row" style="color:var(--fg)"><input type="checkbox" name="llm_apply" value="1" style="width:auto;margin-right:8px"
    {{ 'checked' if s.llm_apply else '' }}> Auto-file mail by LLM category (uses the folder map below)</label>
  <label class="row" style="color:var(--fg)"><input type="checkbox" name="assistant_actions_apply" value="1" style="width:auto;margin-right:8px"
    {{ 'checked' if s.assistant_actions_apply else '' }}> Assistant may act on mail (create folders, move, flag) — uncheck = dry-run</label>
  <label class="row" style="color:var(--fg)"><input type="checkbox" name="index_enabled" value="1" style="width:auto;margin-right:8px"
    {{ 'checked' if s.index_enabled else '' }}> Build the semantic search index (local embeddings on GPU 1)</label>
  <label class="row" style="color:var(--fg)"><input type="checkbox" name="rerank_enabled" value="1" style="width:auto;margin-right:8px"
    {{ 'checked' if s.rerank_enabled else '' }}> Rerank search results with the cross-encoder (better precision, slightly slower)</label>
  <label>Indexed folders <span class="sub">(comma separated; blank = all except Junk / Deleted / Trash / system folders)</span></label>
  <input type="text" name="index_folders" value="{{ s.index_folders|join(', ') }}">
  <div class="grid2">
    <div><label>Max LLM calls per hour</label><input type="number" name="max_llm_per_hour" value="{{ s.max_llm_per_hour }}" min="0"></div>
    <div><label>LLM classifications per check</label><input type="number" name="llm_batch_per_cycle" value="{{ s.llm_batch_per_cycle }}" min="1"></div>
  </div>
  <label>Categories (comma separated)</label>
  <input type="text" name="categories" value="{{ s.categories|join(', ') }}">
  <label>Category → folder map <span class="sub">(one per line, "Category = Folder"; blank folder = keep in inbox)</span></label>
  <textarea name="category_folders" rows="6" style="min-height:100px">{% for k, v in s.category_folders.items() %}{{ k }} = {{ v }}
{% endfor %}</textarea>
  <div class="grid2">
    <div><label>Drafts folder <span class="sub">(blank = auto-detect)</span></label><input type="text" name="drafts_folder" value="{{ s.drafts_folder }}"></div>
  </div>
  <p style="margin-top:14px"><button class="btn primary" type="submit">Save settings</button></p>
</form>

<div class="card">
  <h3>Connection &amp; model</h3>
  <div class="sub mono">
    IMAP: {{ cfg.IMAP_USER }} @ {{ cfg.IMAP_HOST }}:{{ cfg.IMAP_PORT }} (via email-oauth2-proxy) ·
    LLM: {{ cfg.LLM_BASE_URL }} · model {{ cfg.LLM_MODEL }} · key {{ 'set' if cfg.LLM_API_KEY else 'MISSING' }}{% if cfg.LLM_FALLBACK_BASE_URL %} · fallback: {{ cfg.LLM_FALLBACK_MODEL }}{% endif %} ·
    state: {{ engine_state }} · db: {{ cfg.DB_PATH }}
  </div>
  <p><form class="inline" method="post" action="{{ url_for('settings_test_llm') }}"><button class="btn" type="submit">Test LLM endpoint</button></form> <span class="sub">tests the primary endpoint (fallback engages automatically at runtime if it fails)</span></p>
  <p class="sub" style="margin-bottom:0">Connection details and keys live in the container env file
  (<code>~/.hermes</code>-style secrets stay out of the database); everything on this page is stored in SQLite.</p>
</div>
"""


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        try:
            store.set_setting("poll_interval", max(15, int(request.form.get("poll_interval", 90))))
        except ValueError:
            pass
        try:
            store.set_setting("classify_concurrency",
                              max(1, min(12, int(request.form.get("classify_concurrency", 6)))))
        except ValueError:
            pass
        try:
            store.set_setting("lookback_hours", max(1, int(request.form.get("lookback_hours", 48))))
        except ValueError:
            pass
        store.set_setting("watch_folders",
                          [f.strip() for f in (request.form.get("watch_folders") or "INBOX").split(",") if f.strip()])
        store.set_setting("my_name", (request.form.get("my_name") or "Sean").strip())
        store.set_setting("rules_apply", bool(request.form.get("rules_apply")))
        store.set_setting("llm_suggest", bool(request.form.get("llm_suggest")))
        store.set_setting("llm_apply", bool(request.form.get("llm_apply")))
        store.set_setting("assistant_actions_apply", bool(request.form.get("assistant_actions_apply")))
        store.set_setting("index_enabled", bool(request.form.get("index_enabled")))
        store.set_setting("rerank_enabled", bool(request.form.get("rerank_enabled")))
        store.set_setting("index_folders",
                          [f.strip() for f in (request.form.get("index_folders") or "").split(",") if f.strip()])
        try:
            store.set_setting("max_llm_per_hour", max(0, int(request.form.get("max_llm_per_hour", 40))))
        except ValueError:
            pass
        try:
            store.set_setting("llm_batch_per_cycle", max(1, int(request.form.get("llm_batch_per_cycle", 5))))
        except ValueError:
            pass
        store.set_setting("categories",
                          [c.strip() for c in (request.form.get("categories") or "").split(",") if c.strip()])
        mapping = {}
        for line in (request.form.get("category_folders") or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    mapping[k.strip()] = v.strip()
        store.set_setting("category_folders", mapping)
        store.set_setting("drafts_folder", (request.form.get("drafts_folder") or "").strip())
        flash("Settings saved.", "ok")
        return redirect(url_for("settings"))
    return render(render_template_string(SETTINGS_TMPL, s=store.all_settings(),
                                         engine_state=worker.state, cfg=config))


@app.route("/settings/test-llm", methods=["POST"])
def settings_test_llm():
    started = time.time()
    try:
        client = engine.LLMClient()
        out = client._chat_once(client.base, client.key, client.model,
                                "You are a connectivity test. Reply with the single word ok.",
                                [{"role": "user", "content": "Reply with the single word ok."}],
                                json_mode=False)
        flash("LLM OK in %.1fs - model %s - replied: %s"
              % (time.time() - started, client.model, (out or "").strip()[:60]), "ok")
    except Exception as exc:
        flash("LLM FAILED after %.1fs: %r - is the local model running? "
              "(cd gemma && docker compose ps)" % (time.time() - started, exc), "err")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------- log

LOG_TMPL = """
<h2>Activity log</h2>
<p class="sub">{% if show_debug %}<a href="{{ url_for('log') }}">hide debug lines</a>{% else %}<a href="{{ url_for('log', debug='1') }}">show debug lines</a>{% endif %}</p>
<div class="card">
  {% for e in events %}
  <div class="sub"><span class="mono">{{ e.when }}</span> <span class="badge {{ e.cls }}">{{ e.level }}</span> {{ e.message }}</div>
  {% else %}<div class="sub">No events yet.</div>{% endfor %}
</div>
"""


@app.route("/log")
def log():
    show_debug = request.args.get("debug") == "1"
    events = store.recent_events(1000)
    if not show_debug:
        events = [e for e in events if e.get("level") != "debug"]
    events = events[:300]
    for e in events:
        e["when"] = fmt_ts(e["ts"])
        e["cls"] = {"error": "err", "info": "ok", "debug": ""}.get(e.get("level"), "")
    return render(render_template_string(LOG_TMPL, events=events, show_debug=show_debug))


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "worker": worker.state.get("last_ok"), "error": worker.state.get("last_error")})


if __name__ == "__main__":
    store.init_db()
    if "--check" in sys.argv:
        print(json.dumps(engine.connectivity_check(), indent=1))
        sys.exit(0)
    if "--index" in sys.argv or "--reindex" in sys.argv:
        if "--reindex" in sys.argv:
            rag.rebuild()
            print("index cleared (rebuild)", flush=True)
        tries, last_remaining, stall = 0, None, 0
        while True:
            try:
                res = rag.index_pass(limit=40)
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
    worker.start()
    indexer.start()
    classifier.start()
    app.run(host=config.UI_HOST, port=config.UI_PORT, threaded=True)
