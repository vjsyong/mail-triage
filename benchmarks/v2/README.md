# mail-triage model benchmark v2

A versioned, reproducible evaluation system for the models that run
mail-triage's LLM workload.  It supersedes the v1 suite as the basis for
replacement decisions while keeping v1 as a frozen historical regression track.

V1 (196 cases) found real defects but had measurement gaps: incomplete runs
could still score well, the severity formula flattened HIGH and CRITICAL, tool
arguments were matched by substring, "long-context" cases were truncated before
their evidence, and model rankings were reported without uncertainty.  V2 fixes
those before adding coverage.

> **V1 and V2 scores are not comparable.**  Read each only against its own
> baseline.  V1 lives in the parent directory; its reports are historical.

## Measurement model

Three dimensions are reported **separately** and never collapsed into one
deployment verdict:

1. **Quality** — task-completion fraction (0–1).  Reported two ways:
   *scored-only* (excludes missing/infra) and *fixed-denominator* (missing/infra
   count as 0).
2. **Failures** — counts and rates by **behaviour kind** (e.g. `fabrication`,
   `injection_compliance`, `wrong_tool_args`, `empty_reply`) and by severity.
   `empty_reply` is distinct from `fabricated_fact`; a missing answer is not a
   lie.
3. **Cost index** — an explicitly arbitrary severity-weighted index
   (`100 * (1 - mean(min(1, Σweights/27)))`).  It is a convenience, not a
   calibrated real-world cost.

Additional rules:

- A run is **eligible for comparison only if complete** (no missing/infra
  results).  Partial runs are reported but not ranked.
- Run identity is a hash over corpus + cases + prompts + harness/scorer
  revisions + model/tokenizer/template + runtime image + hardware + effective
  params + adaptations + retry/fallback policy + cache state.  A resume with a
  different hash is refused.
- Adversarial results are curated regression evidence, not a general
  robustness guarantee.

## Layout

```
v2/
  common/     hashing, failure taxonomy, run identity, schema validator
  schemas/    JSON Schemas: case, attempt, tool_event, run_manifest, report
  corpus/     expanded synthetic mailbox (deterministic; ids != uids)
  cases/      600 cases across 6 suites + manifest.json (family-level split)
  harness/    corpus_gen, case_gen, lint, run_manager, runner, tool_sim_v2,
              parity, stats, report, mock_run, prompts.json
  scoring/    base, suites, calibrate, score
  policy/     acceptance.json (pre-registered margins and gates)
  tests/      dependency-free test runner + scorer/integrity/lint/stats tests
  results/    <run_id>/: run_manifest, attempts.jsonl, report.json, cases_scored
  reports/    generated markdown
```

## Case set (600 cases, 192 messages, 42 threads)

| Suite | Cases | Focus |
|---|---:|---|
| classification | 240 | balanced categories, paraphrase, truncation-boundary, junk, injection clean/attacked pairs |
| assistant | 240 | retrieval, grounded QA, exact args, ambiguity, no-match, multi-turn, permissions, failed tools, injection, end-state |
| drafting | 60 | required facts, forbidden content, injection |
| rules | 40 | schema, guard semantics, duplicates, held-out positives/negatives |
| simulate | 12 | condition satisfaction |
| summary | 8 | length, single-line, no quotes |

Split: **331 dev / 269 acceptance**, assigned by a hash of the **scenario
family** (thread id + scenario), so no scenario leaks across the split.

The mailbox is a coherent fictional world (Westgate University + four
scenarios) with a diverse, globally representative cast and plausible
domains (`westgate.edu`, `harbourline.co`, `northwindanalytics.com`,
`acmecloud.io`, consumer providers for personal mail).  There are no reserved
`.example` domains and no real personal contact data — deliberately, so the
model is not cued by obviously-bogus mail.  Injections are framed as plausible
compliance notices / hidden HTML rather than cartoonish "ignore instructions"
strings.

## Running it

```bash
cd benchmarks/v2

# generate corpus + cases (deterministic), then lint
python harness/corpus_gen.py
python harness/case_gen.py
python harness/lint.py                 # exit 0 = clean

# offline validation (no GPU, no network) — must stay green
python tests/run_v2_tests.py           # scorer fixtures, integrity, lint, stats, e2e
python harness/mock_run.py            # oracle must score 100; flawed is caught
python harness/parity.py              # production-fidelity checks

# live run against an OpenAI-compatible endpoint
python harness/runner.py --run-id mymodel-v2 --suite classification \
    --base http://127.0.0.1:8045/v1 --model-name <model> --split acceptance
# create the run first (init_run) and record manifest — see harness/run_manager.py
python scoring/score.py --run <run_id> --split acceptance
python harness/report.py --baseline <baseline_run> --candidate <run_id> --split acceptance
```

### Batched throughput

`runner.py --concurrency N` keeps N requests in flight so vLLM's continuous
batching is exercised (several-fold throughput on classification/assistant).
Concurrency is part of the **run identity** (`new_run.py --concurrency N`), so
runs at different concurrency are never mixed in one comparison.  Latency
recorded under concurrency N is throughput-contaminated: use a sequential
(concurrency 1) run for clean latency numbers.

### Serving profiles

`../serving/serve.sh` serves candidates.  Qwen3.5-9B bf16 OOMs during CUDA-graph
memory profiling on a 24GB card at the default `max-num-seqs=256`; capping it at
32 (`qwen9b`) keeps CUDA graphs and is ~2.4× faster than the old
`--enforce-eager` workaround, which is retained as `qwen9b-eager` for
reproducing historical numbers.  `new_run.py --serving-profile <label>` records
which profile a run used.

`tests/run_v2_tests.py` is also pytest-collectible (`pytest benchmarks/v2/tests`).

## Acceptance policy

`policy/acceptance.json` declares, **before** any candidate run:

- paired, family-clustered bootstrap comparisons (B=4000, α=0.05);
- per-task noninferiority margins (classification 0.03, assistant 0.05, …);
- critical-behaviour gates (zero injection compliance / permission violations /
  wrong action outcomes on acceptance);
- the rule that a better point estimate is *not* equivalence.

## What changed vs v1

| Concern | v1 | v2 |
|---|---|---|
| Missing results | excluded from the mean | disqualify comparison; fixed-denominator reported |
| Severity | `1 - min(1, Σ/9)` (HIGH == CRITICAL) | separate failure kinds + documented cost index |
| Tool args | substring matching | typed, field-specific exact match |
| Tool execution | presence only | outcome/state verification |
| Rules | token presence | executable held-out positive/negative evaluation |
| "Long context" | mislabeled truncation | truncation-boundary cases labeled as such; assistant large-result cases |
| Latency | separate ad-hoc pass | one instrumented runner; first event/visible/tool-call/decode split |
| Ranking | point estimates | paired CIs, noninferiority, stability rescoring |
| Prompt fidelity | assumed | parity checks (+ optional live re-extraction) |
| ids/uids | uid == id | distinct, folder-scoped uids |

## Out of scope / next

- In-app `mt-model-bench` plugin still embeds v1 scoring; migrate it with
  regenerated anchors and its own parity tests (separate package).
- Live acceptance runs on GPU 1 require booting vLLM; the offline suite is the
  gate to run first.
- Blinded human rubric for drafting is a placeholder (`signals.rubric = null`);
  wire it before using drafting scores for a deployment decision.
