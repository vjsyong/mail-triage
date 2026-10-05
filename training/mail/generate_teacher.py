#!/usr/bin/env python3
"""Real teacher generations -> verified full source records (M2).

Reads authored briefs, generates each email with the pinned MiniCPM5-2B, verifies
it deterministically against the brief's required facts, and writes full source
records (explicit role/domain + authored semantic intent) that feed **both** the
classifier export and the bounded tool dialogue.  Failed generations are counted
and excluded, never substituted.
"""
from __future__ import annotations

import argparse
import hashlib
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
from benchmarks.v3.training import reply_gold  # noqa: E402

SYSTEM = ("You write short, realistic business and personal emails for a mail "
          "triage dataset. Output exactly a subject line beginning with "
          "'Subject:' followed by a blank line and the email body. Invent no "
          "other text.")

FORBIDDEN = ("as an ai", "i cannot", "i'm sorry", "language model", "</think>",
             "<think>", "blank line", "email body", "invent no other text",
             "subject line beginning")


def parse_email(text):
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    m = re.search(r"Subject:\s*(.+)", text)
    subject = m.group(1).strip() if m else ""
    body = text[m.end():].strip() if m else text
    body = re.sub(r"^[:\-\s]+", "", body, count=1)
    return subject, body


def verify_email(subject, body, brief):
    problems = []
    if not subject or len(subject) < 3 or len(subject) > 140:
        problems.append("subject_length")
    if any(p in subject.lower() for p in ("blank line", "subject line",
                                          "email body", "followed by")):
        problems.append("subject_echo")
    if len(body) < 40:
        problems.append("body_too_short")
    if len(body) > 2000:
        problems.append("body_too_long")
    low = (subject + "\n" + body).lower()
    for tok in brief["facts"]:
        if tok.lower() not in low:
            problems.append("missing_fact:%s" % tok)
    for phrase in FORBIDDEN:
        if phrase in low:
            problems.append("refusal_or_leak:%s" % phrase)
            break
    problems.extend(reply_gold.reply_problems(subject + "\n" + body,
                                              brief["needs_reply"]))
    return problems


def strip_framing(text):
    for marker in ("<|im_start|>", "<|im_end|>", "<|im_sep|>", "<s>", "</s>"):
        text = text.replace(marker, "")
    return text


def generate(model, tokenizer, prompt, max_new_tokens, temperature, seed):
    import torch
    torch.manual_seed(seed)
    ids = tokenizer(prompt, return_tensors="pt",
                    add_special_tokens=False).to(model.device)
    prompt_len = ids["input_ids"].shape[1]
    kwargs = dict(max_new_tokens=max_new_tokens,
                  pad_token_id=tokenizer.pad_token_id)
    if temperature and temperature > 0:
        kwargs.update(do_sample=True, temperature=temperature, top_p=0.9)
    else:
        kwargs.update(do_sample=False)
    with torch.no_grad():
        out = model.generate(**ids, **kwargs)
    new = out[0][prompt_len:]
    text = strip_framing(tokenizer.decode(new, skip_special_tokens=False))
    finish = "length" if len(new) >= max_new_tokens else "stop"
    usage = {"prompt_tokens": int(prompt_len),
             "completion_tokens": int(len(new)),
             "total_tokens": int(prompt_len + len(new))}
    return text, finish, usage


def source_id_for(email):
    blob = LC and json.dumps({k: email.get(k, "") for k in
                              ("from_addr", "to_addr", "subject", "body")},
                             sort_keys=True, ensure_ascii=False)
    return "src_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=LC.DEFAULT_MODEL_DIR)
    ap.add_argument("--briefs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--role", default="training")
    ap.add_argument("--domain", default="training")
    ap.add_argument("--max-new-tokens", type=int, default=180)
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--attempts", type=int, default=3)
    args = ap.parse_args(argv)

    with open(args.briefs, encoding="utf-8") as f:
        briefs = json.load(f)
    tokenizer = LC.load_tokenizer(args.model_dir)
    model = LC.load_model(args.model_dir, device=args.device)

    records, accepted, rejected = [], 0, 0
    started = time.time()
    for brief in briefs:
        user = ("Write %s from %s at %s. Include exactly these facts verbatim: %s."
                % (brief["purpose"], brief["from_name"], brief["org"],
                   ", ".join(brief["facts"])))
        prompt = tokenizer.apply_chat_template(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": user}],
            tokenize=False, add_generation_prompt=True, enable_thinking=False)
        chosen = None
        for attempt in range(args.attempts):
            text, finish, usage = generate(
                model, tokenizer, prompt, args.max_new_tokens, args.temperature,
                brief["seed"] * 1000 + attempt)
            subject, body = parse_email(text)
            problems = verify_email(subject, body, brief)
            if not problems:
                chosen = {"subject": subject, "body": body, "raw": text,
                          "finish_reason": finish, "usage": usage,
                          "attempt": attempt}
                break
        if chosen is None:
            rejected += 1
            continue
        email = {"from_addr": brief["from_addr"], "to_addr": brief["to_addr"],
                 "subject": chosen["subject"], "date": brief["date"],
                 "body": chosen["body"]}
        sid = source_id_for(email)
        records.append({
            "source_id": sid, "lineage_id": "lin_" + brief["pair_id"],
            "pair_id": brief["pair_id"], "variant": brief["variant"],
            "role": args.role, "domain": args.domain, "family": brief["family"],
            "owner": "Priya Raman",
            "intent": {"category": brief["category"],
                       "needs_reply": brief["needs_reply"],
                       "observable": "visible"},
            "reply_phrase": brief.get("reply_phrase"),
            "no_reply_phrase": brief.get("no_reply_phrase"),
            "action": brief["action"], "folder": brief["folder"],
            "brief_id": brief["brief_id"],
            "email": email,
            "provenance": {
                "kind": "teacher_generation",
                "teacher": {"model": LC.MODEL_ID, "revision": LC.MODEL_REVISION},
                "seed": brief["seed"], "temperature": args.temperature,
                "attempt": chosen["attempt"],
                "finish_reason": chosen["finish_reason"],
                "usage": chosen["usage"],
                "raw_completion": chosen["raw"][:600],
                "verified": True,
                "visible_reply_obligation": reply_gold.visible_reply_obligation(
                    chosen["subject"] + "\n" + chosen["body"]),
            },
        })
        accepted += 1
    LC.write_json(args.out, records)
    pairs = {}
    for r in records:
        pairs.setdefault(r["pair_id"], set()).add(r["intent"]["needs_reply"])
    receipt = {
        "kind": "mail-sft-teacher-sources",
        "teacher": {"model": LC.MODEL_ID, "revision": LC.MODEL_REVISION},
        "role": args.role, "domain": args.domain,
        "briefs": len(briefs), "accepted": accepted, "rejected": rejected,
        "reply_true_accepted": sum(1 for r in records
                                   if r["intent"]["needs_reply"]),
        "reply_false_accepted": sum(1 for r in records
                                    if not r["intent"]["needs_reply"]),
        "counterfactual_pairs_with_both": sum(
            1 for v in pairs.values() if len(v) == 2),
        "attempts_per_brief": args.attempts,
        "seconds": round(time.time() - started, 2),
        "note": "Real model output, deterministically verified; draft only.",
    }
    base = os.path.basename(args.out).replace(".json", "")
    LC.write_json(os.path.join(os.path.dirname(args.out),
                               base + "_receipt.json"), receipt)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
