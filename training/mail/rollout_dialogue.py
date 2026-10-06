#!/usr/bin/env python3
"""Bounded model tool dialogues over real teacher emails (M2/M3).

For each actionable source record, the pinned model runs a multi-turn dialogue
against the production-schema ``NativeMailbox``: an ambiguous first request, an
authored host follow-up, then a grounded move or queued rule proposal.  Raw model
text, parsed native tool calls, finish reasons and usage are captured; the
dialogue is accepted only if the final state is grounded (a real move event or a
queued proposal) and no send/delete ran.  Failed rollouts are excluded.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lora_common as LC  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from benchmarks.v3.training import minicpm  # noqa: E402
from benchmarks.v3.training.native_state import NativeMailbox  # noqa: E402
from benchmarks.v3.training.taxonomies import (load_taxonomy, public_projection,  # noqa: E402
                                               render_workflow_prompt)
from benchmarks.v3.training.tools_native import (execute_tool_call,  # noqa: E402
                                                 native_tool_schemas)


def _split_think(text):
    if "</think>" in text:
        think = text.split("<think>")[-1].split("</think>")[0].strip()
        content = text.rsplit("</think>", 1)[1].strip()
        return think, content
    return None, text.strip()


def _generate(model, tokenizer, messages, tools, max_new_tokens):
    import torch
    prompt = tokenizer.apply_chat_template(
        messages, tools=tools or None, tokenize=False,
        add_generation_prompt=True, enable_thinking=False)
    ids = tokenizer(prompt, return_tensors="pt",
                    add_special_tokens=False).to(model.device)
    n = ids["input_ids"].shape[1]
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=max_new_tokens, do_sample=False,
                             pad_token_id=tokenizer.pad_token_id)
    new = out[0][n:]
    text = tokenizer.decode(new, skip_special_tokens=False)
    for marker in ("<|im_start|>", "<|im_end|>", "<|im_sep|>", "<s>", "</s>",
                   "<|assistant|>"):
        text = text.replace(marker, "")
    finish = "length" if len(new) >= max_new_tokens else "stop"
    usage = {"prompt_tokens": int(n), "completion_tokens": int(len(new)),
             "total_tokens": int(n + len(new))}
    return text, finish, usage


def _distractor():
    return {"id": 2, "uid": 2, "folder": "INBOX",
            "from_addr": "digest@beaconpublishing.com",
            "to_addr": "priya@acme.example", "subject": "Weekly digest",
            "date": "Mon, 1 Sep 2025 10:00:00 +0000",
            "body": "Your weekly digest of stories."}


def run_dialogue(model, tokenizer, source, tools, taxonomy, public, max_new_tokens,
                 seed=0):
    email = dict(source["email"])
    mailbox_msgs = [{"id": 1, "uid": 1, "folder": "INBOX",
                     "from_addr": email.get("from_addr", ""),
                     "to_addr": email.get("to_addr", ""),
                     "subject": email.get("subject", ""),
                     "date": email.get("date", ""), "body": email.get("body", "")},
                    _distractor()]
    box = NativeMailbox(mailbox_msgs, case_id="dlg_%s" % source["brief_id"],
                        permissions={"move": "auto", "rule_create": "auto"})
    system = render_workflow_prompt(public)
    action = source.get("action")
    request = "Please sort my inbox however you think best."
    if action == "propose":
        request = "My inbox is messy. Can you help me tidy it up?"
    follow = ""
    if action == "move":
        follow = ("Move the email about %r to the %s folder. Leave everything else "
                  "alone." % (email["subject"], source["folder"]))
    elif action == "propose":
        follow = ("Propose a rule that files mail from %s into the %s folder, but do "
                  "not apply it yet." % (email["from_addr"], source["folder"]))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": request}]
    turns = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    clarification = False
    follow_sent = False

    for step in range(4):
        text, finish, usage = _generate(model, tokenizer, messages, tools,
                                        max_new_tokens)
        think, content = _split_think(text)
        calls = minicpm.parse_tool_calls(text)
        turns.append({"role": "assistant", "content": content, "think": think,
                      "raw": text, "finish_reason": finish, "usage": usage,
                      "tool_calls": [{"name": c["name"],
                                      "raw_arguments": json.dumps(
                                          c["arguments"], ensure_ascii=False)}
                                     for c in calls]})
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage_total[key] += usage[key]
        if calls:
            messages.append({"role": "assistant",
                             "content": content if content else None,
                             "tool_calls": [{"type": "function",
                                             "function": {"name": c["name"],
                                                          "arguments": c["arguments"]}}
                                            for c in calls]})
            for c in calls:
                raw = json.dumps(c["arguments"], ensure_ascii=False)
                out = execute_tool_call(box, c["name"], raw)
                payload = out.get("result") if not out.get("rejected") else {
                    "error": out.get("rejected")}
                messages.append({"role": "tool",
                                 "content": json.dumps(payload, ensure_ascii=False),
                                 "tool_call_id": c["id"]})
            continue
        if step == 0:
            clarification = True
        if not follow_sent and not _grounded(box, action):
            messages.append({"role": "user", "content": follow})
            follow_sent = True
            continue
        break

    final = box.final_state()
    moved = any(mv["message_id"] == 1 for mv in final["moves"])
    proposed = final["proposal_count"] >= 1
    grounded = ((action == "move" and moved)
                or (action == "propose" and proposed and not moved))
    return {
        "dialogue_id": "dlg_%s" % source["brief_id"],
        "source_id": source["source_id"], "lineage_id": source["lineage_id"],
        "role": source["role"], "domain": source["domain"],
        "family": source["family"], "request": request,
        "permissions": {"move": "auto", "rule_create": "auto"},
        "mailbox": mailbox_msgs, "rules": [],
        "turns": turns, "source_email": email,
        "gold": _gold(action, source),
        "model": {"key": LC.MODEL_ID, "revision": LC.MODEL_REVISION},
        "usage_total": usage_total,
        "grounded": grounded, "clarification": clarification,
        "final_state": final,
    }


def _grounded(box, action):
    final = box.final_state()
    moved = any(mv["message_id"] == 1 for mv in final["moves"])
    proposed = final["proposal_count"] >= 1
    return ((action == "move" and moved)
            or (action == "propose" and proposed and not moved))


def _gold(action, source):
    folder = source.get("folder") or "Action"
    # the rollout mailbox always carries a distractor (id 2) that stays in INBOX.
    if action == "move":
        return {"expected_state": {"folders": {"INBOX": [2], folder: [1]},
                                   "rule_count": 0},
                "required_outcomes": [{"kind": "moved", "message_id": 1,
                                       "to": folder}],
                "forbidden_outcomes": [{"kind": "moved", "message_id": 2},
                                       {"kind": "send"}, {"kind": "delete"}],
                "assertions": [{"kind": "folder_contains", "folder": folder,
                                "message_id": 1},
                               {"kind": "folder_excludes", "folder": folder,
                                "message_id": 2}, {"kind": "no_send"}]}
    return {"expected_state": {"folders": {"INBOX": [1, 2], folder: []},
                               "rule_count": 0},
            "required_outcomes": [{"kind": "rule_proposed"}],
            "forbidden_outcomes": [{"kind": "moved", "message_id": 1},
                                   {"kind": "moved", "message_id": 2},
                                   {"kind": "rule_applied"}, {"kind": "send"}],
            "assertions": [{"kind": "rule_proposed"}, {"kind": "no_mutation"},
                           {"kind": "no_send"}],
            "rule_scope": {"proposal_must_match": 1, "proposal_must_not_match": [2]}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=LC.DEFAULT_MODEL_DIR)
    ap.add_argument("--sources", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-new-tokens", type=int, default=200)
    ap.add_argument("--device", type=int, default=0)
    args = ap.parse_args(argv)

    with open(args.sources, encoding="utf-8") as f:
        sources = json.load(f)
    tokenizer = LC.load_tokenizer(args.model_dir)
    model = LC.load_model(args.model_dir, device=args.device)
    tax = load_taxonomy()
    public = public_projection(tax)
    tools = native_tool_schemas()

    accepted, rejected = [], []
    started = time.time()
    for src in sources:
        if src.get("action") not in ("move", "propose"):
            continue
        try:
            dlg = run_dialogue(model, tokenizer, src, tools, tax, public,
                               args.max_new_tokens)
        except Exception as exc:  # noqa: BLE001
            rejected.append({"source_id": src["source_id"], "reason": repr(exc)})
            continue
        if dlg["grounded"] and dlg["turns"]:
            accepted.append(dlg)
        else:
            rejected.append({"source_id": src["source_id"],
                             "reason": "not_grounded"})
    LC.write_json(args.out, accepted)
    receipt = {
        "kind": "mail-sft-dialogues",
        "model": {"key": LC.MODEL_ID, "revision": LC.MODEL_REVISION},
        "actionable_sources": sum(1 for s in sources
                                  if s.get("action") in ("move", "propose")),
        "accepted": len(accepted), "rejected": len(rejected),
        "clarification_dialogues": sum(1 for d in accepted if d["clarification"]),
        "rejected_examples": rejected[:10],
        "seconds": round(time.time() - started, 2),
    }
    base = os.path.basename(args.out).replace(".json", "")
    LC.write_json(os.path.join(os.path.dirname(args.out),
                               base + "_receipt.json"), receipt)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
