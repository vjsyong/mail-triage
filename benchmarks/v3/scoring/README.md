# Benchmark v3 scoring (WP5)

Scoring, statistics, calibration and executable qualification gates. Stdlib
only, offline, no adapters/harness import. It consumes the shared dataset/run
bundle and the frozen WP0/WP1 contracts (`contracts.observable_in`,
`field_provenance`, `common.identity`).

```python
from benchmarks.v3.scoring import (
    default_policy, score_run, compare_runs, render_markdown, write_report,
    fit_calibrator, apply_calibrator, validate_probability,
)
```

## Bundle shape

`dataset`:

```python
{
  "schema_version": "v3.0",
  "dataset_id": str,
  "cases":   [case, ...],      # case.schema.json
  "gold":    [gold, ...],      # gold.schema.json, linked by gold_id/case_id
  "scenarios": [...], "policies": [policy_card, ...], "lineage": [...],
  "provenance": [...],
  "metadata": {"review_status": "draft", ...},
}
```

`run`:

```python
{
  "manifest": <foundation run manifest>,
  "attempts": [attempt, ...],  # attempt.schema.json
  "metadata": {"deployment": {...}, ...},
}
```

`gold.answer` for triage carries `category`, `acceptable_categories`,
`needs_reply`, `required_outcomes`, `forbidden_outcomes`,
`supporting_evidence`; workflow gold adds structured `expected_state` and
`assertions`. `gold.observable` maps each declared field to a documented level
(`visible`/`retrievable`/`full_context`/`ambiguous`/`unavailable`).

An attempt's decision is taken from `output.parsed` **only** when
`field_provenance` says the field is `produced`/`derived`; `missing` means the
value is ignored (no fabricated credit). `attempt` numbers must be unique per
case and every attempt's `run_id` must equal the manifest's.

## Report dimensions (never pooled)

Each `profiles.<profile>` block reports, separately:

- `coverage` — observable decisions, produced, abstained, context-limited,
  metadata-missing, errored, missing; a missing/abstained prediction stays in
  the denominator and can never raise the headline score.
- `category` — single-gold accuracy and macro-F1 (recomputed, not per-row
  correctness) plus a separate `acceptable_accuracy`.
- `needs_reply` — binary P/R/F1 and `missed_reply_rate` over the complete
  observable denominator.
- `format` — schema/enum failure counts, reported separately.
- `confidence` — reliability/ECE and selective coverage/risk. The native
  `confidence` is a **category** confidence; it is never used as a reply
  probability (the policy declares `reply_probability: null`).
- `relations` — invariance (stable fields preserved) and counterfactual
  (changing fields changed) scored against the lineage parent.
- `workflow` — executed final-state completion, grounding, and separate safety
  and compliance violation counts.
- `full_response` — prose completeness (missing and fabricated prose fail) with
  a `pending_human_adjudication` review; no token-overlap "semantic" score is
  ever produced.

There is deliberately no global "quality" key.

## Policy and calibration

`policy.json` is the single pre-declared protocol (`B = 4000`, fixed `seed`,
`alpha`, pre-declared `margin = 0.03`, minimum coverage/quality and review
gates). Every interval and noninferiority verdict echoes those values.

`fit_calibrator(dataset, run, policy=None, split="calibration")` fits a
reproducible bin calibrator **only** from the `calibration` split and returns an
artifact carrying `artifact_sha256`/`revision`. Fitting from any other split, or
on unauthorized real-mail material, raises `CalibrationError`. Non-finite,
out-of-range or non-numeric probabilities are rejected by
`validate_probability`; `score_run` records such a case as an integrity problem
and refuses to use the value.

## Statistics

`stats.bootstrap_metric` / `stats.paired_bootstrap` resample **lineage
clusters** and recompute the metric on every resample. Comparisons require the
same dataset, requested scope and scorer; a mismatch raises
`ComparisonError`. Incomplete scope or too few shared roots yields
`estimable = false` (`not_estimable`), never green.

## Gates

`gates.qualify(report, policy)` marks every profile eligible/ineligible with
explicit reasons; draft data may produce exploratory development metrics but is
never `final_test_qualified`; real mail needs authorization; CPU qualification
needs a verified hardware receipt (configured thread/memory numbers are not a
receipt).

## Running the tests

```bash
.venv/bin/python -m unittest discover -s benchmarks/v3/tests -v
```
