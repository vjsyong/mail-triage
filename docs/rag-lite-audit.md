# RAG-Lite Phase 1: Audit of the current retrieval stack (branch rag-lite-eval)

Repo: ~/mail-triage @ d2ebcfd. All line numbers are as of this branch point.
Method: read the code (rag.py, store.py, engine.py, app.py, config.py, embed/)
plus live-DB queries against a read-only copy of production triage.db.

## The 12 audit questions

### 1. Where email ingestion occurs
- Scan path: `engine.Worker` (`run_cycle`) → `engine._process_folder(folder)` walks IMAP
  UIDs from `index_state`-like tracking (`store.last_uid`), fetches each message with
  `MailClient.fetch_meta` (one full `BODY.PEEK[]` + MIME walk, `engine.py` ~L395-430),
  stores `messages` row + decoded `snippet` (≤4000 chars).
- RAG ingestion is a SEPARATE loop: `rag.Indexer` thread (`rag.py` L350-415) walks the
  same folders from its own `index_state(last_uid, uidvalidity)` and calls
  `rag._index_one` (L225) which does `fetch_meta` **again** plus `fetch_full`
  (another full fetch, text ≤20000 chars) — i.e. two full MIME fetches per message.

### 2. How email text is cleaned and chunked
- Cleaning: `engine.parse_full_message` (L294) — prefers text/plain, falls back to
  HTML→text, SKIPS attachments (L331). **No quoted-history stripping anywhere.**
- Chunking: `rag.chunk_text` (L160) — sentence-packed, 1600 chars target / 2400 cap /
  300 min, NO overlap, and every chunk is prefixed with a From/To/Date/Folder/Subject
  header (`_header_for`, L191) for contextual retrieval.

### 3. Where embeddings are generated
- `rag.embed()` (L88) — HTTP client: TEI `POST /embed` or OpenAI `POST /embeddings`;
  `EMBED_BATCH=24`; query-side instruction prefix from setting `embed_query_prefix`
  (live value: "Instruct: Given a search query, retrieve relevant email messages from
  the user's mailbox\nQuery: ").
- Called from: `_index_one`/`_repair_vectors` (doc side), `rag.search` (query side),
  engine `_topic_matches` (flow topic conditions), app settings test buttons.

### 4. Where embeddings are stored
- SQLite `vec_chunks`, a sqlite-vec vec0 table: `embedding float[dim] distance_metric=cosine`
  (`store.ensure_vec_table`, L883); rowid = `chunks.id`. dim + model recorded in `meta`
  (`embed_dim`, `embed_model`); changing either **hard-errors and demands a full rebuild**
  (`rag._ensure_dim`, L206).

### 5. How vector search currently works
- `store.vec_search` (L915): KNN `WHERE embedding MATCH ? AND k=? ORDER BY distance`,
  K=100 (`K_VEC`), fused in `rag.search` (L428).

### 6. Is FTS5 already enabled
- Yes. `chunks_fts` is a single-column FTS5 table (`text` = full chunk text incl.
  header), maintained in `store.add_chunks`; `store.fts_search` (L925) ranks with
  `bm25(chunks_fts)`; queries are built by `rag.fts_query` (L420) as OR-joined quoted
  terms. No field weighting possible (one column).

### 7. sqlite-vec present?
- Yes, `sqlite-vec>=0.1.9` (requirements.txt), loaded per-connection in `store.db(vec=True)`.

### 8. How reranking works
- `rag.rerank` (L125) → TEI `:8042` `BAAI/bge-reranker-v2-m3` (GPU), TEI or Cohere
  protocol. In `search`: fused top-50 → first 30 (`RERANK_TOP`) sent to the
  cross-encoder, re-scored; `rerank_enabled` setting gates it. Misses are graceful
  (notes appended on failure).

### 9. Metadata available
- `messages`: msgid, from_addr, to_addr, subject, date (string), date_ts, sort_ts,
  folder, uid, uidvalidity, snippet, body_html(+cids/at), status, llm_* columns.
- **thread_id: absent.** No In-Reply-To / References parsing at ingestion (the only
  occurrence, `engine.py` L387-388, sets those headers on OUTGOING drafts).
- **attachments: not captured.** Skipped at parse time (L331); `body_cids` covers only
  inline images for the HTML renderer. No filename index anywhere.

### 10. Is quoted history repeatedly embedded?
- Yes. The 20k-char body text (with full quoted replies) is chunked and embedded as-is.
- Measured on the live corpus (3,561 messages with snippets):
  - 1,404 (39%) contain `> ` quoted blocks; 695 contain Outlook-style quoted headers
    ("Sent:"); 188 inline "From: " quote starts; 57 classic "wrote:".
  - The prototype's conservative quote-splitter finds 410 clear cut-points and removes
    932,860 chars (~13% of all body characters) — the reproduced part of the archive.

### 11. What search results operate on
- Unit: chunk candidates → fused → reranked → **dedupe to one result per message**
  (best chunk). No thread-level results, no context expansion.
- Metadata filtering today: only `folder` + `since`, applied **after** fusion on the
  50 fused candidates (`rag.search` L476-488) — i.e. filtering cannot widen recall;
  it can only prune. Sender/date-range filters do not exist.

### 12. Public interfaces that depend on the RAG implementation
- `rag.search(q, k, folder, since, rerank_on, mode)` → dict {ok, results[], meta};
  consumed by the assistant tool `semantic_search` (`engine.py` L2139 definition,
  L3127 implementation; results feed the LLM with message_id/folder/from/subject/date/
  excerpt).
- `rag.index_pass(limit)` / `rag.rebuild()` / `rag.index_stats()` / `rag.Indexer`
  → app dashboard routes `/index/run`, `/index/rebuild`, dashboard status card,
  CLI `app.py --index|--reindex`.
- `rag.embed_one` → flow topic conditions (`engine._topic_matches`) + settings tests.
- `meta embed_dim/embed_model` + the `_ensure_dim` guard.
- Settings surface: embed/rerank base URL/model/protocol/key/timeout + query prefix.

## Problems / inefficiencies found (ranked)

1. **GPU dependency for retrieval.** Both jobs (embed 4B, rerank v2-m3) are TEI
   containers pinned to the second RTX 3090; retrieval stops if that GPU is needed
   elsewhere. Everything else in the app already has a CPU path.
2. **Quoted history embedded and BM25-indexed.** ~39% of messages carry quote blocks;
   they inflate every chunk set, blur embeddings, and make "what did X say" queries
   surface old content again.
3. **Filtering after fusion.** folder/since prune the 50 fused candidates; no sender /
   date-range pushdown, so metadata constraints cannot improve recall (and exact
   identifiers lean entirely on the OR-term BM25).
4. **Single-column FTS.** No subject/sender weighting; the From/To header text is
   indistinguishable from body text in BM25.
5. **Model change ⇒ full rebuild behind a hard error.** No versioned indexes, no
   coexistence, no rollback path; `clear_rag()` wipes everything (including FTS rows).
6. **Double MIME fetch per message** (fetch_meta for the row + fetch_full for chunking)
   and re-parse of the same bytes; text is not cached.
7. **No thread awareness** despite strong signals (630 Re:/Fwd: subjects; 402
   normalized-subject clusters covering 1,951 messages = 55% of the corpus).
8. **Reranker sends 30 full chunks** (up to 2,400 chars each) — far beyond a
   cross-encoder's useful window; latency spent on tokens that are truncated anyway.
9. Index coverage in production was 59.5% of wanted-folder messages at audit time
   (2,119/3,560) — an operational gap, not an architecture flaw, but it suppresses
   recall for whatever is missed.

## Existing quality baseline (from tests/retrieval_eval.py, 48 labelled queries)
Skill-recorded numbers on a previously full index: fts R@1 ~52%, vector ~88%,
hybrid R@1 93.8% / R@5 97.9% (MRR .951); hybrid+rerank R@5/R@10 100% (MRR .941).
These are being re-measured on the current index in Phase 5.
