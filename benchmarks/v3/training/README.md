# `benchmarks/v3/training` — configurable-taxonomy mail SFT slice

A **training-only** addition to benchmark v3. It consumes public synthetic
material through `benchmarks.v3.build` and the **production** assistant contract,
and emits an explicit `mail-sft-0.1` training artifact. It does not change the
frozen v3.0 schemas, adapters, runner, scorer or any production file
(`app.py`/`engine.py`/`store.py`/UI/OAuth are untouched).

Status: **draft**. Synthetic-only; no human review or seal is asserted and no
test-set qualification is claimed.

## What the slice proves

| Acceptance | Where |
|---|---|
| AC1 configurable taxonomy, name-independent resolution, one shared public taxonomy block in classifier and workflow prompts | `taxonomies.py` |
| AC2 explicit role/domain ingestion, hard fail-closed denylist, cross-task source/lineage + email-text contamination | `export.py` |
| AC3 **production** `engine.ASSISTANT_TOOLS` schemas (AST-extracted), production result shapes, untrusted wrapper, off/ask/auto host enforcement | `engine_contract.py`, `tools_native.py`, `native_state.py` |
| AC4 exact per-turn trace, context reconstruction, malformed-arg rejection | `trace.py` |
| AC5 verifier (actual assistant JSON + state/outcome/rule scope), native serialization, per-turn loss mask through the real training path | `verify.py`, `messages.py`, `minicpm.py` |
| M2/M3 real teacher emails feed both classifier and a bounded model tool dialogue sharing source/lineage | `training/mail/briefs.py`, `generate_teacher.py`, `rollout_dialogue.py` |
| M4 base-vs-adapter on independent dev records with category+needs_reply and grounded workflow rollout | `training/mail/evaluate.py` |
| M5 one-command reproducible run with HEAD/tree, commands, env and captured exit status | `training/mail/run_slice.py` |

The tool surface is partial by **name only** (`search_messages`, `read_message`,
`move_message`, `list_rules`, `propose_rule`); every schema entry is the
production entry verbatim and result payloads match production. `propose_rule`
queues a proposal (`status: "queued for the user's one-click approval"`) and
never applies it; `send`/`delete` are never exposed.

## CLI (deterministic, offline)

```bash
.venv/bin/python -m benchmarks.v3.training.cli build-slice --out /tmp/slice \
    [--sources sources.json --dialogues dialogues.json \
     --dev-sources dev_sources.json --dev-dialogues dev_dialogues.json]
.venv/bin/python -m benchmarks.v3.training.cli render <example.json>
.venv/bin/python -m benchmarks.v3.training.cli verify <example.json>
```

Without `--sources`/`--dialogues` the authored fixtures are used; with them, the
live teacher + rollout output is used. `build-slice` never relabels an
absent-role record and writes `training.jsonl` / `development.jsonl` directly
(no undocumented manual split).

## Boundaries

* Synthetic mailbox only; `send`/`delete` never execute; `propose_rule` never
  applies.
* `off`/`ask`/`auto` is host-enforced; a model cannot self-approve.
* No confidence target is supervised; no `.env`, secret, live DB or real mailbox
  is read. The committed `samples/` are compact public synthetic DRAFT examples.
