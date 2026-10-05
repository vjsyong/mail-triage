"""Shared loading / masking / hashing for the GPU LoRA driver (AC6/AC7).

The base interpreter is ``/home/xrim/jev-sweep/venv-cuda`` (torch 2.14.0+cu130,
transformers 5.17.0) with the pinned peft/accelerate overlay on ``PYTHONPATH``.
Nothing here touches the running mail-triage app, its DB or any live mailbox.
"""
from __future__ import annotations

import hashlib
import json
import os

MODEL_ID = "openbmb/MiniCPM5-2B"
MODEL_REVISION = "f97400052a43d642bbc6e9975e2397e3ae6a6b52"
DEFAULT_MODEL_DIR = ("/home/xrim/datasets/benchmark-v3/mail-sft-slice/"
                     "models/MiniCPM5-2B")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(path):
    """A stable hash of a directory's file names + contents."""
    h = hashlib.sha256()
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            fp = os.path.join(root, name)
            rel = os.path.relpath(fp, path)
            h.update(rel.encode())
            if name.endswith((".safetensors", ".bin")):
                # weight blobs: hash by size + first/last MiB (fast, sufficient)
                size = os.path.getsize(fp)
                h.update(str(size).encode())
                with open(fp, "rb") as f:
                    h.update(f.read(1048576))
                    if size > 2097152:
                        f.seek(-1048576, os.SEEK_END)
                        h.update(f.read(1048576))
            else:
                h.update(sha256_file(fp).encode())
    return h.hexdigest()


def load_tokenizer(model_dir=None):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(model_dir or DEFAULT_MODEL_DIR)


def load_model(model_dir=None, adapter_dir=None, dtype=None, device=0):
    import torch
    from transformers import AutoModelForCausalLM
    dtype = dtype or torch.bfloat16
    model = AutoModelForCausalLM.from_pretrained(
        model_dir or DEFAULT_MODEL_DIR, dtype=dtype, device_map={"": device})
    if adapter_dir:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, adapter_dir)
    model.eval()
    return model


def native_messages(example):
    """Canonical training messages -> the dicts ``apply_chat_template`` wants."""
    import sys
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from benchmarks.v3.training.messages import to_native_messages
    return to_native_messages(example["messages"])


def tool_defs(example):
    """The tool JSON defs for the template ([] when the example has none)."""
    return list(example.get("tools") or [])


def supervised_spans(tokenizer, messages, tools):
    """Character spans of supervised (assistant) segments in the native render.

    Re-renders the conversation prefix per message; the new suffix is that
    message's segment.  Assistant segments that are supervised become spans.  The
    released template has no ``{% generation %}`` marker, so the assistant mask is
    derived here rather than from ``return_assistant_tokens_mask`` (which would be
    all zeros).
    """
    meta = [m for m in messages]
    spans = []
    prev = ""
    full = ""
    for i, msg in enumerate(meta):
        cur = tokenizer.apply_chat_template(
            messages[:i + 1], tools=tools or None, tokenize=False,
            add_generation_prompt=False)
        if not cur.startswith(prev):
            raise RuntimeError("template render is not prefix-stable at message %d"
                               % i)
        if msg.get("role") == "assistant" and _supervised(msg):
            spans.append((len(prev), len(cur)))
        prev = cur
        full = cur
    return full, spans


def _supervised(msg):
    if "supervised" in msg:
        return bool(msg["supervised"])
    return msg.get("role") == "assistant"


def build_supervised(tokenizer, messages, tools, max_len=4096):
    """Tokenize an example into ``{input_ids, labels, supervised_tokens}``."""
    full, spans = supervised_spans(tokenizer, messages, tools)
    enc = tokenizer(full, return_offsets_mapping=True, add_special_tokens=False)
    ids = list(enc["input_ids"])[:max_len]
    offsets = list(enc["offset_mapping"])[:max_len]
    labels = [-100] * len(ids)
    for start, end in spans:
        for j, (a, b) in enumerate(offsets):
            if b > a and a >= start and b <= end:
                labels[j] = ids[j]
            elif b > a and a >= start and a < end:
                labels[j] = ids[j]
    sup = sum(1 for x in labels if x != -100)
    return {"input_ids": ids, "labels": labels, "supervised_tokens": sup,
            "n_tokens": len(ids)}


def trainable_parameter_report(model):
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    return {"trainable": trainable, "total": total,
            "trainable_fraction": round(trainable / max(1, total), 6)}


def adapter_fingerprint(adapter_dir):
    """Hash + file list of a saved adapter (weights-changed evidence)."""
    files = []
    for root, _dirs, names in os.walk(adapter_dir):
        for name in sorted(names):
            fp = os.path.join(root, name)
            files.append({"path": os.path.relpath(fp, adapter_dir),
                          "sha256": sha256_file(fp),
                          "bytes": os.path.getsize(fp)})
    return files


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    return path


def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
