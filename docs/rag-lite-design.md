# RAG-Lite Phase 2/3: proposed replacement architecture (branch rag-lite-eval)

## Goals, in priority order
1. retrieval quality  2. auditability  3. CPU efficiency  4. simplicity
5. low dependency count  6. maintainability. No LangChain/LlamaIndex/vector DB.

## Proposed pipeline (prototype implements this exactly)

    query
      ↓ parse_query(): sender hints ('from sarah'), date words ('last month'),
        exact tokens (INV-39281, >5-digit refs, "quoted phrases")
      ↓ metadata prefilter (SQL over the messages table) — pushdown, not post-filter
      ↓ ┌ FTS5 BM25  (weighted: subject 4.0 / sender 2.0 / body 1.0 on the clean set)
        └ sqlite-vec KNN (Qwen3-Embedding-0.6B, 1024-d, cosine)
      ↓ RRF fusion, k=60 → top 20
      ↓ small CPU cross-encoder (optional; top-20 → reorder)
      ↓ dedupe per message → top 5-8 results
      ↓ thread/context expansion (same normalized subject + counterpart, ± days)
      ↓ diagnostics per result: bm25_rank, vector_rank, rrf, reranker_score,
        sender, subject, thread_size — auditable end to end

Design rules:
- **Never make the embedding model solve what SQL solves exactly.** Sender, date
  range, folder, exact identifiers are filters/boosts, not fuzzy problems.
- **Chunk sets are explicit objects.** `raw` reproduces today's representation
  (header + full body incl. quotes) for baseline comparison; `clean` is the proposed
  representation (quotes stripped, same packing, ≤3 chunks/message in practice).
- **Versioned everything.** Chunk set and embedding version are part of the index
  identity, not row mutations:
      index_meta: chunker_version, embed_version (e.g. qwen3-06b-1024-v1),
                  embed_dim, embed_model, built_at
      vec_<cset>_<embed_version>   (prototype naming, adopted on migration)
  → old and new indexes coexist; switching backend = flipping a setting; rollback =
  flipping back. The current single `vec_chunks` + `_ensure_dim` hard-error goes away.

## Email-aware indexing (Phase 3 decisions)
- `strip_quoted()` heuristic (prototype `raglite.py`): explicit markers
  (On ... wrote:, Outlook From:/Sent: block, Original Message, forwarded banners,
  CN 发件人:/写道:, long separators) first; otherwise the first ≥3-line `>` block.
  Conservative: only cuts when ≥40 chars of new content remain.
- FTS5 fields for the clean set: **subject / sender / body as separate columns**,
  bm25 weights (4,2,1). Attachments: none available (documented gap; would need a
  parse-side change to extract filenames — worth doing later, low cost).
- Embedding text for clean chunks keeps a light context header
  (From/To/Date/Subject) — contextual retrieval stays, quoted noise goes.
- Threads (prototype): normalized subject + counterpart address → `thread_key`,
  used for expansion/diagnostics. Recommended follow-up: parse In-Reply-To /
  References in `fetch_meta` (one-line header read at scan time) and persist real
  thread ids; the expansion step then uses protocol data instead of heuristics.

## Reranker options evaluated (small, CPU, ONNX)
| name | size | notes |
|---|---|---|
| Xenova/ms-marco-MiniLM-L-6-v2 | 80 MB | English-only, very fast |
| Xenova/ms-marco-MiniLM-L-12-v2 | 120 MB | English-only, slightly better |
| jinaai/jina-reranker-v1-turbo-en | 150 MB | English-only, fast |
| BAAI/bge-reranker-base | 1.0 GB | bilingual EN/ZH (mailbox-aware pick) |
| jinaai/jina-reranker-v2-base-multilingual | 1.1 GB | multilingual |
| BAAI/bge-reranker-v2-m3 (control, GPU) | TEI | current production |

## Embedding options
- `Qwen/Qwen3-Embedding-0.6B` via **FastEmbed (ONNX, CPU), 1024-d native** — the target.
  Same instruction prefix as production for queries.
- Control: production Qwen3-Embedding-4B via live TEI (GPU), 2560-d.

## Deployment notes (Phase 6 inputs)
- FastEmbed = pip `fastembed` (+ `onnxruntime` CPU). ~150 MB of wheels; no GPU, no
  service to run, model files ~2.4 GB on disk (Qwen3-0.6B fp32 ONNX).
- **Known install trap (hit in the prototype):** the HF snapshot for
  `Qdrant/Qwen3-Embedding-0.6B-onnx` uses symlinked blobs; ONNX Runtime ≥1.17 rejects
  the external-data file (`model.onnx_data`, 2.3 GB) because its resolved path escapes
  the model directory. Fix: dereference the snapshot (`cp -L` over the symlinks) once
  after download, or ship a corrected download step. Without this, load fails with
  "External data path validation failed".
- Integration shape for production: add an in-process provider to `rag.embed()`
  (`embed_protocol: local`) and `rag.rerank()` (`rerank_protocol: local`) so the
  Settings page keeps the existing remote options AND gains "Local (CPU, ONNX)" with
  model choice. No new service, no schema break for callers.

## Migration sketch (Phase 7)
1. Ship `rag_lite` indexer writing into NEW tables next to the old ones:
   `chunks2` (cset/node), `chunks_fts2` (fielded), `vec_<cset>_<version>` +
   `index_meta`. Old `chunks`/`chunks_fts`/`vec_chunks` stay untouched.
2. Setting `rag_backend = legacy | lite` (+ `rag_embed_version`, `rag_chunk_set`).
   `rag.search` dispatches; both indexes served by the same assistant tool.
3. Background backfill builds the lite index from the existing `messages` rows
   (no IMAP traffic needed for text already in `snippet`; full-text backfill can
   reuse `fetch_full` opportunistically).
4. Verify with `tests/retrieval_eval.py` + bench set; flip default when quality ≥
   legacy; keep the legacy tables for one release, then drop via a settings button.
5. Rollback = set `rag_backend=legacy` (instant, index still present).

## Scratch services used for this evaluation (host gpu-vm1)
- `rag06b` container: TEI Qwen3-Embedding-0.6B on GPU1, port 8043 (built the 0.6B
  vectors at 98 chunks/s). Relaunch if stopped:
  `docker start rag06b`  (or the docker run in the bench notes)
- Prototype venv: `~/mail-triage-rag/.venv-rag` (fastembed 0.8.1, sqlite-vec, numpy)
- Rebuild everything: `prototype/build.py all` then `prototype/eval.py`
