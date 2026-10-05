#!/usr/bin/env python3
"""Authored briefs for real teacher generation (M2).

Deterministic, coherent synthetic businesses/people (no real domain contacted).
Each brief carries an explicit semantic intent (taxonomy category + reply fact)
and required facts the generated email must contain, so the teacher output is
verified against an authored target rather than free prose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lora_common as LC  # noqa: E402

WORLD = [
    {"org": "Helios Energy", "domain": "helios-energy.com", "person": "Dana Okafor"},
    {"org": "Acme Supplies", "domain": "acme-supplies.com", "person": "Marco Silva"},
    {"org": "Atlas Tools", "domain": "atlas-tools.com", "person": "Lena Fischer"},
    {"org": "Beacon Publishing", "domain": "beaconpublishing.com", "person": "Ravi Menon"},
]

FAMILIES = [
    {"family": "invoice_due", "category": "Billing", "needs_reply": True,
     "action": "move", "folder": "Billing",
     "purpose": "a short overdue-invoice reminder from a supplier's billing team",
     "facts": ["Invoice {n}", "{amount}", "overdue"]},
    {"family": "receipt", "category": "Receipt", "needs_reply": False,
     "action": "none", "folder": "Receipts",
     "purpose": "a purchase receipt confirmation from an online seller",
     "facts": ["Order {n}", "total {amount}", "receipt"]},
    {"family": "newsletter", "category": "Promo", "needs_reply": False,
     "action": "propose", "folder": "Promo",
     "purpose": "a short marketing newsletter from a shop",
     "facts": ["sale", "unsubscribe"]},
    {"family": "login_alert", "category": "Security", "needs_reply": False,
     "action": "none", "folder": "Security",
     "purpose": "an account security sign-in alert",
     "facts": ["sign-in", "new device"]},
    {"family": "friend_note", "category": "Personal", "needs_reply": True,
     "action": "none", "folder": "Personal",
     "purpose": "a short personal note from a friend about dinner",
     "facts": ["dinner", "Saturday"]},
    {"family": "task_request", "category": "Action", "needs_reply": True,
     "action": "none", "folder": "Action",
     "purpose": "a colleague asking the owner to review a document",
     "facts": ["review", "document"]},
]

OWNER = "priya@acme.example"


def _money(n):
    return "$%d.%02d" % (n // 100, n % 100)


def build_briefs(seed, per_family=2):
    out = []
    for i, fam in enumerate(FAMILIES):
        for k in range(per_family):
            w = WORLD[(seed + i + k) % len(WORLD)]
            n = 1000 + (seed * 17 + i * 31 + k) % 8999
            facts = [f.format(n=n, amount=_money(1200 + n % 900 * 100))
                     for f in fam["facts"]]
            out.append({
                "brief_id": "b_%s_%d_%d" % (fam["family"], seed, k),
                "seed": seed,
                "family": fam["family"],
                "category": fam["category"],
                "needs_reply": fam["needs_reply"],
                "action": fam["action"],
                "folder": fam["folder"],
                "purpose": fam["purpose"],
                "facts": facts,
                "from_addr": "%s@%s" % (fam["family"].replace("_", "."), w["domain"]),
                "from_name": w["person"],
                "org": w["org"],
                "to_addr": OWNER,
                "date": "Mon, 1 Sep 2025 09:0%d:00 +0000" % (k % 10),
            })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seeds", default="11,22,33")
    ap.add_argument("--per-family", type=int, default=2)
    args = ap.parse_args(argv)
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    briefs = []
    for s in seeds:
        briefs.extend(build_briefs(s, args.per_family))
    LC.write_json(args.out, briefs)
    print(json.dumps({"briefs": len(briefs), "seeds": seeds, "out": args.out}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
