#!/usr/bin/env python3
"""BASE vs ADAPTER evaluation on independently generated development cases (AC7).

Identical generation config for both.  Classification is scored by the category
in the model's JSON; bounded tool use is scored by whether the model emits a
matching tool call.  This is a small smoke comparison, not a statistical claim.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lora_common as LC  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _minicpm():
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from benchmarks.v3.training import minicpm
    return minicpm


def render_prompt(tokenizer, messages, tools=None, thinking=False):
    return tokenizer.apply_chat_template(
        messages, tools=tools or None, tokenize=False,
        add_generation_prompt=True, enable_thinking=thinking)


def generate(model, tokenizer, prompt, max_new_tokens=256):
    import torch
    ids = tokenizer(prompt, return_tensors="pt",
                    add_special_tokens=False).to(model.device)
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=max_new_tokens,
                             do_sample=False, temperature=0.0,
                             pad_token_id=tokenizer.pad_token_id)
    text = tokenizer.decode(out[0][ids["input_ids"].shape[1]:],
                            skip_special_tokens=False)
    # strip only the turn/framing markers; keep <function>/<param> tool tokens.
    for marker in ("<|im_start|>", "<|im_end|>", "<|im_sep|>", "<s>", "</s>"):
        text = text.replace(marker, "")
    return text


def parse_category(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj.get("category")


def first_tool_name(text):
    calls = _minicpm().parse_tool_calls(text)
    return calls[0]["name"] if calls else None


def _first_tool_index(example):
    for i, msg in enumerate(example["messages"]):
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            return i
    return None


def run_eval(model, tokenizer, examples, max_new_tokens=256):
    results = []
    for ex in examples:
        task = ex["task"]
        if task == "decision":
            prompt = render_prompt(tokenizer, LC.native_messages(ex)[:-1])
            text = generate(model, tokenizer, prompt, max_new_tokens)
            got = parse_category(text)
            want = ex["decision"]["category"]
            results.append({"example_id": ex["example_id"], "task": task,
                            "want": want, "got": got, "match": got == want,
                            "raw": text[:200]})
        else:
            idx = _first_tool_index(ex)
            if idx is None:
                continue
            want_calls = ex["messages"][idx]["tool_calls"]
            want = want_calls[0]["name"]
            prompt = render_prompt(tokenizer, LC.native_messages(ex)[:idx],
                                   tools=LC.tool_defs(ex))
            text = generate(model, tokenizer, prompt, max_new_tokens)
            got = first_tool_name(text)
            results.append({"example_id": ex["example_id"], "task": task,
                            "want": want, "got": got, "match": got == want,
                            "raw": text[:400]})
    return results


def summarize(results):
    decision = [r for r in results if r["task"] == "decision"]
    tool = [r for r in results if r["task"] == "workflow"]
    def acc(rows):
        return (round(sum(1 for r in rows if r["match"]) / len(rows), 3)
                if rows else None)
    return {"n": len(results),
            "decision_n": len(decision), "decision_accuracy": acc(decision),
            "tool_n": len(tool), "tool_accuracy": acc(tool),
            "results": results}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=LC.DEFAULT_MODEL_DIR)
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--examples", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-new-tokens", type=int, default=256)
    ap.add_argument("--device", type=int, default=0)
    args = ap.parse_args(argv)

    examples = LC.read_jsonl(args.examples)
    tokenizer = LC.load_tokenizer(args.model_dir)

    model = LC.load_model(args.model_dir, device=args.device)
    base = summarize(run_eval(model, tokenizer, examples, args.max_new_tokens))
    del model
    import torch
    torch.cuda.empty_cache()

    adapter = {"adapter": args.adapter,
               "adapter_files": LC.adapter_fingerprint(args.adapter)
               if args.adapter else []}
    if args.adapter:
        amodel = LC.load_model(args.model_dir, adapter_dir=args.adapter,
                               device=args.device)
        adapter.update(summarize(run_eval(amodel, tokenizer, examples,
                                          args.max_new_tokens)))
    receipt = {
        "kind": "mail-sft-base-vs-adapter",
        "model": LC.MODEL_ID,
        "model_revision": LC.MODEL_REVISION,
        "generation_config": {"do_sample": False, "temperature": 0.0,
                              "max_new_tokens": args.max_new_tokens},
        "examples_file": args.examples,
        "base": base,
        "adapter": adapter,
        "note": ("Smoke comparison on a few authored cases; not a statistical "
                 "or qualification claim."),
    }
    LC.write_json(os.path.join(args.out, "eval_receipt.json"), receipt)
    print(json.dumps({"base": {k: base[k] for k in base if k != "results"},
                      "adapter": {k: adapter[k] for k in adapter if k != "results"}},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
