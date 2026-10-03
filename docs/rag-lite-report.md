# RAG-Lite evaluation: CPU retrieval for a mail archive

This consolidates the October 1, 2026 retrieval study previously documented on
the `rag-lite-eval` branch, plus the production-port validation recorded in the
[feature guide](features.md#semantic-search-rag). It preserves the measured
aggregate results without distributing the author's mailbox or prototype database.

Historical sources: `rag-lite-eval` at `483c326` (`docs/rag-lite-report.md` and
`docs/rag-lite-design.md`); production-port figures in `docs/features.md` at
`56848d9`. This consolidation reports existing measurements, not a new benchmark run.

## Question and implementation

Can a CPU retrieval stack replace GPU embedding/reranking services while keeping
useful search quality for semantic questions and exact email details?

The production implementation is [rag_lite.py](../rag_lite.py), with local model
providers in [rag.py](../rag.py):

```text
Query → sender/date/exact hints → metadata prefilter
      → fielded FTS5/BM25 + sqlite-vec candidates
      → reciprocal rank fusion (k=60) → top-20 cross-encoder rerank
      → deduplicated messages + context
```

The default uses Qwen3-Embedding-0.6B (1024 dimensions) and
jina-reranker-v1-turbo-en through FastEmbed/ONNX on CPU. Quote stripping reduces
repeated email history; subject/sender/body BM25 weights are 4/2/1. The legacy
index remains separate for switching back; a backend needs its own built index.

## Prototype evaluation

The study used **3,560 messages**, 5,761 raw / 5,235 cleaned chunks, **48 semantic
queries**, and a constructed **34-query metadata/lexical set**. Hardware was an
80-logical-core Xeon E5-2699 v4 host with 125 GB RAM. CPU candidates used ONNX;
GPU controls used TEI. This is not a laptop performance claim.

The full-coverage controls below used the same prototype corpus. Recall@k means
at least one expected message was found in the top k; MRR is mean reciprocal rank.

| Configuration | Semantic R@1 | Semantic R@5 | Semantic MRR | Metadata R@1 | Metadata MRR |
| --- | ---: | ---: | ---: | ---: | ---: |
| Lexical only, raw | 83.3% | 93.8% | 0.878 | 73.5% | 0.811 |
| Dense only, 0.6B, raw | 79.2% | 93.8% | 0.852 | 61.8% | 0.636 |
| Legacy control: 4B + GPU v2-m3 reranker, raw | 93.8% | 97.9% | 0.956 | 91.2% | 0.941 |
| 0.6B + same GPU v2-m3 reranker, raw | 93.8% | 97.9% | 0.956 | 91.2% | 0.934 |
| 0.6B, clean, no rerank | 89.6% | 95.8% | 0.924 | 79.4% | 0.826 |
| 0.6B + CPU jina-turbo, clean | 95.8% | 97.9% | 0.972 | 82.4% | 0.876 |

The embedding swap matched the 4B control on observed recall in these small sets.
The CPU reranker performed well on semantic queries but gave up metadata/exact
query quality versus the GPU reranker. This supports a practical CPU default,
not a universal equivalence claim.

## Production-port validation

The subsequent live backfill covered **3,556 messages / 6,642 chunks / 18 folders**.
On the 48-query semantic set:

| Mode | R@1 | R@5 | R@10 | MRR | Mean ms/query |
| --- | ---: | ---: | ---: | ---: | ---: |
| FTS | 85.4% | 91.7% | 95.8% | 0.884 | 16 |
| Vector | 81.2% | 97.9% | 100.0% | 0.885 | 233 |
| Hybrid | 91.7% | 97.9% | 100.0% | 0.942 | 175 |
| Hybrid + rerank | 97.9% | 100.0% | 100.0% | 0.990 | 1188 |

The port capped reranker passages at 1,200 characters. Prototype and live results
use different chunk counts and preprocessing and should not be pooled. The old
live GPU index had only about 60% coverage, so its live score is not a fair
model-quality comparator; the full-coverage prototype control is the relevant one.

## Resource tradeoffs

- **Retrieval VRAM:** zero for the CPU path versus approximately 10 GB for the
  evaluated GPU embedding/reranking services. LLM inference is separate.
- **Embedding vectors:** roughly 21–24 MB per prototype 0.6B set versus 53.6 MB
  for the larger-dimensional control; this excludes database/text/model files.
- **Model storage:** the evaluated fp32 0.6B ONNX embedder is about 2.4 GB on disk.
- **Memory:** about 1.9 GB RSS after embedder load. Sustained prototype backfills
  reached 13–18 GB with default ONNX settings; steady-state and bulk indexing
  requirements differ substantially.
- **Indexing:** prototype CPU throughput was about 0.62 chunks/s. First backfills
  can take hours; incremental updates are much smaller. Thread count, batching,
  sequence length, and hardware affect these figures.
- **Latency:** live hybrid search was about 0.2 s/query without reranking and
  1.2 s with it on the measured host. Some prototype timings were contaminated
  by concurrent work and are not used as release headline numbers.

## Reproduction boundaries

The [retrieval evaluator](../tests/retrieval_eval.py) and
[48-query set](../tests/eval_queries.json) are available. They require a populated,
indexed mailbox containing the expected messages; they are **not** an offline
synthetic acceptance test or a fresh-install demo. The author's source mailbox,
prototype database, and 34-query prototype artifacts are not included in this
release, so the historical figures cannot be reproduced from a clean clone alone.
Use a separately labeled query set to evaluate your own archive.

## Limits and next experiments

- Small query sets: one query changes recall by about 2.1 points (48 queries) or
  2.9 points (34). Small score differences do not establish statistical superiority.
- English evaluation dominates; multilingual mail is underrepresented.
- Prototype text came from stored snippets, whereas legacy indexing fetched
  longer bodies. This limits claims about long-email behavior.
- Thread grouping uses subject/counterpart heuristics, not full protocol threading.
- Measure on smaller CPU machines and add a redistributable synthetic retrieval
  corpus before claiming portable benchmark reproduction or laptop performance.
