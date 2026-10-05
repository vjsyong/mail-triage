#!/usr/bin/env python3
"""Real teacher generations for the SFT slice (acceptance AC6).

Uses the pinned ``openbmb/MiniCPM5-2B`` as the generation teacher (recorded in
provenance) over three seed batches.  Each candidate is verified deterministically
(subject/body present, required facts included, length bounds, no refusal text);
accepted and rejected counts are reported honestly and a few compact accepted
samples are committed.  No real mailbox or corpus is read.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lora_common as LC  # noqa: E402

SYSTEM = ("You write short, realistic business and personal emails for a mail "
          "triage dataset. Output exactly a subject line beginning with "
          "'Subject:' followed by a blank line and the email body. Invent no "
          "other text.")

BRIEFS = [
    {"id": "invoice_due", "purpose": "an overdue invoice reminder from a "
     "supplier's billing team", "required": ["INV-4471", "120"]},
    {"id": "receipt", "purpose": "a purchase receipt confirmation", "required":
     ["PO-88213", "54.20"]},
    {"id": "newsletter", "purpose": "a monthly shop newsletter", "required":
     ["AUTUMN25"]},
    {"id": "login_alert", "purpose": "an account security sign-in alert",
     "required": ["Lisbon", "03:14"]},
    {"id": "meeting_request", "purpose": "a request for a 30-minute project "
     "review meeting", "required": ["30", "Q4"]},
    {"id": "personal_invitation", "purpose": "an invitation to dinner", "required":
     ["Saturday"]},
]

FORBIDDEN = ("as an ai", "i cannot", "i'm sorry", "language model",
             "</think>", "<think>", "blank line", "email body",
             "invent no other text", "subject line beginning")


def parse_email(text):
    text = (text or "").strip()
    # drop any leaked reasoning / prompt echo before the real answer
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    m = re.search(r"Subject:\s*(.+)", text)
    subject = m.group(1).strip() if m else ""
    body = text[m.end():].strip() if m else text
    body = re.sub(r"^[:\-\s]+", "", body, count=1)
    return subject, body


def verify_email(subject, body, brief):
    problems = []
    if not subject:
        problems.append("no_subject")
    if len(subject) < 3 or len(subject) > 140:
        problems.append("subject_length")
    if any(p in subject.lower() for p in ("blank line", "subject line",
                                          "email body", "followed by")):
        problems.append("subject_echo")
    if len(body) < 40:
        problems.append("body_too_short")
    if len(body) > 2000:
        problems.append("body_too_long")
    low = (subject + "\n" + body).lower()
    for tok in brief["required"]:
        if tok.lower() not in low:
            problems.append("missing_fact:%s" % tok)
    for phrase in FORBIDDEN:
        if phrase in low:
            problems.append("refusal_or_leak:%s" % phrase)
            break
    return problems


def generate(model, tokenizer, prompt, max_new_tokens, temperature, seed):
    import torch
    torch.manual_seed(seed)
    ids = tokenizer(prompt, return_tensors="pt",
                    add_special_tokens=False).to(model.device)
    kwargs = dict(max_new_tokens=max_new_tokens, pad_token_id=tokenizer.pad_token_id)
    if temperature and temperature > 0:
        kwargs.update(do_sample=True, temperature=temperature, top_p=0.9)
    else:
        kwargs.update(do_sample=False)
    with torch.no_grad():
        out = model.generate(**ids, **kwargs)
    return tokenizer.decode(out[0][ids["input_ids"].shape[1]:],
                            skip_special_tokens=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=LC.DEFAULT_MODEL_DIR)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample-dir", default=None)
    ap.add_argument("--seeds", default="11,22,33")
    ap.add_argument("--max-new-tokens", type=int, default=220)
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--device", type=int, default=0)
    args = ap.parse_args(argv)

    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    tokenizer = LC.load_tokenizer(args.model_dir)
    model = LC.load_model(args.model_dir, device=args.device)

    batches = []
    committed = []
    for si, seed in enumerate(seeds):
        batch = {"seed": seed, "generations": []}
        for brief in BRIEFS:
            user = ("Write %s. Include exactly these facts verbatim: %s."
                    % (brief["purpose"], ", ".join(brief["required"])))
            prompt = tokenizer.apply_chat_template(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": user}],
                tokenize=False, add_generation_prompt=True)
            text = generate(model, tokenizer, prompt, args.max_new_tokens,
                            args.temperature, seed * 100 + BRIEFS.index(brief))
            subject, body = parse_email(text)
            problems = verify_email(subject, body, brief)
            record = {"brief": brief["id"], "seed": seed,
                      "accepted": not problems, "problems": problems,
                      "subject": subject, "body": body[:600]}
            batch["generations"].append(record)
            if not problems and len(committed) < 4:
                committed.append(record)
        accepted = sum(1 for g in batch["generations"] if g["accepted"])
        batch["accepted"] = accepted
        batch["rejected"] = len(batch["generations"]) - accepted
        batches.append(batch)

    if args.sample_dir:
        os.makedirs(args.sample_dir, exist_ok=True)
        with open(os.path.join(args.sample_dir, "teacher_generations.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"model": LC.MODEL_ID, "model_revision": LC.MODEL_REVISION,
                       "system_prompt": SYSTEM, "temperature": args.temperature,
                       "batches": batches}, f, ensure_ascii=False, indent=1,
                      sort_keys=True)
            f.write("\n")

    receipt = {
        "kind": "mail-sft-teacher-generations",
        "teacher": {"model": LC.MODEL_ID, "model_revision": LC.MODEL_REVISION,
                    "role": "generation_teacher",
                    "note": "pinned official HF MiniCPM5-2B used as teacher"},
        "system_prompt": SYSTEM,
        "temperature": args.temperature,
        "max_new_tokens": args.max_new_tokens,
        "batches": [{"seed": b["seed"], "accepted": b["accepted"],
                     "rejected": b["rejected"]} for b in batches],
        "totals": {"accepted": sum(b["accepted"] for b in batches),
                   "rejected": sum(b["rejected"] for b in batches),
                   "generated": sum(len(b["generations"]) for b in batches)},
        "committed_samples": len(committed),
        "review_status": "draft",
        "human_seal": False,
        "test_qualified": False,
        "note": ("Real model output, deterministically verified; draft only, no "
                 "human review or test qualification."),
    }
    LC.write_json(os.path.join(args.out, "teacher_receipt.json"), receipt)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
