# Model benchmark v2 — comparison report

Split: **acceptance** · scorer v2.0 · policy v2.0

## Measurement model

Quality (task completion), failure counts by behaviour, and the severity cost index are reported separately; missing/infra results disqualify a comparison rather than being silently dropped.

## Headline (baseline = baseline-gemma26b-4ef27e42f8af)

| run | complete | quality (fixed denom) | quality (scored only) | cost index | critical cases |
|---|---|---|---|---|---|
| baseline-gemma26b-4ef27e42f8af | True | 89.1 | 89.1 | 90.5 | 0 |
| gemma4e4b-508611f5dd39 | True | 88.1 | 88.1 | 88.7 | 6 |
| qwen9b-9afaeb5a6c29 | True | 87.9 | 87.9 | 90.6 | 0 |
| qwen4b-f1caa95f2a16 | True | 85.9 | 85.9 | 87.6 | 10 |
| lfm8b-18138b80d248 | True | 81.0 | 81.0 | 86.1 | 5 |
| ling3-a3263c27bd9e | True | 82.3 | 82.3 | 85.8 | 0 |
| granite3b-5f8510bbf498 | True | 74.9 | 74.9 | 75.2 | 23 |

## Failure taxonomy (MODEL failures)

| run | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| baseline-gemma26b-4ef27e42f8af | 0 | 65 | 30 | 0 |
| gemma4e4b-508611f5dd39 | 6 | 67 | 29 | 0 |
| qwen9b-9afaeb5a6c29 | 0 | 65 | 28 | 0 |
| qwen4b-f1caa95f2a16 | 10 | 65 | 43 | 0 |
| lfm8b-18138b80d248 | 5 | 91 | 25 | 0 |
| ling3-a3263c27bd9e | 0 | 97 | 51 | 0 |
| granite3b-5f8510bbf498 | 23 | 132 | 57 | 0 |

Top failure kinds across runs: `wrong_needs_reply`×234, `wrong_category`×184, `missing_required_content`×93, `task_incomplete`×93, `missing_required_call`×46, `wrong_tool_args`×45, `wrong_action_outcome`×44, `rule_schema_invalid`×31, `rule_guard_broken`×27, `too_many_calls`×25, `rule_missed_positives`×21, `schema_violation`×20


## Paired comparisons vs baseline (family-clustered bootstrap)

### gemma4e4b-508611f5dd39
- mean quality diff: **-0.010** (95% CI -0.027 … +0.007), n=262 families=47
- verdict: **inconclusive** (wins 15 / losses 19 / ties 228)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.044)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.052)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.000)
  - rules noninferiority (margin 0.08): **PASS** (CI low -0.028)
### qwen9b-9afaeb5a6c29
- mean quality diff: **-0.012** (95% CI -0.040 … +0.026), n=262 families=47
- verdict: **inconclusive** (wins 32 / losses 30 / ties 200)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.055)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.076)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.000)
  - rules noninferiority (margin 0.08): **PASS** (CI low +0.031)
### qwen4b-f1caa95f2a16
- mean quality diff: **-0.032** (95% CI -0.059 … +0.003), n=262 families=47
- verdict: **inconclusive** (wins 24 / losses 35 / ties 203)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.051)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.122)
  - drafting noninferiority (margin 0.05): **FAIL** (CI low -0.056)
  - rules noninferiority (margin 0.08): **PASS** (CI low +0.017)
### lfm8b-18138b80d248
- mean quality diff: **-0.081** (95% CI -0.141 … -0.016), n=262 families=47
- verdict: **worse** (wins 32 / losses 62 / ties 168)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.285)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.069)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.000)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.214)
### ling3-a3263c27bd9e
- mean quality diff: **-0.068** (95% CI -0.108 … -0.022), n=262 families=47
- verdict: **worse** (wins 16 / losses 62 / ties 184)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.211)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.060)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.000)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.261)
### granite3b-5f8510bbf498
- mean quality diff: **-0.142** (95% CI -0.182 … -0.096), n=262 families=47
- verdict: **worse** (wins 16 / losses 84 / ties 162)
  - classification noninferiority (margin 0.03): **FAIL** (CI low -0.261)
  - assistant noninferiority (margin 0.05): **FAIL** (CI low -0.239)
  - drafting noninferiority (margin 0.05): **PASS** (CI low +0.000)
  - rules noninferiority (margin 0.08): **FAIL** (CI low -0.212)

## Calibration (classification)

- baseline-gemma26b-4ef27e42f8af: n=104 ECE=0.1899
- gemma4e4b-508611f5dd39: n=104 ECE=0.1971
- qwen9b-9afaeb5a6c29: n=104 ECE=0.1589
- qwen4b-f1caa95f2a16: n=104 ECE=0.1348
- lfm8b-18138b80d248: n=104 ECE=0.2255
- ling3-a3263c27bd9e: n=103 ECE=0.3092
- granite3b-5f8510bbf498: n=104 ECE=0.4152
