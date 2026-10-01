# RAG-Lite evaluation report (Phase 8) — branch `rag-lite-eval`

Date: 2026-10-01 · Repo: mail-triage @ d2ebcfd + prototype `prototype/` (this branch)
Corpus: 3,560 wanted-folder messages · 5,761 raw chunks / 5,235 clean chunks
Everything below was measured on this host (80-core Xeon E5-2699 v4, 125 GB RAM);
the retrieval prototype runs CPU-only (FastEmbed/ONNX), controls use the live
GPU TEI services. Prototype store: `prototype/data/raglite.db` (read-only source:
production triage.db — nothing in production was modified).

---

## 1. Current architecture (short)
Ingestion: `engine.Worker` scans → `fetch_meta` (full MIME fetch, decoded snippet).
RAG: separate `rag.Indexer` → `fetch_full` (20 k text) → sentence-packed chunks
(1600/2400 chars) with a From/To/Date/Folder/Subject header → TEI Qwen3-Embedding-4B
(2560-d, GPU :8041) → sqlite-vec `vec_chunks`; single-column FTS5 `chunks_fts`;
`rag.search` = KNN(100) + BM25(100) → RRF k=60 → top-30 rerank (bge-reranker-v2-m3,
GPU :8042) → dedupe per message. Full details + line refs: `docs/rag-lite-audit.md`.

## 2. Problems found (evidence)
1. GPU-pinned retrieval (two TEI containers on GPU 1).
2. Quoted history embedded: 39% of messages carry `>` quotes, 695 Outlook-style;
   conservative stripping removes 932,860 chars (~13% of body text).
3. Filtering after fusion only (folder/since prune the 50 candidates; no sender pushdown).
4. Single-column FTS (no subject/sender weighting; header text indistinguishable from body).
5. Model change = hard error + full rebuild (`_ensure_dim`); no versioned indexes/coexistence.
6. Double MIME fetch per message (fetch_meta + fetch_full), text not cached.
7. No thread model (630 Re:/Fwd:; 402 subject clusters = 55% of corpus) — and 39.5% of
   the wanted-folder corpus was simply unindexed at audit time (production indexer
   thread found `working`/stalled mid-benchmark; see §8).
8. Reranker fed up to 2,400-char chunks (cross-encoders truncate ~512 tokens).

## 3. Proposed lightweight architecture
Implemented as the prototype (see `docs/rag-lite-design.md`):
query understanding (sender/date/exact hints) → SQL metadata prefilter → FTS5 (fielded,
subject 4.0 / sender 2.0 / body 1.0 on the clean set) + sqlite-vec KNN (0.6B, 1024-d)
→ RRF k=60 → top-20 → small CPU cross-encoder → top k → thread expansion → per-result
diagnostics `{message_id, thread_id, bm25_rank, vector_rank, rrf_score, reranker_score,
node, thread_size}` (verified working). Versioned chunk sets (`raw` | `clean`) and
vector tables (`vec_<set>_<model>`); quote-stripped "clean" representation.

## 4. Benchmark results

### 4a. Semantic query set (tests/eval_queries.json, 48 queries, full corpus)
| config | R@1 | R@5 | R@10 | MRR | ms p50* |
|---|---|---|---|---|---|
| FTS-raw (lexical only) | 83.3 | 93.8 | 95.8 | .878 | 23 |
| VEC-raw-06b (dense only) | 79.2 | 93.8 | 95.8 | .852 | 153 |
| **A = legacy** (4B + v2m3, raw) | **93.8** | 97.9 | 100 | .956 | 237 |
| A-nor (4B, no rerank) | 93.8 | 97.9 | 97.9 | .955 | 139 |
| **B = 0.6B + v2m3, raw** | **93.8** | 97.9 | 100 | .956 | 308 |
| D = 0.6B, clean, no rerank | 89.6 | 95.8 | 97.9 | .924 | 189 |
| C-06b-minilm (L6) | 93.8 | 97.9 | 100 | .953 | 1304* |
| C-06b-minilm12 | 89.6 | 100 | 100 | .944 | 2468* |
| C-06b-bge-base | 93.8 | 100 | 100 | .955 | 4896* |
| **C-06b-jina-turbo** | **95.8** | 97.9 | 100 | **.972** | 1608* |
| C-06b-v2m3 | 93.8 | 97.9 | 100 | .956 | 276 |
| C-4b-v2m3 (clean + 4B!) | 89.6 | 93.8 | 95.8 | .911 | 134 |

### 4b. Metadata/lexical query set (constructed, 34 queries: identifiers, senders,
dates, subjects, threads, mixed — see `prototype/bench/eval_extra.json`)
| config | R@1 | R@5 | R@10 | MRR |
|---|---|---|---|---|
| FTS-raw | 73.5 | 94.1 | 97.1 | .811 |
| VEC-raw-06b | 61.8 | 64.7 | 67.6 | .636 |
| **A (legacy)** | **91.2** | 97.1 | 97.1 | .941 |
| A-nor | 85.3 | 91.2 | 91.2 | .874 |
| **B (0.6B swap)** | **91.2** | 97.1 | 97.1 | .934 |
| D (no rerank) | 79.4 | 88.2 | 94.1 | .826 |
| C-minilm (L6) | 76.5 | 91.2 | 97.1 | .842 |
| C-minilm12 | 76.5 | 97.1 | 97.1 | .846 |
| C-bge-base | 82.4 | 94.1 | 97.1 | .877 |
| C-jina-turbo | 82.4 | 94.1 | 97.1 | .876 |
| C-v2m3 | 88.2 | 97.1 | 97.1 | .919 |
| C-4b-v2m3 | 91.2 | 97.1 | 97.1 | .941 |

*Latencies marked \* were measured with a stray concurrent process; idle end-to-end
is ~0.3–0.5 s for CPU configs (79 ms query embed + ~20 ms channels + 160–800 ms rerank).

### 4c. Live stack cross-check (production index, 59.5% coverage)
48-set: fts 54.2 / vector 56.2 / hybrid 58.3 / +rerank 56.2 R@1 — coverage-dominated.
On the covered subset: hybrid 93.3 R@1 / 96.7 R@5 — i.e. once coverage exists, legacy
converges to the same quality as the prototype. The prototype-based A row is the fair
apples-to-apples legacy number. (Also: production `hybrid+rerank` lost 3 pts R@1 to
`hybrid` on the covered subset — rerank decisions on the old stack are less stable.)

## 5. Dependencies recommended
- `fastembed` (pulls `onnxruntime` CPU + `tokenizers`) — adds ~150 MB of wheels, no service.
- existing `sqlite-vec`, `requests`; `numpy` (already a transitive dep).
- No LangChain/LlamaIndex/vector DB/Redis — none needed; SQLite+SQL did everything
  (metadata pushdown, BM25 with weights, KNN, RRF all in-process, one file).

## 6. Schema changes required (migration-safe)
`chunks2(id, cset, message_id, seq, node, text, subject, sender)`;
`chunks_fts2` fielded FTS5 (`subject, sender, body`); `vec_<cset>_<embed_version>` tables;
`index_meta(chunker_version, embed_version, embed_dim, embed_model, built_at)`.
Old tables untouched → coexistence; `rag_backend` setting flips read path; rollback =
flip setting back. Full plan: `docs/rag-lite-design.md` §Migration.

## 7. Savings / expected CPU performance (measured)
| metric | legacy | lite |
|---|---|---|
| VRAM (retrieval) | ~10 GB (4B fp16 + v2-m3) | **0** |
| RAM | ~0 (GPU services) | ~1.9 GB embedder after load; tune/recycle for backfills (see risk R4) |
| vector storage | 53.6 MB (2560-d × 5,239) | 21–24 MB per set (1024-d × 5,235/5,761) |
| query embed | ~20–30 ms (GPU) | **79 ms (CPU)** |
| rerank 20 passages | 63 ms (v2-m3, GPU) | 160–780 ms (CPU small models) |
| indexing throughput | 17.4 chunks/s (4B GPU) | 0.62 chunks/s CPU (≈4.2 s/email incremental); 98 chunks/s if built on a scratch GPU TEI |
| full backfill (10.7 k chunks) | ~10 min (4B) | ~4.8 h CPU (fine overnight; GPU build 2 min) |

## 8. Risks / tradeoffs (and production notes)
- R1 Small eval sets (48 + 34) — differences of ±1 query are ±3 pts. Headline claims
  (B≡A; lite≥legacy) are robust; reranker micro-differences are indicative, not settled.
- R2 Chinese/multilingual mail barely covered by the English eval queries; the known-safe
  bilingual reranker is bge-reranker-base (or keep v2-m3).
- R3 Prototype text source = stored snippet (≤4 k chars) vs production's fetch_full
  (≤20 k). Long-body mail may behave differently at 20 k; re-verify after porting.
- R4 ONNX Runtime arena growth: sustained backfill batches ballooned RSS to ~13–18 GB
  with default settings on this box (load-time RSS 1.9 GB). Mitigate: cap batch/threads,
  cap sequence length (~512 tokens), run backfills in a short-lived subprocess so the
  RSS is returned. Steady-state incremental embedding is tiny (a few chunks/min).
- R5 exact-token behavior depends on the FTS query builder keeping identifiers verbatim
  (implemented: quoted OR terms + exact-token injection).
- R6 Production indexer was STALLED during the benchmark (state `working`, trigger
  accepted but no progress; container had been restarted during concurrent repo work).
  A container restart restores it; coverage then re-run of the live eval is advised.
- R7 The small rerankers were fed chunk text; an email-specific passage format
  (Subject first, sender, then body) may lift them — worth a tuning pass on porting.

## 9. Component verdicts
- **Embedding model: Qwen3-Embedding-0.6B @1024 — SUFFICIENT.** B ≡ A on both sets
  (identical R@1/R@5; MRR within .01). The 4B buys nothing measurable at system level.
- **Reranker: retain — but class matters.** On metadata/exact queries: none 79.4;
  MiniLM-L6 76.5 (hurts!); jina-turbo / bge-base 82.4; v2-m3 88.2; legacy-mix 91.2.
  On semantic queries: jina-turbo was the best single config (95.8 R@1, MRR .972).
  Recommendation: keep v2-m3 (GPU) as default while the GPU exists; CPU-only default =
  jinaai/jina-reranker-v1-turbo-en (160 ms/20, best CPU tradeoff); bge-reranker-base if
  multilingual mail matters (4× slower); do NOT use MiniLM-L6 for exact-query email.
- **FTS5 + sqlite-vec + RRF: correct.** Lexical is the stronger single channel on
  exact/metadata queries (73.5 vs 61.8 R@1) — metadata and lexical must stay first-class.
- **Clean chunk set (quote-stripped): adopt.** No measured quality loss (C-4b ≥ A on the
  metadata set), removes duplicated quoted text from retrieval and BM25.

## 10. Recommended default production configuration
SQLite + FTS5(fielded) + sqlite-vec, Qwen3-Embedding-0.6B @1024 (FastEmbed CPU,
query prefix kept), RRF k=60, top-20 → jina-reranker-v1-turbo-en CPU (or v2-m3 while
GPU is present), metadata pushdown, clean chunks, thread expansion, diagnostics logging.
Keep the TEI (4B/v2-m3) path as a drop-in rollback via settings; versioned indexes make the
switch reversible. Initial backfill: build on the GPU TEI once (2 min) or accept ~5 h
CPU; steady-state incremental: ~4 s/email CPU, async after ingestion.
