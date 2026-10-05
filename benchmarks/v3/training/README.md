# `benchmarks/v3/training` — configurable-taxonomy mail SFT slice

A **training-only** addition to benchmark v3. It consumes the public
`development` material through `benchmarks.v3.build` and the frozen sandbox, and
emits an explicit `mail-sft-0.1` training artifact. It does **not** change the
frozen v3.0 schemas, the adapters, the runner, the scorer or any production code
(`app.py`/`engine.py`/`store.py`/UI/OAuth are untouched).

Status: **draft**. Synthetic-only; no human review or seal is asserted, and no
test-set qualification is claimed.

## What the slice proves

| Acceptance | Where |
|---|---|
| AC1 configurable taxonomy, name-independent resolution, hidden-fact-free public prompt | `taxonomies.py`, `test_training_taxonomy.py` |
| AC2 fail-closed, generation-domain-separated export with contamination (incl. workflow-source text) | `export.py`, `test_training_export.py` |
| AC3 bounded native tool surface, AST-extracted schemas, result-only untrusted payload, off/ask/auto host enforcement | `tools_native.py`, `test_training_tools.py` |
| AC4 exact per-turn trace, context reconstruction, malformed-arg rejection | `trace.py`, `test_training_trace.py` |
| AC5 verification + native serialization + per-turn loss mask | `verify.py`, `messages.py`, `minicpm.py`, `test_training_messages.py` |

AC5 additionally requires each model's **own** native template. The offline
reference renderer `minicpm.py` mirrors the released `openbmb/MiniCPM5-2B`
`chat_template.jinja` (preserving the `tool` role, `<function>` XML calls with
CDATA, and `<think>` reasoning). Training uses the tokenizer's own
`apply_chat_template`; the reference renderer is what the committed examples and
tests inspect. This deliberately avoids the upstream chat-only SFT patch that
drops tool roles.

## CLI

```bash
# deterministic, offline; writes examples.jsonl + report.json + samples/
.venv/bin/python -m benchmarks.v3.training.cli build-slice \
    --out /tmp/mail-sft-slice --seed 7 --triage-roots 8 \
    --sample-dir benchmarks/v3/training/samples
.venv/bin/python -m benchmarks.v3.training.cli render <example.json>
.venv/bin/python -m benchmarks.v3.training.cli verify <example.json>
```

The committed `samples/*.json` are generated public synthetic examples (an
explicit generated-file exception): canonical messages, the per-turn mask, the
MiniCPM5 native render, and a compact trace summary.

## Generation domains

`training`, `development` and `evaluation` each carry their own
`domain_sha256 = hash(domain, seed, revision)`. The exporter is fail-closed: a
case whose split is not allowed (calibration/private/test for a training export),
whose decision gold is ambiguous or unobservable, whose source is real mail, or
whose example fails verification is **rejected with a reason**, never silently
dropped. `contamination_report` checks distinct domain hashes, no shared
`source_id` across domains, no shared workflow source text, and no gold hidden
evidence in any model-facing input.

## Boundaries

* Synthetic mailbox only; `send`/`delete` are never exposed and never execute.
* `off`/`ask`/`auto` is host-enforced; a model cannot self-approve a write.
* `propose_rule` is proposal-only; it is never applied.
* No confidence target is supervised (it is not authored gold).
* No `.env`, secret, live DB or real mailbox is read.
