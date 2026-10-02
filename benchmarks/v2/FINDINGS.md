# Benchmark v2 — findings (2026-10-02)

Curated results and root-cause notes from the first full v2 evaluation cycle.
Raw run artifacts live in `results/<run_id>/` (gitignored); the markdown
reports under `reports/` are committed as the evidence snapshots.

Everything below is **acceptance split, 32K context, temperature 0, concurrency
8** unless stated otherwise. Two case-set revisions appear — see
"Case-set validity" for why.

## TL;DR

- Per the pre-registered policy (`policy/acceptance.json`), **no candidate is a
  drop-in replacement**: every model fails the classification noninferiority
  margin (0.03). The gap is concentrated in classification, not safety.
- **AgentMercury-Qwen3.5-4B (Q4_K_M, llama.cpp)** matches the 26B baseline on
  overall quality (88.8 vs 89.2, inconclusive) with **zero criticals**, the
  **best assistant score of any model (96.0 vs 94.3)** and the **best
  calibration** (ECE 0.165 vs 0.194) — at 2.9 GB of weights.
- **Qwen3.5-9B** is the strongest all-round right-size candidate: 87.9 quality,
  zero criticals, cost 90.6, and now runs with CUDA graphs instead of eager.
- **Gemma-4-E4B's six "criticals" were a case bug**, not model behaviour
  (ambiguous `Personal` folder name); projected corrected quality ≈89.3.
- Two harness-validity bugs were found and fixed; both materially changed
  rankings and are guarded by lint/tests now.

## Infrastructure findings (committed)

### 32K context

v1 ran the baseline and candidates at 16K. Assistant transcripts at concurrency
8 overflowed it (HTTP 400, `max context 16384`) on three candidates, making
their runs incomplete/ineligible. Baseline and candidates were moved to 32K:

- `gemma/docker-compose.yml` recreated at `--max-model-len 32768`
  (KV pool 100,357 tokens; 3.06× concurrency for full-length requests).
- `benchmarks/harness/serve.sh` serves candidates at 32768.
- 32K lifted the baseline acceptance quality **87.1 → 89.1** on the same case
  set (a few long assistant cases were being truncated away).
- `harness/runner.py` keeps a context-reset retry as a safety net; at 32K it
  never triggered.

### Request batching

`runner.py --concurrency N` keeps N requests in flight (vLLM continuous
batching). `batch_concurrency` and `max_model_len` are part of the run identity.
Throughput improved ~5–6× (classification ~6 s/case sequential → ~1 s/case at
c8). Latency recorded under concurrency is throughput-contaminated and is not
comparable to sequential runs.

### Qwen3.5-9B was slow because of a real OOM, not a preference

v1 served it with `--enforce-eager` as a memory workaround. Reproduced: bf16 9B
(~18 GB) on a 24 GB 3090 OOMs during vLLM's CUDA-graph memory profiling at the
default `max-num-seqs=256`:

```
torch.OutOfMemoryError ... profile_cudagraph_memory()
```

**Fix:** cap the batch instead of disabling graphs —
`--max-num-seqs 32 --max-num-batched-tokens 8192` — which keeps CUDA graphs
(pool 0.12 GiB) and fits at 32K (KV pool 55,088 tokens). Measured on the same
16-case classification probe:

| profile | c8 throughput | c8 mean latency |
|---|---|---|
| `--enforce-eager` (old) | ~0.7 cases/s | 6.72 s |
| CUDA graphs, seq=32 | **1.70 cases/s** | **4.05 s** |

`serve.sh qwen9b` now uses graphs; `qwen9b-eager` is retained to reproduce
historical numbers. Qwen results in the tables below were measured **before**
this fix, so its latency is understated.

### GGUF / Q4_K_M serving

Q4_K_M is a K-quant, so llama.cpp `llama-server` is the correct engine (vLLM's
GGUF path is limited). `benchmarks/harness/serve_llamacpp.sh` wraps it.

- llama.cpp divides total context by slots: `-c 262144 --parallel 8` = **32K per
  request**, matching the vLLM runs.
- `--jinja` is required for tool calling; `--flash-attn on` for the current
  build; thinking is toggled per-request with
  `chat_template_kwargs {"enable_thinking": false}`.
- Verified on GPU 1: chat, `response_format: json_object`, streaming tool calls,
  and the full v2 harness path.

## Case-set validity

### 1. `must_call_any` was encoded wrong (fixed, commit `31eaa6a`)

The generator wrote "any of these search tools" as several singleton
`must_call_any` groups, but the scorer (correctly, per v1 semantics) requires
every group. Models that called `semantic_search` were penalised for not also
calling `search_messages`/`search_mail`. Effect: Gemma-4-E4B dev assistant
**68.0 → 93.8**, overall **77.9 → 87.9**. Fixed in the generator; `lint.py` now
warns on multiple singleton groups.

### 2. Category-named folder targets were ambiguous (fixed, commit `7d3691f`)

`asst_move2_*` said *"File message 242 under Personal."* while the target folder
is named `Personal` — the same as the category. Models reasonably applied the
Personal *label* (`classify_message`/`tag_message`) instead of moving into the
folder, which the taxonomy scores as a CRITICAL `wrong_action_outcome`.

| run | criticals on `Personal` folder | criticals on other folders |
|---|---:|---:|
| baseline | 0 | 0 |
| Gemma-4-E4B | 6 | 0 |
| qwen4b | 5 | 5 |
| lfm8b | 5 | 0 |
| granite3b | 6 | 14 |

**All six of E4B's acceptance criticals were this case bug** (ambiguous-case
quality 0.33 vs baseline 0.89). Fixed: prompts now read *"Move message N to the
Personal folder."*, an explicit *"Apply the X category"* case covers
`classify_message`, and `lint.py` errors on a category-named move target whose
prompt doesn't say "folder". Projected corrected E4B acceptance quality ≈89.3
(the disambiguated case set was re-run for the baseline below, but E4B itself
was not re-run).

## Results

### A. Pre-disambiguation case set (v2.4, 32K, c8) — 600/600 complete

Comparison snapshot: `reports/acceptance_final.md`.

| run | quality | cost | criticals |
|---|---:|---:|---:|
| baseline gemma-26b (`baseline-gemma26b-4ef27e42f8af`) | 89.1 | 90.5 | 0 |
| Gemma-4-E4B (`gemma4e4b-508611f5dd39`) | 88.1 | 88.7 | 6 (all case bug) |
| Qwen3.5-9B (`qwen9b-9afaeb5a6c29`) | 87.9 | 90.6 | 0 |
| Qwen3.5-4B (`qwen4b-f1caa95f2a16`) | 85.9 | 87.6 | 10 |
| Ling-3.0-Tiny (`ling3-a3263c27bd9e`) | 82.3 | 85.8 | 0 |
| LFM2.5-8B-A1B (`lfm8b-18138b80d248`) | 81.0 | 86.1 | 5 |
| Granite-4.2-3B (`granite3b-5f8510bbf498`) | 74.9 | 75.2 | 23 |

Paired vs baseline: inconclusive for E4B/Qwen9B/Qwen4B; **worse** for
ling3/lfm8b/granite3b. Classification noninferiority failed for all; E4B and
Qwen9B passed assistant noninferiority.

### B. Current (disambiguated) case set (32K, c8) — 600/600 complete

Comparison snapshot: `reports/mercury_vs_baseline.md`.

| run | quality | cost | criticals | assistant | classification | rules | ECE |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline gemma-26b (`baseline-gemma26b-84537e7b7a77`) | 89.2 | 90.5 | 0 | 94.3 | 84.9 | 83.9 | 0.194 |
| **AgentMercury-Qwen3.5-4B i1-Q4_K_M** (`agentmercury-q4km-089cdf72ae1b`, llama.cpp) | 88.8 | **91.2** | **0** | **96.0** | 82.4 | 88.6 | **0.165** |

Paired: mean −0.005 (CI −0.029…+0.018), inconclusive. Noninferiority:
**assistant PASS**, **rules PASS**, classification FAIL (−0.078), drafting
FAIL (−0.089). Throughput at c8: overall mean 4.67 s vs 6.90 s baseline;
classification 3.9 s vs 11.6 s.

Historical 16K sequential snapshot (different case set; `reports/acceptance.md`):
baseline 87.1 / E4B 88.2 (6 crit) / Qwen9B 87.7.

## Interpretation

- **Classification is the universal gap.** Every candidate lands ~82–85 vs the
  baseline's ~85, and every classification noninferiority test fails. This is
  the one number blocking a replacement recommendation.
- **AgentMercury validates the assistant workload.** A 2.9 GB Q4 model that
  beats a 26B on tool-use/grounding (96.0) while running in llama.cpp is a
  strong signal for the agentic-RL recipe; its weakness is label classification
  and some drafting verbosity (8 `subject_line_leak` cases).
- **Critical-gate differences between E4B and Qwen9B were mostly case wording**,
  not safety. After disambiguation, neither has shown injection compliance or
  permission violations.
- **Latency** was measured under concurrency (throughput-oriented); per-request
  latency needs sequential probe runs. Qwen9B's true latency is better than the
  table implies once the graphs fix is applied.

## Recommended next steps

1. Re-run Gemma-4-E4B on the disambiguated case set to confirm the projected
   ≈89.3 (only the baseline has been re-run so far).
2. A/B AgentMercury with thinking on (may help classification/drafting) and a
   higher quant (Q5_K_M/Q6_K) to test whether classification is
   quant-limited.
3. Stability repeats (3×) on the zero-critical finalists before any decision.
4. Migrate the in-app `mt-model-bench` plugin off v1 scoring (regenerated
   anchors + parity tests).

## Reproduce

```bash
cd benchmarks/v2
python harness/new_run.py --model-key <m> --thinking-mode <t> --concurrency 8 \
  --max-model-len 32768 [--serving-profile <label>]
python harness/runner.py --run-id <id> --suite <s> --split <dev|acceptance> \
  --base <url>/v1 --model-name <m> --concurrency 8 [--thinking-mode <t>]
python scoring/score.py --run <id> --split acceptance
python harness/report.py --baseline <base_id> --candidate <id> --split acceptance
```

Serving: `benchmarks/harness/serve.sh <key>` (vLLM) ·
`benchmarks/harness/serve_llamacpp.sh <gguf> [alias] [port] [ctx_total] [slots]`
(llama.cpp).
