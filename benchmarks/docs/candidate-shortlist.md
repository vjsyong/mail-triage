# Candidate Shortlist (max 5) — selected 2026-10-02

Selection rule (from the brief): candidates must be **genuinely promising**,
**substantially smaller than the baseline** (26B/3.8B-active), and cover useful
architectural points. Baseline does not count against the 5-candidate limit.

## The shortlist

### C1. `Qwen/Qwen3.5-9B` — the "closest to baseline" candidate
- 9B dense (hybrid DeltaNet+GQA), 262K ctx, Apache 2.0, ~19 GB bf16.
- Best documented agentic/tool numbers of any small current model: BFCL-V4 66.1,
  τ²-bench 79.1, IFEval 91.5. Huge adoption (9.3M downloads).
- vLLM v0.22.0 supports the arch; `--tool-call-parser hermes`, reasoning parser.
- Question it answers: *can a single 9B dense fully replace a 26B MoE?*

### C2. `Qwen/Qwen3.5-4B` — the "smallest likely-viable dense" candidate
- 4B dense, 262K ctx, Apache 2.0, ~9 GB bf16.
- τ²-bench 79.9 (≈9B), IFEval 89.8, BFCL-V4 50.3.
- Question: *how much quality is left at the 4B workhorse point?*

### C3. `google/gemma-4-E4B-it` — the same-family shrink (integration-parity)
- 4.5B effective (8B incl. embeddings), 128K ctx, Apache 2.0, ~16 GB bf16;
  official QAT-4bit for vLLM also exists.
- SAME family as baseline → same chat template, `gemma4` tool/reasoning parsers,
  enable_thinking semantics, JSON-mode behavior. If viable, swap = zero migration.
- Weaker on Google's own τ² (42.2) — but that metric is agentic tool use, exactly
  what this benchmark must verify (or falsify) on OUR task mix.
- Question: *can we just shrink the family?*

### C4. `ibm-granite/granite-4.2-3b` — structured/tool specialist at minimum size
- 3B dense, 128K ctx, Apache 2.0, ~7 GB bf16. Built-in `<think>`.
- "Reasoning-augmented tool calling"; BFCL v4 52.4 at 3B (≈ its own 8B), IFBench
  ~73. Enterprise JSON/tool pedigree, RL-trained on structured output + tool use.
- Question: *does a purpose-built small tool model beat generalists at our tasks?*

### C5. `LiquidAI/LFM2.5-8B-A1B` — efficient MoE + alternate architecture
- 8.3B total / **1.5B active** MoE, conv-hybrid, 128K ctx, LFM Open License 1.0.
- Agentic RL inside real harnesses; day-one vLLM (`lfm2` tool parser).
- Question: *can a 1.5B-active MoE hold quality while slashing compute?*
  (Closest structural analogue of the baseline's own MoE design.)

## Reserves (documented; not benchmarked this round)
| Model | Why reserve |
|---|---|
| Qwen3.5-2B | 2B floor; likely fails JSON/tool bars; keep for the routing study |
| gemma-4-12B-it | needs vLLM ≥0.25 (Unified arch); add if 9B-class wins and family parity matters |
| granite-4.2-8b | same bench scores as 3B sibling; 3B is the tighter test |
| LFM2.5-2.6B | 2.6B; reserve for the very-small tier |
| MiniCPM5-2B | no vLLM tool parser for its format; can't drive the agent loop cleanly |
| Nemotron-3-Nano-4B | gated repo + NVIDIA license; not offline-first friendly |
| gpt-oss-20b | not substantially smaller than baseline |
| gemma-4-E2B-it | τ² 24.5 — below plausibility threshold for the assistant |

## Deployment plan for candidates (identical to baseline where possible)
- **Same runtime**: vLLM **v0.22.0** (the production image) for every candidate.
- GPU **1** (free RTX 3090; baseline keeps GPU 0). Sequential serving.
- `--max-model-len 16384` (matches baseline; benchmark cases designed ≤ ~13K).
- Precision: bf16 checkpoints (all fit a 3090) → candidates get best-case quality;
  practical quantized footprints (QAT-4bit / AWQ-4bit) recorded in the report and
  spot-checked on the recommended model later.
- Sampling: `temperature=0`; app payloads replayed verbatim (thinking kwarg,
  json_object mode, tools, streaming) with the app's per-field 4xx stripping.
- Per-model tool parsers: hermes (Qwen), gemma4 (Gemma), granite4 (Granite),
  lfm2 (LFM). Reasoning parsers set where the family defines one.
