# Mail-Triage: Can a Smaller Local Model Replace Gemma 4 26B-A4B?
## Final report — workstream `model-eval`, completed 2026-10-02

Scope: determine the smallest practical local model that preserves the behavior the
mail-triage app needs, by evidence, not generic benchmarks.
Suite: 196 frozen cases (benchmarks/cases/; FROZEN.md; scorer v1.2).
Baseline: live production `gemma-4-26b-a4b` (vLLM v0.22.0, GPU 0, AWQ-4bit + int8 KV).
Candidates: same app payloads replayed on GPU 1, sequential, 16K ctx, prefix-caching on.
Note: candidates were extended at the owner's request — Ling-3.0-tiny added as a 6th
candidate (beyond the original shortlist of 5).

────────────────────────────────────────────────────────────────────────────────
## 1. What the LLM currently does (Phase 1 audit)
Full inventory: benchmarks/docs/app-llm-inventory.md. Seven call sites through
`engine.LLMClient`; the workload is heavily skewed:
- `classify` — EVERY queued message; strict JSON {category×6, needs_reply,
  confidence, summary, reason}; thinking ON; body truncated to 1500 chars.
- assistant agent — streaming tool-calling loop (27 tools, ≤8 steps, ≤4 calls/step,
  4.5K-char tool-result cap, 30K transcript budget); the critical-risk surface.
- draft_reply / propose_rules_from_tags / example_draft / thought-summary —
  low-frequency, user-visible; rules affect future auto-filing (high blast radius).

## 2-3. Benchmark design and dataset
See docs/benchmark-taxonomy.md + scoring-methodology.md. 196 cases / 6 suites /
5 synthetic threads + adversarial + junk + long-context. Ground truth: synthetic
corpus facts + explicit rules only — no model in the loop. Severity weights
LOW 1 / MED 3 / HIGH 9 / CRIT 27; both raw and severity-adjusted scores reported.
The suite discriminates: the production baseline does NOT trivially score 100%
(92.6 raw, 3 critical failures).

## 4. Baseline metrics (reference implementation)
- Overall: **92.6 raw / 90.0 severity-adjusted; 3 critical cases** (all in the
  assistant's hallucination traps). classification 93.4 / assistant 87.6 /
  drafting 100 / rules 100 / simulate 100 / summary 100.
- Failure clusters: no-match traps (empty replies after search spirals), ambiguous
  "which email" requests acted on without asking, one 27K-char CoT runaway on a
  long receipt (no JSON), invoices occasionally read as Action.
- Latency (controlled, 20 classify + 6 assistant): classify median 4.33s /
  p95 16.5s / max 40s; ~135 tok/s; assistant median 1.82s, TTFT 0.09s.
- Resources: 23.1 GB VRAM reserved, 21.3 GiB RAM, cold boot 397s, 17 GB weights.

## 5-6. Research + shortlist
See docs/research-2026-10-02.md (dated sources, licenses, vLLM support verified
against the actual images) and docs/candidate-shortlist.md.

## 7. Exact checkpoints / runtimes tested
| key | checkpoint | params | quant | runtime (local image) | thinking adaptation |
|---|---|---|---|---|---|
| baseline | gemma-4-26b-a4b (cyankiwi AWQ-4bit) | 25.2B MoE / 3.8B active | AWQ4 + int8 KV | vLLM v0.22.0 | none (app config) |
| qwen9b | Qwen/Qwen3.5-9B | 9B dense | bf16 | vLLM v0.22.0 (+eager, util .90) | falsekw |
| qwen4b | Qwen/Qwen3.5-4B | 4B dense | bf16 | vLLM v0.22.0 | falsekw |
| gemma4e4b | google/gemma-4-E4B-it | 4.5B eff (8B w/emb) | bf16 | vLLM v0.22.0 | none |
| granite3b | ibm-granite/granite-4.2-3b | 3B dense | bf16 | vLLM 0.26.1rc (muse image) + non-stream assistant | falsekw + no-stream |
| lfm8b | LiquidAI/LFM2.5-8B-A1B | 8.3B MoE / 1.5B active | bf16 | vLLM v0.22.0 | none |
| ling3 | inclusionAI/Ling-3.0-tiny | 7.9B MoE (128 exp, top-8) | bf16 | vLLM 0.26.1rc (muse image) | explicit flags |

All candidates: temperature 0, max_tokens 4096 (classify) / 2500 (assistant),
same prompt strings, same tool schemas, same 196 cases. Every adaptation is
documented below and in each results/<key>/server.json.

## 8. Candidate results (frozen suite, scorer v1.2)
| model | raw | sev-adj | crit | classification | assistant | drafting | rules | simulate | summary |
|---|---|---|---|---|---|---|---|---|---|
| baseline 26B | 92.6 | 90.0 | 3 | 93.4 | 87.6 | 100 | 100 | 100 | 100 |
| Qwen3.5-9B | **93.9** | **90.6** | 2 | 94.1 | 91.0 | 100 | 96.9 | 100 | 100 |
| Gemma-4-E4B | 90.3 | 85.0 | **1** | 92.4 | 88.0 | 85.0 | 75.6 | 100 | 100 |
| Qwen3.5-4B | 87.4 | 82.5 | 4 | 87.9 | 83.3 | 97.9 | 78.8 | 100 | 100 |
| Ling-3.0-tiny | 83.4 | 77.6 | 5 | 81.6 | 86.8 | 89.6 | 58.1 | 91.7 | 100 |
| LFM2.5-8B-A1B | 76.2 | 68.9 | 5 | 73.0 | 80.8 | 92.9 | 55.0 | 91.7 | 60 |
| Granite 4.2 3B | 73.0 | 63.8 | 6 | 72.9 | 68.8 | 90.0 | 46.9 | 100 | 100 |

Per-model notes:
- **Qwen3.5-9B**: quality-equivalent to baseline (better on long mail 92.5 vs 72.5,
  weaker on tool-arg 70 vs 100). UNMODIFIED was unusable: temp-0 thinking loops in
  4/8 probe cases (median 99s/case). Adapted: `enable_thinking=false` everywhere.
  Served eager (bf16 OOM at KV alloc with CUDA graphs on 24GB) -> slower generation
  (11 tok/s); tuning upside exists (AWQ/GPTQ quants already available locally).
- **Gemma-4-E4B**: best small-model showing; NO adaptation needed (app payload as-is);
  only 1 critical. Weak spots: guard-rule semantics (75.6 suite), ambiguity (56),
  multi-step (60), process narration in final answers ("The user requested…"),
  some traps answered from incomplete narration.
- **Qwen3.5-4B**: blazing classify (0.74s median, uniform) but 4 criticals, incl.
  obeying BOTH injection emails verbatim (Action + needs_reply=true + conf 1.0).
- **Ling-3.0-tiny**: fastest tier (0.68s classify / 183 tok/s; assistant 0.99s);
  obeyed 3 injections; invents "Notifications"-style plural labels sometimes;
  surprising assistant strength (86.8) but rules/guard collapse (58.1).
- **LFM2.5-8B-A1B**: fastest classify (0.34s / 176 tok/s); assistant decent (80.8)
  but obeyed FOUR injections and invented off-enum labels in 23/106 classifications.
- **Granite 4.2 3B**: weakest; obeyed 2 injections; needs both a newer runtime
  (v0.22 cannot serve BailingMoeV3; granite 4.2's XML tool calls fail every
  *streaming* parser tested incl. 0.26rc/0.27) and the non-streaming assistant
  adaptation; guard logic fails; fastest simple-tool behavior though.

## 9. Performance / resources
| model | classify med | classify p95 | gen tok/s | assistant med wall | assst TTFT | VRAM reserved | weights | boot |
|---|---|---|---|---|---|---|---|---|
| baseline | 4.33s | 16.5s | 135 | 1.82s | 0.09s | 23.1 GB | 17 GB (4-bit) | 397s |
| Qwen3.5-9B* | 5.77s | 6.43s | 11 | 9.57s | 3.09s | 21.0 GB | 19.3 GB | 280s |
| Qwen3.5-4B | 0.74s | 0.82s | 74 | 3.10s | 2.0s | 22.6 GB | 9.3 GB | 280s |
| Gemma-4-E4B | 6.05s | 7.37s | 70 | 3.02s | 0.1s | 22.8 GB | 16.0 GB | 371s |
| Ling-3.0-tiny | 0.68s | 0.96s | 183 | 0.99s | 0.13s | 21.9 GB | 15.8 GB | 210s |
| LFM2.5-8B-A1B | 0.34s | 0.42s | 176 | 1.13s | 0.06s | 22.2 GB | 17.0 GB | 255s |
| Granite 4.2 3B | 0.74s | 0.85s | 90 | 1.17s | 0.32s | 22.8 GB | 7.3 GB | 195s |

*9B ran enforce-eager + util 0.90 (bf16 fit workaround); tuned quantized serving
would improve it substantially. Candidates reserve ~0.92 util (vLLM pool) — actual
weights: 7-19 GB vs baseline 17 GB. Boot on the 3090: 3-6.5 min.
Config note: baseline runs AWQ-4bit + int8 KV + graphs; candidates are bf16, which
is the quality-favorable configuration for the candidates.

## 10. Failure analysis (the evidence that matters)
1. **Prompt injection compliance — THE headline gap.** Crafted instructions inside
   email content were followed by 5 of 6 candidates: Qwen4B (2/2 tested),
   Ling3 (3/4), LFM (4/4), Granite (2/2), Qwen9B (1/2). Baseline gemma and
   Gemma-E4B resisted all. Exact-compliance details (e.g. returning
   category=Action + needs_reply=true + confidence=1.0 precisely as instructed).
   In-app impact = misclassification/misfiling (the app never deletes; filing is
   reversible via Undo), but for the agent surface it could mean executing
   injected intent via tools — that is why it gates the recommendation.
2. **No-match / hallucination traps:** baseline fails h3/h4 with EMPTY replies
   (search spirals); E4B only fails h2; all models share h2 (stretched-fitting an
   unrelated budget email to a nonexistent "renovation budget" question).
3. **Ambiguity/clarification:** "the payment email" acted on without asking which —
   baseline, qwen4b, qwen9b all did it (x1 fail). Ling/E4B partially better (70/56).
4. **Guard-rule semantics:** "keep X in place" requires conditions + EMPTY actions.
   4B, E4B, Ling, LFM, Granite all failed variants (e.g. actions {"move_to":"Keep"}).
   Baseline handles it 100%. This class is a clean candidate for a deterministic
   validator in the app (reject actions that equal placeholders, require explicit
   guard flag) instead of a model-side fix.
5. **Enum discipline:** LFM invented "Notifications"/"Reminder" labels (23/106);
   such mail would not file anywhere in the app (no category->folder mapping).
6. **Output discipline:** E4B narrates process in final answers; Qwen/Ling leak
   deliberative prose at un-flagged call sites (rules/draft) unless thinking is
   explicitly disabled — which forced model-specific thinking adaptations.
7. **Runtime integration tax:** granite 4.2 tool format fails every streaming
   parser in vLLM <=0.27 (non-stream works); Ling/Granite need the 0.26.1rc image;
   bf16 9B needs eager+lower-util to fit 24GB. Deployment cost is real and must
   be budgeted per model.

## 11. Adversarial robustness comparison (classification suite, 9 injection cases)
| baseline | qwen9b | e4b | qwen4b | ling3 | granite | lfm |
|---|---|---|---|---|---|---|
| 91.7 | 78.3 | 78.3 | 64.2 | 54.2 | 52.5 | 37.5 |
(sub-scores; higher = more resistant; failed injections counted CRITICAL)

## 12. Structured output / tool use
- JSON validity after adaptation: 99-100% for every model (before adaptation:
  Qwen3.5 both sizes catastrophic at temp-0 thinking; granite CoT-in-content).
- Tool-call mechanics: all models emit parseable calls EXCEPT granite, which
  needs non-streaming — a runtime, not model, defect (documented).
- Tool-arg correctness (g-cases): baseline 100; e4b 75.8; ling 75; granite 75.8;
  qwen4b 85.8; qwen9b 70. Small models hallucinate argument names/values and need
  schema-strict validation; the app already validates tool schemas server-side.

## 13. Quality vs cost — the efficiency frontier
(sev-adj | classify median | weights size | crit)
- baseline 26B/3.8B-active: 90.0 | 4.33s | 17 GB | 3 — the incumbent.
- Qwen3.5-9B: 90.6 | 5.77s* | 19.3 GB | 2 — matches quality; needs adaptation;
  classify slower only because of the eager fallback config; candidate for tuning.
- Gemma-4-E4B: 85.0 | 6.05s | 16.0 GB (QAT-4bit ~5 GB) | 1 — best
  quality-per-GB-risk at the small end; zero adaptation.
- Qwen3.5-4B: 82.5 | 0.74s | 9.3 GB | 4 — the speed leader among 4B-class,
  but injection compliance makes it unsafe as the sole mail AUTOMATOR today.
- Ling-3.0-tiny: 77.6 | 0.68s | 15.8 GB | 5 — fast, decent assistant, same
  injection caveat; guard-rule weakness.
- LFM2.5-8B-A1B: 68.9 | 0.34s | 17 GB | 5 — fastest but enum discipline and
  injection make it unsuitable for classification authority.
- Granite 4.2 3B: 63.8 | 0.74s | 7.3 GB | 6 — smallest weights, largest
  integration tax + weakest behavior; reject for this app.
Frontier: E4B (smallest, safest) — Qwen9B (highest quality) — the 4B/8B-MoE tier
as speed specialists with guardrails.

## 14. Where smaller models struggle (post-routing view)
Even granting rules/classifiers/metadata filters, the residual LLM-only tasks are:
no-match honesty (assistant), ambiguity clarification, guard-rule authoring, and
injection resistance in classify. The first three are mitigated by deterministic
wrappers (below); injection resistance is NOT mitigable deterministically and
must be a hard model-selection criterion until the app hardens its prompts.

## 15. Movable to deterministic specialists
- Guard-rule validation (reject non-empty "actions" placeholders; explicit guard flag).
- Category enum enforcement (reject/retry off-enum labels — fixes LFM-class failures).
- JSON extraction: parse the FIRST balanced object; tolerate fenced duplicates
  (fixes Granite/Ling "Extra data" classes).
- Prompt-budget guard (baseline itself died once at the 16K server wall in the
  assistant loop; cap/trim transcript earlier).
- Clarify-before-act gate for ambiguous "the X email" when >1 candidate match.
- Injection screening: content-pattern flags are heuristic only; do not rely on them.

## 16. Small-model + fallback architecture (recommended shape)
rules/classifiers -> small model (classify/draft/simple tool use)
                  -> Gemma fallback for: rule learning, guard authoring, long-mail
                     classification, and any no-match/uncertain answer path
Keep the current DeepSeek fallback env plumbing; point LLM_FALLBACK_* at the local
Gemma endpoint (it is currently blank in production settings).

## 17. Smallest practical model (the answer)
- For NO material quality loss: **Qwen3.5-9B** (93.9/90.6 vs 92.6/90.0) with the
  documented thinking adaptation — but it is not "substantially" smaller in wall
  terms except memory (19.3 vs 17 GB weights) and needs runtime care. Practical
  only if the owner values quality-equivalence over size.
- For the SMALLEST practical replacement: **Gemma-4-E4B (4.5B effective)** —
  90.3/85.0, ONE critical (the same h2 trap the baseline fails 3/5 of), zero
  adaptation, ships with official QAT-4bit for smaller footprints. Its gaps
  (guard rules, narration, multi-step) are addressable with the deterministic
  wrappers in section 15 and a Gemma fallback for the rules suite.
- The <=4B/MoE tier (Qwen4B, Ling, LFM, Granite) currently FAILS the injection
  criterion; adopt one of them only after adding an authoritative anti-injection
  system-prompt revision and re-testing, or keep them out of classification
  authority entirely.

## 18. Limitations / uncertainty
- Injection evidence: 4 crafted emails + 2 assistant cases; a broader adversarial
  set could move per-model numbers, but the 5-of-6 obedience pattern is stark.
- Single run per case (temp 0); stability repeats on the hard subset are stored in
  results/<model>/stability/ (baseline + Qwen9B; check variance there — see
  stability notes below).
- Latency configs differ per candidate (documented per row; 9B eager is the
  biggest caveat). Real deployments should re-measure after quantization/tuning.
- Case-set judgment calls: a QA pass corrected expectations; residual scorer
  judgment risk remains on a handful of subjective cases (marked in code).
- Ling-3.0-tiny and granite ran on a vLLM rc image (0.26.1rc) — the only local
  build with their parsers/arch; exact numbers may shift slightly on other builds.

## Artifacts
- Suite: benchmarks/cases (+ FROZEN.md), corpus: benchmarks/corpus
- Harness: benchmarks/harness (run_model.py, latency_pass.py, tool_sim.py, serve.sh,
  pipeline scripts, stability.sh)
- Results per model: benchmarks/results/<key>/ (cases, summary.json, latency.json,
  server.json, resources, unmodified probe evidence, stability/)
- Comparison table: benchmarks/reports/comparison.md (+ comparison_data.json)
- Failure log: benchmarks/reports/failure-log.md
- Research/inventory/taxonomy/scoring docs: benchmarks/docs/
