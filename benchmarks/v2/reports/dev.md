# Model benchmark v2 — comparison report

Split: **dev** · scorer v2.0 · policy v2.0

## Measurement model

Quality (task completion), failure counts by behaviour, and the severity cost index are reported separately; missing/infra results disqualify a comparison rather than being silently dropped.

## Headline (baseline = baseline-gemma26b-3bcefbe3c053)

| run | complete | quality (fixed denom) | quality (scored only) | cost index | critical cases |
|---|---|---|---|---|---|
| baseline-gemma26b-3bcefbe3c053 | False | 88.4 | 88.6 | 89.6 | 0 |
| gemma4e4b-d70166c8ce1f | False | 87.9 | 87.9 | 88.9 | 3 |
| qwen9b-54ce74ca9550 | False | 87.2 | 87.2 | 88.7 | 1 |

## Failure taxonomy (MODEL failures)

| run | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| baseline-gemma26b-3bcefbe3c053 | 0 | 92 | 31 | 0 |
| gemma4e4b-d70166c8ce1f | 3 | 95 | 34 | 0 |
| qwen9b-54ce74ca9550 | 1 | 101 | 34 | 0 |

Top failure kinds across runs: `wrong_category`×113, `wrong_needs_reply`×93, `missing_required_content`×56, `task_incomplete`×51, `rule_missed_positives`×31, `missing_required_call`×19, `too_many_calls`×6, `rule_schema_invalid`×6, `wrong_tool_args`×4, `wrong_action_outcome`×4, `rule_omitted`×4, `rule_guard_broken`×2


## Paired comparisons vs baseline (family-clustered bootstrap)

### gemma4e4b-d70166c8ce1f
- mean quality diff: **-0.007** (95% CI -0.025 … +0.009), n=337 families=67
- verdict: **inconclusive** (wins 22 / losses 29 / ties 286)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.032)
  - assistant noninferiority (margin 0.05): **PASS** (CI low -0.037)
  - drafting noninferiority (margin 0.05): **PASS** (CI low -0.024)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.131)
### qwen9b-54ce74ca9550
- mean quality diff: **-0.015** (95% CI -0.033 … +0.001), n=337 families=67
- verdict: **inconclusive** (wins 26 / losses 32 / ties 279)
  - classification noninferiority (margin 0.03): **PASS** (CI low -0.030)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.066)
  - drafting noninferiority (margin 0.05): **FAIL** (CI low -0.060)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.138)

## Calibration (classification)

- baseline-gemma26b-3bcefbe3c053: n=135 ECE=0.2693
- gemma4e4b-d70166c8ce1f: n=136 ECE=0.3077
- qwen9b-54ce74ca9550: n=136 ECE=0.2334
