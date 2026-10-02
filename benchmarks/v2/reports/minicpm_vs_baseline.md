# Model benchmark v2 — comparison report

Split: **acceptance** · scorer v2.0 · policy v2.0

## Measurement model

Quality (task completion), failure counts by behaviour, and the severity cost index are reported separately; missing/infra results disqualify a comparison rather than being silently dropped.

## Headline (baseline = baseline-gemma26b-4612367f444c)

| run | complete | quality (fixed denom) | quality (scored only) | cost index | critical cases |
|---|---|---|---|---|---|
| baseline-gemma26b-4612367f444c | True | 88.7 | 88.7 | 90.0 | 1 |
| minicpm5-2b-q4km-7ac2749c14d3 | True | 84.3 | 84.3 | 86.3 | 2 |
| minicpm5-1b-q4km-1859ec3a8b5f | True | 61.5 | 61.5 | 68.4 | 23 |

## Failure taxonomy (MODEL failures)

| run | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| baseline-gemma26b-4612367f444c | 1 | 67 | 28 | 0 |
| minicpm5-2b-q4km-7ac2749c14d3 | 2 | 96 | 23 | 0 |
| minicpm5-1b-q4km-1859ec3a8b5f | 23 | 174 | 109 | 0 |

Top failure kinds across runs: `wrong_category`×121, `wrong_needs_reply`×95, `task_incomplete`×50, `missing_required_content`×45, `too_many_calls`×38, `subject_line_leak`×27, `rule_missed_positives`×25, `wrong_action_outcome`×21, `wrong_tool_args`×20, `rule_guard_broken`×14, `empty_reply`×13, `missing_required_call`×9


## Paired comparisons vs baseline (family-clustered bootstrap)

### minicpm5-2b-q4km-7ac2749c14d3
- mean quality diff: **-0.044** (95% CI -0.075 … +0.002), n=260 families=51
- verdict: **inconclusive** (wins 26 / losses 48 / ties 186)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.159)
  - assistant noninferiority (margin 0.05): **PASS** (CI low -0.047)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.000)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.125)
### minicpm5-1b-q4km-1859ec3a8b5f
- mean quality diff: **-0.272** (95% CI -0.314 … -0.214), n=260 families=51
- verdict: **worse** (wins 26 / losses 159 / ties 75)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.351)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.326)
  - drafting noninferiority (margin 0.05): **FAIL** (CI low -0.218)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.700)

## Calibration (classification)

- baseline-gemma26b-4612367f444c: n=106 ECE=0.2033
- minicpm5-2b-q4km-7ac2749c14d3: n=102 ECE=0.3662
- minicpm5-1b-q4km-1859ec3a8b5f: n=101 ECE=0.4287
