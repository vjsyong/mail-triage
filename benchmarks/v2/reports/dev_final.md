# Model benchmark v2 — comparison report

Split: **dev** · scorer v2.0 · policy v2.0

## Measurement model

Quality (task completion), failure counts by behaviour, and the severity cost index are reported separately; missing/infra results disqualify a comparison rather than being silently dropped.

## Headline (baseline = baseline-gemma26b-4ef27e42f8af)

| run | complete | quality (fixed denom) | quality (scored only) | cost index | critical cases |
|---|---|---|---|---|---|
| baseline-gemma26b-4ef27e42f8af | True | 87.8 | 87.8 | 89.6 | 2 |
| gemma4e4b-508611f5dd39 | True | 88.0 | 88.0 | 89.0 | 3 |
| qwen9b-9afaeb5a6c29 | True | 87.3 | 87.3 | 88.8 | 1 |
| qwen4b-f1caa95f2a16 | True | 88.7 | 88.7 | 91.2 | 1 |
| lfm8b-18138b80d248 | True | 80.8 | 80.8 | 86.3 | 3 |
| ling3-a3263c27bd9e | True | 83.1 | 83.1 | 86.0 | 0 |
| granite3b-5f8510bbf498 | True | 76.5 | 76.5 | 79.9 | 21 |

## Failure taxonomy (MODEL failures)

| run | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| baseline-gemma26b-4ef27e42f8af | 2 | 88 | 42 | 0 |
| gemma4e4b-508611f5dd39 | 3 | 94 | 34 | 0 |
| qwen9b-9afaeb5a6c29 | 1 | 100 | 34 | 0 |
| qwen4b-f1caa95f2a16 | 1 | 70 | 52 | 0 |
| lfm8b-18138b80d248 | 3 | 122 | 29 | 14 |
| ling3-a3263c27bd9e | 0 | 122 | 64 | 0 |
| granite3b-5f8510bbf498 | 21 | 139 | 78 | 0 |

Top failure kinds across runs: `wrong_needs_reply`×267, `wrong_category`×259, `task_incomplete`×140, `missing_required_content`×98, `rule_schema_invalid`×60, `rule_missed_positives`×53, `too_many_calls`×47, `missing_required_call`×47, `wrong_tool_args`×31, `wrong_action_outcome`×31, `schema_violation`×23, `subject_line_leak`×11


## Paired comparisons vs baseline (family-clustered bootstrap)

### gemma4e4b-508611f5dd39
- mean quality diff: **+0.001** (95% CI -0.020 … +0.014), n=338 families=67
- verdict: **inconclusive** (wins 30 / losses 31 / ties 277)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.050)
  - assistant noninferiority (margin 0.05): **PASS** (CI low -0.004)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.000)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.141)
### qwen9b-9afaeb5a6c29
- mean quality diff: **-0.006** (95% CI -0.030 … +0.010), n=338 families=67
- verdict: **inconclusive** (wins 33 / losses 34 / ties 271)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.044)
  - assistant noninferiority (margin 0.05): **PASS** (CI low -0.031)
  - drafting noninferiority (margin 0.05): **FAIL** (CI low -0.056)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.162)
### qwen4b-f1caa95f2a16
- mean quality diff: **+0.008** (95% CI -0.013 … +0.028), n=338 families=67
- verdict: **inconclusive** (wins 44 / losses 37 / ties 257)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.038)
  - assistant noninferiority (margin 0.05): **PASS** (CI low -0.001)
  - drafting noninferiority (margin 0.05): **FAIL** (CI low -0.063)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.101)
### lfm8b-18138b80d248
- mean quality diff: **-0.070** (95% CI -0.124 … -0.027), n=338 families=67
- verdict: **worse** (wins 38 / losses 70 / ties 230)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.222)
  - assistant noninferiority (margin 0.05): **PASS** (CI low -0.010)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.026)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.223)
### ling3-a3263c27bd9e
- mean quality diff: **-0.048** (95% CI -0.072 … -0.027), n=338 families=67
- verdict: **worse** (wins 28 / losses 76 / ties 234)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.140)
  - assistant noninferiority (margin 0.05): **PASS** (CI low -0.000)
  - drafting noninferiority (margin 0.05): **FAIL** (CI low -0.058)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.294)
### granite3b-5f8510bbf498
- mean quality diff: **-0.113** (95% CI -0.157 … -0.075), n=338 families=67
- verdict: **worse** (wins 36 / losses 93 / ties 209)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.223)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.192)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.050)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.478)

## Calibration (classification)

- baseline-gemma26b-4ef27e42f8af: n=136 ECE=0.2408
- gemma4e4b-508611f5dd39: n=136 ECE=0.2897
- qwen9b-9afaeb5a6c29: n=136 ECE=0.2272
- qwen4b-f1caa95f2a16: n=136 ECE=0.1347
- lfm8b-18138b80d248: n=136 ECE=0.2621
- ling3-a3263c27bd9e: n=136 ECE=0.2982
- granite3b-5f8510bbf498: n=136 ECE=0.3282
