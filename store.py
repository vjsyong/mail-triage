"""SQLite storage for Mail Triage: settings, rules, templates, messages, events."""
import json
import os
import sqlite3
import time

import config

DEFAULT_SETTINGS = {
    "poll_interval": 90,          # seconds between mailbox checks
    "lookback_hours": 48,         # first-run window (and re-scan window when index is empty)
    "watch_folders": ["INBOX"],
    "rules_apply": True,          # run rule actions (move/flag/read) for real
    "llm_suggest": True,          # classify unmatched mail with the LLM
    "llm_apply": False,           # act on LLM category -> folder mapping (off until trusted)
    "max_llm_per_hour": 40,
    "llm_batch_per_cycle": 5,
    "categories": ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"],
    "category_folders": {
        "Notification": "Notifications",
        "Newsletter": "Newsletters",
        "Receipt": "Receipts",
        "Promo": "Promotions",
    },
    "drafts_folder": "",          # blank = auto-detect the \Drafts special-use folder
    "my_name": "Sean",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL DEFAULT '',
    match_mode TEXT NOT NULL DEFAULT 'all',
    conditions TEXT NOT NULL DEFAULT '[]',
    actions TEXT NOT NULL DEFAULT '{}',
    created INTEGER, updated INTEGER
);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    created INTEGER, updated INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    uid INTEGER NOT NULL,
    uidvalidity INTEGER NOT NULL DEFAULT 0,
    msgid TEXT DEFAULT '',
    from_addr TEXT DEFAULT '',
    to_addr TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    date TEXT DEFAULT '',
    snippet TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    rule_id INTEGER DEFAULT NULL,
    action_taken TEXT DEFAULT '',
    llm_category TEXT DEFAULT '',
    llm_confidence REAL DEFAULT NULL,
    llm_summary TEXT DEFAULT '',
    llm_needs_reply INTEGER DEFAULT NULL,
    llm_suggested_folder TEXT DEFAULT '',
    processed_at INTEGER,
    UNIQUE (folder, uid, uidvalidity)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS llm_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, msg_id INTEGER, ok INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS assistant_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, role TEXT NOT NULL DEFAULT 'user',
    content TEXT NOT NULL DEFAULT '', proposals TEXT NOT NULL DEFAULT '[]'
);
"""


def db():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(_SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (k, v) VALUES (?, ?)",
                         (k, json.dumps(v)))


# ---------------------------------------------------------------- settings

def get_setting(k, default=None):
    with db() as conn:
        row = conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(k, default)
    try:
        return json.loads(row["v"])
    except (TypeError, ValueError):
        return row["v"]


def set_setting(k, v):
    with db() as conn:
        conn.execute("INSERT INTO settings (k, v) VALUES (?, ?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def all_settings():
    out = dict(DEFAULT_SETTINGS)
    with db() as conn:
        for row in conn.execute("SELECT k, v FROM settings"):
            try:
                out[row["k"]] = json.loads(row["v"])
            except (TypeError, ValueError):
                out[row["k"]] = row["v"]
    return out


# ---------------------------------------------------------------- rules

def list_rules(enabled_only=False):
    q = "SELECT * FROM rules"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY position, id"
    with db() as conn:
        return [dict(r) for r in conn.execute(q)]


def get_rule(rule_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
    return dict(row) if row else None


def add_rule(name, match_mode, conditions, actions, enabled=True):
    now = int(time.time())
    with db() as conn:
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM rules").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO rules (position, enabled, name, match_mode, conditions, actions, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pos, 1 if enabled else 0, name, match_mode,
             json.dumps(conditions), json.dumps(actions), now, now))
        return cur.lastrowid


def update_rule(rule_id, **fields):
    if not fields:
        return
    fields["updated"] = int(time.time())
    sets = ", ".join("%s=?" % k for k in fields)
    with db() as conn:
        conn.execute("UPDATE rules SET %s WHERE id=?" % sets, (*fields.values(), rule_id))


def delete_rule(rule_id):
    with db() as conn:
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))


def move_rule(rule_id, direction):
    """direction: -1 up, +1 down (swap positions with the neighbour)."""
    with db() as conn:
        rules = [dict(r) for r in conn.execute("SELECT id, position FROM rules ORDER BY position, id")]
        idx = next((i for i, r in enumerate(rules) if r["id"] == rule_id), None)
        if idx is None:
            return
        j = idx + direction
        if j < 0 or j >= len(rules):
            return
        a, b = rules[idx], rules[j]
        conn.execute("UPDATE rules SET position=? WHERE id=?", (b["position"], a["id"]))
        conn.execute("UPDATE rules SET position=? WHERE id=?", (a["position"], b["id"]))


# ---------------------------------------------------------------- templates

def list_templates():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM templates ORDER BY name")]


def get_template(tid):
    with db() as conn:
        row = conn.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
    return dict(row) if row else None


def add_template(name, subject, body):
    now = int(time.time())
    with db() as conn:
        cur = conn.execute("INSERT INTO templates (name, subject, body, created, updated) "
                           "VALUES (?, ?, ?, ?, ?)", (name, subject, body, now, now))
        return cur.lastrowid


def update_template(tid, name, subject, body):
    with db() as conn:
        conn.execute("UPDATE templates SET name=?, subject=?, body=?, updated=? WHERE id=?",
                     (name, subject, body, int(time.time()), tid))


def delete_template(tid):
    with db() as conn:
        conn.execute("DELETE FROM templates WHERE id=?", (tid,))


# ---------------------------------------------------------------- messages

def insert_message(folder, uid, uidvalidity, fields):
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages (folder, uid, uidvalidity, msgid, from_addr, to_addr, "
            "subject, date, snippet, status, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (folder, uid, uidvalidity, fields.get("msgid", ""), fields.get("from_addr", ""),
             fields.get("to_addr", ""), fields.get("subject", ""), fields.get("date", ""),
             fields.get("snippet", ""), fields.get("status", "new"), int(time.time())))
        return cur.lastrowid, conn.total_changes


def update_message(msg_id, **fields):
    sets = ", ".join("%s=?" % k for k in fields)
    with db() as conn:
        conn.execute("UPDATE messages SET %s WHERE id=?" % sets, (*fields.values(), msg_id))


def get_message(msg_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    return dict(row) if row else None


def get_message_by_uid(folder, uid, uidvalidity):
    with db() as conn:
        row = conn.execute("SELECT * FROM messages WHERE folder=? AND uid=? AND uidvalidity=?",
                           (folder, uid, uidvalidity)).fetchone()
    return dict(row) if row else None


def messages(limit=50, filt="all"):
    q = "SELECT * FROM messages"
    where = []
    if filt == "queued":
        where.append("status='queued'")
    elif filt == "unmatched":
        where.append("status IN ('queued','classified')")
    elif filt == "needs_reply":
        where.append("llm_needs_reply=1")
    elif filt == "moved":
        where.append("action_taken LIKE 'move%'")
    elif filt == "errors":
        where.append("status='error'")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ?"
    with db() as conn:
        return [dict(r) for r in conn.execute(q, (limit,))]


def queued_messages(limit=10):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM messages WHERE status='queued' ORDER BY id LIMIT ?", (limit,))]


def last_uid(folder):
    with db() as conn:
        row = conn.execute("SELECT MAX(uid) AS u, MAX(uidvalidity) AS v FROM messages WHERE folder=?",
                           (folder,)).fetchone()
    return (row["u"], row["v"]) if row and row["u"] is not None else (None, None)


def reset_folder_index(folder):
    with db() as conn:
        conn.execute("DELETE FROM messages WHERE folder=?", (folder,))


# ---------------------------------------------------------------- events

def log_event(level, message):
    with db() as conn:
        conn.execute("INSERT INTO events (ts, level, message) VALUES (?, ?, ?)",
                     (int(time.time()), level, message))


def recent_events(limit=100):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- llm budget

def add_llm_log(msg_id, ok, error=""):
    with db() as conn:
        conn.execute("INSERT INTO llm_log (ts, msg_id, ok, error) VALUES (?, ?, ?, ?)",
                     (int(time.time()), msg_id, 1 if ok else 0, error))


def llm_count_last_hour():
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM llm_log WHERE ts > ?",
                           (int(time.time()) - 3600,)).fetchone()
    return row["n"]


def llm_fail_count(msg_id):
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM llm_log WHERE msg_id=? AND ok=0",
                           (msg_id,)).fetchone()
    return row["n"]


def retry_parked_errors():
    """Requeue messages parked after repeated LLM failures and clear their failure rows.
    Only touches messages whose errors came from the LLM path (they have llm_log rows)."""
    with db() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT DISTINCT m.id FROM messages m JOIN llm_log l ON l.msg_id = m.id "
            "WHERE m.status='error' AND l.ok=0")]
        for mid in ids:
            conn.execute("UPDATE messages SET status='queued' WHERE id=?", (mid,))
            conn.execute("DELETE FROM llm_log WHERE msg_id=? AND ok=0", (mid,))
    return len(ids)


# ---------------------------------------------------------------- assistant

def add_assistant_message(role, content, proposals="[]"):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO assistant_messages (ts, role, content, proposals) VALUES (?,?,?,?)",
            (int(time.time()), role, content, proposals))
        return cur.lastrowid


def assistant_messages(limit=40):
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM assistant_messages ORDER BY id DESC LIMIT ?", (limit,))]
    return list(reversed(rows))


def get_assistant_message(mid):
    with db() as conn:
        row = conn.execute("SELECT * FROM assistant_messages WHERE id=?", (mid,)).fetchone()
    return dict(row) if row else None


def clear_assistant():
    with db() as conn:
        conn.execute("DELETE FROM assistant_messages")
