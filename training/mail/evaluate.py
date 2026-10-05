#!/usr/bin/env python3
"""BASE vs ADAPTER evaluation on independent development records (M4).

Classification scores category **and** needs_reply; an invalid/unparseable output
stays in the denominator.  Bounded workflow behaviour is a real model rollout
executed against ``NativeMailbox`` and judged on required arguments, scope and
the grounded final outcome (a move event or a queued proposal) -- a stray
``search_messages`` with no later correct action cannot count as success.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lora_common as LC  # noqa: E402
import rollout_dialogue as RD  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from benchmarks.v3.training import minicpm  # noqa: E402
from benchmarks.v3.training.taxonomies import (load_taxonomy,  # noqa: E402
                                               public_projection,
                                               render_classifier_prompt)
from benchmarks.v3.training.tools_native import native_tool_schemas  # noqa: E402


def _classify(model, tokenizer, source, system):
    prompt = tokenizer.apply_chat_template(
        [{"role": "system", "content": system},
         {"role": "user", "content": _user_body(source["email"])}],
        tokenize=False, add_generation_prompt=True, enable_thinking=False)
    text, _finish, _usage = RD._generate(model, tokenizer, prompt, None, 160)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _user_body(email):
    body = (email.get("body") or "")[:1500]
    return ("From: %s\nTo: %s\nSubject: %s\nDate: %s\n\n%s"
            % (email.get("from_addr", ""), email.get("to_addr", ""),
               email.get("subject", ""), email.get("date", ""), body))


def evaluate(model, tokenizer, sources, taxonomy, max_new_tokens):
    public = public_projection(taxonomy)
    classify_system = render_classifier_prompt(public)
    tools = native_tool_schemas()
    decision = []
    workflow = []
    for src in sources:
        obj = _classify(model, tokenizer, src, classify_system)
        intent = src["intent"]
        cat_ok = bool(obj) and obj.get("category") == intent["category"]
        reply_ok = bool(obj) and obj.get("needs_reply") == intent["needs_reply"]
        decision.append({"source_id": src["source_id"],
                         "category_correct": cat_ok, "reply_correct": reply_ok,
                         "valid_json": obj is not None,
                         "got": obj})
        if src.get("action") in ("move", "propose"):
            dlg = RD.run_dialogue(model, tokenizer, src, tools, taxonomy, public,
                                  max_new_tokens)
            final = dlg["final_state"]
            target = src.get("folder")
            if src["action"] == "move":
                state_ok = (final["folders"].get(target) == [1]
                            and final["folders"].get("INBOX", []) == [])
            else:
                state_ok = (final["proposal_count"] >= 1
                            and final["rule_count"] == 0
                            and final["folders"].get("INBOX") == [1])
            workflow.append({"source_id": src["source_id"],
                             "grounded": dlg["grounded"],
                             "state_matches_gold": state_ok,
                             "clarification": dlg["clarification"],
                             "turns": len(dlg["turns"])})

    def rate(rows, key):
        return (round(sum(1 for r in rows if r[key]) / len(rows), 3)
                if rows else None)

    return {
        "decision_n": len(decision),
        "category_accuracy": rate(decision, "category_correct"),
        "needs_reply_accuracy": rate(decision, "reply_correct"),
        "valid_json_rate": rate(decision, "valid_json"),
        "workflow_n": len(workflow),
        "workflow_grounded_rate": rate(workflow, "grounded"),
        "workflow_state_match_rate": rate(workflow, "state_matches_gold"),
        "clarification_dialogues": sum(1 for w in workflow if w["clarification"]),
        "decision": decision, "workflow": workflow,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=LC.DEFAULT_MODEL_DIR)
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--dev-sources", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-new-tokens", type=int, default=200)
    ap.add_argument("--device", type=int, default=0)
    args = ap.parse_args(argv)

    with open(args.dev_sources, encoding="utf-8") as f:
        sources = json.load(f)
    tokenizer = LC.load_tokenizer(args.model_dir)
    tax = load_taxonomy()

    model = LC.load_model(args.model_dir, device=args.device)
    base = evaluate(model, tokenizer, sources, tax, args.max_new_tokens)
    del model
    import torch
    torch.cuda.empty_cache()

    result = {"kind": "mail-sft-base-vs-adapter",
              "model": LC.MODEL_ID, "model_revision": LC.MODEL_REVISION,
              "adapter": None, "adapter_files": [],
              "dev_records": len(sources), "base": base}
    if args.adapter:
        amodel = LC.load_model(args.model_dir, adapter_dir=args.adapter,
                               device=args.device)
        result["adapter"] = args.adapter
        result["adapter_files"] = LC.adapter_fingerprint(args.adapter)
        result["candidate"] = evaluate(amodel, tokenizer, sources, tax,
                                       args.max_new_tokens)
    LC.write_json(os.path.join(args.out, "eval_receipt.json"), result)
    summary = {k: {kk: v[kk] for kk in v if kk not in ("decision", "workflow")}
               for k, v in (("base", base),) + (
                   (("candidate", result["candidate"]),) if "candidate" in result else ())}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
