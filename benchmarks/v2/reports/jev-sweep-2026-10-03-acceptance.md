# Jev-family models on benchmark v2 classification (acceptance split)

Protocol: every model answers the same two typed questions over the case's rendered email — category (choice over the six production labels + descriptions) and needs_reply (noul/boolean) — then the production JSON shape is synthesized for the official scorer. `confidence` is min(category top probability, needs_reply margin); **summary/reason are adapter artifacts** (decision models never generate text). Runs are classification-scoped, so the six-suite `coverage.complete` rule marks them incomplete; quality shown is `by_suite.classification`.


## Headline

| model | params | quality | cost idx | category acc | needs_reply P/R/F1 | ECE | critical cases | p50/email |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| gemma-4-26b-a4b (baseline) | - | 84.9 | 89.9 | 79.2 | 0.569 / 1.0 / 0.725 | 0.2033 | 1 | 9.6 |
| MiniCPM5-2B Q4_K_M (ref) | - | 75.8 | 82.5 | 57.5 | 0.633 / 0.655 / 0.644 | 0.3662 | 2 | 3.52 |
| GLiNER2.5-Decide 340M | 340M | 79.2 | 82.4 | 55.7 | 0.76 / 0.655 / 0.704 | 0.108 | 2 | 0.6 |
| Laya Typed Decisions 421M | 421M | 78.3 | 84.6 | 63.2 | 0.333 / 0.034 / 0.062 | 0.1399 | 0 | 0.75 |
| TinyJev-0.6B 596M | 596M | 79.9 | 86.2 | 67.9 | 0.5 / 0.034 / 0.065 | 0.1993 | 0 | 0.96 |
| Kev-0.8B 800M | 800M | 67.0 | 78.5 | 56.6 | 0.328 / 0.759 / 0.458 | 0.1549 | 2 | 3.0 |
| NanoJev-0.6B 596M | 596M | 49.4 | 65.5 | 22.6 | 0.284 / 1.0 / 0.443 | 0.0266 | 1 | 0.07 |
| Nano-Jev RAG v1.0 33M | 33M | 60.1 | 66.2 | 7.5 | 0.0 / 0.0 / 0.0 | 0.1549 | 0 | 0.14 |
| Nano-Jev RAG v0.1 23M | 23M | 69.2 | 75.3 | 34.9 | 0.0 / 0.0 / 0.0 | 0.1716 | 0 | 0.07 |

Critical case ids (injection compliance unless noted):

- gemma-4-26b-a4b (baseline): cls_adv_a_242
- MiniCPM5-2B Q4_K_M (ref): cls_adv_a_242, cls_adv_a_243
- GLiNER2.5-Decide 340M: cls_adv_a_242, cls_adv_a_243
- Kev-0.8B 800M: cls_adv_a_242, cls_adv_a_243
- NanoJev-0.6B 596M: cls_adv_a_242

Failure kinds: gemma-4-26b-a4b (baseline): {"wrong_needs_reply": 24, "wrong_category": 22, "injection_compliance": 1}; MiniCPM5-2B Q4_K_M (ref): {"wrong_category": 41, "wrong_needs_reply": 21, "malformed_json": 4, "injection_compliance": 2}; GLiNER2.5-Decide 340M: {"wrong_category": 47, "wrong_needs_reply": 16, "injection_compliance": 2}; Laya Typed Decisions 421M: {"wrong_needs_reply": 30, "wrong_category": 39}; TinyJev-0.6B 596M: {"wrong_needs_reply": 30, "wrong_category": 34}; Kev-0.8B 800M: {"wrong_needs_reply": 56, "wrong_category": 46, "injection_compliance": 2}; NanoJev-0.6B 596M: {"wrong_category": 82, "wrong_needs_reply": 77, "injection_compliance": 1}; Nano-Jev RAG v1.0 33M: {"wrong_category": 98, "wrong_needs_reply": 29}; Nano-Jev RAG v0.1 23M: {"wrong_category": 69, "wrong_needs_reply": 29}

## How each model was run (verified inference path)

| model | install | inference entry point | device |
|---|---|---|---|
| GLiNER2.5-Decide | `pip install gliner2` | `AutoExtractor.from_pretrained("fastino/GLiNER2.5-Decide").classify_text(text, tasks, include_confidence=True)` | CPU |
| Laya Typed Decisions | `pip install laya` | `Router(device="cpu").predict(state, questions, model="typed-decisions")` (`convaiinnovations/laya-typed-decisions`) | CPU |
| TinyJev-0.6B | `pip install 'tinyjev[torch]'` | `tinyjev.load("TinyJev-0.6B").predict({"state", "questions"})` (System One payload) | CPU |
| Kev-0.8B | clone `jaredpalmer/kev`, `pip install -e . --no-deps` + deps | `python -m kev.serve --run jaredpalmer/kev-0.8b`, POST `/v1/systemone` | CPU fp32 |
| NanoJev-0.6B | clone `TianyuCodings/NanoJev` | `scripts/predict_toy_decisions.DecisionPredictor` on `C-Tianyu/NanoJev@unified-games-v1` | CUDA bf16 |
| Nano-Jev RAG | `pip install nano-jev` | `nanojev.load().decide(question, options, state)` (`sdmlai/nano-jev`) | CPU |


## Paired comparisons (family-clustered bootstrap, margin 0.03)

| comparison | mean diff (pp) | 95% CI | verdict | noninferior |
|---|---:|---|---|---|
| GLiNER2.5-Decide 340M vs baseline | -5.66 | -13.37 … +4.41 | inconclusive | FAIL |
| Laya Typed Decisions 421M vs baseline | -6.60 | -14.95 … +4.69 | inconclusive | FAIL |
| TinyJev-0.6B 596M vs baseline | -5.03 | -11.20 … +3.41 | inconclusive | FAIL |
| Kev-0.8B 800M vs baseline | -17.93 | -25.65 … -10.20 | worse | FAIL |
| NanoJev-0.6B 596M vs baseline | -35.55 | -41.69 … -29.64 | worse | FAIL |
| Nano-Jev RAG v1.0 33M vs baseline | -24.84 | -30.96 … -16.45 | worse | FAIL |
| Nano-Jev RAG v0.1 23M vs baseline | -15.73 | -23.28 … -5.94 | worse | FAIL |
| GLiNER2.5-Decide 340M vs MiniCPM5-2B | +3.46 | -1.16 … +10.39 | inconclusive | PASS |
| Laya Typed Decisions 421M vs MiniCPM5-2B | +2.52 | -5.32 … +10.59 | inconclusive | FAIL |
| TinyJev-0.6B 596M vs MiniCPM5-2B | +4.09 | -2.69 … +11.02 | inconclusive | PASS |
| Kev-0.8B 800M vs MiniCPM5-2B | -8.81 | -19.05 … -0.96 | worse | FAIL |
| NanoJev-0.6B 596M vs MiniCPM5-2B | -26.43 | -38.69 … -17.50 | worse | FAIL |
| Nano-Jev RAG v1.0 33M vs MiniCPM5-2B | -15.72 | -25.23 … -7.67 | worse | FAIL |
| Nano-Jev RAG v0.1 23M vs MiniCPM5-2B | -6.61 | -15.71 … +1.80 | inconclusive | FAIL |

## Notes & caveats

- `quality` is the official task-completion fraction (scored-only) on the split;
  missing/infra cases disqualify cross-suite comparisons but none occurred here
  (240/240 attempts `ok` unless noted).
- TinyJev's `needs_reply` collapse (Noul answered "no" almost always) reproduces
  the documented statement-form yes-bias of the 0.6B head.
- GLiNER2.5-Decide exposes label confidence, not a full distribution; its
  confidence is used as the top probability, so its ECE mixes precision with the
  score scale and is not directly comparable.
- Nano-Jev RAG is a RAG relevance/sufficiency/groundedness model; its `decide`
  custom-option path is documented as weak on v1.0 (v0.1 included as a control).
- Kev is served by its own repo server over `/v1/systemone` and answers ~3 s per
  email on 8 CPU threads; other models run in-process CPU (TinyJev, Laya,
  GLiNER, nano-jev) or CUDA bf16 (NanoJev).
- Run identity: `config_hash` per run; case set matches the stored baseline
  (`case_manifest_sha256`), so per-case joins are same-revision.

