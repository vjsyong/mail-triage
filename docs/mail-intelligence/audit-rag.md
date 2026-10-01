# Audit — Retrieval / Embedding stack (mail-triage @ c6db3c5)

**Scope:** `rag.py`, `embed/`, `engine.py`, `store.py`, `app.py`, `config.py` in `/home/xrim/mail-triage-intel`, plus live read-only queries against `/data/triage.db` inside the `mail-triage` container.
**Method:** source reading (all cited files diff-identical between the working tree and the running container) + read-only SQLite queries (`file:/data/triage.db?mode=ro`, sqlite-vec extension loaded only to read `vec_chunks`). No code or DB was modified.
**Audit date:** 2026-10-01 (DB snapshot 15:05 UTC; the indexer is live, so counts move — see §4 caveat).

---

## 1. rag.py — index schema and pipeline

### 1.1 Tables (all in the same `triage.db`)

`chunks` — one row per chunk, created in `store.py:184-194`:

```sql
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    folder TEXT NOT NULL DEFAULT '',
    uid INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    created INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunks_message ON chunks(message_id);   -- store.py:193
CREATE INDEX IF NOT EXISTS idx_chunks_folder  ON chunks(folder);       -- store.py:194
```

`chunks_fts` — FTS5 keyword index, `store.py:204`: `CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)`. Its `rowid` **is** the `chunks.id` (populated in `store.add_chunks`, `store.py:1056`). No explicit tokenizer → FTS5 default (`unicode61`).

`vec_chunks` — sqlite-vec KNN table, created by `store.ensure_vec_table(dim)` (`store.py:1038-1042`):
`CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[<dim>] distance_metric=cosine)`.
- **Name:** `vec_chunks`; **dims:** fixed by the first embedding at index time (`rag.py:246-247`); live DDL is `float[2560]`; **metric:** `cosine`.
- **Keying:** `rowid = chunks.id` (`store.add_vectors`, `store.py:1061-1067`, `INSERT INTO vec_chunks (rowid, embedding) VALUES (?,?)` with the vector packed as little-endian float32 via `struct.pack("%df", ...)`).
- Shipped sqlite-vec shadow tables exist alongside: `vec_chunks_rowids`, `vec_chunks_chunks`, `vec_chunks_info`, `vec_chunks_vector_chunks00` (verified live in `sqlite_master`).
- Read path: `store.vec_search(qvec, k)` → `SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance` (`store.py:1070-1077`).

`index_state` — resumable indexing state, `store.py:195-202`:
`folder TEXT PRIMARY KEY, uidvalidity INTEGER, last_uid INTEGER, messages_indexed INTEGER, status TEXT ('new'|'working'|'done'), updated INTEGER`.

`meta` — model/versioning, `store.py:203`; holds `embed_dim` and `embed_model` (set in `rag._ensure_dim`, `rag.py:206-222`).

### 1.2 How chunks map to messages

- `chunks.message_id` → `messages.id` (`messages` DDL `store.py:126-150`; identity = autoincrement id, with `UNIQUE(folder, uid, uidvalidity)` at `store.py:149`).
- `chunks.folder` and `chunks.uid` are denormalized snapshots of the message's location at index time — they let `rag.search` filter by folder without joining (`rag.py:476`) and are shown in results (`rag.py:489-492`). ⚠️ **They are not updated when mail moves**: `store.record_move` (`store.py:935`) only touches the `messages`/undo rows, and nothing ever runs `UPDATE chunks` (verified: 5 live rows where `chunks.folder != messages.folder`). `rag.search`'s folder filter therefore operates on the folder the message had *when chunked*.
- Moves between folders don't duplicate: `_index_one` re-resolves a moved message by Message-ID and reuses the canonical row (`rag.py:228-232`); message lookup by id via `store.messages_by_ids` (`store.py:1096-1101`), by uid via `store.get_message_by_uid` (`store.py:754-758`), by msgid via `store.find_message_by_msgid` (`store.py:770`).
- Lookup from a vec row: `SELECT ... FROM chunks c JOIN vec_chunks v ON v.rowid = c.id` (verified working under a connection with sqlite-vec loaded).

### 1.3 How embeddings are produced

- **Config resolution:** `rag.embed_config()` (`rag.py:24-37`) — UI settings win, blank falls back to env: `embed_base_url` → `config.EMBED_BASE_URL` (`config.py:32`), `embed_model` → `config.EMBED_MODEL` (`config.py:33`, default `Qwen/Qwen3-Embedding-4B`), `embed_protocol` (default `"tei"`), `embed_timeout` (default 180s, `config.py:34`), plus `embed_query_prefix`.
- **Server:** TEI (text-embeddings-inference 1.9) container `mail-triage-embed`, GPU 1, host port **8041** → container :80, `--model-id Qwen/Qwen3-Embedding-4B --dtype float16` (`embed/docker-compose.yml`). Live setting `embed_base_url = http://100.93.139.49:8041`.
- **`rag.embed(texts, kind)`** (`rag.py:88-118`):
  - Batches of `EMBED_BATCH = 24` (`rag.py:77`, loop `rag.py:101-102`).
  - **Instruct prefix usage:** `kind="query"` prepends the configured `embed_query_prefix`; **documents stay raw** (`rag.py:96-98`). Live setting `embed_query_prefix` = `"Instruct: Given a search query, retrieve relevant email messages from the user's mailbox\nQuery: "`.
  - TEI payload: `POST {base}/embed`, JSON `{"inputs": [texts...]}` → response is a raw JSON **array of vectors** (`rag.py:111-114`). OpenAI-compatible alternative: `POST {base}/embeddings`, `{"model","input"}` → `data[].embedding` sorted by `index` (`rag.py:103-109`).
  - Length check: server must return exactly one vector per input (`rag.py:115-117`), else `RuntimeError`.
  - `rag.embed_one(text, kind="query")` (`rag.py:121-122`).
- **Chunking:** `CHUNK_CHARS=1600` (~400 tokens), `CHUNK_MAX=2400`, `CHUNK_MIN=300` (`rag.py:74-76`); `chunk_text(header, body)` does sentence-packed splitting with a per-chunk header prefix (`rag.py:160-188`); header = `From: … | To: … | Date: … | Folder: … | Subject: …` (`_header_for`, `rag.py:191-194`), prepended to **every** chunk (`rag.py:188`). No overlap.
- **Dimension/model guard:** `_ensure_dim` (`rag.py:206-222`) stores `embed_dim`/`embed_model` in `meta` on first build; a change raises `RuntimeError` demanding a rebuild. `store.clear_rag()` wipes chunks/vec/fts/state/meta (`store.py:1130-1141`); `rag.rebuild()` wraps it (`rag.py:337-340`).

### 1.4 Rerank endpoint

- **Config:** `rag.rerank_config()` (`rag.py:40-52`); env defaults `config.py:35-37` (model default `BAAI/bge-reranker-v2-m3`, timeout 90s).
- **Server:** TEI container `mail-triage-rerank`, host port **8042** → :80, `--model-id BAAI/bge-reranker-v2-m3` (`embed/docker-compose.yml`). Live setting `rerank_base_url = http://100.93.139.49:8042`.
- **`rag.rerank(query, texts, top_n)`** (`rag.py:125-152`):
  - TEI protocol: `POST {base}/rerank`, `{"query": q, "texts": [...]}` → raw JSON `[{index, score}, ...]` (`rag.py:144-149`).
  - Cohere/Jina/Infinity protocol: `{"query","documents","top_n"}` → `{"results":[{index, relevance_score}]}` sorted desc (`rag.py:134-143`).
  - Returns `None` when no endpoint is configured or `texts` is empty (`rag.py:131-132`) — callers treat that as "rerank off".

### 1.5 RRF fusion + rerank flow — `rag.search()` (`rag.py:428-526`)

Constants (`rag.py:79-83`): `K_VEC=100`, `K_FTS=100`, `RRF_K=60`, `CANDIDATES=50`, `RERANK_TOP=30`.

1. Guard: empty index → `{"ok": False, "error": "the search index is empty …"}` (`rag.py:438-439`).
2. Vector channel: `qv = embed_one(q, kind="query")` → `store.vec_search(qv, 100)`; each hit contributes `1.0/(RRF_K + rank)` to `fused[chunk_id]` (`rag.py:443-448`).
3. Keyword channel: `fts_query(q)` builds an OR of quoted terms (`rag.py:420-425`) → `store.fts_search(..., 100)`; same RRF contribution (`rag.py:453-458`).
4. Failures are channel-local: vector failure degrades (note in `meta.note`) unless `mode="vector"`; keyword failure likewise (`rag.py:449-452`, `457-458`). `mode` ∈ `hybrid|vector|fts` (diagnostics / evals, `rag.py:431`).
5. Fused top `CANDIDATES=50` chunk ids (`rag.py:462`) → fetch chunks + messages (`rag.py:463-464`), apply `folder`/`since` filters (`rag.py:476-488`), build result rows carrying the fused `score` (`rag.py:489-494`).
6. Rerank (on by default via setting `rerank_enabled`, or `rerank_on` override; only if >1 row): top `RERANK_TOP=30` rows, chunk text truncated to 2400 chars, `rerank(q, texts)` → attach `rerank_score` and sort the head (`rag.py:496-511`). Errors degrade to a note (`rag.py:512-513`).
7. Dedupe: one result per message, best-scored chunk wins, then cut to `k` (`rag.py:515-523`).
8. Result shape: `{ok, results:[{chunk_id, message_id, folder, uid, from_addr, to_addr, subject, date, excerpt, score[, rerank_score]}], meta:{query, ms, channels, note}}` (`rag.py:489-493`, `524-526`). `excerpt` = chunk text minus header, whitespace-collapsed, 320 chars (`rag.py:197-201`).

Index write path: `_index_one` (`rag.py:225-251`) → fetch full text (`mc.fetch_full`, fallback snippet, `rag.py:240-241`) → `chunk_text` → **embed before insert** (`rag.py:245-246`) → `_ensure_dim` → `store.add_chunks` (chunks + FTS, `store.py:1045-1058`) → `store.add_vectors` (vec, `store.py:1061-1067`). `_repair_vectors` backfills vec rows for chunks that exist without vectors (`rag.py:254-276`).

---

## 2. What is stored per chunk; can we get a message-level vector?

**Per chunk row:** `id`, `message_id`, `folder`, `uid`, `seq` (0-based position in the message), `text` (chunk body **including** the From/To/Date/Folder/Subject header line), `created` (`store.py:184-192`). Note `folder`/`uid` are index-time snapshots, not live locations (§1.2). Plus one FTS row (`chunks_fts.rowid = chunks.id`, text) and one vector row (`vec_chunks.rowid = chunks.id`, 2560 × float32 = 10,240-byte blob). There is **no generic metadata JSON column** — sender/subject/category/labels live on the `messages` row (`store.py:126-150`).

**Message-level embedding vector: no, not stored today.** The only persisted vectors are chunk-level in `vec_chunks`. There is no `message_vectors` table, no centroid/class-prototype artifact anywhere (grep for `centroid`/`tfidf` across the repo returns nothing), and `meta` only holds `embed_dim` + `embed_model`.

The one message-level embedding that exists is **transient** and belongs to the flow topic-condition path: `engine._topic_vector` (`engine.py:781-792`) embeds `subject + "\n" + snippet[:2000]` as a *document* and caches it **in process memory only** (`_TOPIC_QUERY_CACHE`, `engine.py:753`; keyed by `(kind, text)`, wholesale-cleared when >300 entries, `engine.py:789-790`). It is never written to the DB.

**How many chunks per message (live, 2026-10-01 15:05 UTC):** mean **2.47**, median **1**, max **16**; **54.4%** of indexed messages have exactly one chunk. Distribution:

| chunks/msg | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| messages | 1154 | 321 | 220 | 130 | 91 | 46 | 23 | 32 | 34 | 13 | 20 | 18 | 13 | 2 | 2 | 1 |

**Derivable today:** a message vector can be obtained by pooling its chunk vectors — `SELECT c.id, v.embedding FROM chunks c JOIN vec_chunks v ON v.rowid = c.id WHERE c.message_id = ?` (verified read-only; mean-pooling a 3-chunk message returned a valid 2560-d vector). No code does this today — it would be new code (see §6).

---

## 3. index_state / folder-by-folder resumable indexing

**State:** `index_state` keyed by folder (`store.py:195-202`); helpers `index_state_get/put/touch` (`store.py:1144-1169`) and `index_overview` (`store.py:1172-1179`). `put` records `uidvalidity`, `last_uid`, `status`; `touch` increments `messages_indexed` per message.

**Resume logic** — `rag.index_pass(limit)` (`rag.py:279-334`):
- Skips entirely if `index_enabled` is off (`rag.py:281-283`).
- Folder list = setting `index_folders`, else `default_folders(sorted(mc.folders()))` = all folders minus case-insensitive substring exclusions (`rag.py:287-288`; `default_folders`/`excluded_substrings` `rag.py:55-71`; live exclusions: junk, deleted, trash, sync issues, calendar, contacts, journal, conversation history, outbox, rss feeds).
- Per folder: read state; if IMAP `UIDVALIDITY` changed → `store.delete_chunks_folder(folder)` + reset state (`rag.py:296-299`); resume from `UID last+1:*`, else `ALL` (`rag.py:300-304`); no UIDs → mark `done` (`rag.py:305-307`).
- Per message `_index_one` (`rag.py:314`, def `rag.py:225-251`); idempotent (chunk count check `rag.py:237-239`); `index_state_touch` after each (`rag.py:323`). 5 consecutive failures abort the pass (`rag.py:317-321`). When a folder's UID list is exhausted it is marked `done` (`rag.py:324-325`).
- Pass stops after `limit` messages and returns `{processed, folders_done, folders_total, remaining, summary}` (`rag.py:326-332`) — this is what makes it resumable across passes.

**Background thread** — `rag.Indexer` (`rag.py:350-415`): `trigger(rebuild=False)` (`rag.py:362-365`); `run()` does a forced continuous backfill (passes of 40 until `remaining==0`/no progress/400-call safety valve), otherwise an idle timer `index_refresh_minutes` (default 10) runs a passive pass of 60 (`rag.py:367-384`, `386-415`). Started at app boot: `indexer.start()` (`app.py:6841`).

**Triggers:**
- Dashboard buttons "Index now" / "Rebuild" (`app.py:2011-2013` and `app.py:2123-2132`) → `POST /index/run` → `indexer.trigger()` (`app.py:2250-2252`); `POST /index/rebuild` → `indexer.trigger(rebuild=True)` (`app.py:2257-2260`). Rebuild clears the index first (`rag.rebuild()`, `rag.py:337-340`; `store.clear_rag`, `store.py:1130-1141`).
- CLI: `docker exec mail-triage python app.py --index` (loop of `index_pass(limit=40)` with retry + stall detection) and `--reindex` (wipe then loop) (`app.py:6810-6839`; README.md:191-192).
- Settings: `index_enabled`, `index_folders`, `index_refresh_minutes` (`store.py:45-48`; UI `app.py:5694-5711`); embedding/rerank endpoints in Settings → RAG (`app.py:5549`, connectivity tests `app.py:5840-5854`).
- Status surface: `app.index_status()` (`app.py:252-267`) = thread state + `rag.index_stats()` (`rag.py:343-347`) + `store.index_overview()`.

---

## 4. Live DB stats (read-only)

Recipe used (read-only; extension loaded only to read the vec0 table):

```python
con = sqlite3.connect("file:/data/triage.db?mode=ro", uri=True)
con.enable_load_extension(True); import sqlite_vec; sqlite_vec.load(con)  # only needed for vec_chunks
```

Run inside the container: `docker exec -i mail-triage python3 - < script.py`.

**Snapshot 2026-10-01 15:05 UTC** — ⚠️ the resumable backfill is *running* (it grew 5,239 → 5,250 chunks, 2,120 → 2,127 distinct messages during this audit; folder `Notifications` status = `working`), so treat these as a point-in-time view:

| Metric | Value |
|---|---|
| `chunks` rows | **5,250** |
| `chunks_fts` rows | 5,250 (1:1 with chunks) |
| `vec_chunks` rows | **5,250** (1:1, keyed by chunk id) |
| Distinct messages with ≥1 chunk | **2,127** |
| `messages` rows total | 3,563 |
| **Messages with NO indexed chunks** | **1,436** |
| Embedding dims | **2560** (`meta.embed_dim=2560`; `vec_length()=2560`; blob = 10,240 B) |
| Distance metric | cosine (`vec0` DDL) |
| `meta.embed_model` | `Qwen/Qwen3-Embedding-4B` |
| Chunks per message | mean 2.47 / median 1 / max 16; 54.4% single-chunk |
| Chunk `created` window | 2026-10-01 03:22 → 13:27 (local) |

**Coverage by folder** (snapshot minutes earlier; `with_chunks / messages`):

| Folder | messages | with chunks | missing |
|---|---|---|---|
| Notifications | 1357 | 514 | 843 (backfill in progress) |
| Newsletters | 807 | 806 | 1 |
| INBOX | 649 | 647 | 2 |
| Sent Items | 309 | 0 | 309 (not yet reached) |
| Promotions | 264 | 0 | 264 (not yet reached) |
| Archive | 136 | 133 | 3 |
| Canvas | 18 | 18 | 0 |
| Receipts | 16 | 0 | 16 |
| Personal | 2 | 0 | 2 |
| Tasks | 2 | 0 | 2 |
| Drafts | 1 | 1 | 0 |
| PO/DPO | 1 | 0 | 1 |
| Trash | 1 | 1 | 0 |

Footnote: the `Trash` row is 1/1 despite Trash being on the exclusion list because that message was indexed while it lived in `Newsletters` and was moved later — `chunks.folder` is not rewritten on moves (§1.2). The 1-3 missing messages in `INBOX`/`Newsletters`/`Archive` are newer than their folder's last index pass (incremental refresh pending or vectors awaiting `_repair_vectors`). `Sent Items` / `Promotions` / `Receipts` / `Personal` / `Tasks` / `PO/DPO` are simply not yet reached by the folder-by-folder backfill.

**`index_state` rows** (folder, status, last_uid, messages_indexed): Archive `done` 1708/133 · Canvas `done` 22/18 · Drafts `done` 1083/1 · INBOX `done` 23542/646 · Newsletters `done` 1519/813 · Notes `done` 0/0 · Notifications **`working`** 852/513 (as of the 13:2x snapshot). Only 7 folders have state rows; the rest of the wanted folders are queued behind the resumable pass.

---

## 5. engine.py usage of rag functions during scan/classify

### 5.1 `semantic_search` assistant tool
- Schema/description: `engine.py:2405-2410`; dispatch is `getattr(self, "_tool_" + name)` (`engine.py:3220-3226`); implementation `_tool_semantic_search` (`engine.py:3497-3525`).
- Calls `rag.search(q, k=limit (≤20), folder, since)` (`engine.py:3507-3509`).
- Returns `message_id / folder / from / subject / date / excerpt` per hit (`engine.py:3514-3516`) — **`score` and `rerank_score` are used only for ordering and are not exposed**; a learner wanting them must call `rag.search` directly.
- Rerank applies per the `rerank_enabled` setting (default true) (`rag.py:496-497`).

### 5.2 Topic flow conditions (`kind:"topic"`) — scan and classify
Flow conditions of kind `topic` are evaluated by `_cond_matches` (`engine.py:838-839`) → `_topic_matches` (`engine.py:795-819`).

- **Endpoint:** the configured embed endpoint — same one as the index/query path (`rag.embed_one`, `rag.py:121-122`; live: TEI `http://100.93.139.49:8041`, model `Qwen/Qwen3-Embedding-4B`).
- **What text is embedded for the message:** `ctx["text"][:2000]` (`engine.py:804`), embedded as a **document** (no prefix). The ctx text depends on the caller:
  - Scan (`_process_folder`, `engine.py:1328-1418`): `subject + "\n" + snippet` (`engine.py:1358`).
  - Classify phase (`classify_and_store`, `engine.py:1916+`, fuzzy flows evaluated after the verdict): `subject + "\n" + snippet` plus the verdict in ctx (`engine.py:1973-1974`).
  - Simulator/dry-run (`engine.py:1581`): `subject + "\n" + body` — full body, so dry-run topic scores can differ from live scan scores.
- **What text is embedded for the condition:** the description is embedded as a **query** as `_TOPIC_INSTR + desc` (`engine.py:808`), where `_TOPIC_INSTR = "Instruct: Given a description of the kind of email the user wants to find, retrieve matching inbox messages\nQuery: "` (`engine.py:758-759`). Note: `rag.embed(kind="query")` **also** prepends the configured `embed_query_prefix` (`rag.py:96-98`) — and the live setting already holds a Qwen instruct string — so in the current live config the topic-query payload is *settings prefix + `_TOPIC_INSTR` + description*.
- **Score/threshold:** plain Python cosine (`_cosine`, `engine.py:772-778`) of document vs. query vectors ≥ threshold. Default `_TOPIC_MIN_DEFAULT = 0.45` (`engine.py:754`; calibration comment `engine.py:755-757`: relevant ~0.45-0.55, noise p90 ~0.36-0.41). Per-condition override via `c["threshold"]`; flow validation resolves the threshold at `engine.py:2963-2965` and stores `{"value": desc[:300], "threshold": round(max(0.2, min(0.95, th)), 2)}` (`engine.py:2966-2967`); UI renders "is about … (>=0.45)" (`engine.py:2788-2793`).
- **Failure handling:** embedding errors set `ctx["_topic_failed"]`, log at most once per 300 s, and the condition returns `False` (no match) (`engine.py:809-816`). The score is stashed as `ctx["last_topic_score"]` (`engine.py:817`) and surfaces in flow-run reasons as `"topic match %.2f"` (`engine.py:1411-1413`).
- **Caching:** `_topic_vector` memoizes `(kind, text) → vector` in `_TOPIC_QUERY_CACHE` (`engine.py:753`, `781-792`; cap 300 entries, wholesale clear).
- **When it runs:** scan-time flows = those without `kind:"category"` conditions (`engine.py:1355-1359`); category-dependent flows run in the classify phase after the verdict, first matching flow wins (`engine.py:1969-1977`). Deterministic conditions are checked first and short-circuit (`engine.py:843-864`).

---

## 6. Reusable for ML features

### 6.1 Consumable today, without new infrastructure

| Artifact | Where | Notes for a learning subsystem |
|---|---|---|
| Chunk embeddings (2560-d f32, cosine) | `vec_chunks.embedding`, keyed by `chunks.id` (`store.py:1061-1067`) | Full-scan read works under sqlite-vec; join to `chunks`/`messages` for labels. Ready for clustering/kNN/pooling. |
| Chunk text incl. header | `chunks.text` (`store.py:184-192`, header format `rag.py:191-194`) | Lossy-recoverable message content; `seq` gives order within a message. |
| Message metadata + labels | `messages` (`store.py:126-150`): `llm_category`, `llm_confidence`, `user_tag`, `status`, `action_taken`, `date_ts`, `processed_at`, … | The supervised signal for specialist models; `user_tag` is human ground truth. |
| Trained heuristic classifiers | `heuristics` table (`store.py:155-168`); `heuristics.py` (decision_list / naive_bayes) | Existing deterministic models; their outputs are already stored per message (`classified_by`). |
| BM25 sparse scores | `chunks_fts` + `store.fts_search` (`store.py:1080-1085`) | TF-IDF-style retrieval score, available per query; not persisted. |
| Dense distances / RRF fused scores / rerank scores | computed in `rag.search` (`rag.py:447`, `455`, `493`, `507`) | Ephemeral per query; capture at call time if needed as features. |
| Search API in diagnostic modes | `rag.search(mode='fts'\|'vector'\|'hybrid')` (`rag.py:428-431`) | Lets ML code isolate channels. |
| Retrieval eval harness + labelled set | `tests/retrieval_eval.py` (modes `tests/retrieval_eval.py:31-34`), `tests/eval_queries.json`; baseline table README.md:157-167 | Existing recall@1/5/10 + MRR harness for measuring regressions of any new feature. |
| Per-folder index telemetry | `index_state` (`store.py:195-202`, `index_overview` `store.py:1172`) | Coverage/progress signal. |
| Feedback trails | `undo_log`, `msg_events`, `flow_runs` (`store.py:111-118`, `289-312`) | Implicit (undo/move) and explicit history usable as labels/outcome signals. |

### 6.2 Gaps — what would need to be added

1. **Message-level vector cache (the main missing artifact).** Only chunk vectors are persisted; there is no message vector table. Today it can be derived on demand by mean-pooling chunk vectors (`chunks c JOIN vec_chunks v ON v.rowid = c.id`, verified working) or by embedding `subject+snippet` once — but nothing stores it. A small `message_vectors(message_id PK, model, dim, vector, source, created)` table (or reuse of the in-process topic cache) would serve both ML specialists and the topic-condition path, which currently re-embeds per pass with an in-memory-only cache (`engine.py:781-792`).
2. **Centroids / class prototypes.** No centroid artifact exists anywhere in the repo (grep for `centroid` is empty). Building them needs message-level vectors + `messages.llm_category` / `user_tag` labels; storage would be a new table or small JSON blob.
3. **Persisted retrieval/feature log.** No table records query → candidates → fused/rerank scores; `rag.search` returns them but callers (`semantic_search` tool `engine.py:3497-3525`, topic matching) discard the numbers. A learner wanting retrieval-score features must either call `rag.search` itself or a new capture hook/table must be added.
4. **Coverage bias.** 1,436 / 3,563 messages have no chunks today (backfill mid-run; `Sent Items`, `Promotions`, `Receipts` etc. not yet reached), and the `messages` table itself is a scan window, not the whole mailbox. Any feature extraction over "the index" is trained on a currently biased subset — needs backfill completion or explicit coverage flags per message.
5. **Versioning.** A change of embedding model or dimension hard-requires a full rebuild (`rag.py:206-222`, `store.py:1130-1141`); any cached derived artifact (message vectors, centroids) must be versioned by `(embed_model, embed_dim)` and invalidated on rebuild.
6. **Optional quality note.** Rerank is on by default for search but topic conditions use raw cosine only; if ML features rely on ranking quality, keep the `tests/retrieval_eval.py` harness in the loop (README.md:160-167: hybrid R@1 93.8%, hybrid+rerank R@5 100% on 48 labelled queries).
