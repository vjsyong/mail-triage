# Model bench plugin (`mt-model-bench`) — design record (2026-10-02)

An in-app benchmark that answers one question for a user: **will the model I have
configured actually work in this environment, and what should I expect from it?**
It runs a frozen subset of the model-eval suite (workstream `model-bench`,
worktree `~/mail-triage-bench`) against the configured LLM and prints an
interpretable scorecard: severity-adjusted score, critical-failure list, JSON
validity, classify latency, and **reference anchors from the same cases** for
the models that were evaluated in the original study.

## Shape

- **Kind**: `tool` (single tool `model_bench`).
- **Permissions**: `llm.complete` only. No mailbox reads, no `net.http`; the
  only data that reaches the endpoint is the embedded synthetic case text.
- **Limits**: 30s wall clock (sandbox max), 64 MB, 256 KB kv (run state +
  last report).
- **Probes**: `quick` = 14 (3 compatibility + 11 classification), `standard` =
  28 (4 + 21 + 3 instruction probes: draft, rule-learning guard case, thought
  summary). Scope from the tool argument or the plugin's `default_scope`.
- Case data lives **inside `dist/plugin.js`** (generated; ~56 KB). Embedded
  copies replace the mailbox address with `user@example.com`; expectations are
  untouched.

## Why it runs in slices

The sandbox caps every invocation at `limits.timeout_ms` (30s) and kills the
worker past it — three kills auto-disable a plugin. The bench therefore runs
as many probes as safely fit in the remaining deadline (it measures elapsed
time and observed call latency to decide), **persists run state in kv after
every probe**, and returns "call again to continue". A bare re-call continues
the run in progress; an explicit `scope` change or `reset: true` starts over.
A 3-in-a-row endpoint error streak aborts the run with an explanatory card; a
missing `llm.complete` grant returns a clear "enable it on the Plugins page".

## Scoring

A faithful port of the eval scorer's classification logic (severity weights
LOW 1 / MEDIUM 3 / HIGH 9 / CRITICAL 27; per-case severity-adjusted score =
`1 - min(1, sum(weights)/9)`; junk handling incl. fabrication checks; injection
obedience → CRITICAL with score 0), plus condensed scorers for drafting
(length bounds, required/forbidden content, no-subject-line contract),
rule learning (schema, guard semantics = empty actions, placement, no-dup
tokens) and thought summary (length, no quotes, single line). Reported:
raw + severity-adjusted aggregates, critical list, JSON validity over
classification probes, per-section scores, classify median/p95 latency, and
a verdict band calibrated to the reference fleet (see anchors below).

## Reference anchors (same cases, from the committed eval results)

Severity-adjusted, computed from `benchmarks/results/<model>/classification.jsonl`
for exactly this subset:

| Subset | local gemma-26b | qwen3.5-9b | gemma-e4b | qwen3.5-4b |
|---|---|---|---|---|
| quick (11 cls) | 78.8 | 90.9 (1 crit) | 84.8 | 90.9 (1 crit) |
| standard (21 cls) | 79.4 | 84.1 (1 crit) | 82.5 | 81.0 (2 crit) |

The subset is adversarially weighted (junk + injection + tricky cases), so
scores sit below full-suite numbers; the card says so and tells users to
compare against the reference row, not the full-suite report.

## Fidelity notes (deliberate deviations from production)

- `ctx.llm.complete` runs through `LLMClient._chat`: temperature 0,
  `response_format: json_object` for classify probes, **no thinking flag**
  (production classify runs `enable_thinking=true`). Timings therefore differ:
  local gemma measured ~0.45s/probe here vs ~7.3s with thinking on. Both are
  honest; the card mentions the plugin path.
- `max_tokens` is 2048 (host clamp) vs 4096 + 8192-retry in production.
- If the endpoint rejects `response_format`, the app strips it automatically;
  that shows up as reduced JSON-parseability rather than an error — which is
  exactly what the user would experience.
- If a fallback endpoint is configured and the primary is down, a fallback may
  serve the run (the app logs "served by fallback"); the card footer says to
  check the Log page.
- Assistant tool-calling is **not probed** (needs the app's agent loop); the
  card says JSON reliability + instruction adherence are the proxies.

## Verification

- Suite section **T50** (+16 checks): real sandbox completion, exact scoring
  math against a deterministic mock fixture (85.7/85.7, 1 critical, 90.9%
  JSON), truthy critical-injection detection, scorecard card, stored-report
  re-call, **3s-wall-clock slice + resume with identical scores**, assistant
  inventory + capability entry, detail page, config form round-trip.
- Live smoke against the real `gemma-4-26b-a4b` endpoint (isolated data dir):
  quick = 81.0 sev (0 crit, JSON 100%), standard = 86.9 sev (0 crit, JSON
  100%, drafting/rules/summary 100).

## Case ids (pinned)

- Quick (11): cls_normal_101/104/170/130/190/180, cls_reply_112/113,
  cls_ml_zh_invoice, cls_junk_mash, cls_adv_299.
- Standard adds: cls_reply_140, cls_ambig_270/121, cls_ml_zh_meeting,
  cls_junk_empty/ctrl, cls_adv_290/official_claim, cls_long_statement,
  cls_contra_subj_body.
- Instruction probes: draft_d1_citations (msg 214), rules_lg3_guard_never_move,
  sum_1.

## Re-generating the embedded data

The generator reads the bench worktree and rewrites `dist/plugin.js` +
`manifest.json` (kept out of the repo as a one-off build step; the committed
plugin.js is the artifact). If the frozen suite changes: regenerate, bump the
version, re-check the T50 quick-set fixture counts (the pinned 14/85.7 numbers
come from the mock, not the data), and re-run the suite.
