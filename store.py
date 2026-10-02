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
    # ---- learning loop (docs/mail-intelligence/design.md) ----
    "learning_enabled": True,        # record decisions/observations; run shadow specialists
    "learning_route_mode": "shadow", # shadow = log what routing WOULD do; enforce = confident specialists may skip the LLM call
    "learning_sample_pct": 1.0,      # % of high-confidence accepts still routed to the LLM for audit (0 = none)
    "categories": ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"],
    "category_folders": {
        "Notification": "Notifications",
        "Newsletter": "Newsletters",
        "Receipt": "Receipts",
        "Promo": "Promotions",
    },
    "drafts_folder": "",          # blank = auto-detect the \Drafts special-use folder
    "my_name": "",
    "assistant_actions_apply": True,  # RETIRED (2026-10): read only by the one-time migration; superseded by perm_*
    # ---- agent permissions: off | ask (needs your approval) | auto ----
    "perm_classify": "auto",       # run classification on a message (filing still follows llm_apply)
    "perm_flag": "auto",           # read/unread, star
    "perm_tag": "auto",            # apply tags (recorded as assistant-sourced)
    "perm_move": "auto",           # move messages between folders
    "perm_create_folder": "auto",  # create folders
    "perm_classifiers": "auto",    # train / enable / delete heuristic classifiers
    "perm_draft": "auto",          # generate a reply draft and save it to Drafts
    "perm_delete": "off",          # DANGEROUS - move mail to Trash (recoverable until the server purges)
    "perm_send": "off",            # DANGEROUS - send mail through the account via the proxy
    "sends_per_hour": 5,           # cap on agent sends per hour (0 = unlimited)
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

    # ---- plugins (docs/plugin-architecture.md) ----
    "plugins_enabled": 1,         # master switch for plugin tool discovery + runtime
    "plugin_tools_budget": 8,     # max plugin tool schemas offered to the model per turn
    "plugin_classifiers": [],     # plugin ids allowed to run in the classify pipeline
    "plugin_matchers": [],        # plugin ids allowed as rule/flow condition matchers
    "plugin_retrievers": [],      # plugin ids allowed to re-rank semantic search
    "plugin_schedules_enabled": 1,  # master switch for schedule() onSchedule runs
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
CREATE TABLE IF NOT EXISTS assistant_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    created INTEGER, updated INTEGER
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
-- lite backend (RAG v2): clean chunks + fielded FTS; vec table created lazily
CREATE TABLE IF NOT EXISTS chunks2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT '',
    uid INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL DEFAULT 0,
    node TEXT NOT NULL DEFAULT 'new',
    text TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    created INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunks2_message ON chunks2(message_id);
CREATE INDEX IF NOT EXISTS idx_chunks2_folder ON chunks2(folder);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks2_fts USING fts5(subject, sender, body);
CREATE TABLE IF NOT EXISTS index2_state (
    folder TEXT PRIMARY KEY,
    uidvalidity INTEGER NOT NULL DEFAULT 0,
    last_uid INTEGER NOT NULL DEFAULT 0,
    messages_indexed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    updated INTEGER
);
CREATE TABLE IF NOT EXISTS rule_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, source TEXT NOT NULL DEFAULT '',
    rule TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '',
    applied INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts INTEGER, session_id INTEGER NOT NULL DEFAULT 0,
    capability TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    preview TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '', applied_ts INTEGER, updated_ts INTEGER
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
    if cols and "session_id" not in cols:
        conn.execute("ALTER TABLE assistant_messages ADD COLUMN session_id INTEGER NOT NULL DEFAULT 0")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(assistant_messages)")]
    if cols and "session_id" in cols:
        orphan = conn.execute("SELECT COUNT(*) FROM assistant_messages WHERE session_id=0").fetchone()[0]
        if orphan:
            ts = conn.execute("SELECT COALESCE(MIN(ts), 0) FROM assistant_messages").fetchone()[0]
            now = int(time.time())
            cur = conn.execute("INSERT INTO assistant_sessions (title, created, updated) VALUES (?,?,?)",
                               ("Earlier conversation", ts or now, now))
            conn.execute("UPDATE assistant_messages SET session_id=? WHERE session_id=0", (cur.lastrowid,))
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
    if "user_tag_by" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN user_tag_by TEXT NOT NULL DEFAULT ''")
    if "snoozed_until" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN snoozed_until INTEGER NOT NULL DEFAULT 0")
    # undo trail for machine-applied filings + per-Message-ID keep registry
    conn.execute("""CREATE TABLE IF NOT EXISTS undo_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        msg_id INTEGER NOT NULL,
        from_folder TEXT NOT NULL DEFAULT '',
        to_folder TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        prev_status TEXT NOT NULL DEFAULT '',
        prev_action_taken TEXT NOT NULL DEFAULT '',
        undone_ts INTEGER NOT NULL DEFAULT 0,
        undo_uid INTEGER DEFAULT 0
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_undo_pending ON undo_log(undone_ts, id DESC)")
    conn.execute("""CREATE TABLE IF NOT EXISTS keep_ids (
        msgid TEXT PRIMARY KEY, ts INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS msg_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id INTEGER NOT NULL,
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msgev_msg ON msg_events(msg_id, id)")
    # ---- learning loop: specialists registry, decision store, observations, labels
    # See docs/mail-intelligence/design.md. Decisions are per (message, task) records
    # with provenance; shadow=1 rows never influenced behavior. Labels are versioned
    # training material with a source strength (explicit > inferred > weak LLM).
    conn.execute("""CREATE TABLE IF NOT EXISTS specialists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        task TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'proposed',
        feature_schema_version INTEGER NOT NULL DEFAULT 1,
        model TEXT NOT NULL DEFAULT '{}',
        stats TEXT NOT NULL DEFAULT '{}',
        metrics TEXT NOT NULL DEFAULT '{}',
        min_confidence REAL NOT NULL DEFAULT 0.9,
        enabled INTEGER NOT NULL DEFAULT 0,
        created_by TEXT NOT NULL DEFAULT '',
        created INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0,
        superseded_by INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_spec_task ON specialists(task, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL DEFAULT 0,
        msg_id INTEGER NOT NULL DEFAULT 0,
        task TEXT NOT NULL DEFAULT '',
        predicted_value TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0,
        source_type TEXT NOT NULL DEFAULT '',
        source_id TEXT NOT NULL DEFAULT '',
        model_version TEXT NOT NULL DEFAULT '',
        feature_version INTEGER NOT NULL DEFAULT 0,
        shadow INTEGER NOT NULL DEFAULT 0,
        routed TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dec_task ON decisions(task, id DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dec_msg ON decisions(msg_id, task)")
    conn.execute("""CREATE TABLE IF NOT EXISTS decision_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL DEFAULT 0,
        feature_name TEXT NOT NULL DEFAULT '',
        feature_value TEXT NOT NULL DEFAULT '',
        contribution REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devev_dec ON decision_evidence(decision_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL DEFAULT 0,
        msg_id INTEGER NOT NULL DEFAULT 0,
        event_type TEXT NOT NULL DEFAULT '',
        event_value TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_msg ON observations(msg_id, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS labels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL DEFAULT 0,
        msg_id INTEGER NOT NULL DEFAULT 0,
        task TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 1.0,
        source TEXT NOT NULL DEFAULT '',
        source_detail TEXT NOT NULL DEFAULT '',
        UNIQUE(msg_id, task, label, source)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lab_msg ON labels(msg_id, task)")
    conn.execute("""CREATE TABLE IF NOT EXISTS eval_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        set_name TEXT NOT NULL DEFAULT 'golden',
        msg_id INTEGER NOT NULL DEFAULT 0,
        task TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        created INTEGER NOT NULL DEFAULT 0,
        labeled_at INTEGER NOT NULL DEFAULT 0,
        UNIQUE(set_name, msg_id, task)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_msg ON eval_items(set_name, msg_id)")
    # ---- plugin system: registry + namespaced key/value store
    # See docs/plugin-architecture.md. `enabled` is user consent to run the
    # plugin at all; `grants_json` is the subset of the manifest's declared
    # permissions that host functions may exercise. A version bump that grows
    # the permission set resets both (re-grant).
    conn.execute("""CREATE TABLE IF NOT EXISTS plugins (
        id TEXT PRIMARY KEY,
        version TEXT NOT NULL,
        root TEXT NOT NULL DEFAULT 'user',
        dir TEXT NOT NULL,
        manifest_json TEXT NOT NULL DEFAULT '{}',
        manifest_sha256 TEXT NOT NULL DEFAULT '',
        entry_sha256 TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 0,
        grants_json TEXT NOT NULL DEFAULT '[]',
        last_error TEXT NOT NULL DEFAULT '',
        installed_ts INTEGER NOT NULL DEFAULT 0,
        updated_ts INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS plugin_kv (
        plugin_id TEXT NOT NULL,
        k TEXT NOT NULL,
        v TEXT NOT NULL,
        updated_ts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (plugin_id, k)
    )""")
    # one-time: retire assistant_actions_apply (False meant dry-run -> the three gated
    # tools become 'ask', so nothing the assistant did before can now happen silently)
    has_perm = conn.execute("SELECT COUNT(*) FROM settings WHERE k GLOB 'perm_*'").fetchone()[0]
    if not has_perm:
        row = conn.execute("SELECT v FROM settings WHERE k='assistant_actions_apply'").fetchone()
        legacy = None
        if row is not None:
            try:
                legacy = json.loads(row["v"])
            except (TypeError, ValueError):
                legacy = None
        if legacy is False:
            for k in ("perm_move", "perm_flag", "perm_create_folder"):
                conn.execute("INSERT OR IGNORE INTO settings (k, v) VALUES (?, ?)", (k, json.dumps("ask")))


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


# ---------------------------------------------------------------- learning loop
# Decisions, observations, labels and the specialist registry used by learning.py
# (design: docs/mail-intelligence/design.md). These are additive: nothing in the
# existing pipeline reads them except the shadow hook + the /learning page.

def list_specialists(task=None, enabled_only=False, statuses=None):
    q = "SELECT * FROM specialists"
    where, args = [], []
    if task:
        where.append("task=?")
        args.append(task)
    if enabled_only:
        where.append("enabled=1")
    if statuses:
        where.append("status IN (%s)" % ",".join("?" * len(statuses)))
        args.extend(statuses)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY task, id DESC"
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args)]


def get_specialist(sid):
    with db() as conn:
        row = conn.execute("SELECT * FROM specialists WHERE id=?", (int(sid),)).fetchone()
    return dict(row) if row else None


def add_specialist(name, task, kind, version=1, status="proposed", feature_schema_version=1,
                   model="{}", stats="{}", metrics="{}", min_confidence=0.9,
                   enabled=False, created_by=""):
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO specialists (name, task, kind, version, status, feature_schema_version, "
            "model, stats, metrics, min_confidence, enabled, created_by, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, task, kind, int(version), status, int(feature_schema_version), model, stats,
             metrics, float(min_confidence), 1 if enabled else 0, created_by, now, now))
        return cur.lastrowid


def update_specialist(sid, **fields):
    if not fields:
        return
    fields["updated"] = int(time.time())
    sets = ", ".join("%s=?" % k for k in fields)
    with db() as conn:
        conn.execute("UPDATE specialists SET %s WHERE id=?" % sets, (*fields.values(), int(sid)))


def next_specialist_version(name):
    with db() as conn:
        row = conn.execute("SELECT MAX(version) FROM specialists WHERE name=?", (name,)).fetchone()
    return int(row[0] or 0) + 1


def record_decision(msg_id, task, predicted_value, confidence, source_type, source_id="",
                    model_version="", feature_version=0, shadow=0, routed="", ts=None):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO decisions (ts, msg_id, task, predicted_value, confidence, source_type, "
            "source_id, model_version, feature_version, shadow, routed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(ts or time.time()), int(msg_id or 0), task, str(predicted_value),
             float(confidence or 0), source_type, source_id, str(model_version),
             int(feature_version or 0), 1 if shadow else 0, routed))
        return cur.lastrowid


def record_decision_evidence(decision_id, items):
    """items: iterable of (feature_name, feature_value, contribution-or-None)."""
    with db() as conn:
        conn.executemany(
            "INSERT INTO decision_evidence (decision_id, feature_name, feature_value, contribution) "
            "VALUES (?,?,?,?)",
            [(int(decision_id), str(n)[:80], str(v)[:120],
              None if c is None else float(c)) for n, v, c in items])


def list_decisions(task=None, msg_id=None, source_type=None, source_id=None, shadow=None,
                   since=None, limit=200, newest_first=True):
    q = "SELECT * FROM decisions"
    where, args = [], []
    if task:
        where.append("task=?")
        args.append(task)
    if msg_id:
        where.append("msg_id=?")
        args.append(int(msg_id))
    if source_type:
        where.append("source_type=?")
        args.append(source_type)
    if source_id:
        where.append("source_id=?")
        args.append(source_id)
    if shadow is not None:
        where.append("shadow=?")
        args.append(1 if shadow else 0)
    if since:
        where.append("ts>=?")
        args.append(int(since))
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id %s LIMIT ?" % ("DESC" if newest_first else "ASC")
    args.append(int(limit))
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args)]


def count_decisions(task=None, source_type=None, since=None):
    q = "SELECT COUNT(*) FROM decisions"
    where, args = [], []
    if task:
        where.append("task=?")
        args.append(task)
    if source_type:
        where.append("source_type=?")
        args.append(source_type)
    if since:
        where.append("ts>=?")
        args.append(int(since))
    if where:
        q += " WHERE " + " AND ".join(where)
    with db() as conn:
        return conn.execute(q, args).fetchone()[0]


def decision_evidence(decision_id, limit=50):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM decision_evidence WHERE decision_id=? ORDER BY ABS(COALESCE(contribution,0)) DESC LIMIT ?",
            (int(decision_id), int(limit)))]


def record_observation(msg_id, event_type, event_value="", source="", ts=None):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO observations (ts, msg_id, event_type, event_value, source) VALUES (?,?,?,?,?)",
            (int(ts or time.time()), int(msg_id or 0), event_type, str(event_value)[:300], source))
        return cur.lastrowid


def list_observations(msg_id=None, event_type=None, limit=100):
    q = "SELECT * FROM observations"
    where, args = [], []
    if msg_id:
        where.append("msg_id=?")
        args.append(int(msg_id))
    if event_type:
        where.append("event_type=?")
        args.append(event_type)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args)]


def count_observations(event_type=None):
    q = "SELECT COUNT(*) FROM observations"
    args = []
    if event_type:
        q += " WHERE event_type=?"
        args.append(event_type)
    with db() as conn:
        return conn.execute(q, args).fetchone()[0]


def record_label(msg_id, task, label, confidence=1.0, source="", source_detail=""):
    """Idempotent per (msg, task, label, source) - richer sources add rows, they
    never silently overwrite; strength ordering lives in learning.LABEL_SOURCES."""
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO labels (ts, msg_id, task, label, confidence, source, source_detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (int(time.time()), int(msg_id or 0), task, str(label), float(confidence or 1.0),
             source, str(source_detail)[:200]))
        return cur.lastrowid if cur.rowcount else None


def list_labels(msg_id=None, task=None, source=None, limit=500):
    q = "SELECT * FROM labels"
    where, args = [], []
    if msg_id:
        where.append("msg_id=?")
        args.append(int(msg_id))
    if task:
        where.append("task=?")
        args.append(task)
    if source:
        where.append("source=?")
        args.append(source)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args)]


def count_labels(task=None, source=None):
    q = "SELECT COUNT(*) FROM labels"
    where, args = [], []
    if task:
        where.append("task=?")
        args.append(task)
    if source:
        where.append("source=?")
        args.append(source)
    if where:
        q += " WHERE " + " AND ".join(where)
    with db() as conn:
        return conn.execute(q, args).fetchone()[0]


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


def snooze_message(mid, until_ts):
    with db() as conn:
        conn.execute("UPDATE messages SET snoozed_until=? WHERE id=?",
                     (int(until_ts or 0), int(mid)))
        conn.commit()


def clear_needs_reply(ids):
    """Clear the needs-reply flag on the given ids. Returns (count, changed_ids).
    Only rows that were actually flagged change, so only real corrections get
    labels and audit events downstream."""
    changed = []
    with db() as conn:
        for x in ids:
            try:
                mid = int(x)
            except (TypeError, ValueError):
                continue
            row = conn.execute("SELECT llm_needs_reply FROM messages WHERE id=?", (mid,)).fetchone()
            if row is not None and row["llm_needs_reply"] == 1:
                conn.execute("UPDATE messages SET llm_needs_reply=0 WHERE id=?", (mid,))
                changed.append(mid)
        conn.commit()
    return len(changed), changed


def user_needs_reply(msg_id):
    """The user's latest explicit needs_reply correction (0/1) or None when the
    user never corrected this message. Corrections outrank every model verdict."""
    with db() as conn:
        row = conn.execute(
            "SELECT label FROM labels WHERE msg_id=? AND task='needs_reply' "
            "AND source='explicit_user_correction' ORDER BY id DESC LIMIT 1",
            (int(msg_id),)).fetchone()
    if not row:
        return None
    return 1 if str(row["label"]) == "1" else 0


def record_move(msg, to_folder, source, from_folder=None):
    """Record a filing for the undo trail + the per-message audit (called before
    the IMAP move). msg = pre-move dict; from_folder overrides msg['folder'] for
    multi-step flows."""
    if not msg or not msg.get("id"):
        return
    origin = from_folder or msg.get("folder") or ""
    with db() as conn:
        conn.execute(
            "INSERT INTO undo_log (ts, msg_id, from_folder, to_folder, source, prev_status, prev_action_taken)"
            " VALUES (?,?,?,?,?,?,?)",
            (int(time.time()), int(msg["id"]), origin,
             to_folder or "", source or "", msg.get("status") or "", msg.get("action_taken") or ""))
        conn.execute("INSERT INTO msg_events (msg_id, ts, kind, detail) VALUES (?,?,?,?)",
                     (int(msg["id"]), int(time.time()), "move",
                      "%s: \u201c%s\u201d \u2192 \u201c%s\u201d" % (source or "move", origin, to_folder or "?")))
        conn.commit()


def log_msg_event(msg_id, kind, detail, ts=None):
    if not msg_id:
        return
    with db() as conn:
        conn.execute("INSERT INTO msg_events (msg_id, ts, kind, detail) VALUES (?,?,?,?)",
                     (int(msg_id), int(ts or time.time()), str(kind or "")[:24], str(detail or "")[:8000]))
        conn.commit()


def log_msg_events_bulk(items):
    """items = [(msg_id, kind, detail, ts), ...] - one transaction (audit sweep)."""
    if not items:
        return 0
    with db() as conn:
        conn.executemany("INSERT INTO msg_events (msg_id, ts, kind, detail) VALUES (?,?,?,?)",
                         [(int(i), int(ts or time.time()), str(k or "")[:24], str(d or "")[:8000])
                          for (i, k, d, ts) in items])
        conn.commit()
    return len(items)


def get_msg_events(msg_id, limit=200):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM msg_events WHERE msg_id=? ORDER BY id LIMIT ?", (int(msg_id), int(limit)))]


def recent_moves(limit=8):
    """Pending undo entries, newest first, with the message's current folder (stale detection)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT u.id, u.ts, u.msg_id, u.from_folder, u.to_folder, u.source,"
            "       m.subject AS subject, m.from_addr AS from_addr, m.folder AS cur_folder"
            " FROM undo_log u LEFT JOIN messages m ON m.id = u.msg_id"
            " WHERE u.undone_ts = 0 ORDER BY u.id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["stale"] = (d.get("cur_folder") or "") != (d.get("to_folder") or "")
            out.append(d)
        return out


def get_undo(log_id):
    with db() as conn:
        r = conn.execute("SELECT * FROM undo_log WHERE id = ?", (log_id,)).fetchone()
        return dict(r) if r else None


def mark_undone(log_id, uid=0):
    with db() as conn:
        conn.execute("UPDATE undo_log SET undone_ts=?, undo_uid=? WHERE id=?",
                     (int(time.time()), int(uid or 0), int(log_id)))
        conn.commit()


def keep_message(msgid):
    """Remember that this Message-ID must not be re-filed by rules/LLM/flows (undo guard)."""
    msgid = (msgid or "").strip().strip("<>").strip()
    if not msgid:
        return
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO keep_ids (msgid, ts) VALUES (?,?)",
                     (msgid, int(time.time())))
        conn.commit()


def clear_keep(msgid):
    msgid = (msgid or "").strip().strip("<>").strip()
    if not msgid:
        return
    with db() as conn:
        conn.execute("DELETE FROM keep_ids WHERE msgid=?", (msgid,))
        conn.commit()


def kept_ids():
    with db() as conn:
        return set(r[0] for r in conn.execute("SELECT msgid FROM keep_ids"))


def is_kept(msgid):
    msgid = (msgid or "").strip().strip("<>").strip()
    if not msgid:
        return False
    with db() as conn:
        return conn.execute("SELECT 1 FROM keep_ids WHERE msgid=?", (msgid,)).fetchone() is not None


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


# rows with no subject, sender or Message-ID at all are IMAP sync artifacts,
# never real mail - excluded from every user-facing count and list
REAL_MSG = "NOT (coalesce(subject,'')='' AND coalesce(from_addr,'')='' AND coalesce(msgid,'')='')"


def _messages_filter_where(filt):
    now = int(time.time())
    where = [REAL_MSG]
    if filt == "snoozed":
        where.append("snoozed_until > %d" % now)
    else:
        where.append("coalesce(snoozed_until, 0) <= %d" % now)
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


def neighbors(mid, filt="all"):
    """Adjacent ids in list order (sort_ts DESC, id DESC) for the viewer queue.
    Returns (newer_id, older_id) - i.e. (prev, next) as shown in the list."""
    cur = get_message(mid)
    if not cur:
        return None, None
    where = _messages_filter_where(filt)
    s, i = cur.get("sort_ts") or 0, cur["id"]
    with db() as conn:
        newer = conn.execute(
            "SELECT id FROM messages" + where +
            " AND (sort_ts > ? OR (sort_ts = ? AND id > ?)) ORDER BY sort_ts ASC, id ASC LIMIT 1",
            (s, s, i)).fetchone()
        older = conn.execute(
            "SELECT id FROM messages" + where +
            " AND (sort_ts < ? OR (sort_ts = ? AND id < ?)) ORDER BY sort_ts DESC, id DESC LIMIT 1",
            (s, s, i)).fetchone()
    return (newer[0] if newer else None), (older[0] if older else None)


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

def add_assistant_message(role, content, proposals="[]", meta="", session_id=0):
    if not isinstance(meta, str):
        meta = json.dumps(meta)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO assistant_messages (ts, role, content, proposals, meta, session_id) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), role, content, proposals, meta, int(session_id or 0)))
        msg_id = cur.lastrowid
    if session_id:
        # first user message names the chat (touch_session keeps an existing title)
        touch_session(session_id, title=(content or "")[:70] if role == "user" else None)
    return msg_id


def set_assistant_proposals(mid, proposals_json):
    with db() as conn:
        conn.execute("UPDATE assistant_messages SET proposals=? WHERE id=?",
                     (proposals_json, mid))


def delete_assistant_message(mid):
    with db() as conn:
        conn.execute("DELETE FROM assistant_messages WHERE id=?", (int(mid),))


def assistant_messages(limit=40, session_id=None):
    q = "SELECT * FROM assistant_messages"
    args = []
    if session_id is not None:
        q += " WHERE session_id=?"
        args.append(int(session_id))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = [dict(r) for r in conn.execute(q, args)]
    return list(reversed(rows))


def session_messages(session_id, limit=400):
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM assistant_messages WHERE session_id=? ORDER BY id ASC LIMIT ?",
            (int(session_id), limit))]
    return rows


# ------------------------------------------------- assistant chat sessions

def create_session(title=""):
    now = int(time.time())
    with db() as conn:
        cur = conn.execute("INSERT INTO assistant_sessions (title, created, updated) VALUES (?,?,?)",
                           (title, now, now))
        return cur.lastrowid


def get_session(session_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM assistant_sessions WHERE id=?", (int(session_id),)).fetchone()
    return dict(row) if row else None


def find_or_create_session():
    """The most recent empty chat, else a fresh one ("new chat" semantics that
    never piles up empty sessions)."""
    with db() as conn:
        row = conn.execute(
            "SELECT s.id FROM assistant_sessions s WHERE "
            "(SELECT COUNT(*) FROM assistant_messages m WHERE m.session_id = s.id) = 0 "
            "ORDER BY s.id DESC LIMIT 1").fetchone()
    if row:
        return row["id"]
    return create_session()


def list_sessions(limit=60):
    with db() as conn:
        rows = conn.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM assistant_messages m WHERE m.session_id=s.id) AS n, "
            "(SELECT COALESCE(MAX(ts),0) FROM assistant_messages m WHERE m.session_id=s.id) AS last_ts "
            "FROM assistant_sessions s ORDER BY COALESCE(last_ts, s.created) DESC, s.id DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def touch_session(session_id, title=None):
    now = int(time.time())
    with db() as conn:
        if title is not None:
            conn.execute(
                "UPDATE assistant_sessions SET updated=?, "
                "title=CASE WHEN title='' THEN ? ELSE title END WHERE id=?",
                (now, title, int(session_id)))
        else:
            conn.execute("UPDATE assistant_sessions SET updated=? WHERE id=?", (now, int(session_id)))


def delete_session(session_id):
    with db() as conn:
        conn.execute("DELETE FROM assistant_messages WHERE session_id=?", (int(session_id),))
        conn.execute("DELETE FROM assistant_sessions WHERE id=?", (int(session_id),))


def get_assistant_message(mid):
    with db() as conn:
        row = conn.execute("SELECT * FROM assistant_messages WHERE id=?", (mid,)).fetchone()
    return dict(row) if row else None


def clear_assistant(session_id=None):
    with db() as conn:
        if session_id is None:
            conn.execute("DELETE FROM assistant_messages")
        else:
            conn.execute("DELETE FROM assistant_messages WHERE session_id=?", (int(session_id),))


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


# ---------------------------------------------------------------- lite RAG backend (v2)

def ensure_vec_table2(dim):
    """Create the lite-backend KNN table (dimension fixed by the active embed model)."""
    with db(vec=True) as conn:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks2 "
                     "USING vec0(embedding float[%d] distance_metric=cosine)" % int(dim))


def add_chunks2(rows):
    """rows: [{message_id, folder, uid, seq, node, text, subject, sender}] -> ids."""
    now = int(time.time())
    ids = []
    with db() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT INTO chunks2 (message_id, folder, uid, seq, node, text, subject, "
                "sender, created) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["message_id"], r["folder"], r["uid"], r["seq"], r.get("node", "new"),
                 r["text"], r.get("subject", ""), r.get("sender", ""), now))
            cid = cur.lastrowid
            conn.execute("INSERT INTO chunks2_fts (rowid, subject, sender, body) "
                         "VALUES (?,?,?,?)",
                         (cid, r.get("subject", ""), r.get("sender", ""), r["text"]))
            ids.append(cid)
    return ids


def add_vectors2(pairs):
    """pairs: [(chunk2_id, [floats])] -> stored in vec_chunks2."""
    import struct
    with db(vec=True) as conn:
        for cid, vec in pairs:
            blob = struct.pack("%df" % len(vec), *vec)
            conn.execute("INSERT INTO vec_chunks2 (rowid, embedding) VALUES (?, ?)",
                         (cid, blob))


def vec_search2(qvec, k):
    import struct
    blob = struct.pack("%df" % len(qvec), *qvec)
    with db(vec=True) as conn:
        rows = conn.execute(
            "SELECT rowid, distance FROM vec_chunks2 WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance", (blob, k)).fetchall()
    return [(r["rowid"], r["distance"]) for r in rows]


def fts_search2(query, k, weights=(4.0, 2.0, 1.0), cand_ids=None):
    cond, args = "", [query]
    if cand_ids is not None:
        ids = list(cand_ids)[:900]
        cond = " AND c.message_id IN (%s)" % ",".join("?" * len(ids))
        args += ids
    q = ("SELECT f.rowid, bm25(chunks2_fts, ?, ?, ?) AS score FROM chunks2_fts f "
         "JOIN chunks2 c ON c.id = f.rowid WHERE chunks2_fts MATCH ?" + cond +
         " ORDER BY score LIMIT ?")
    args = list(weights) + args + [k]
    with db() as conn:
        return [(r["rowid"], r["score"]) for r in conn.execute(q, args)]


def chunks2_by_ids(ids):
    if not ids:
        return []
    q = "SELECT * FROM chunks2 WHERE id IN (%s)" % ",".join("?" * len(ids))
    with db() as conn:
        return [dict(r) for r in conn.execute(q, ids)]


def chunk2_count():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM chunks2").fetchone()[0]


def message_chunk2_count(message_id):
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM chunks2 WHERE message_id=?",
                            (message_id,)).fetchone()[0]


def delete_chunks2_folder(folder):
    with db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM chunks2 WHERE folder=?", (folder,))]
        if not ids:
            return 0
        conn.execute("DELETE FROM chunks2 WHERE folder=?", (folder,))
        conn.executemany("DELETE FROM chunks2_fts WHERE rowid=?", [(i,) for i in ids])
    try:
        with db(vec=True) as conn:
            conn.executemany("DELETE FROM vec_chunks2 WHERE rowid=?", [(i,) for i in ids])
    except Exception:
        pass
    return len(ids)


def clear_rag2():
    """Wipe the lite index (chunks2, fts2, vectors, state, lite meta)."""
    with db() as conn:
        conn.execute("DELETE FROM chunks2")
        conn.execute("DELETE FROM chunks2_fts")
        conn.execute("DELETE FROM index2_state")
        conn.execute("DELETE FROM meta WHERE k IN ('lite_embed_dim','lite_embed_model')")
    try:
        with db(vec=True) as conn:
            conn.execute("DELETE FROM vec_chunks2")
    except Exception:
        pass


def index2_state_get(folder):
    with db() as conn:
        row = conn.execute("SELECT * FROM index2_state WHERE folder=?", (folder,)).fetchone()
    return dict(row) if row else None


def index2_state_put(folder, uidvalidity, last_uid, status=None):
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO index2_state (folder, uidvalidity, last_uid, messages_indexed, status, updated) "
            "VALUES (?,?,?,0,?,?) ON CONFLICT(folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, "
            "last_uid=excluded.last_uid, status=COALESCE(?, index2_state.status), updated=excluded.updated",
            (folder, int(uidvalidity), int(last_uid), status or "working", now, status))
        conn.commit()
        return cur


def index2_state_touch(folder, uidvalidity, last_uid):
    now = int(time.time())
    with db() as conn:
        conn.execute(
            "INSERT INTO index2_state (folder, uidvalidity, last_uid, messages_indexed, status, updated) "
            "VALUES (?,?,?,1,'working',?) ON CONFLICT(folder) DO UPDATE SET "
            "uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid, "
            "messages_indexed=index2_state.messages_indexed+1, status='working', updated=excluded.updated",
            (folder, int(uidvalidity), int(last_uid), now))
        conn.commit()


def index2_overview(folders=None):
    """Folder state rows for the lite index. folders=None -> every known row
    (dashboard view); a list -> one row per folder, missing ones marked new."""
    with db() as conn:
        have = {r["folder"]: dict(r) for r in conn.execute("SELECT * FROM index2_state")}
    if folders is None:
        return sorted(have.values(), key=lambda r: r.get("folder") or "")
    return [have.get(f, {"folder": f, "status": "new", "messages_indexed": 0}) for f in folders]


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

def tag_messages(ids, tag, by="user"):
    ids = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
    if not ids:
        return 0
    q = "UPDATE messages SET user_tag=?, user_tag_by=? WHERE id IN (%s)" % ",".join("?" * len(ids))
    with db() as conn:
        cur = conn.execute(q, [tag, by if tag else ""] + ids)
        return cur.rowcount


def untag_messages(ids):
    return tag_messages(ids, "")


def tagged_examples(limit=80, include_agent=False):
    where = "coalesce(user_tag,'') != ''"
    if not include_agent:
        where += " AND coalesce(user_tag_by,'') != 'assistant'"
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, folder, from_addr, subject, date, snippet, user_tag, user_tag_by FROM messages "
            "WHERE " + where + " ORDER BY id DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- agent actions
# Pending approvals + audit trail for the fine-grained agent permissions.

def add_agent_action(capability, tool, preview, payload, session_id=0):
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_actions (created_ts, session_id, capability, tool, status, preview, payload, "
            "result, applied_ts, updated_ts) VALUES (?,?,?,?,'pending',?,?,'',NULL,?)",
            (now, int(session_id or 0), capability, tool, preview,
             json.dumps(payload or {}, ensure_ascii=False), now))
        return cur.lastrowid


def get_agent_action(action_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM agent_actions WHERE id=?", (int(action_id),)).fetchone()
    return dict(row) if row else None


def set_agent_action(action_id, status, result=""):
    now = int(time.time())
    with db() as conn:
        if status in ("applied", "failed"):
            conn.execute("UPDATE agent_actions SET status=?, result=?, applied_ts=?, updated_ts=? WHERE id=?",
                         (status, result or "", now, now, int(action_id)))
        else:
            conn.execute("UPDATE agent_actions SET status=?, result=?, updated_ts=? WHERE id=?",
                         (status, result or "", now, int(action_id)))


def pending_agent_actions(limit=50):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM agent_actions WHERE status='pending' ORDER BY id DESC LIMIT ?", (int(limit),))]


def count_pending_agent_actions():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM agent_actions WHERE status='pending'").fetchone()[0]


def agent_actions_since(capability, since_ts, statuses=("applied",)):
    marks = ",".join("?" * len(statuses))
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM agent_actions WHERE capability=? AND status IN (%s) "
            "AND coalesce(applied_ts, created_ts)>=?" % marks,
            (capability,) + tuple(statuses) + (int(since_ts),)).fetchone()[0]


def unclassified_count():
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE status IN ('new','queued') "
            "AND " + REAL_MSG
        ).fetchone()[0]


def unclassified_next(skip=None):
    q = ("SELECT * FROM messages WHERE status IN ('new','queued') "
         "AND " + REAL_MSG)
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


# ---------------------------------------------------------------- eval sets (test sets)

def add_eval_item(msg_id, task, set_name="golden"):
    """Idempotent: one row per (set, message, task). Returns 1 when added."""
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO eval_items (set_name, msg_id, task, created) VALUES (?,?,?,?)",
            (set_name, int(msg_id), task, int(time.time())))
        return cur.rowcount


def eval_items(set_name="golden", task=None):
    sql = "SELECT * FROM eval_items WHERE set_name=?"
    args = [set_name]
    if task:
        sql += " AND task=?"
        args.append(task)
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def eval_counts(set_name="golden"):
    with db() as conn:
        rows = conn.execute(
            "SELECT task, COUNT(*) total, SUM(CASE WHEN labeled_at>0 THEN 1 ELSE 0 END) labeled "
            "FROM eval_items WHERE set_name=? GROUP BY task", (set_name,)).fetchall()
    return {r["task"]: {"total": r["total"], "labeled": r["labeled"] or 0} for r in rows}


def eval_msg_ids(set_name="golden"):
    with db() as conn:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT msg_id FROM eval_items WHERE set_name=?", (set_name,))}


def set_eval_label(msg_id, task, label, set_name="golden"):
    with db() as conn:
        cur = conn.execute(
            "UPDATE eval_items SET label=?, labeled_at=? "
            "WHERE set_name=? AND msg_id=? AND task=?",
            (str(label), int(time.time()), set_name, int(msg_id), task))
        return cur.rowcount


def next_eval_msg(set_name="golden"):
    """First message that still has an unlabeled item (stable order)."""
    with db() as conn:
        row = conn.execute(
            "SELECT msg_id FROM eval_items WHERE set_name=? AND labeled_at=0 "
            "GROUP BY msg_id ORDER BY MIN(id) LIMIT 1", (set_name,)).fetchone()
    return row[0] if row else None


def eval_items_for_msg(msg_id, set_name="golden"):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM eval_items WHERE set_name=? AND msg_id=?",
            (set_name, int(msg_id)))]


def eval_created(set_name="golden"):
    with db() as conn:
        row = conn.execute(
            "SELECT MIN(created) c FROM eval_items WHERE set_name=?", (set_name,)).fetchone()
    return row["c"] if row and row["c"] else None
