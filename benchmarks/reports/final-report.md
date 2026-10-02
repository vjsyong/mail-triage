# Mail-Triage: Can a Smaller Local Model Replace Gemma 4 26B-A4B?
## Final report — workstream `model-eval` (2026-10-02)

> Status: IN PROGRESS — sections fill in as candidate runs complete.
> All numbers come from the frozen suite in `benchmarks/cases/` (196 cases,
> `FROZEN.md`); baseline = live production `gemma-4-26b-a4b` endpoint.

## 1. Current LLM responsibilities
See `benchmarks/docs/app-llm-inventory.md` (7 call sites; classify is the volume
workload; the assistant agent is the critical-risk surface; drafts + rule learning
are low-frequency but user-visible).

## 2. Benchmark design
See `benchmarks/docs/benchmark-taxonomy.md` + `scoring-methodology.md`.
196 frozen cases: 106 classification (normal/ambiguous/multilingual/long/junk/
adversarial/contradictory), 60 assistant tool-use (selection/args/QA/multi-step/
ambiguity/injection/hallucination traps/tool misuse), 12 drafting, 8 rule-learning,
6 simulator, 4 thought-summary. Ground truth from the synthetic corpus + task
rules only (no model in the loop). Severity weights LOW 1 / MED 3 / HIGH 9 / CRIT 27.

## 3. Dataset composition
Synthetic fictional mailbox: 64 messages / 7 threads / adversarial set; deterministic.
Paraphrase+robustness variants live in the same files as separate cases.

## 4. Baseline Gemma results (PRODUCTION endpoint, frozen suite)
- Overall: **92.6 raw / 90.0 severity-adjusted**; 3 critical cases.
- classification 93.4 (weak: long_mail 72.5 — one 27K-char CoT runaway + 2 misses)
- assistant 87.6 (weak: hallucination traps 35 — confabulation + 2 empty replies after search spirals; ambiguity 75; multi-step 75)
- drafting 100 · rules 100 · simulate 100 · summary 100
- Latency (controlled pass, prod endpoint, 20 classify + 6 assistant cases):
  classify median **4.33s**, p95 **16.5s**, max 40s (thinking CoT variance; 135 tok/s gen);
  assistant median wall **1.8s**, median TTFT 0.09s (short queries).
- VRAM: **23.1 GB reserved** (gpu-mem-util 0.96 of 24GB; int8 KV pool + AWQ-4bit weights),
  container RAM 21.3 GiB, cold boot 397s measured.
- Baseline weakness notes: with thinking ON, gemma occasionally burns its whole
  output budget in CoT (observed 27K-char runaway on a long receipt → no JSON).
  Misfiles observed: invoices → Action (2×), phishing → Notification, zh invoice → Notification.

## 5. Web research (2026-10-02)
See `benchmarks/docs/research-2026-10-02.md` (Gemma 4 family / Qwen3.5 / Granite 4.2 /
LFM2.5 / MiniCPM5 / Nemotron; licenses; vLLM support verified against local images).

## 6. Candidate rationale
See `benchmarks/docs/candidate-shortlist.md`.

## 7. Exact models/runtimes tested
| key | checkpoint | served as | quant | runtime | notes |
|---|---|---|---|---|---|
| baseline | gemma-4-26b-a4b AWQ-4bit (cyankiwi) | live :8040 | AWQ4 + int8 KV | vLLM v0.22.0 | 16K ctx, GPU0 |
| qwen3.5-4b | Qwen/Qwen3.5-4B | :8045 | bf16 | vLLM v0.22.0 | GPU1, 16K ctx, parser qwen3_xml |
| (tbd) | | | | | |

## 8. Candidate results
(fills in)

## 9. Performance / resources
(fills in)

## 10. Failure analysis
(fills in)

## 11. Adversarial robustness
(fills in)

## 12. Structured output / tool use
(fills in)

## 13. Quality vs memory/latency tradeoff
(fills in)

## 14. Where smaller models struggle
(fills in)

## 15. Movable to deterministic specialists
(fills in)

## 16. Small-model + Gemma fallback architecture
(fills in)

## 17. Smallest practical model
(fills in)

## 18. Limitations / open items
- Case-set QA happened once with the baseline (documented in FROZEN.md).
- Latency measured under light background load (parallel candidate runs on the other GPU); treated as indicative.
- Pairwise paraphrase coverage is small for some classes; repeat-stability done on the hard subset only.
