# `training/mail` — GPU LoRA driver (AC6/AC7)

Actual GPU runs against the pinned `openbmb/MiniCPM5-2B`. Nothing here touches
the running mail-triage app, its DB, or a live mailbox.

## Environment (own, isolated)

The base interpreter is `/home/xrim/jev-sweep/venv-cuda` (torch 2.14.0+cu130,
transformers 5.17.0) — **never modified**. The pinned peft/accelerate overlay is
installed into an owned directory and put on `PYTHONPATH`:

```bash
TARGET=/home/xrim/datasets/benchmark-v3/mail-sft-slice/train-env
/home/xrim/jev-sweep/venv-cuda/bin/pip install --no-deps --target "$TARGET" \
    peft==0.21.2 accelerate==1.15.0 psutil==7.2.2 PyYAML==6.0.3
```

Model weights are downloaded at the pinned revision into an owned cache
(`models/MiniCPM5-2B`, revision `f97400052a43d642bbc6e9975e2397e3ae6a6b52`); the
weights, the env overlay and the verbose run artifacts stay **outside Git**.

## GPU safety (external lease; not driver-enforced)

**`run_slice.py` does not itself acquire a lock or probe ownership.** It only sets
`CUDA_VISIBLE_DEVICES` to the pinned GPU0 UUID. GPU0 ownership on this host is an
**external lease**: the whole run must be launched *under* `flock` after an idle
preflight, e.g.

```bash
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader \
  | awk -F', ' '$1==0{ if($2+0>500){exit 3} }'        # fail if GPU0 busy
flock -w 60 /tmp/opencode/gpu0.lock \
  /home/xrim/mail-triage/.venv/bin/python training/mail/run_slice.py \
    --run-dir /home/xrim/datasets/benchmark-v3/mail-sft-slice/run-<unique> \
    --steps 20
```

Historical runs (round 1/2) were likewise owned by an **external** lease, not by
the driver; this bounded limitation is retained rather than claiming the driver
enforces isolation. GPU1 and its ports are never touched and nothing is evicted.
No teacher container is started: the pinned MiniCPM5-2B is the generation teacher
and the rollout model (recorded in provenance).

## Commands

One command runs the whole pipeline (briefs → teacher → dialogues → dev → export
→ train → eval) with per-stage `*.exit` files and a `run_receipt.json`:

```bash
# launch under the external GPU0 lease (see GPU safety above)
flock -w 60 /tmp/opencode/gpu0.lock \
  .venv/bin/python training/mail/run_slice.py \
    --run-dir /home/xrim/datasets/benchmark-v3/mail-sft-slice/run-<unique> \
    --steps 20
```

Individual stages (all GPU stages need `PYTHONPATH=<train-env>`):

```bash
python training/mail/briefs.py --out "$RUN/briefs.json" --seeds 11,22,33
python training/mail/generate_teacher.py --briefs "$RUN/briefs.json" \
    --out "$RUN/sources.json"
python training/mail/rollout_dialogue.py --sources "$RUN/sources.json" \
    --out "$RUN/dialogues.json"
.venv/bin/python -m benchmarks.v3.training.cli build-slice --out "$RUN/slice" \
    --sources "$RUN/sources.json" --dialogues "$RUN/dialogues.json" \
    --dev-sources "$RUN/sources_dev.json" --dev-dialogues "$RUN/dialogues_dev.json"
python training/mail/train_lora.py --examples "$RUN/slice/training.jsonl" \
    --out "$RUN/train" --steps 20
python training/mail/evaluate.py --dev-sources "$RUN/sources_dev.json" \
    --adapter "$RUN/train/adapter" --out "$RUN/eval"
```

## Method notes

* The released `chat_template.jinja` has **no** `{% generation %}` marker, so
  `return_assistant_tokens_mask` is all zeros. The driver derives the
  assistant-only mask by locating each rendered `<|im_start|>assistant` segment
  up to its `<|im_end|>` (robust to consecutive `tool` messages), so it covers
  tool calls, think markers and final answers while masking system/user/tool
  turns. The `tool` role is preserved (not the upstream chat-only patch).
* The teacher uses MiniCPM5-2B as generator; candidates are verified
  deterministically and rejected counts are reported honestly.
* A tiny run proves the pipeline, not model quality. See
  `benchmarks/v3/training/samples/evidence_round2.json` for the round-2 run and
  the retained regression.

## Retained bounded limitations

* **R2-N4 (ask/off result parity).**  The trained/rolled-out path is `auto`
  (the proof is auto-only).  Production's `ask` returns a pending-approval shape
  (`pending_approval`/`action_id`/`preview`) and `off` a `permission_denied`
  note; `native_state` returns a generic `approval_required`/`permission_denied`
  error for those untrained branches.  Not full parity; no runtime change.
* **R2-N6 (contamination scope).**  `contamination_report` covers source ids,
  lineage ids and the structured source email text (including the workflow source
  email); the constant synthetic rollout distractor's text is not included.  This
  is a bounded limitation, not full isolation.
