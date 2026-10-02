# mail-triage model benchmark (`model-bench` workstream)

> **v2 is available.** The expanded, versioned evaluation system lives in
> [`v2/`](v2/README.md). V1 (this page) is a frozen historical regression track;
> v1 and v2 scores are not comparable. Use v2 for any new model decision.

A frozen 196-case regression suite that evaluates a local/offline LLM against
**the mail-triage app's real LLM workload** — exact production prompts, tool
schemas and call sites — so model choices are made on this app's evidence, not
generic leaderboards. Built 2026-10-02; findings and analysis in `reports/`.

## Headline results (frozen suite, scorer v1.2; severity-adjusted / criticals)

| model | sev-adj | raw | crit | classify median |
|---|---|---|---|---|
| gemma-4-26b-a4b (baseline, production) | 90.0 | 92.6 | 3 | 4.33 s |
| Qwen3.5-9B | 90.6 | 93.9 | 2 | 5.77 s* |
| Gemma-4-E4B | 85.0 | 90.3 | 1 | 6.05 s |
| Qwen3.5-4B | 82.5 | 87.4 | 4 | 0.74 s |
| Ling-3.0-tiny | 77.6 | 83.4 | 5 | 0.68 s |
| LFM2.5-8B-A1B | 68.9 | 76.2 | 5 | 0.34 s |
| Granite-4.2-3B | 63.8 | 73.0 | 6 | 0.74 s |

\* Qwen9B served enforce-eager (memory workaround); speed understated.

Read `reports/final-report.md` for the full analysis (injection robustness,
no-match honesty, adaptation costs, operating points, hardening backlog).
App-facing digest lives in the app repo: `docs/model-evaluation.md`.

## Layout

- `corpus/`   — synthetic 64-message mailbox (deterministic; threads, quotes,
  injections, multilingual cases)
- `cases/`    — 196 frozen cases: normal / ambiguous / junk / adversarial /
  long-context / tool-use / drafting / rules / simulate / summary (`FROZEN.md`)
- `harness/`  — runner (`run_model.py`, exact production payloads + agent loop),
  tool simulator, serving (`serve.sh`), pipeline scripts, latency pass,
  stability + comparison tooling
- `scoring/`  — deterministic scorer (severity weights 1/3/9/27)
- `results/`  — per-model runs: cases, `summary.json`, `latency.json`,
  `server.json` (config + adaptation), resources, stability, probes
- `reports/`  — `final-report.md`, `comparison.md`, `failure-log.md`
- `docs/`     — app LLM audit, dated research, candidate shortlist, taxonomy,
  scoring methodology

## Run a model (GPU 1, port 8045, never the production GPU)

```bash
cd harness
bash serve.sh <key>                      # boot vLLM for a candidate (keys in serve.sh)
bash finish_candidate.sh <key> <mode>    # all suites + latency (mode: auto | falsekw | explicit)
python ../scoring/score.py --model <key>
python compare.py --keys <k1,k2,...>     # renders ../reports/comparison.md
```

Adaptation modes: `falsekw` pins `enable_thinking=false` on every call (models
that grit greedy CoT otherwise); `explicit` passes the thinking flag per call
site as the app intends (models whose template defaults thinking on for
un-flagged calls). Per-model choice is recorded in `results/<key>/server.json`.

## Adding a model

1. download line in `harness/download_models.sh` + a `serve.sh` case entry
   (dir, model name, parser flags, image if the arch needs a newer vLLM);
2. `bash serve.sh <key>` — smoke must pass before any run;
3. `finish_candidate.sh <key> <mode>`, score, compare.

The suite is frozen; regenerate cases only via `harness/gen_cases.py` with the
same seed inputs, and re-score all models afterwards for comparability.
