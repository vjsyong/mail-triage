# Model benchmark v2 — comparison report

Split: **acceptance** · scorer v2.0 · policy v2.0

## Measurement model

Quality (task completion), failure counts by behaviour, and the severity cost index are reported separately; missing/infra results disqualify a comparison rather than being silently dropped.

## Headline (baseline = baseline-gemma26b-4612367f444c)

| run | complete | quality (fixed denom) | quality (scored only) | cost index | critical cases |
|---|---|---|---|---|---|
| baseline-gemma26b-4612367f444c | True | 88.7 | 88.7 | 90.0 | 1 |
| agentmercury-q4km-e77568e1aea0 | True | 87.8 | 87.8 | 90.2 | 2 |

## Failure taxonomy (MODEL failures)

| run | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| baseline-gemma26b-4612367f444c | 1 | 67 | 28 | 0 |
| agentmercury-q4km-e77568e1aea0 | 2 | 58 | 44 | 0 |

Top failure kinds across runs: `wrong_needs_reply`×58, `wrong_category`×47, `missing_required_content`×30, `task_incomplete`×26, `rule_guard_broken`×9, `subject_line_leak`×9, `rule_missed_positives`×6, `too_many_calls`×5, `rule_schema_invalid`×4, `injection_compliance`×3, `empty_reply`×2, `forbidden_content`×1


## Paired comparisons vs baseline (family-clustered bootstrap)

### agentmercury-q4km-e77568e1aea0
- mean quality diff: **-0.009** (95% CI -0.036 … +0.016), n=260 families=51
- verdict: **inconclusive** (wins 23 / losses 29 / ties 208)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.094)
  - assistant noninferiority (margin 0.05): **PASS** (CI low +0.002)
  - drafting noninferiority (margin 0.05): **FAIL** (CI low -0.104)
  - rules noninferiority (margin 0.08): **PASS** (CI low -0.012)

## Calibration (classification)

- baseline-gemma26b-4612367f444c: n=106 ECE=0.2033
- agentmercury-q4km-e77568e1aea0: n=106 ECE=0.1918
