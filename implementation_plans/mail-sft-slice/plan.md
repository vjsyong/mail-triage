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
* **AC3** `engine_contract.py` + `tools_native.py` + `native_state.py`:
  schemas are the **production** `engine.ASSISTANT_TOOLS` entries, AST-extracted
  without importing engine; the bounded surface limits tool **names** only
  (`search_messages/read_message/move_message/list_rules/propose_rule`).
  Production result sub-payload shapes and the `<untrusted_email_content>` wrapper
  (with neutralisation) are reproduced; schema type/enum/required violations and
  malformed arguments are rejected before any mutation; off/ask/auto is host
  enforced and a model cannot self-approve; `propose_rule` queues a proposal and
  never applies it; send/delete never execute.
* **AC4** `trace.py`: full per-turn model input, completion, raw tool-call
  arguments, production result payloads, events, finish reason, per-turn +
  aggregate usage, state snapshots and identities; malformed arguments rejected
  before execution and never `{}`; context reconstruction from the recorded
  per-turn input.
* **AC5** `verify.py` + `messages.py` + `minicpm.py`: the verifier consumes the
  actual supervised assistant JSON (enums/types/needs_reply) and rejects
  contradictions/fabrications; the dialogue verifier diffs real final state and
  events against authored `expected_state`/required/forbidden outcomes,
  assertions and positive+negative rule scope, and rejects a claimed completion
  with no matching event (an "approval" word elsewhere does not excuse it).
  Canonical + native messages preserve the supervised flag so the assistant-only
  loss mask (covering calls and finals, masking inputs and bad turns) is derived
  from it; MiniCPM5 native template preserves XML calls, tool responses and think
  markers; nested rule arg / bool / array / multiline round-trip.
* **AC6/M2/M3** `training/mail/briefs.py` -> `generate_teacher.py` produce real
  MiniCPM5-2B emails over three seed batches; `rollout_dialogue.py` runs a real
  bounded multi-turn dialogue (ambiguous request -> host follow-up -> grounded
  move/proposal) over the same email. One source record feeds both the classifier
  and the dialogue under a shared `source_id`/`lineage_id`.
* **AC7/M4** pinned HF MiniCPM5-2B LoRA smoke; `evaluate.py` compares BASE vs
  ADAPTER on independent dev records with category **and** needs_reply metrics
  (invalid outputs stay in the denominator) and a grounded workflow rollout
  (required args/scope/state), not just a first tool name.
* **AC8/M5** `training/mail/run_slice.py` runs the whole pipeline with per-stage
  captured exit files and a receipt bound to the exact clean HEAD/tree; required
  suites: `unittest discover -s benchmarks/v3/tests -v`,
  `benchmarks/v2/tests/run_v2_tests.py`, `tests/mock_e2e.py --all`,
  `git diff --check .`

## Evidence and honesty

A tiny run proves the pipeline, not model quality. Training failures or quality
regressions are reported honestly; no synthetic green. Weights, venvs and verbose
artifacts stay outside Git; only compact public synthetic DRAFT samples plus an
evidence summary are committed. Earlier (round-1) sample artifacts are preserved
for audit and superseded by the round-2 evidence.
