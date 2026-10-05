# Mail SFT vertical slice — implementation plan

Status: **draft**. This records the contract for the `mail-sft-slice` worktree
(branch `mail-sft-slice`, base `17e1919d7aeca7319863c496461fc793c2e95ef5`). It is
a thin vertical slice reusing the accepted WP0–WP7 spine, not full coverage.

## Outcome

A configurable-taxonomy, bounded-native-tool SFT slice for v3 mail triage,
**including a small real `openbmb/MiniCPM5-2B` LoRA training/evaluation smoke**.
Risk STANDARD: synthetic-only new interfaces, no production execution or settings
changes; frozen v3.0 defaults are preserved.

## Owned paths

* `benchmarks/v3/training/**` (new package, fixtures, samples)
* `benchmarks/v3/tests/test_training_*.py` (focused tests)
* `training/mail/**` (GPU driver + pinned requirements), outside the Python package
* `implementation_plans/mail-sft-slice/plan.md`, `benchmarks/v3/training/README.md`

Existing `benchmarks/v3` contracts/build/adapters/runner/cli/schema are untouched.
The slice adds a **separate** training artifact schema (`mail-sft-0.1`) and CLI so
v3.0 default semantics and tests stay unchanged. The one intentional contrast is
documented, not edited: `tools_native.parse_arguments` rejects malformed
arguments before execution, whereas the v3 workflow loop's `_json_args` coerces
them to `{}`.

## Acceptance mapping

* **AC1** `taxonomies.py`: authored semantic intents + category `id`-keyed
  coverage, independent of display names; `public_projection` strips
  `covers`/`roles`/`authoring_notes`; `rename`/`reorder`/`remap_folders`;
  `cross_task_lineage` links the classifier and workflow views of one source.
* **AC2** `export.py`: hashed training/development/evaluation domains; fail-closed
  rejection of disallowed splits, ambiguous/unobservable gold, real mail, mixed
  domains; contamination includes workflow mailbox source text.
* **AC3** `tools_native.py`: schemas AST-extracted from `sandbox/tools.py`;
  bounded surface `search_messages/read_message/move_message/list_rules/
  propose_rule`, explicitly labelled partial (no all-tool parity); result-only
  untrusted wrapper; host-enforced off/ask/auto; no self-approval; send/delete
  never execute; ambiguous-sort → clarification → authored follow-up → grounded
  move/proposal scenario plus an injection variant
  (`fixtures/workflow_scenarios.json`).
* **AC4** `trace.py`: full per-turn model input, completion, raw tool-call
  arguments, tool responses, events, finish reason, per-turn + aggregate usage,
  state snapshots and identities; malformed arguments rejected before execution
  and never `{}`; context reconstruction from the recorded per-turn input.
* **AC5** `verify.py` + `messages.py` + `minicpm.py`: factual slots, scope/state/
  proposal conditions, no fabricated completion, authored classification
  decisions, verified summary/reason, no confidence target, no auto seal or
  test-qualification; canonical messages + per-turn masks; MiniCPM5 native
  template preserving XML calls, tool responses and think markers; nested rule
  arg / bool / array / multiline round-trip; assistant-only loss mask covering
  calls and finals, masking inputs and bad student turns.
* **AC6** real teacher generations (three seed batches) with accepted/rejected
  counts and provenance, compact committed samples.
* **AC7** pinned official HF MiniCPM5-2B LoRA smoke on an owned GPU; weights
  changed + finite loss + reload; BASE vs ADAPTER on independently generated
  development cases with identical config; classification and bounded tool-call
  behavior checked.
* **AC8** required commands with captured SHA/tree/command/exit evidence:
  `.venv/bin/python -m unittest discover -s benchmarks/v3/tests -v`;
  `.venv/bin/python benchmarks/v2/tests/run_v2_tests.py`;
  `.venv/bin/python tests/mock_e2e.py --all`; `git diff --check .`

## Evidence and honesty

A tiny run proves the pipeline, not model quality. Training failures or quality
regressions are reported honestly; no synthetic green. Weights, venvs and verbose
artifacts stay outside Git; only compact public synthetic samples are committed.

## Delivered evidence (this slice)

* **AC1–AC5**: `benchmarks/v3/tests/test_training_*.py` (57 tests) plus the
  committed `samples/*.json` (canonical messages, per-turn mask, MiniCPM5 native
  render, trace summary). `samples.py` + a separate `mail-sft-0.1` schema and CLI
  keep v3.0 semantics unchanged.
* **AC6**: `training/mail/generate_teacher.py` on the pinned MiniCPM5-2B over
  three seed batches (11/22/33): 9 accepted / 9 rejected of 18; committed
  `samples/teacher/teacher_generations.json`. The pinned 2B is the recorded
  teacher (no temporary container started).
* **AC7**: `training/mail/train_lora.py` — 20 optimizer steps, loss 3.072 -> 0.181
  (all finite), base weights unchanged (max delta 0), LoRA weight norm 28.24,
  adapter saved and reloaded (reload loss 0.181 matches). Trainable params
  12.56M / 2.53B (0.50%), peak 13.3 GiB on GPU0. `training/mail/evaluate.py` on
  independent development cases (seed 101 + disjoint dev workflow scenarios),
  identical config: classification base 1.0 -> adapter **0.833 (regression)**;
  bounded tool calls 0.5 base / 0.5 adapter. Reported honestly; not a quality
  claim.
* **AC8**: `481 v3 / 57 training included`, `52 v2`, `1026 mock e2e`, `git diff
  --check` clean. Committed evidence summary at
  `benchmarks/v3/training/samples/training_evidence.json`.
