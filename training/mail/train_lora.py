#!/usr/bin/env python3
"""Actual MiniCPM5-2B LoRA smoke on the owned GPU (acceptance AC7).

Runs a small number of optimizer steps on the authored SFT examples with an
assistant-only loss mask, records the loss trajectory, verifies the weights
changed and reload, and writes an evidence receipt.  This proves the pipeline
end-to-end; it is **not** a quality claim.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lora_common as LC  # noqa: E402


def build_batch(tokenizer, example, device, max_len):
    import torch
    built = LC.build_supervised(tokenizer, LC.native_messages(example),
                                LC.tool_defs(example), max_len=max_len)
    if built["supervised_tokens"] == 0:
        return None
    ids = torch.tensor([built["input_ids"]], dtype=torch.long, device=device)
    labels = torch.tensor([built["labels"]], dtype=torch.long, device=device)
    return {"input_ids": ids, "attention_mask": torch.ones_like(ids),
            "labels": labels, "supervised_tokens": built["supervised_tokens"]}


def _repo_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _code_fingerprints():
    root = _repo_root()
    paths = [os.path.join(os.path.dirname(__file__), name) for name in (
        "train_lora.py", "lora_common.py", "generate_teacher.py",
        "rollout_dialogue.py", "evaluate.py", "briefs.py")]
    paths.append(os.path.join(root, "engine.py"))
    return LC.code_fingerprints(paths)


def _template_sha256():
    import hashlib
    root = _repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    from benchmarks.v3.training import minicpm
    text = minicpm.render_messages([
        {"role": "user", "content": "x"},
        {"role": "tool", "content": "y", "tool_call_id": "c1"}])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def loss_on(model, batch):
    import torch
    with torch.no_grad():
        out = model(**batch)
    return float(out.loss.detach().float().cpu())


def probe_logits(model, batch):
    import torch
    with torch.no_grad():
        out = model(input_ids=batch["input_ids"],
                    attention_mask=batch["attention_mask"])
    return out.logits.detach().float().cpu()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=LC.DEFAULT_MODEL_DIR)
    ap.add_argument("--examples", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--max-len", type=int, default=2048)
    ap.add_argument("--lora-r", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--device", type=int, default=0)
    args = ap.parse_args(argv)

    import torch
    torch.manual_seed(args.seed)
    device = "cuda:%d" % args.device

    tokenizer = LC.load_tokenizer(args.model_dir)
    model = LC.load_model(args.model_dir, device=args.device)
    base_fingerprint = LC.sha256_tree(args.model_dir)

    examples = LC.read_jsonl(args.examples)
    batches = []
    for ex in examples:
        b = build_batch(tokenizer, ex, device, args.max_len)
        if b is not None:
            batches.append((ex["example_id"], b))
    if not batches:
        raise SystemExit("no trainable examples with supervised tokens")

    from peft import LoraConfig, get_peft_model
    lora = LoraConfig(r=args.lora_r, lora_alpha=args.lora_r * 2,
                      lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                      "gate_proj", "up_proj", "down_proj"])
    model = get_peft_model(model, lora)
    model.train()
    param_report = LC.trainable_parameter_report(model)

    probe_id, probe = batches[0]
    loss_before = loss_on(model, probe)
    logits_before = probe_logits(model, probe)

    base_before = {n: p.detach().float().cpu().clone()
                   for n, p in model.named_parameters()
                   if "lora_" not in n}
    opt = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad], lr=args.lr)

    losses = []
    started = time.time()
    for step in range(args.steps):
        ex_id, batch = batches[step % len(batches)]
        out = model(**{k: batch[k] for k in
                       ("input_ids", "attention_mask", "labels")})
        loss = out.loss
        if not torch.isfinite(loss):
            raise SystemExit("non-finite loss at step %d: %r" % (step, loss))
        opt.zero_grad()
        loss.backward()
        opt.step()
        losses.append({"step": step, "example_id": ex_id,
                       "loss": round(float(loss.detach().cpu()), 6)})
    elapsed = time.time() - started

    model.eval()
    loss_after = loss_on(model, probe)
    logits_after = probe_logits(model, probe)
    adapter_delta = float((logits_after - logits_before).abs().max())
    del logits_before, logits_after
    base_delta = 0.0
    for n, p in model.named_parameters():
        if "lora_" not in n:
            base_delta = max(base_delta, float(
                (p.detach().float().cpu() - base_before[n]).abs().max()))
    lora_sq = 0.0
    for n, p in model.named_parameters():
        if "lora_" in n:
            lora_sq += float((p.detach().float().cpu() ** 2).sum())
    lora_norm = lora_sq ** 0.5

    adapter_dir = os.path.join(args.out, "adapter")
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    adapter_files = LC.adapter_fingerprint(adapter_dir)

    # reload the adapter and confirm the same probe loss
    del model
    torch.cuda.empty_cache()
    reloaded = LC.load_model(args.model_dir, adapter_dir=adapter_dir,
                             device=args.device)
    reload_loss = loss_on(reloaded, probe)

    receipt = {
        "kind": "mail-sft-lora-smoke",
        "model": LC.MODEL_ID,
        "model_revision": LC.MODEL_REVISION,
        "model_dir": args.model_dir,
        "base_weights_sha256": base_fingerprint,
        "examples_file": args.examples,
        "examples_sha256": LC.sha256_file(args.examples),
        "code_fingerprints": _code_fingerprints(),
        "template_sha256": _template_sha256(),
        "adapter_dir": adapter_dir,
        "adapter_files": adapter_files,
        "seed": args.seed,
        "steps": args.steps,
        "optimizer_steps_performed": len(losses),
        "lr": args.lr,
        "lora_r": args.lora_r,
        "max_len": args.max_len,
        "parameters": param_report,
        "losses": losses,
        "loss_first": losses[0]["loss"],
        "loss_last": losses[-1]["loss"],
        "loss_before_training": round(loss_before, 6),
        "loss_after_training": round(loss_after, 6),
        "loss_reload": round(reload_loss, 6),
        "all_losses_finite": all(abs(l["loss"]) < 1e6 for l in losses),
        "base_weight_max_delta": base_delta,
        "lora_weight_norm": lora_norm,
        "adapter_logits_delta": adapter_delta,
        "weights_changed": bool(abs(loss_after - loss_before) > 1e-9
                                or lora_norm > 0 or adapter_delta > 0),
        "reload_matches": bool(abs(reload_loss - loss_after) < 1e-3),
        "seconds": round(elapsed, 2),
        "device": device,
        "gpu": torch.cuda.get_device_name(args.device),
        "peak_mem_mib": torch.cuda.max_memory_allocated() // 1048576,
        "note": ("Pipeline smoke only; a small run proves the machinery, not "
                 "model quality."),
    }
    LC.write_json(os.path.join(args.out, "train_receipt.json"), receipt)
    print(json.dumps({
        "steps": receipt["steps"],
        "loss_first": receipt["loss_first"],
        "loss_last": receipt["loss_last"],
        "loss_before_training": receipt["loss_before_training"],
        "loss_after_training": receipt["loss_after_training"],
        "loss_reload": receipt["loss_reload"],
        "base_weight_max_delta": receipt["base_weight_max_delta"],
        "lora_weight_norm": receipt["lora_weight_norm"],
        "weights_changed": receipt["weights_changed"],
        "reload_matches": receipt["reload_matches"],
        "trainable_fraction": receipt["parameters"]["trainable_fraction"],
    }, indent=2, default=str))
    return 0 if (receipt["weights_changed"]
                 and receipt["reload_matches"]
                 and receipt["all_losses_finite"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
