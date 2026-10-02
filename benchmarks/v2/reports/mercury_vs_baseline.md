# Model benchmark v2 — comparison report

Split: **acceptance** · scorer v2.0 · policy v2.0

## Measurement model

Quality (task completion), failure counts by behaviour, and the severity cost index are reported separately; missing/infra results disqualify a comparison rather than being silently dropped.

## Headline (baseline = baseline-gemma26b-84537e7b7a77)

| run | complete | quality (fixed denom) | quality (scored only) | cost index | critical cases |
|---|---|---|---|---|---|
| baseline-gemma26b-84537e7b7a77 | True | 89.2 | 89.2 | 90.5 | 0 |
| agentmercury-q4km-089cdf72ae1b | True | 88.8 | 88.8 | 91.2 | 0 |

## Failure taxonomy (MODEL failures)

| run | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| baseline-gemma26b-84537e7b7a77 | 0 | 64 | 29 | 0 |
| agentmercury-q4km-089cdf72ae1b | 0 | 54 | 42 | 0 |

Top failure kinds across runs: `wrong_needs_reply`×58, `wrong_category`×43, `missing_required_content`×30, `task_incomplete`×24, `rule_guard_broken`×9, `subject_line_leak`×8, `rule_missed_positives`×7, `too_many_calls`×5, `rule_schema_invalid`×4, `empty_reply`×1


## Paired comparisons vs baseline (family-clustered bootstrap)

### agentmercury-q4km-089cdf72ae1b
- mean quality diff: **-0.005** (95% CI -0.029 … +0.018), n=258 families=47
- verdict: **inconclusive** (wins 20 / losses 22 / ties 216)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.078)
  - assistant noninferiority (margin 0.05): **PASS** (CI low -0.001)
  - drafting noninferiority (margin 0.05): **FAIL** (CI low -0.089)
  - rules noninferiority (margin 0.08): **PASS** (CI low +0.012)

## Calibration (classification)

- baseline-gemma26b-84537e7b7a77: n=104 ECE=0.1938
- agentmercury-q4km-089cdf72ae1b: n=104 ECE=0.1654
