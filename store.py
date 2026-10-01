"""SQLite storage for Mail Triage: settings, rules, templates, messages, events."""
import email.utils
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
    "flows_apply": True,          # run multi-step flow automations for real (off = dry-run)
    "render_images": False,       # viewer: load remote images without asking first
    "classify_concurrency": 8,    # parallel LLM requests for batch classification
    "heuristics_enabled": True,   # run trained heuristic classifiers before the LLM
    "heuristic_autorefine": True,  # retrain tag-sourced heuristics when labels grow
    "categories": ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"],
    "category_folders": {
        "Notification": "Notifications",
        "Newsletter": "Newsletters",
        "Receipt": "Receipts",
        "Promo": "Promotions",
    },
    "drafts_folder": "",          # blank = auto-detect the \Drafts special-use folder
    "my_name": "Sean",
    "assistant_actions_apply": True,  # assistant may move/flag mail (False = dry-run)
    "index_enabled": True,        # build/refresh the semantic search index
    "index_folders": [],          # blank = all folders except the exclusion list below
    "rerank_enabled": True,       # cross-encoder rerank on top of hybrid retrieval
    "index_refresh_minutes": 10,  # idle incremental index refresh cadence

    # ---- LLM endpoint (blank = fall back to the container env: LLM_BASE_URL etc.) ----
    "llm_base_url": "",           # any OpenAI-compatible base URL, e.g. http://host:8040/v1
    "llm_api_key": "",            # optional bearer; stored in SQLite, shown masked
    "llm_model": "",              # model name served by that endpoint
    "llm_timeout": 0,             # seconds; 0 = env/default
    "llm_thinking": "auto",       # auto = send the thinking extension, drop it on 4xx; off = never send
    "llm_fallback_base_url": "",  # optional second endpoint, used when the primary fails
    "llm_fallback_api_key": "",
    "llm_fallback_model": "",     # blank = same model name as the primary

    # ---- RAG endpoints ----
    "embed_base_url": "",         # e.g. http://host:8041 (TEI) or an OpenAI-style .../v1
    "embed_model": "",            # blank = env EMBED_MODEL
    "embed_api_key": "",          # optional bearer for the embeddings endpoint
    "embed_protocol": "tei",      # tei = POST /embed {"inputs":..}; openai = POST /embeddings
    "embed_timeout": 0,
    "embed_query_prefix": ("Instruct: Given a search query, retrieve relevant email messages "
                           "from the user's mailbox\nQuery: "),
    "rerank_base_url": "",
    "rerank_model": "",
    "rerank_api_key": "",
    "rerank_protocol": "tei",     # tei = {"query","texts"}; cohere = {"query","documents"}
    "rerank_timeout": 0,
    "rag_exclude_folders": ["junk", "deleted", "trash", "sync issues", "calendar", "contacts",
                            "journal", "conversation history", "outbox", "rss feeds"],

    # ---- UI ----
    "display_tz_offset": 8,       # hours from UTC for displayed timestamps (float ok)

    # ---- mail connection / embedded proxy ----
    "proxy_mode": "embedded",     # embedded = read mail via the in-app emailproxy
    "proxy_tailnet_host": "",     # e.g. node.tailnet.ts.net, for tailnet-mode redirect URIs
    "imap_host": "",              # external-mode overrides (blank = container env)
    "imap_port": "",
    "imap_user": "",
    "imap_password": "",
    "imap_tls": "",               # "", "0"/"1" (blank = env)
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
CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL DEFAULT '',
    match_mode TEXT NOT NULL DEFAULT 'all',
    conditions TEXT NOT NULL DEFAULT '[]',
    actions TEXT NOT NULL DEFAULT '[]',
    created INTEGER, updated INTEGER
);
CREATE TABLE IF NOT EXISTS flow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL,
    msgid TEXT NOT NULL DEFAULT '',
    message_id INTEGER DEFAULT 0,
    ran_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_flow_runs ON flow_runs(flow_id, msgid);
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
    llm_reason TEXT DEFAULT '',
    llm_thinking TEXT DEFAULT '',
    llm_needs_reply INTEGER DEFAULT NULL,
    llm_suggested_folder TEXT DEFAULT '',
    classified_by TEXT DEFAULT '',
    processed_at INTEGER,
    UNIQUE (folder, uid, uidvalidity)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS heuristics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    min_confidence REAL DEFAULT 0.8,
    priority INTEGER DEFAULT 0,
    model TEXT NOT NULL DEFAULT '{}',
    stats TEXT NOT NULL DEFAULT '{}',
    excluded TEXT NOT NULL DEFAULT '[]',
    created_by TEXT DEFAULT '',
    created INTEGER, updated INTEGER
);
CREATE TABLE IF NOT EXISTS llm_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, msg_id INTEGER, ok INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS assistant_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, role TEXT NOT NULL DEFAULT 'user',
    content TEXT NOT NULL DEFAULT '', proposals TEXT NOT NULL DEFAULT '[]',
    meta TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT '',
    uid INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    created INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunks_message ON chunks(message_id);
CREATE INDEX IF NOT EXISTS idx_chunks_folder ON chunks(folder);
CREATE TABLE IF NOT EXISTS index_state (
    folder TEXT PRIMARY KEY,
    uidvalidity INTEGER NOT NULL DEFAULT 0,
    last_uid INTEGER NOT NULL DEFAULT 0,
    messages_indexed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    updated INTEGER
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text);
CREATE TABLE IF NOT EXISTS rule_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, source TEXT NOT NULL DEFAULT '',
    rule TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '',
    applied INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS proxy_accounts (
    email TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'custom',
    password TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    client_secret TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '',
    auth_url TEXT NOT NULL DEFAULT '',
    token_url TEXT NOT NULL DEFAULT '',
    use_pkce INTEGER NOT NULL DEFAULT 0,
    redirect_port INTEGER NOT NULL DEFAULT 0,
    redirect_mode TEXT NOT NULL DEFAULT 'tailnet',
    imap_host TEXT NOT NULL DEFAULT '',
    imap_port INTEGER NOT NULL DEFAULT 993,
    imap_local_port INTEGER NOT NULL DEFAULT 0,
    smtp_host TEXT NOT NULL DEFAULT '',
    smtp_port INTEGER NOT NULL DEFAULT 465,
    smtp_local_port INTEGER NOT NULL DEFAULT 0,
    smtp_starttls INTEGER NOT NULL DEFAULT 0,
    created INTEGER, updated INTEGER
);
"""


def _migrate(conn):
    """Additive migrations for databases created by older versions."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(assistant_messages)")]
    if "meta" not in cols:
        conn.execute("ALTER TABLE assistant_messages ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
    mcols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
    if "date_ts" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN date_ts INTEGER DEFAULT 0")
        _backfill_date_ts(conn)
    if "llm_reason" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN llm_reason TEXT DEFAULT ''")
    if "llm_thinking" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN llm_thinking TEXT DEFAULT ''")
    if "classified_by" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN classified_by TEXT DEFAULT ''")
    if "body_html" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN body_html TEXT NOT NULL DEFAULT ''")
    if "body_cids" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN body_cids TEXT NOT NULL DEFAULT ''")
    if "body_html_at" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN body_html_at INTEGER DEFAULT 0")
    if "sort_ts" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN sort_ts INTEGER DEFAULT 0")
        conn.execute("UPDATE messages SET sort_ts = COALESCE(NULLIF(date_ts,0), processed_at, 0)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_sort ON messages(sort_ts DESC, id DESC)")
    hcols = [r[1] for r in conn.execute("PRAGMA table_info(heuristics)")]
    if hcols and "excluded" not in hcols:
        conn.execute("ALTER TABLE heuristics ADD COLUMN excluded TEXT NOT NULL DEFAULT '[]'")
    if "user_tag" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN user_tag TEXT NOT NULL DEFAULT ''")


def _backfill_date_ts(conn):
    rows = conn.execute("SELECT id, date FROM messages WHERE coalesce(date_ts,0)=0 "
                        "AND coalesce(date,'')!=''").fetchall()
    for r in rows:
        ts = date_ts_from(r["date"])
        if ts:
            conn.execute("UPDATE messages SET date_ts=? WHERE id=?", (ts, r["id"]))


def date_ts_from(date_str):
    """Epoch seconds from an RFC822 date header (0 when unparseable)."""
    if not date_str:
        return 0
    try:
        return int(email.utils.parsedate_to_datetime(date_str).timestamp())
    except Exception:
        return 0


def db(vec=False):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if vec:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


def init_db():
    with db() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
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


def add_rule(name, match_mode, conditions, actions, enabled=True, position="bottom"):
    now = int(time.time())
    with db() as conn:
        if position == "top":
            pos = conn.execute("SELECT COALESCE(MIN(position), 0) - 1 FROM rules").fetchone()[0]
        else:
            pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM rules").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO rules (position, enabled, name, match_mode, conditions, actions, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pos, 1 if enabled else 0, name, match_mode,
             json.dumps(conditions), json.dumps(actions), now, now))
        return cur.lastrowid


def move_rule_top(rule_id):
    with db() as conn:
        row = conn.execute("SELECT id FROM rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            return
        mn = conn.execute("SELECT MIN(position) FROM rules").fetchone()[0] or 0
        conn.execute("UPDATE rules SET position=? WHERE id=?", (mn - 1, rule_id))


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


# ---------------------------------------------------------------- flows

def list_flows(enabled_only=False):
    q = "SELECT * FROM flows"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY position, id"
    with db() as conn:
        return [dict(r) for r in conn.execute(q)]


def get_flow(flow_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone()
    return dict(row) if row else None


def add_flow(name, match_mode, conditions, actions, enabled=True, position="bottom"):
    now = int(time.time())
    with db() as conn:
        if position == "top":
            pos = conn.execute("SELECT COALESCE(MIN(position), 0) - 1 FROM flows").fetchone()[0]
        else:
            pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM flows").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO flows (position, enabled, name, match_mode, conditions, actions, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pos, 1 if enabled else 0, name, match_mode,
             json.dumps(conditions), json.dumps(actions), now, now))
        return cur.lastrowid


def update_flow(flow_id, **fields):
    if not fields:
        return
    fields["updated"] = int(time.time())
    sets = ", ".join("%s=?" % k for k in fields)
    with db() as conn:
        conn.execute("UPDATE flows SET %s WHERE id=?" % sets, (*fields.values(), flow_id))


def delete_flow(flow_id):
    with db() as conn:
        conn.execute("DELETE FROM flows WHERE id=?", (flow_id,))
        conn.execute("DELETE FROM flow_runs WHERE flow_id=?", (flow_id,))


def move_flow(flow_id, delta):
    """Move a flow one slot up/down (renumbers positions to keep it simple)."""
    with db() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM flows ORDER BY position, id")]
        if flow_id not in ids:
            return
        i = ids.index(flow_id)
        j = i + (1 if delta > 0 else -1)
        if not (0 <= j < len(ids)):
            return
        ids[i], ids[j] = ids[j], ids[i]
        for pos, fid in enumerate(ids):
            conn.execute("UPDATE flows SET position=? WHERE id=?", (pos, fid))


def flow_already_ran(flow_id, key):
    with db() as conn:
        row = conn.execute("SELECT 1 FROM flow_runs WHERE flow_id=? AND msgid=? LIMIT 1",
                           (flow_id, key)).fetchone()
    return row is not None


def record_flow_run(flow_id, key, message_id):
    with db() as conn:
        conn.execute("INSERT INTO flow_runs (flow_id, msgid, message_id, ran_at) VALUES (?,?,?,?)",
                     (flow_id, key, message_id, int(time.time())))


# ---------------------------------------------------------------- heuristics

def list_heuristics(enabled_only=False):
    q = "SELECT * FROM heuristics"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY priority, id"
    with db() as conn:
        return [dict(r) for r in conn.execute(q)]


def get_heuristic(hid):
    with db() as conn:
        row = conn.execute("SELECT * FROM heuristics WHERE id=?", (hid,)).fetchone()
    return dict(row) if row else None


def add_heuristic(name, kind, category, model="{}", stats="{}", min_confidence=0.8,
                  enabled=True, created_by="assistant"):
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO heuristics (name, kind, category, enabled, min_confidence, model, "
            "stats, created_by, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, kind, category, 1 if enabled else 0, float(min_confidence), model, stats,
             created_by, now, now))
        return cur.lastrowid


def update_heuristic(hid, **fields):
    if not fields:
        return
    fields["updated"] = int(time.time())
    sets = ", ".join("%s=?" % k for k in fields)
    with db() as conn:
        conn.execute("UPDATE heuristics SET %s WHERE id=?" % sets, (*fields.values(), hid))


def delete_heuristic(hid):
    with db() as conn:
        conn.execute("DELETE FROM heuristics WHERE id=?", (hid,))


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
            "subject, date, date_ts, snippet, status, processed_at, body_html, body_cids, "
            "body_html_at, sort_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (folder, uid, uidvalidity, fields.get("msgid", ""), fields.get("from_addr", ""),
             fields.get("to_addr", ""), fields.get("subject", ""), fields.get("date", ""),
             fields.get("date_ts") or date_ts_from(fields.get("date", "")),
             fields.get("snippet", ""), fields.get("status", "new"),
             fields.get("processed_at") or int(time.time()),
             fields.get("body_html", ""), fields.get("body_cids", ""),
             fields.get("body_html_at") or 0,
             fields.get("date_ts") or date_ts_from(fields.get("date", "")) or int(time.time())))
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


def find_message_by_uid(folder, uid):
    """Latest indexed row for folder+uid regardless of uidvalidity."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE folder=? AND uid=? "
            "ORDER BY uidvalidity DESC, id DESC LIMIT 1", (folder, uid)).fetchone()
    return dict(row) if row else None


def find_message_by_msgid(msgid):
    """Canonical row for a Message-ID (used when mail was moved between folders)."""
    if not msgid:
        return None
    with db() as conn:
        row = conn.execute("SELECT * FROM messages WHERE msgid=? ORDER BY id LIMIT 1",
                           (msgid,)).fetchone()
    return dict(row) if row else None


def _messages_filter_where(filt):
    where = ["NOT (coalesce(subject,'')='' AND coalesce(from_addr,'')='' AND coalesce(msgid,'')='')"]
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
    elif filt == "tagged":
        where.append("coalesce(user_tag,'') != ''")
    return " WHERE " + " AND ".join(where)


_MSG_LIST_COLS = ("id, folder, uid, uidvalidity, msgid, from_addr, to_addr, subject, date, "
                  "date_ts, snippet, status, rule_id, action_taken, llm_category, llm_confidence, "
                  "llm_summary, llm_reason, llm_needs_reply, llm_suggested_folder, classified_by, "
                  "processed_at, user_tag, body_html_at")


def messages(limit=50, filt="all", order="date", offset=0):
    # NOTE: heavy columns (body_html/body_cids/llm_thinking) are excluded here -
    # lists only need the light fields; use get_message(id) for the full row.
    q = "SELECT " + _MSG_LIST_COLS + " FROM messages" + _messages_filter_where(filt)
    if order == "id":
        q += " ORDER BY id DESC"
    else:
        q += " ORDER BY sort_ts DESC, id DESC"
    q += " LIMIT ? OFFSET ?"
    with db() as conn:
        return [dict(r) for r in conn.execute(q, (limit, offset))]


def count_messages(filt="all"):
    q = "SELECT COUNT(*) FROM messages" + _messages_filter_where(filt)
    with db() as conn:
        return conn.execute(q).fetchone()[0]


def queued_messages(limit=10):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT " + _MSG_LIST_COLS + " FROM messages WHERE status='queued' ORDER BY id LIMIT ?",
            (limit,))]


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

def add_assistant_message(role, content, proposals="[]", meta=""):
    if not isinstance(meta, str):
        meta = json.dumps(meta)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO assistant_messages (ts, role, content, proposals, meta) VALUES (?,?,?,?,?)",
            (int(time.time()), role, content, proposals, meta))
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


# ---------------------------------------------------------------- RAG index

def ensure_vec_table(dim):
    """Create the sqlite-vec KNN table (dimension fixed by the embed model)."""
    with db(vec=True) as conn:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks "
                     "USING vec0(embedding float[%d] distance_metric=cosine)" % int(dim))


def add_chunks(rows):
    """rows: [{message_id, folder, uid, seq, text}] -> list of new chunk ids."""
    now = int(time.time())
    ids = []
    with db() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT INTO chunks (message_id, folder, uid, seq, text, created) "
                "VALUES (?,?,?,?,?,?)",
                (r["message_id"], r["folder"], r["uid"], r["seq"], r["text"], now))
            cid = cur.lastrowid
            conn.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", (cid, r["text"]))
            ids.append(cid)
    return ids


def add_vectors(pairs):
    """pairs: [(chunk_id, [floats])] -> stored in vec_chunks (needs sqlite-vec)."""
    import struct
    with db(vec=True) as conn:
        for cid, vec in pairs:
            blob = struct.pack("%df" % len(vec), *vec)
            conn.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)", (cid, blob))


def vec_search(qvec, k):
    import struct
    blob = struct.pack("%df" % len(qvec), *qvec)
    with db(vec=True) as conn:
        rows = conn.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance", (blob, k)).fetchall()
    return [(r["rowid"], r["distance"]) for r in rows]


def fts_search(query, k):
    with db() as conn:
        rows = conn.execute(
            "SELECT rowid, bm25(chunks_fts) AS score FROM chunks_fts "
            "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?", (query, k)).fetchall()
    return [(r["rowid"], r["score"]) for r in rows]


def chunks_by_ids(ids):
    if not ids:
        return []
    q = "SELECT * FROM chunks WHERE id IN (%s)" % ",".join("?" * len(ids))
    with db() as conn:
        return [dict(r) for r in conn.execute(q, ids)]


def messages_by_ids(ids):
    if not ids:
        return {}
    q = "SELECT * FROM messages WHERE id IN (%s)" % ",".join("?" * len(ids))
    with db() as conn:
        return {r["id"]: dict(r) for r in conn.execute(q, ids)}


def chunk_count():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def message_chunk_count(message_id):
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM chunks WHERE message_id=?",
                            (message_id,)).fetchone()[0]


def delete_chunks_folder(folder):
    with db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE folder=?", (folder,))]
        if not ids:
            return 0
        conn.execute("DELETE FROM chunks WHERE folder=?", (folder,))
        conn.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
    try:
        with db(vec=True) as conn:
            conn.executemany("DELETE FROM vec_chunks WHERE rowid=?", [(i,) for i in ids])
    except Exception:
        pass
    return len(ids)


def clear_rag():
    """Wipe chunks, vectors, FTS rows, index state and model meta (rebuild)."""
    with db() as conn:
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("DELETE FROM index_state")
        conn.execute("DELETE FROM meta WHERE k IN ('embed_dim','embed_model')")
    try:
        with db(vec=True) as conn:
            conn.execute("DELETE FROM vec_chunks")
    except Exception:
        pass


def index_state_get(folder):
    with db() as conn:
        row = conn.execute("SELECT * FROM index_state WHERE folder=?", (folder,)).fetchone()
    return dict(row) if row else None


def index_state_put(folder, uidvalidity, last_uid, status=None):
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO index_state (folder, uidvalidity, last_uid, messages_indexed, status, updated) "
            "VALUES (?,?,?,0,?,?) ON CONFLICT(folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, "
            "last_uid=excluded.last_uid, status=COALESCE(?, index_state.status), updated=excluded.updated",
            (folder, int(uidvalidity or 0), int(last_uid or 0), status or "working", now, status))
        return cur


def index_state_touch(folder, uidvalidity, last_uid):
    now = int(time.time())
    with db() as conn:
        conn.execute(
            "INSERT INTO index_state (folder, uidvalidity, last_uid, messages_indexed, status, updated) "
            "VALUES (?,?,?,1,'working',?) ON CONFLICT(folder) DO UPDATE SET "
            "uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid, "
            "messages_indexed=index_state.messages_indexed+1, status='working', updated=excluded.updated",
            (folder, int(uidvalidity or 0), int(last_uid or 0), now))


def index_overview(folders=None):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM index_state")]
    if folders:
        known = {r["folder"] for r in rows}
        for f in folders:
            if f not in known:
                rows.append({"folder": f, "uidvalidity": 0, "last_uid": 0,
                             "messages_indexed": 0, "status": "new", "updated": None})
    return rows


def meta_get(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["v"])
    except (TypeError, ValueError):
        return row["v"]


def meta_set(key, value):
    with db() as conn:
        conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value)))


# ---------------------------------------------------------------- tagging / classification

def tag_messages(ids, tag):
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0
    q = "UPDATE messages SET user_tag=? WHERE id IN (%s)" % ",".join("?" * len(ids))
    with db() as conn:
        cur = conn.execute(q, [tag] + ids)
        return cur.rowcount


def untag_messages(ids):
    return tag_messages(ids, "")


def tagged_examples(limit=80):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, folder, from_addr, subject, date, snippet, user_tag FROM messages "
            "WHERE coalesce(user_tag,'') != '' ORDER BY id DESC LIMIT ?", (limit,))]


def unclassified_count():
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE status IN ('new','queued') "
            "AND NOT (coalesce(subject,'')='' AND coalesce(from_addr,'')='' AND coalesce(msgid,'')='')"
        ).fetchone()[0]


def unclassified_next(skip=None):
    q = ("SELECT * FROM messages WHERE status IN ('new','queued') "
         "AND NOT (coalesce(subject,'')='' AND coalesce(from_addr,'')='' AND coalesce(msgid,'')='')")
    params = []
    if skip:
        q += " AND id NOT IN (%s)" % ",".join("?" * len(skip))
        params += list(skip)
    q += " ORDER BY (CASE WHEN coalesce(date_ts,0)>0 THEN date_ts ELSE processed_at END) DESC LIMIT 1"
    with db() as conn:
        row = conn.execute(q, params).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- rule proposals (learned from tags)

def add_rule_proposal(source, rule, note=""):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO rule_proposals (ts, source, rule, note) VALUES (?,?,?,?)",
            (int(time.time()), source, json.dumps(rule), note))
        return cur.lastrowid


def list_rule_proposals(pending_only=True, limit=20):
    q = "SELECT * FROM rule_proposals"
    if pending_only:
        q += " WHERE applied=0"
    q += " ORDER BY id DESC LIMIT ?"
    with db() as conn:
        out = []
        for r in conn.execute(q, (limit,)):
            d = dict(r)
            try:
                d["rule_obj"] = json.loads(d.get("rule") or "{}")
            except (TypeError, ValueError):
                d["rule_obj"] = {}
            out.append(d)
        return out


def get_rule_proposal(pid):
    with db() as conn:
        row = conn.execute("SELECT * FROM rule_proposals WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def mark_rule_proposal_applied(pid):
    with db() as conn:
        conn.execute("UPDATE rule_proposals SET applied=1 WHERE id=?", (pid,))
