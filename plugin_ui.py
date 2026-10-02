"""Plugin UI — composed controller sessions, validated component trees, trusted
browser-view approvals, and bounded per-view state.

Two tiers (docs/plugin-pages.md):

- **composed** (default, sandboxed): no plugin browser JS runs. The plugin's
  sandbox bundle exports synchronous `uiOpen` / `uiDispatch` / `uiClose`; the
  host owns the bounded controller state, the event ids bound to the rendered
  tree, and the revision/generation. Trees are validated here and rendered by
  `render_tree` with escaping + a strict whitelist, so a malicious/compromised
  backend cannot inject markup, URLs or handlers.
- **trusted** (browser bundle, opaque-origin iframe): must be explicitly
  approved by the user, bound to the exact manifest/backend/frontend digests and
  version. It is disclosed that a page which receives data can transmit it by
  navigating itself (an inherent browser capability, not mediated by net.http).

This module is deliberately Flask-free; routes live in app.py.
"""
from __future__ import annotations

import hashlib
import html
import json
import secrets
import threading
import time

import plugins as kernel
import store

# ---------------------------------------------------------------- limits

MAX_TREE_NODES = 400
MAX_TREE_DEPTH = 12
MAX_TEXT = 4000
MAX_ARRAY = 200
MAX_STATE_BYTES = 16384
SESSION_TTL = 1800
SESSION_MAX = 200
RATE_WINDOW = 10.0          # seconds
RATE_MAX_CALLS = 40         # per plugin within the window
RATE_MAX_EVENTS = 30
MAX_UI_CONCURRENCY = 3      # per plugin
MAX_GLOBAL_UI_CONCURRENCY = 8


class UiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- component tree

_TEXT_VARIANTS = ("body", "title", "caption", "mono")
_STATE_KINDS = ("loading", "empty", "error", "denied")

# name -> (needs_children, allowed_props, is_event_source, special)
_TYPES = {
    "Stack":       (True, {"direction": ("column", "row"), "gap": ("none", "sm", "md")}, False, None),
    "Grid":        (True, {"cols": ("1", "2", "3")}, False, None),
    "SplitPane":   (True, {}, False, "split"),
    "Tabs":        (True, {"active": "id"}, False, "tabs"),
    "Tab":         (True, {"label": "text"}, False, None),
    "Text":        (False, {"value": "text", "variant": _TEXT_VARIANTS}, False, None),
    "Button":      (False, {"label": "text", "variant": ("default", "primary"), "disabled": "bool"}, True, None),
    "Input":       (False, {"name": "id", "value": "text", "placeholder": "text", "label": "text"}, True, "input"),
    "Menu":        (True, {}, False, None),
    "MenuItem":    (False, {"label": "text", "disabled": "bool"}, True, None),
    "Dialog":      (True, {"title": "text", "open": "bool"}, False, None),
    "List":        (True, {}, False, None),
    "ListItem":    (True, {"selected": "bool"}, True, None),
    "Separator":   (False, {}, False, None),
    "Badge":       (False, {"label": "text", "variant": ("default", "warn", "ok", "err")}, False, None),
    "StateView":   (False, {"kind": _STATE_KINDS, "message": "text", "actionLabel": "text"}, False, "state"),
    "MessageList": (False, {"selected": "int"}, False, "msglist"),
    "MessageReader": (False, {}, False, "reader"),
    "SearchField": (False, {"value": "text", "placeholder": "text", "label": "text"}, True, "search"),
}

_EVENT_KEYS = ("event", "retryEvent")
_ALLOWED_NODE_KEYS = {"type", "props", "children", "event", "retryEvent", "items", "message"}
_MSG_PROPS = ("id", "folder", "uid", "subject", "from", "to", "date", "snippet",
              "body_text", "category", "tags", "needs_reply")


def _scalar_ok(kind, value):
    if kind == "text":
        return isinstance(value, str) and len(value) <= MAX_TEXT
    if kind == "id":
        return isinstance(value, str) and 1 <= len(value) <= 64 and \
            all(c.isalnum() or c in "-_:" for c in value)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(kind, tuple):
        return isinstance(value, str) and value in kind
    return False


def _msg_obj(node, name):
    m = node.get(name)
    if not isinstance(m, dict):
        raise UiError("invalid_tree", "%s.message must be an object" % node["type"])
    out = {}
    for k in _MSG_PROPS:
        if k not in m:
            continue
        v = m[k]
        if k in ("id", "uid", "selected"):
            if isinstance(v, int) and not isinstance(v, bool):
                out[k] = v
        elif k == "needs_reply":
            out[k] = bool(v)
        elif k == "tags":
            if isinstance(v, list):
                out[k] = [str(t)[:60] for t in v[:20]]
        else:
            out[k] = str(v)[:MAX_TEXT]
    return out


def validate_tree(node, depth=0, counter=None):
    """Return a normalized tree copy, or raise UiError. Strict whitelist."""
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_TREE_NODES:
        raise UiError("invalid_tree", "component tree exceeds %d nodes" % MAX_TREE_NODES)
    if depth > MAX_TREE_DEPTH:
        raise UiError("invalid_tree", "component tree exceeds depth %d" % MAX_TREE_DEPTH)
    if not isinstance(node, dict):
        raise UiError("invalid_tree", "each node must be an object")
    t = node.get("type")
    if t not in _TYPES:
        raise UiError("invalid_tree", "unknown component type %r" % (t,))
    extra_keys = set(node) - _ALLOWED_NODE_KEYS
    if extra_keys:
        raise UiError("invalid_tree", "unsupported node key(s): %s"
                      % ", ".join(sorted(str(k) for k in extra_keys)[:5]))
    needs_children, props, is_event, special = _TYPES[t]
    out = {"type": t}
    # props: only whitelisted keys, validated
    raw_props = node.get("props") or {}
    if not isinstance(raw_props, dict):
        raise UiError("invalid_tree", "%s.props must be an object" % t)
    clean = {}
    for k, v in raw_props.items():
        if k not in props:
            raise UiError("invalid_tree", "%s has unsupported prop %r" % (t, k))
        if not _scalar_ok(props[k], v):
            raise UiError("invalid_tree", "%s.%s has an invalid value" % (t, k))
        clean[k] = v
    if clean:
        out["props"] = clean
    # event binding
    ev = node.get("event")
    if is_event:
        if not isinstance(ev, str) or not (1 <= len(ev) <= 64) or \
                not all(c.isalnum() or c in "-_.:" for c in ev):
            raise UiError("invalid_tree", "%s requires a safe 'event' id" % t)
        out["event"] = ev
    elif ev is not None:
        if not isinstance(ev, str) or not (1 <= len(ev) <= 64) or \
                not all(c.isalnum() or c in "-_.:" for c in ev):
            raise UiError("invalid_tree", "%s.event must be a safe short id" % t)
        out["event"] = ev
    # children
    kids = node.get("children")
    if kids is not None:
        if not isinstance(kids, list):
            raise UiError("invalid_tree", "%s.children must be an array" % t)
        if len(kids) > MAX_ARRAY:
            raise UiError("invalid_tree", "too many children")
        if not needs_children and kids:
            raise UiError("invalid_tree", "%s cannot have children" % t)
        out["children"] = [validate_tree(k, depth + 1, counter) for k in kids]
    elif needs_children and special not in ("msglist", "reader", "state", "search", "input"):
        out["children"] = []
    # special payloads
    if special == "split":
        if len(out.get("children") or []) != 2:
            raise UiError("invalid_tree", "SplitPane needs exactly two children")
    elif special == "tabs":
        active = out.get("props", {}).get("active", "")
        tab_ids = []
        for child in out.get("children") or []:
            if child["type"] != "Tab":
                raise UiError("invalid_tree", "Tabs children must be Tab")
            tab_ids.append(child["props"]["label"] if "label" in child.get("props", {}) else "")
        if active and active not in tab_ids:
            # tolerate: fall back to the first tab rather than reject the whole tree
            out["props"]["active"] = tab_ids[0] if tab_ids else ""
    elif special == "state":
        retry = node.get("retryEvent")
        if retry is not None:
            if not isinstance(retry, str) or not (1 <= len(retry) <= 64):
                raise UiError("invalid_tree", "StateView.retryEvent must be a short id")
            out["retryEvent"] = retry
    elif special == "msglist":
        items = node.get("items")
        if items is None:
            items = []
        if not isinstance(items, list) or len(items) > MAX_ARRAY:
            raise UiError("invalid_tree", "MessageList.items must be an array <= %d" % MAX_ARRAY)
        out["items"] = [_msg_obj({"type": t, "message": it}, "message") for it in items]
        if "selected" in out.get("props", {}) and out["props"]["selected"] < 0:
            raise UiError("invalid_tree", "MessageList.selected must be >= 0")
    elif special == "reader":
        out["message"] = _msg_obj(node, "message") if node.get("message") else None
    return out


def _events_from(tree, into):
    if not isinstance(tree, dict):
        return
    t = tree.get("type")
    needs_children, _props, is_event, special = _TYPES[t]
    ev = tree.get("event") or tree.get("retryEvent")
    if ev:
        values = None
        if special == "msglist":
            values = set(str(it.get("id")) for it in (tree.get("items") or []))
        into[ev] = {"values": values}
    for child in tree.get("children") or []:
        _events_from(child, into)


def tree_events(tree):
    """Map of event id -> {"values": set|None} for binding dispatch to the tree."""
    out = {}
    _events_from(tree, out)
    return out


# ---------------------------------------------------------------- rendering

def _e(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def render_tree(node):
    t = node["type"]
    props = node.get("props") or {}
    ev = node.get("event")
    ev_attr = (' data-mt-event="%s"' % _e(ev)) if ev else ""
    kids = node.get("children")
    inner = "".join(render_tree(k) for k in (kids or []))
    if t == "Stack":
        d = props.get("direction", "column")
        return '<div class="mtc-stack mtc-%s mtc-gap-%s">%s</div>' % (d, props.get("gap", "sm"), inner)
    if t == "Grid":
        return '<div class="mtc-grid mtc-cols-%s">%s</div>' % (props.get("cols", "2"), inner)
    if t == "SplitPane":
        kk = kids or []
        return ('<div class="mtc-split mtc-view-list"><div class="mtc-pane-list">%s</div>'
                '<div class="mtc-pane-reader">%s</div></div>'
                % (render_tree(kk[0]), render_tree(kk[1])))
    if t == "Tabs":
        active = props.get("active", "")
        tabs = kids or []
        ids = [k.get("props", {}).get("label", "") for k in tabs]
        if active not in ids and ids:
            active = ids[0]
        bar = []
        panels = []
        for i, k in enumerate(tabs):
            label = k.get("props", {}).get("label", "")
            lab = ('<button type="button" class="mtc-tab%s" data-mt-tab="%d">%s</button>'
                   % (" on" if label == active else "", i, _e(label)))
            bar.append(lab)
            panels.append('<div class="mtc-tabpanel"%s>%s</div>'
                          % ("" if label == active else ' hidden', render_tree(k)))
        return '<div class="mtc-tabs"><div class="mtc-tabbar" role="tablist">%s</div>%s</div>' \
            % ("".join(bar), "".join(panels))
    if t == "Tab":
        return inner
    if t == "Text":
        return '<div class="mtc-text mtc-%s">%s</div>' % (
            props.get("variant", "body"), _e(props.get("value", "")))
    if t == "Badge":
        return '<span class="mtc-badge mtc-%s">%s</span>' % (
            props.get("variant", "default"), _e(props.get("label", "")))
    if t == "Separator":
        return '<hr class="mtc-sep">'
    if t == "Button":
        return ('<button type="button" class="mtc-btn mtc-%s"%s%s>%s</button>'
                % (props.get("variant", "default"), ev_attr,
                   " disabled" if props.get("disabled") else "", _e(props.get("label", ""))))
    if t == "MenuItem":
        return ('<button type="button" class="mtc-menu-item"%s%s>%s</button>'
                % (ev_attr, " disabled" if props.get("disabled") else "",
                   _e(props.get("label", ""))))
    if t == "Menu":
        return '<div class="mtc-menu" role="menu">%s</div>' % inner
    if t == "Dialog":
        if props.get("open") is False:
            return ""
        return ('<div class="mtc-dialog" role="dialog" aria-label="%s">'
                '<div class="mtc-dialog-title">%s</div>%s</div>'
                % (_e(props.get("title", "")), _e(props.get("title", "")), inner))
    if t == "List":
        return '<div class="mtc-list" role="list">%s</div>' % inner
    if t == "ListItem":
        return ('<div class="mtc-list-item%s"%s tabindex="0" role="listitem">%s</div>'
                % (" on" if props.get("selected") else "", ev_attr, inner))
    if t == "Input":
        name = props.get("name", "value")
        return ('<label class="mtc-field"><span class="mtc-label">%s</span>'
                '<input type="text" name="%s" value="%s" placeholder="%s"%s></label>'
                % (_e(props.get("label", "")), _e(name), _e(props.get("value", "")),
                   _e(props.get("placeholder", "")), ev_attr))
    if t == "SearchField":
        return ('<label class="mtc-field mtc-search"><span class="mtc-sr">%s</span>'
                '<input type="search" value="%s" placeholder="%s"%s></label>'
                % (_e(props.get("label", "Search")), _e(props.get("value", "")),
                   _e(props.get("placeholder", "Search")), ev_attr))
    if t == "StateView":
        kind = props.get("kind", "empty")
        retry = node.get("retryEvent")
        btn = ('<button type="button" class="mtc-btn"%s>%s</button>'
               % ((' data-mt-event="%s"' % _e(retry)) if retry else "",
                  _e(props.get("actionLabel", "Retry")))) if retry else ""
        return ('<div class="mtc-state mtc-%s" role="%s">%s%s</div>'
                % (kind, "alert" if kind == "error" else "status",
                   _e(props.get("message", "")), btn))
    if t == "MessageList":
        rows = []
        selected = props.get("selected")
        for it in (node.get("items") or []):
            on = " on" if selected is not None and it.get("id") == selected else ""
            meta = []
            if it.get("category"):
                meta.append('<span class="mtc-badge">%s</span>' % _e(it["category"]))
            if it.get("needs_reply"):
                meta.append('<span class="mtc-badge mtc-warn">needs reply</span>')
            for tg in (it.get("tags") or []):
                meta.append('<span class="mtc-badge">#%s</span>' % _e(tg))
            rows.append(
                '<button type="button" class="mtc-msg%s" data-mt-event="%s" data-mt-value="%s">'
                '<span class="mtc-msg-top"><b>%s</b><span class="mtc-dim">%s</span></span>'
                '<span class="mtc-msg-sub">%s</span><span class="mtc-msg-snip">%s</span>%s</button>'
                % (on, _e(ev), _e(it.get("id")), _e(it.get("from", "")), _e(it.get("date", "")),
                   _e(it.get("subject", "")), _e(it.get("snippet", "")), "".join(meta)))
        return '<div class="mtc-msgs" role="listbox">%s</div>' % "".join(rows)
    if t == "MessageReader":
        m = node.get("message")
        if not m:
            return '<div class="mtc-state mtc-empty">Select a message to read.</div>'
        chips = []
        if m.get("category"):
            chips.append('<span class="mtc-badge">%s</span>' % _e(m["category"]))
        if m.get("needs_reply"):
            chips.append('<span class="mtc-badge mtc-warn">needs reply</span>')
        for tg in (m.get("tags") or []):
            chips.append('<span class="mtc-badge">#%s</span>' % _e(tg))
        return ('<article class="mtc-reader"><h2 class="mtc-reader-sub">%s</h2>'
                '<div class="mtc-dim">From: %s</div><div class="mtc-dim">To: %s</div>'
                '<div class="mtc-dim">Date: %s</div><div class="mtc-chips">%s</div>'
                '<pre class="mtc-reader-body">%s</pre>'
                '<div class="mtc-hint">Local-index text only - attachments and rich '
                'mail HTML are not shown.</div></article>'
                % (_e(m.get("subject", "")), _e(m.get("from", "")), _e(m.get("to", "")),
                   _e(m.get("date", "")), "".join(chips), _e(m.get("body_text", ""))))
    return ""


# ---------------------------------------------------------------- sessions

_sessions = {}
_session_locks = {}
_lock = threading.Lock()


def session_lock(sid):
    """Per-view serialization: one dispatch in flight per session at a time."""
    with _lock:
        lk = _session_locks.get(sid)
        if lk is None:
            lk = threading.Lock()
            _session_locks[sid] = lk
        return lk
_rate = {}          # plugin_id -> [timestamps]
_conc = {}          # plugin_id -> int
_conc_global = [0]


def _now():
    return time.time()


def _prune_locked(now):
    for k in [k for k, v in _sessions.items()
              if v.get("disposed") or now - v["last"] > SESSION_TTL]:
        _sessions.pop(k, None)
        _session_locks.pop(k, None)
    if len(_sessions) >= SESSION_MAX:
        victim = min(_sessions, key=lambda k: _sessions[k]["last"])
        _sessions.pop(victim, None)
        _session_locks.pop(victim, None)


def open_session(pid, page, mode, generation=0):
    sid = secrets.token_urlsafe(24)
    now = _now()
    with _lock:
        _prune_locked(now)
        _sessions[sid] = {"pid": pid, "page": page, "mode": mode, "state": {},
                          "revision": 0, "generation": generation, "events": {},
                          "tree": None, "created": now, "last": now, "disposed": False}
    return sid


def view_id(sid):
    """Short, non-secret view id for audit lines (never the session token)."""
    if not isinstance(sid, str) or len(sid) < 8:
        return "view-unknown"
    return "view-" + sid[:8]


def get_session(sid, pid=None, page=None):
    if not isinstance(sid, str) or len(sid) < 16:
        return None
    now = _now()
    with _lock:
        s = _sessions.get(sid)
        if not s or s.get("disposed") or now - s["last"] > SESSION_TTL:
            if s is not None:
                _sessions.pop(sid, None)
            return None
        if pid is not None and s["pid"] != pid:
            return None
        if page is not None and s["page"] != page:
            return None
        s["last"] = now
        return s


def set_tree(sid, tree, events, state=None):
    with _lock:
        s = _sessions.get(sid)
        if s:
            s["tree"] = tree
            s["events"] = events
            if state is not None:
                s["state"] = state


def bump_revision(sid):
    with _lock:
        s = _sessions.get(sid)
        if s:
            s["revision"] += 1
            return s["revision"]
    return 0


def dispose(sid):
    with _lock:
        s = _sessions.get(sid)
        if s:
            s["disposed"] = True
            s["tree"] = None
            s["state"] = {}
        _session_locks.pop(sid, None)
    return True


def drop_plugin(pid):
    with _lock:
        for k in [k for k, v in _sessions.items() if v["pid"] == pid]:
            _sessions.pop(k, None)
            _session_locks.pop(k, None)


def live_count():
    with _lock:
        return len(_sessions)


def touch_plugin(pid):
    """Drop every session whose plugin is no longer enabled or whose approval
    no longer matches current content. Called by the status poll + list/render."""
    row = kernel.get(pid)
    keep = bool(row and row.get("enabled"))
    if keep and kernel.ui_mode(row) == "trusted":
        keep = is_approved(row)
    if not keep:
        drop_plugin(pid)
    return keep


# ---------------------------------------------------------------- rate / concurrency

def rate_ok(pid, kind="call"):
    limit = RATE_MAX_EVENTS if kind == "event" else RATE_MAX_CALLS
    now = _now()
    with _lock:
        bucket = _rate.setdefault(pid, [])
        bucket[:] = [t for t in bucket if now - t < RATE_WINDOW]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


class _Slot:
    """Small per-plugin + global in-flight limiter (non-blocking acquire)."""

    def __init__(self, pid):
        self.pid = pid
        self.got = False

    def __enter__(self):
        with _lock:
            if _conc_global[0] >= MAX_GLOBAL_UI_CONCURRENCY or \
                    _conc.get(self.pid, 0) >= MAX_UI_CONCURRENCY:
                return self
            _conc_global[0] += 1
            _conc[self.pid] = _conc.get(self.pid, 0) + 1
            self.got = True
        return self

    def __exit__(self, *a):
        if self.got:
            with _lock:
                _conc_global[0] = max(0, _conc_global[0] - 1)
                _conc[self.pid] = max(0, _conc.get(self.pid, 0) - 1)
        return False


def slot(pid):
    return _Slot(pid)


# ---------------------------------------------------------------- trusted approvals

def content_digest(row):
    """Current on-disk content digest (manifest + backend + trusted frontend)."""
    return kernel.current_content_digest(row)


def approval(pid):
    rec = store.get_setting("plugin_ui_trust:" + pid, {}) or {}
    return rec if isinstance(rec, dict) else {}


def is_approved(row):
    if not row or kernel.ui_mode(row) != "trusted":
        return False
    rec = approval(row["id"])
    return (rec.get("version") == row.get("version")
            and rec.get("content_digest") == content_digest(row))


def approve(pid, row):
    rec = {"version": row.get("version"), "content_digest": content_digest(row),
           "approved_at": int(_now())}
    store.set_setting("plugin_ui_trust:" + pid, rec)
    store.log_event("warn", "plugin '%s' browser UI approved (trusted egress disclosed)" % pid)
    drop_plugin(pid)
    return rec


def revoke(pid):
    store.set_setting("plugin_ui_trust:" + pid, {})
    store.log_event("info", "plugin '%s' browser UI approval revoked" % pid)
    drop_plugin(pid)
    return True


# ---------------------------------------------------------------- bounded state

def validate_state(state):
    try:
        blob = json.dumps(state if state is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        raise UiError("invalid_state", "controller state is not JSON")
    if len(blob.encode("utf-8")) > MAX_STATE_BYTES:
        raise UiError("invalid_state", "controller state exceeds %d bytes" % MAX_STATE_BYTES)
    if len(blob) > 0 and not isinstance(json.loads(blob), dict):
        raise UiError("invalid_state", "controller state must be a JSON object")
    return json.loads(blob)
