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

## GPU safety

Every run holds `flock /tmp/opencode/gpu0.lock` exclusively and verifies GPU0 has
no foreign PIDs and <= 500 MiB before loading, binding
`CUDA_VISIBLE_DEVICES=GPU-37e9773f-...0f72`. GPU1 (B1/B2) and its ports are never
touched and nothing is evicted. No teacher container is started: the pinned
MiniCPM5-2B itself is used as the generation teacher (recorded in provenance).

## Commands

```bash
RUN=/home/xrim/datasets/benchmark-v3/mail-sft-slice/run-20261005-1
export CUDA_VISIBLE_DEVICES=GPU-37e9773f-42ef-4ea1-dbe1-5567b8690f72
export PYTHONPATH=/home/xrim/datasets/benchmark-v3/mail-sft-slice/train-env

# AC6 real teacher generations (three seed batches)
python training/mail/generate_teacher.py --out "$RUN" \
    --sample-dir benchmarks/v3/training/samples/teacher --seeds 11,22,33

# AC7 small LoRA smoke (assistant-only loss mask)
python training/mail/train_lora.py --examples "$RUN/train.jsonl" \
    --out "$RUN/train" --steps 20

# AC7 BASE vs ADAPTER on independent development cases
python training/mail/evaluate.py --examples "$RUN/dev.jsonl" \
    --adapter "$RUN/train/adapter" --out "$RUN/eval"
```

## Method notes

* The released `chat_template.jinja` has **no** `{% generation %}` marker, so
  `return_assistant_tokens_mask` is all zeros. The driver derives the
  assistant-only mask from per-message rendered character spans, so it covers
  tool calls, think markers and final answers while masking system/user/tool
  turns. The `tool` role is preserved (not the upstream chat-only patch).
* The teacher uses MiniCPM5-2B as generator; candidates are verified
  deterministically and rejected counts are reported honestly.
* A tiny run proves the pipeline, not model quality. Results (including an
  adapter regression on a 6-case decision probe) are in
  `benchmarks/v3/training/samples/training_evidence.json`.
