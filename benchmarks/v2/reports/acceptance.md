# Model benchmark v2 — comparison report

Split: **acceptance** · scorer v2.0 · policy v2.0

## Measurement model

Quality (task completion), failure counts by behaviour, and the severity cost index are reported separately; missing/infra results disqualify a comparison rather than being silently dropped.

## Headline (baseline = baseline-gemma26b-3bcefbe3c053)

| run | complete | quality (fixed denom) | quality (scored only) | cost index | critical cases |
|---|---|---|---|---|---|
| baseline-gemma26b-3bcefbe3c053 | True | 87.1 | 88.8 | 88.5 | 0 |
| gemma4e4b-d70166c8ce1f | True | 88.2 | 88.2 | 88.7 | 6 |
| qwen9b-54ce74ca9550 | True | 87.7 | 87.7 | 90.3 | 0 |

## Failure taxonomy (MODEL failures)

| run | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| baseline-gemma26b-3bcefbe3c053 | 0 | 65 | 31 | 0 |
| gemma4e4b-d70166c8ce1f | 6 | 68 | 27 | 0 |
| qwen9b-54ce74ca9550 | 0 | 67 | 28 | 0 |

Top failure kinds across runs: `wrong_needs_reply`×81, `wrong_category`×64, `missing_required_content`×45, `task_incomplete`×35, `missing_required_call`×21, `rule_missed_positives`×13, `rule_guard_broken`×9, `wrong_tool_args`×6, `wrong_action_outcome`×6, `too_many_calls`×5, `rule_schema_invalid`×4, `empty_reply`×1


## Paired comparisons vs baseline (family-clustered bootstrap)

### gemma4e4b-d70166c8ce1f
- mean quality diff: **-0.006** (95% CI -0.024 … +0.015), n=257 families=47
- verdict: **inconclusive** (wins 19 / losses 19 / ties 219)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.041)
  - assistant noninferiority (margin 0.05): **PASS** (CI low -0.044)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.000)
  - rules noninferiority (margin 0.08): **PASS** (CI low -0.025)
### qwen9b-54ce74ca9550
- mean quality diff: **-0.013** (95% CI -0.037 … +0.026), n=257 families=47
- verdict: **inconclusive** (wins 31 / losses 31 / ties 195)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.057)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.087)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.000)
  - rules noninferiority (margin 0.08): **PASS** (CI low +0.017)

## Calibration (classification)

- baseline-gemma26b-3bcefbe3c053: n=104 ECE=0.1841
- gemma4e4b-d70166c8ce1f: n=104 ECE=0.2043
- qwen9b-54ce74ca9550: n=104 ECE=0.1677
