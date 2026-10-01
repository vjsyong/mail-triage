# Audit: mail-triage SQLite data model (store.py)

Audit of the triage store for designing a *continuous improvement / learning* subsystem.

- Repo: `/home/xrim/mail-triage-intel` (same code as `~/mail-triage`, commit `c6db3c5`, verified `git log`).
- DB: `data/triage.db` (SQLite, WAL) — `store.db()` at `store.py:348-358`; path from `config.DB_PATH` (`config.py`).
- Live DB was probed READ-ONLY on 2026-10-01 for verification: 23 application tables + FTS5/vec0 shadow tables; `messages` 3563 rows, `events` 5272, `msg_events` 2, `undo_log` 2, `heuristics` 2, `llm_log` 3599, `settings` 60 keys, `rules` 12, `flows` 1, `agent_actions` 5, `rule_proposals` 0, `flow_runs` 0, `keep_ids` 0.
- All claims below cite `file:line`. Nothing in the app was modified; no write queries were run.

---

## 0. Schema-creation and migration pattern (how to add tables the same way)

`store.init_db()` — `store.py:361-367`:

```python
def init_db():
    with db() as conn:
        conn.executescript(_SCHEMA)      # CREATE TABLE IF NOT EXISTS ... (store.py:89-240)
        _migrate(conn)                   # additive ALTERs + runtime tables (store.py:243-326)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (k, v) VALUES (?, ?)",
                         (k, json.dumps(v)))
```

- `_SCHEMA` (`store.py:89-240`) is a single script of `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` / one `CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts` (`store.py:204`).
- `_migrate(conn)` (`store.py:243-326`) = "additive migrations for databases created by older versions". Pattern used everywhere:

```python
cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
if "date_ts" not in mcols:
    conn.execute("ALTER TABLE messages ADD COLUMN date_ts INTEGER DEFAULT 0")
```

  - `assistant_messages.meta` + `session_id` + orphan adoption: `store.py:245-258`
  - `messages`: `date_ts` (+backfill `_backfill_date_ts` `store.py:329-335`), `llm_reason`, `llm_thinking`, `classified_by`, `body_html`, `body_cids`, `body_html_at`, `sort_ts` (+ `idx_msg_sort`), `user_tag`, `user_tag_by`, `snoozed_until`: `store.py:259-287`
  - `heuristics.excluded`: `store.py:279-281`
  - NEW TABLES are also created *inside* `_migrate` with `CREATE TABLE IF NOT EXISTS` (so old DBs get them): `undo_log` `store.py:289-301`, `keep_ids` `store.py:302-304`, `msg_events` `store.py:305-312`
  - one-time data backfill (retire `assistant_actions_apply` → seed `perm_*='ask'`): `store.py:313-326`
- `vec_chunks` (sqlite-vec KNN table) is created at runtime, not in `_SCHEMA`: `store.ensure_vec_table(dim)` `store.py:1038-1042` (`CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[N] distance_metric=cosine)`).
- `init_db()` callers: app startup `app.py:6787` (threads then started `app.py:6840-6842`: `worker.start(); indexer.start(); classifier.start()`), `engine.Worker.run` `engine.py:1278`, `engine.ClassifyJob.run` `engine.py:2167`, `rag.Indexer.run` `rag.py:368`.
- **Recipe for new tables**: append `CREATE TABLE IF NOT EXISTS` to `_SCHEMA`; if the live DB already exists it will still be created because `executescript` runs first at every boot, and `_migrate` is where you add `ALTER TABLE ... ADD COLUMN` for columns on existing tables (guarded by `PRAGMA table_info`). There is **no schema-version table and no `PRAGMA user_version`** — idempotent probing is the whole pattern.

---

## 1. Full table inventory (everything created by store.py)

| Table | Created at | Purpose / key columns | Primary writers (file:line) |
|---|---|---|---|
| `settings` | `store.py:90` | `(k TEXT PRIMARY KEY, v TEXT)` JSON values | `store.set_setting` `store.py:383-386`; UI save helpers `app.py:5254-5358`; seeding `store.py:365-367` |
| `rules` | `store.py:91-100` | id, position, enabled, name, match_mode, conditions JSON, actions JSON, created/updated | `store.add_rule` `store.py:417-429` (callers `app.py:2751,3879,5143`), `update_rule` `store.py:441-447`, `delete_rule` `store.py:450-452`, `move_rule` `store.py:573-585` |
| `flows` | `store.py:101-110` | same shape; `actions` = JSON step list | `store.add_flow` `store.py:472-484` (`app.py:3406,5117`), `update_flow` `store.py:487-493`, `delete_flow` `store.py:496-499`, `move_flow` `store.py:502-514` |
| `flow_runs` | `store.py:111-117` + idx `118` | `flow_id, msgid, message_id, ran_at` — dedupe key `(flow_id, msgid)` | `store.record_flow_run` `store.py:524-527` from `engine._process_flow` `engine.py:970-971` (live runs only); read by `flow_already_ran` `store.py:517-521` (`engine.py:962`) |
| `templates` | `store.py:119-125` | name, subject, body, created/updated | `add_template` `store.py:601-606` (`app.py:3547`), `update_template` `store.py:609-612`, `delete_template` `store.py:615-617` |
| `messages` | `store.py:126-150` | the per-message row incl. all verdict columns — see §2 | `insert_message` `store.py:622-636`; many `update_message` sites — see §2 |
| `events` | `store.py:151-154` | `(id, ts, level, message)` — global log line | ONLY `store.log_event` `store.py:873-876`; 105 production call sites (engine 63, app 30, rag 6, proxy 6) |
| `heuristics` | `store.py:155-168` (+`excluded` `279-281`) | classifier artifacts — see §4 | `store.add/update/delete_heuristic` `store.py:547-570`; callers §4 |
| `llm_log` | `store.py:169-172` | `(id, ts, msg_id, ok, error)` — LLM-call counter/failure log — see §5 | ONLY `store.add_llm_log` `store.py:887-890` |
| `assistant_sessions` | `store.py:173-177` | chat sessions | `create_session` `store.py:966-971`, `touch_session` `store.py:1004-1013`, `delete_session` `store.py:1016-1019` |
| `assistant_messages` | `store.py:178-183` | role/content/`proposals` JSON/`meta` JSON/`session_id` | `add_assistant_message` `store.py:922-934` (`engine.py:4133`), `set_assistant_proposals` `store.py:937-940` (`app.py:5065`) |
| `chunks` | `store.py:184-192` + idx `193-194` | RAG chunks per message | `add_chunks` `store.py:1045-1058` (`rag.py:248`), `delete_chunks_folder` `store.py:1115-1127` |
| `chunks_fts` | `store.py:204` | FTS5 shadow of chunk text (`fts5(text)`, rowid=chunk id) | written with chunks `store.py:1056` |
| `index_state` | `store.py:195-202` | per-folder `uidvalidity, last_uid, messages_indexed, status, updated` | `index_state_put` `store.py:1150-1158`, `index_state_touch` `store.py:1161-1169` (`rag.py:306,323,325`) |
| `meta` | `store.py:203` | `(k,v)` misc store | `meta_set` `store.py:1195-1198`; keys `embed_dim`, `embed_model` (`rag.py:207-218`) |
| `rule_proposals` | `store.py:205-210` | learned-rule proposals — §6 | `add_rule_proposal` `store.py:1301-1306` (`app.py:3834`), `mark_rule_proposal_applied` `store.py:1332-1334` (`app.py:3875,3882,3892`) |
| `agent_actions` | `store.py:211-218` | pending approvals + audit for agent tools | `add_agent_action` `store.py:1230-1238` (`engine.py:3254,3280`), `set_agent_action` `store.py:1247-1255` (`engine.py:3260,3267`; `app.py:5180,5200`) |
| `proxy_accounts` | `store.py:219-239` | email-oauth2-proxy accounts + secrets | `proxy.save_account` `proxy.py:173`, `proxy.delete_account` `proxy.py:191-193` |
| `undo_log` | `store.py:289-300` + idx `301` | machine-filing undo trail — §3 | `store.record_move` `store.py:646-662`; `store.mark_undone` `store.py:702-706` |
| `keep_ids` | `store.py:302-304` | keep registry by Message-ID | `store.keep_message` `store.py:709-717`; `store.clear_keep` `store.py:720-726` |
| `msg_events` | `store.py:305-311` + idx `312` | per-message audit trail — §3 | `store.record_move` (kind `move`) and `store.log_msg_event` `store.py:665-671` |
| `vec_chunks` | `store.py:1038-1042` (runtime) | sqlite-vec embeddings, rowid = chunk id | `store.add_vectors` `store.py:1061-1067` (`rag.py:250,273`) |

Live DB confirmed all of these plus FTS5 shadow tables (`chunks_fts_*`) and vec0 shadow tables.

---

## 2. `messages` — per-message verdict storage (the core learning surface)

Trimmed schema (`store.py:126-150`; the migration-added columns shown in creation order in the live DB):

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL, uid INTEGER NOT NULL, uidvalidity INTEGER NOT NULL DEFAULT 0,
    msgid TEXT DEFAULT '', from_addr TEXT DEFAULT '', to_addr TEXT DEFAULT '', subject TEXT DEFAULT '',
    date TEXT DEFAULT '', snippet TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new', rule_id INTEGER DEFAULT NULL, action_taken TEXT DEFAULT '',
    llm_category TEXT DEFAULT '', llm_confidence REAL DEFAULT NULL, llm_summary TEXT DEFAULT '',
    llm_needs_reply INTEGER DEFAULT NULL, llm_suggested_folder TEXT DEFAULT '',
    processed_at INTEGER, date_ts INTEGER DEFAULT 0, user_tag TEXT NOT NULL DEFAULT '',
    llm_reason TEXT DEFAULT '', llm_thinking TEXT DEFAULT '', classified_by TEXT DEFAULT '',
    body_html TEXT NOT NULL DEFAULT '', body_cids TEXT NOT NULL DEFAULT '', body_html_at INTEGER DEFAULT 0,
    sort_ts INTEGER DEFAULT 0, user_tag_by TEXT NOT NULL DEFAULT '', snoozed_until INTEGER NOT NULL DEFAULT 0,
    UNIQUE (folder, uid, uidvalidity)
)
```

### 2.1 Who writes what

| Column(s) | Meaning | Writer (file:line) |
|---|---|---|
| `folder, uid, uidvalidity, msgid, from/to, subject, date, date_ts, snippet, processed_at, body_html, body_cids, body_html_at, sort_ts` | scan-time index row; `status='new'` default (`store.py:137`); `processed_at=now` at insert; `date_ts` from Date header (fallback `processed_at`), `sort_ts` likewise | `insert_message` `store.py:622-636` from scan `engine.py:1349` |
| `status='queued'` | unmatched mail waiting for the LLM | `engine.py:1415` |
| `status='kept'`, `action_taken='kept (undo)'`, `rule_id` | a rule matched but the Message-ID is in `keep_ids` | `engine.py:1360-1364` |
| `status='matched' / 'matched-dry' / 'error'`, `action_taken` (comma list of `move:X`/`read`/`flag`), `rule_id`, `folder`/`uid` | rule execution result | `engine.py:1370-1403` |
| verdict columns `llm_category, llm_confidence, llm_summary, llm_reason, llm_thinking, llm_needs_reply, llm_suggested_folder, classified_by, status='classified'` | one JSON classify call or a heuristic verdict | `classify_and_store` fields dict `engine.py:1934-1944`, persisted at `engine.py:2024` |
| `classified_by` | `"llm"` or `"heuristic:<id> <name>"` | `engine.py:1942` |
| `status='flow'/'flow-dry'`, `action_taken='flow:<name>'` | post-verdict flow ran (or dry-ran) | `engine.py:1993-1994`, `engine.py:967-968` |
| `status='llm-moved'`, `action_taken='move:<folder>'`, `folder`, `uid` | LLM auto-filed | `engine.py:2008-2012` |
| `status='error'` | parked after 3 LLM failures | `engine.py:2039`, `engine.py:2249` |
| restore `folder/status/action_taken/uid` | undo | `engine.py:1448-1452` |
| `user_tag`, `user_tag_by` | manual/assistant tag (label!) | `store.tag_messages` `store.py:1203-1210` (by="user" default; assistant `engine.py:4188`) |
| `user_tag` only (relabel) | dataset relabel of a tag-sourced classifier — **leaves `user_tag_by` stale** | `app.py:2556` |
| `llm_category`, `classified_by='user'`, `llm_confidence=1.0` | dataset relabel of a classified-sourced classifier | `app.py:2558-2559` |
| `snoozed_until` | epoch seconds; 0 = not snoozed | `store.snooze_message` `store.py:639-643`, route `app.py:2219-2231` |
| `folder, uid` on flow move | step update so later steps address the new location | `engine.py:897` |
| `folder, uid, status='assistant-moved'/'assistant-deleted'`, `action_taken` | assistant move / trash | `engine.py:3626-3629`, `engine.py:4248-4251` |
| `snippet`, `body_html_at` self-heal | body repair | `engine.py:1872`, `engine.py:1755,1801`, `app.py:4181` |

`status` vocabulary (UI mapping `app.py:299-309`): `new`, `queued`, `classified`, `matched`, `matched-dry`, `error`, `kept`, `llm-moved`, `assistant-moved`, `flow`, `flow-dry`, `assistant-deleted`.
`action_taken` vocabulary: `move:<folder>`, `flow:<name>`, comma-joined rule actions, `kept (undo)`, `trash`, `""`.

### 2.2 Keep / keep_ids mechanism

- `keep_ids(msgid TEXT PRIMARY KEY, ts INTEGER)` — `store.py:302-304`; keyed by the shared Message-ID, normalised `.strip().strip("<>")` in `keep_message` `store.py:709-717`, `is_kept` `store.py:734-739`, `kept_ids` `store.py:729-731`.
- Set only on undo: `engine.undo_filing` `engine.py:1453` after moving mail back (`apply` → `store.keep_message`). Cleared by the manual "File" action: `app.py:4506` (`store.clear_keep`).
- Enforced at scan (`engine.py:1342,1360-1364`: rule matched → `status='kept'`; flow/LLM skipped) and inside `classify_and_store` (`engine.py:1933,1957,1999`).

### 2.3 Sort / date columns

- `date` (raw RFC822 header) and `date_ts` (epoch; `date_ts_from` `store.py:338-345`, migration + backfill `store.py:260-262,329-335`).
- `sort_ts` = `COALESCE(NULLIF(date_ts,0), processed_at, 0)` on insert (`store.py:635`) and in the migration (`store.py:277`); **no code path ever updates it after insert** (grep `sort_ts` shows only migration/insert/read sites). List order = `ORDER BY sort_ts DESC, id DESC` (`store.py:820`), viewer neighbours `store.py:826-843`.
- `processed_at` is written once at insert (`store.py:632`) — it is the *scan* time, not a classification time (no `classified_at` exists).
- `body_html_at > 0` = "HTML extraction attempted" sentinel (`store.py:274`).

### 2.4 Provenance / confidence / versions / join-back

- **PROVENANCE**: `classified_by` (which classifier/file made the verdict: `llm` vs `heuristic:<id> <name>` vs `user` after a dataset relabel), `user_tag_by` (`user`/`assistant`), `rule_id` (which rule fired, overwritten on every scan), `action_taken` (naming the flow by *name*), `folder`/`uid` (physical location).
- **CONFIDENCE**: `llm_confidence` (LLM self-reported or heuristic probability); `llm_needs_reply` (only meaningful for LLM verdicts — heuristics always set 0, `engine.py:1907`).
- **VERSIONS**: none. No model name, prompt version, heuristic `trained_at`, or app version is stored on the row.
- **Join-back**: `messages.id` is the hub; `msg_events.msg_id`, `undo_log.msg_id`, `flow_runs.message_id`, `llm_log.msg_id` all point at it. But the row itself is mutated in place (reclassify, relabel, rule rescan, undo), so "what was decided when" survives only in `msg_events` (see §3).

---

## 3. Event / audit tables

### 3.1 `events` — global app log

```sql
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, message TEXT)
```
`store.py:151-154`. One row = one log line; `level` in practice `info|debug|warn|error`; `message` is free text.
- Writer: exclusively `store.log_event` `store.py:873-876`. 105 production call sites: `engine.py` (63), `app.py` (30), `rag.py` (6), `proxy.py` (6).
- Contains no `msg_id`, no actor column, no structured fields — verdict/flow/rule context is embedded in prose (e.g. `engine.py:2052` "LLM: '<subject>' → <category> (<conf>%)"; `engine.py:1042` fallback notice).

### 3.2 `msg_events` — per-message audit trail

```sql
CREATE TABLE msg_events (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL,
                         ts INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')
```
`store.py:305-311`, index `idx_msgev_msg(msg_id, id)` `store.py:312`.

Writers:
- `store.record_move` `store.py:646-662` — writes an `undo_log` row AND a `msg_events` row of `kind='move'` (detail = `"<source>: “<from>” → “<to>”"`). Call sites (6): `engine.py:889` (flow step), `engine.py:1382` (rule), `engine.py:2006` (LLM auto-file), `engine.py:3621` (assistant move), `engine.py:4243` (assistant trash), `app.py:4504` (manual file).
- `store.log_msg_event` `store.py:665-671` — generic; `kind` truncated to 24 chars, `detail` to 8000 chars. Call sites (11): `engine.py:976` (flow), `1368` (rule skipped, kept), `1408` (rule result), `1457` (undo), `1945` (classify), `1966` (guard), `2014` (file); `app.py:2224` (snooze), `2230` (wake), `4476` (tag), `4529` (draft).
- Kinds seen in code: `move, rule, flow, classify, guard, file, undo, snooze, wake, tag, draft`.
- **`kind='classify'` is the closest thing to a decision record**: `engine.py:1945-1949` stores a JSON object `{category, confidence (3dp), by (classified_by), reason, llm_reason, summary, needs_reply, thinking}` in `detail`. It is written once per `classify_and_store` call, regardless of source (heuristic or LLM) — but it is untyped JSON embedded in a text column, and `detail` is clipped at 8000 chars (`store.py:670`) while `thinking` alone can be 6000 (`engine.py:1939`), so the JSON can be truncated mid-object.

### 3.3 `undo_log` — machine-filing trail

```sql
CREATE TABLE undo_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, msg_id INTEGER NOT NULL,
    from_folder TEXT NOT NULL DEFAULT '', to_folder TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
    prev_status TEXT NOT NULL DEFAULT '', prev_action_taken TEXT NOT NULL DEFAULT '',
    undone_ts INTEGER NOT NULL DEFAULT 0, undo_uid INTEGER DEFAULT 0)
```
`store.py:289-300`, index `idx_undo_pending(undone_ts, id DESC)` `store.py:301`.

- One row per *applied* move, written by `store.record_move` (`store.py:646-662`) **before** the IMAP move. `source` values: `flow` (`engine.py:889`), `rule` (`engine.py:1382`), `auto-file` (`engine.py:2006`), `assistant` (`engine.py:3621`), `trash` (`engine.py:4243`), `manual` (`app.py:4504`).
- `prev_status`/`prev_action_taken` snapshot the message row pre-move; `undone_ts=0` means pending; `mark_undone` `store.py:702-706` stamps `undone_ts` + `undo_uid`.
- Readers: `store.recent_moves` `store.py:680-693` (dashboard Undo card; JOINs `messages` and computes `stale` = current folder ≠ `to_folder`), `store.get_undo` `store.py:696-700`; undo execution `engine.undo_filing` `engine.py:1421-1459`.
- This table is the only place where a filing decision and its *reversal* are both recorded — but it carries no category/classifier columns.

---

## 4. `heuristics` — trained classifier artifacts

```sql
CREATE TABLE heuristics (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
    min_confidence REAL DEFAULT 0.8, priority INTEGER DEFAULT 0, model TEXT NOT NULL DEFAULT '{}',
    stats TEXT NOT NULL DEFAULT '{}', created_by TEXT DEFAULT '', created INTEGER, updated INTEGER,
    excluded TEXT NOT NULL DEFAULT '[]')          -- excluded added by _migrate store.py:279-281
```
`store.py:155-168`. One row = one classifier "model".

JSON columns:

- **`model`** (algorithm payload, version-less):
  - `decision_list`: `{"conditions": [{"token","label","prob","precision","support","seen"}, ...]}` — `heuristics.py:129-134`.
  - `naive_bayes`: `{"classes": {label: {"log_prior", "log_lik": {token: logp}}}, "vocab": N}` — `heuristics.py:87-93`.
- **`stats`** (training metadata):
  - base: `samples`, `labels` (label→count), plus `vocab` or `conditions` — `heuristics.py:92,133`.
  - added by `train_heuristic`: `source` (`tags`|`classified`), `trained_label_count`, `negatives`, `excluded` (count), `trained_at` (epoch), `params` (`min_precision`/`min_support`/`max_vocab`/`min_df`), `weak_labels` (True when bootstrapped from LLM labels) — `heuristics.py:331-335`.
  - added by `auto_refine`: `refined_from` (previous label count) — `heuristics.py:437`.
- **`excluded`**: JSON array of message ids the user removed from the dataset — parsed by `heuristics.heuristic_excluded` `heuristics.py:240-245`; enforced by *every* training path (`sample_rows` `heuristics.py:248-263`, `train_heuristic` `heuristics.py:322-336`, `auto_refine` `heuristics.py:436`, assistant retrain `engine.py:3908`).
- **`created_by`**: `"assistant"` (`store.py:548` default, `engine.py:3907,3921`), `"auto-refine"` (`heuristics.py:436`), `"ui"` (`app.py:2419`).

Read/write functions:
- Store CRUD: `list_heuristics` `store.py:532-538` (order `priority, id`), `get_heuristic` `store.py:541-544`, `add_heuristic` `store.py:547-556`, `update_heuristic` `store.py:559-565`, `delete_heuristic` `store.py:568-570`.
- Pipeline: `heuristics.classify` `heuristics.py:385-409` (best-confidence winner above `min_confidence`, default 0.8 `heuristics.py:382`; `__other__` abstains `heuristics.py:398`) called from `engine.classify_verdict` `engine.py:1904`; verdict stamped into messages at `engine.py:1942`.
- Training/eval/UI: `train_heuristic` `heuristics.py:322-336`; `auto_refine` `heuristics.py:415-442` (worker hook `engine.py:1316-1318`, `AUTO_REFINE_MIN_NEW=5` `heuristics.py:412`); `evaluate_heuristic` `heuristics.py:339-377` (in-sample only; result returned to the caller, never persisted); `dataset_for` `heuristics.py:278-319`; `view` `heuristics.py:447-464`.
- Write paths: UI create via assistant only; UI toggle/retrain/delete `app.py:2399-2437`; dataset remove/reinclude `app.py:2569-2588` (writes `excluded` at `app.py:2582`); relabel `app.py:2540-2566` (rewrites the *underlying label*, not the heuristic row); assistant tools `engine.py:3881-3986` (`train_classifier` `3905-3921`, `manage_classifier` `3944-3965`, `evaluate_classifier` `3967-3986`, list `3938-3942`).

Questions:
- **PROVENANCE**: `created_by`, `stats.source` (label source), `stats.weak_labels`, `stats.trained_at`, `stats.params`, `excluded`; `updated` timestamp on every write.
- **CONFIDENCE**: `min_confidence` (gate); model payload carries per-condition `prob`/`precision`/`support`/`seen`; NB `log_lik`.
- **VERSIONS**: none. Retraining overwrites `model`+`stats` in place (`app.py:2421`, `engine.py:3912`, `heuristics.py:438`); `trained_at` is the only version-like value, and messages do **not** record it (see §9 gap).
- **Join-back**: messages reference the heuristic only as `classified_by = "heuristic:<id> <name>"` (string, `engine.py:1942`); dataset rows are live queries over `messages` (`tagged_examples` `store.py:1217-1224`, `labels_from_classified` `heuristics.py:228-231`), not a materialised sample table.

---

## 5. `llm_log` — LLM cap / failure counters

```sql
CREATE TABLE llm_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, msg_id INTEGER, ok INTEGER, error TEXT)
```
`store.py:169-172`. One row = one *attempt* (success or failure) against the classify LLM.

- Writer: exclusively `store.add_llm_log` `store.py:887-890`.
  - Worker queue: success `engine.py:2034`, failure `engine.py:2036` (failure row includes `repr(exc)`).
  - ClassifyJob (batch): success `engine.py:2193`, failure `engine.py:2247`.
  - Single-message UI button: `app.py:4457` — **logged unconditionally**, even when a heuristic answered and no LLM call was made.
  - Heuristic verdicts skip it on the queue/job paths: `engine.py:2033` (`if not res.get("_heuristic_id")`), `engine.py:2192`.
- Reads:
  - Hourly cap: `store.llm_count_last_hour` `store.py:893-897` (COUNT where `ts > now-3600`), consumed at `engine.py:2072` (`max_llm_per_hour` setting `store.py:17`, default 40).
  - Failure park rule: `store.llm_fail_count` `store.py:900-904` (COUNT `ok=0` per msg), `>=3` → `status='error'` at `engine.py:2038-2039` and `engine.py:2248-2249`.
- Deletion: `store.retry_parked_errors` `store.py:907-917` (DELETE `ok=0` rows for parked messages — the only delete path).
- No columns for model/endpoint/fallback/latency/tokens/category → cannot attribute calls or failures to a model or a decision.

---

## 6. `settings`, `rules`/`rule_proposals`, `flows`/`flow_runs`

### 6.1 `settings` (key→JSON value)

`CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT)` `store.py:90`. Seeded from `DEFAULT_SETTINGS` (`store.py:10-87`) with `INSERT OR IGNORE` at `store.py:365-367`; values are JSON-encoded (`set_setting` `store.py:383-386`, `get_setting` `store.py:372-380`, `all_settings` `store.py:389-397`).

Default keys by group (all `store.py:10-87`):
- Mailbox/behavior: `poll_interval` 90, `lookback_hours` 48, `watch_folders` `["INBOX"]`, `rules_apply` true, `llm_suggest` true, `llm_apply` false, `max_llm_per_hour` 40, `llm_batch_per_cycle` 5, `flows_apply` true, `render_images` false, `classify_concurrency` 8, `heuristics_enabled` true, `heuristic_autorefine` true, `categories` (6), `category_folders` (4), `drafts_folder` "", `my_name` "Sean".
- Agent permissions: `assistant_actions_apply` (RETIRED; read only by the one-time migration `store.py:313-326`), `perm_classify/flag/tag/move/create_folder/classifiers/draft` = `auto`, `perm_delete/send` = `off`, `sends_per_hour` 5.
- Index/RAG: `index_enabled` true, `index_folders` [], `rerank_enabled` true, `index_refresh_minutes` 10, `embed_base_url/model/api_key/protocol(tei)/timeout/query_prefix`, `rerank_base_url/model/api_key/protocol(tei)/timeout`, `rag_exclude_folders` (10 names).
- LLM endpoint: `llm_base_url/api_key/model/timeout/thinking(auto)`, `llm_fallback_base_url/api_key/model`.
- UI/connection: `display_tz_offset` 8, `proxy_mode` "embedded", `proxy_tailnet_host`, `imap_host/port/user/password/tls`.
- Live DB check: 60 keys present, including the retired `assistant_actions_apply` (never deleted) — new keys are added automatically at each boot by `INSERT OR IGNORE`.
- Writes come from `_save_behavior_settings` `app.py:5254-5304`, `_save_llm_settings` `app.py:5308-5318`, `_save_rag_settings` `app.py:5322-5343`, `_save_connection_settings` `app.py:5347-5358`, secrets via `_save_secret` `app.py:5242-5250`. **Settings changes produce no event/audit row.**

### 6.2 `rules` and `rule_proposals`

- `rules` `store.py:91-100`: `conditions` = JSON `[{field,op,value}]`; `actions` = JSON `{move_to, mark_read, flag}` or `{}`/`{"keep": true}` = **guard** (`engine.is_guard_rule` `engine.py:726-733`). Matching `engine.rule_matches` `engine.py:713-723`, first match wins `engine.py:735`. Execution + `rule_id`/`status`/`action_taken` writes `engine.py:1370-1403`.
- `rule_proposals` `store.py:205-210`: rows are *LLM-proposed rules learned from tags*; `rule` = JSON rule object, `note` = model reply, `applied` flag. Writer `app.py:3834` (`engine.propose_rules_from_tags` `engine.py:2296-2334`); marking applied `app.py:3875,3882,3892`. No proposal-decision metadata (no model, no ts of apply, no editor).
- (Assistant rule/flow proposals are *not* here — they live as JSON in `assistant_messages.proposals`, `store.py:181`; apply/`_mark_proposal_applied` `app.py:5063-5067`, `assistant_apply` `app.py:5069-5151`.)

### 6.3 `flows` and `flow_runs`

- `flows` `store.py:101-110`: `conditions` JSON supports deterministic `{field,op,value}` plus fuzzy `{kind:"category", value, min_confidence}` and `{kind:"topic", value, threshold}` (`engine._needs_verdict` `engine.py:763`, `flow_matches` `engine.py:843-863`, topic scoring `engine.py:795-839`); `actions` = ordered step list (`move`/`mark_read`/`flag`/`tag`/`draft`) run by `engine._apply_flow` `engine.py:873-955`.
- `flow_runs` `store.py:111-117`: one row per live flow execution, dedupe key = Message-ID or `id:<n>` (`engine.py:961`), written at `engine.py:970-971` only when `flows_apply` is true. Dry runs leave no `flow_runs` row (status `flow-dry` only).
- Provenance: `flows.created/updated`; `flow_runs.ran_at`; `messages.action_taken = "flow:<name>"` (name string, not flow id).
- **Versions**: none — a flow edited or renamed leaves earlier verdicts pointing at the old name.

---

## 7. Learning-relevant roll-up (provenance / confidence / versions / join-back)

| Concern | Where it lives today |
|---|---|
| **PROVENANCE** | `messages.classified_by` (`llm` / `heuristic:<id> <name>` / `user`), `messages.user_tag_by`, `messages.rule_id`, `messages.action_taken`; `heuristics.created_by` + `stats.source`/`weak_labels`; `undo_log.source`; `msg_events.kind`; `flow_runs.flow_id`; `agent_actions.capability/tool/status/session_id`; `events` has **no actor**. |
| **CONFIDENCE** | `messages.llm_confidence`; `heuristics.min_confidence`; decision-list `prob/precision/support` inside `heuristics.model`; topic-match score exists only in the `events` text (`engine.py:1412`). |
| **VERSIONS** | **Nothing.** No schema version, model version/hash, prompt version, feature-set version, or app version anywhere in the DB. Closest: `heuristics.stats.trained_at`, `heuristics.updated`, `rules.updated`, `flows.updated`. |
| **Decision → later outcome** | `messages.id` hub: `msg_events.msg_id` (incl. the `classify` JSON record and every later move/tag/snooze/undo), `undo_log.msg_id` (reversal), `flow_runs.message_id`, `llm_log.msg_id`, `keep_ids.msgid` (suppression outcome). Weaknesses: `messages` row is overwritten in place; `msg_events` has no typed columns; joins through `msgid` rely on the Message-ID normalisation (`store.py:711,721,735`; `engine.py:313,574`). |

---

## 8. Gaps for a learning subsystem (what does NOT exist today)

1. **No immutable decision records.** A verdict exists as mutable `messages` columns plus one untyped JSON blob in `msg_events.detail` (`kind='classify'`, `engine.py:1945-1949`). There is no decision id, no `decided_at`, no model/prompt version, no input/feature snapshot, and the blob is clipped at 8000 chars (`store.py:670`) so it is not reliably machine-readable.
2. **No verdict timestamps on messages.** `processed_at` is scan-insert time (`store.py:632`); `classified_at`/`updated_at` do not exist. The only time of a decision is `msg_events.ts` of the classify row.
3. **No model versioning / registry.** Retraining overwrites `heuristics.model`+`stats` in place (`app.py:2421`, `engine.py:3912`, `heuristics.py:438`). `classified_by` carries only `"heuristic:<id> <name>"` — a verdict cannot be joined to the model version (`trained_at`/`params`) that produced it. Same for the LLM: no model name/endpoint per verdict, and fallback usage is only a free-text `events` row (`engine.py:1042`).
4. **No label provenance history.** `user_tag`/`user_tag_by` are last-write-wins (`store.py:1203-1210`). The dataset relabel path writes `user_tag` **without** touching `user_tag_by` (`app.py:2556`) — source lost. Bulk tag/untag writes no `msg_events` row at all (`app.py:3782,3794` only `log_event`, which has no `msg_id`). Label changes are not reconstructible.
5. **No correction/label-change table.** Corrections happen implicitly: dataset relabel (`app.py:2540-2566`), reclassify (overwrites), dataset remove/reinclude (a bare id array in `heuristics.excluded`, `app.py:2582`) — no who/when/why, no propagation to other classifiers, no audit beyond a generic `events` line.
6. **No outcome/feedback table.** The closest signals are `undo_log` (filing reversed) and `keep_ids` (automation suppressed by Message-ID, no reason/link). Neither references a category, classifier, or decision id.
7. **No training-set records.** Training reads live queries over `messages` each time (`heuristics.py:248-275,278-319`); `stats` stores only counts (`trained_label_count`, `negatives`, `excluded` count). You cannot reconstruct which message ids trained a given model version, nor reproduce/evaluate it.
8. **No held-out evaluation data or persisted metrics.** `evaluate_heuristic` is in-sample (`heuristics.py:339-377`) and its result is never stored. No accuracy/precision history over time, no coverage/precision tracking of when a classifier fired in production.
9. **`llm_log` lacks provenance.** Columns are `(ts, msg_id, ok, error)` only: no model/endpoint/fallback flag, no latency, tokens, prompt hash, category, or cost; failures store `repr(exc)` text. Cannot attribute spend, failures, or quality to a model. Also, the single-message UI path logs an attempt even when no LLM was called (`app.py:4457`), so the cap counter is not a pure LLM-call count.
10. **No flow/rule identity in verdicts.** `messages.action_taken` embeds `flow:<name>` and `rule_id` is overwritten on every scan (and dangles after `delete_rule` `store.py:450-452` or `delete_flow` `store.py:496-499`). `flow_runs.message_id` is the only reliable link and only exists for live runs.
11. **No foreign keys / referential integrity.** `msg_events`, `undo_log`, `flow_runs`, `llm_log`, `keep_ids` all reference messages without FKs; `reset_folder_index` (`store.py:866-868`, on UIDVALIDITY change `engine.py:1331-1334`) deletes message rows and orphans history. `keep_ids`/`flow_runs` key on `msgid` text with no unique index on `messages.msgid` (`find_message_by_msgid` returns the lowest id, `store.py:770-777`).
12. **No actor/audit for settings, permissions, and most UI actions.** `set_setting` (`store.py:383-386`) has no log; permission changes, tag bulk actions, proposal dismissals have at best prose in `events`.
13. **Confidence is uncalibrated and unverified.** Stored as raw model output (`engine.py:1928,1936`) or heuristic probability; there is nowhere to record whether a verdict was later confirmed/corrected, so calibration/learning-to-reject is impossible from the store as-is.
14. **NULL/absent conflation and unbounded growth.** `llm_needs_reply` default NULL means "never classified", while `0` means "classified no" (filters use `=1`, `store.py:797`); `events` (5272 rows), `llm_log` (3599) and `msg_events` have no retention/rotation policy.
15. **No error/uncertainty capture for the LLM path.** Parse failures, truncated JSON, fallback switches and retries are only `events` text; `llm_log.ok` is binary with an `error` string, no error class.

---

### Appendix: live-DB verification (read-only)

`SELECT` probes against `/home/xrim/mail-triage/data/triage.db` on 2026-10-01 confirmed: all 23 tables above exist with the columns/defaults shown (including migration-added columns on `messages` and `heuristics.excluded`), indexes `idx_msg_sort`, `idx_msgev_msg`, `idx_undo_pending`, `idx_flow_runs`, `idx_chunks_message`, `idx_chunks_folder`; row counts: messages 3563, events 5272, msg_events 2, undo_log 2, heuristics 2, llm_log 3599, rules 12, flows 1, flow_runs 0, rule_proposals 0, keep_ids 0, agent_actions 5; `settings` has 60 keys (defaults + retired `assistant_actions_apply`).
