# Model bench plugin (`mt-model-bench`) — design record (2026-10-02, v2)

An in-app benchmark that answers one question for a user: **will the model I have
configured actually work in this environment, and what should I expect from it?**
It runs a bounded subset of the **benchmark v2** suite against the configured LLM
and prints an interpretable scorecard: severity-adjusted score, critical-failure
list, JSON validity, classify latency, and **reference anchors from the same
cases** for models evaluated in the v2 cycle.

## Shape

- **Kind**: `tool` (single tool `model_bench`).
- **Permissions**: `llm.complete` only. No mailbox reads, no `net.http`; the
  only data that reaches the endpoint is the embedded synthetic case text.
- **Limits**: 30s wall clock (sandbox max), 64 MB, 256 KB kv (run state +
  last report).
- **Probes**: `quick` = 14 (3 compatibility + 11 classification), `standard` =
  28 (4 + 21 + 3 instruction probes: draft, rule-learning guard case, thought
  summary). Scope from the tool argument or the plugin's `default_scope`.
- Case data lives **inside `dist/plugin.js`** (generated, ~30 KB). The subset is
  pinned in `benchmarks/v2/harness/gen_plugin_data.py`; the addresses in v2 are
  already fictional (`sean@westgate.edu`), no real contact data.

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

The classification scorer is a port of the **v2** scorer: a strict schema gate
(category enum, boolean `needs_reply`, numeric confidence in range, non-empty
`summary`/`reason`; any violation → HIGH "schema violation" and score 0), then a
checks-fraction per-case score (category acceptable, needs_reply, confidence),
junk handling incl. fabrication checks, and injection obedience → CRITICAL with
score 0. Severity weights are the v2 taxonomy (LOW 1 / MEDIUM 3 / HIGH 9 /
CRITICAL 27) and the aggregate keeps the saturating `1 - min(1, sum/9)` index, so
it stays comparable with the reference anchors. Condensed scorers cover drafting
(length bounds, required/forbidden content, no-subject-line contract), rule
learning (schema, guard semantics = empty actions, placement, no-dup tokens) and
thought summary (length, no quotes, single line). Reported: raw +
severity-adjusted aggregates, critical list, JSON validity over classification
probes, per-section scores, classify median/p95 latency, and a verdict band
calibrated to the reference fleet (see anchors below).

## Reference anchors (same cases, from the v2 run results)

Severity-adjusted over exactly this subset, computed by
`gen_plugin_data.py` from the latest `benchmarks/v2/results/<run>/attempts.jsonl`
per reference model (the v2.1 case set, 32K, concurrency 8):

| Subset | local gemma-26b | AgentMercury 4B Q4_K_M |
|---|---|---|
| quick (11 cls) | _generated_ | _generated_ |
| standard (21 cls) | _generated_ | _generated_ |

The subset is adversarially weighted (junk + two label-injections + tricky
cases), so scores sit below full-suite numbers; the card says so and tells users
to compare against the reference row, not the full-suite report. Anchors are
displayed from the embedded `DATA.reference` (regenerated with `--write`).

## Fidelity notes (deliberate deviations from production)

- `ctx.llm.complete` runs through `LLMClient._chat`: temperature 0,
  `response_format: json_object` for classify probes, **no thinking flag**
  (production classify runs `enable_thinking=true`). Timings therefore differ
  from the full benchmark; the card mentions the plugin path.
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
- v2 parity test (`benchmarks/v2/tests/test_plugin_data.py`): the embedded
  classify/draft/rules/summary data must equal what `gen_plugin_data.py`
  derives from the frozen v2 cases, and the subset must keep a label-injection
  case.

## Case ids (pinned)

- Quick (11): `cls_base_201/205/209/216/219/224/240/242`, `cls_junk_mash`,
  `cls_adv_a_242`, `cls_para0_205`.
- Standard adds: `cls_base_229/231/243/248/253/263`, `cls_junk_empty`,
  `cls_adv_b_201`, `cls_trunc_late_201`, `cls_adv_a_216`.
- Instruction probes: `draft_205_v0` (msg 205), `rules_guard`, `sum_1`.

## Re-generating the embedded data

```bash
cd benchmarks/v2
python harness/gen_plugin_data.py --check   # parity with the frozen cases
python harness/gen_plugin_data.py --write   # splice DATA into dist/plugin.js
```

The generator computes reference anchors from the latest committed-shaped run
results on disk (baseline gemma-26b, AgentMercury Q4_K_M) and keeps the existing
anchors if no run artifacts are present. If the frozen suite changes:
regenerate, bump the plugin/manifest version, and re-run the v2 suite + T50.
