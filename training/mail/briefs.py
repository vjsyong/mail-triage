#!/usr/bin/env python3
"""Authored briefs with **visible reply obligations** for real teacher generation.

Each family has counterfactual variants that differ only in whether the email
asks the owner to reply.  The reply/no-reply obligation is part of the required
facts, so the generated email must express it *in the classifier-visible text*;
the exporter then verifies that the authored ``needs_reply`` is justified by the
visible email (S1), never assumed from the family.

Pairs share ``pair_id`` (same event/lineage) so the reply/no-reply counterfactual
stays inside one generation domain.
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

# Authored canonical phrases that make a reply obligation visible.
REPLY_PHRASE = "Please reply to confirm"
NO_REPLY_PHRASE = "No reply is needed"

# family -> ordered variants (index k).  Same family intentionally holds BOTH
# reply and no-reply variants (never a family->bool shortcut).
FAMILIES = [
    {"family": "invoice_due", "category": "Billing", "folder": "Billing",
     "action": "move", "variants": [
         {"needs_reply": True, "reply": REPLY_PHRASE,
          "purpose": ("an overdue-invoice reminder from a supplier's billing team that "
                      "asks the owner to reply and confirm when they will pay"),
          "facts": ["Invoice {n}", "{amount}", "overdue"]},
         {"needs_reply": False, "no_reply": NO_REPLY_PHRASE,
          "purpose": ("an automated overdue-invoice notification telling the owner to pay "
                      "through the online billing portal, which states no reply is needed"),
          "facts": ["Invoice {n}", "{amount}", "overdue", "online billing portal"]},
     ]},
    {"family": "receipt", "category": "Receipt", "folder": "Receipts",
     "action": "none", "variants": [
         {"needs_reply": False, "no_reply": NO_REPLY_PHRASE,
          "purpose": "an automated purchase receipt confirmation",
          "facts": ["Order {n}", "total {amount}", "receipt"]},
         {"needs_reply": False, "no_reply": NO_REPLY_PHRASE,
          "purpose": "an automated order-shipped notice with tracking",
          "facts": ["Order {n}", "tracking", "shipped"]},
     ]},
    {"family": "newsletter", "category": "Promo", "folder": "Promo",
     "action": "propose", "variants": [
         {"needs_reply": False, "no_reply": NO_REPLY_PHRASE,
          "purpose": "a short marketing newsletter from a shop",
          "facts": ["sale", "unsubscribe"]},
         {"needs_reply": False, "no_reply": NO_REPLY_PHRASE,
          "purpose": "a promotional offer email from a shop",
          "facts": ["discount", "unsubscribe"]},
     ]},
    {"family": "login_alert", "category": "Security", "folder": "Security",
     "action": "none", "variants": [
         {"needs_reply": False, "no_reply": NO_REPLY_PHRASE,
          "purpose": "an automated account security sign-in alert",
          "facts": ["sign-in", "new device"]},
         {"needs_reply": False, "no_reply": NO_REPLY_PHRASE,
          "purpose": "an automated password-changed security notice",
          "facts": ["password", "changed"]},
     ]},
    {"family": "friend_note", "category": "Personal", "folder": "Personal",
     "action": "none", "variants": [
         {"needs_reply": True, "reply": REPLY_PHRASE,
          "purpose": ("a personal note from a friend inviting the owner to dinner and "
                      "asking them to reply"),
          "facts": ["dinner", "Saturday"]},
         {"needs_reply": False, "no_reply": NO_REPLY_PHRASE,
          "purpose": ("a personal note from a friend sharing some news, with no reply "
                      "needed"),
          "facts": ["news"]},
     ]},
    {"family": "task_request", "category": "Action", "folder": "Action",
     "action": "none", "variants": [
         {"needs_reply": True, "reply": REPLY_PHRASE,
          "purpose": ("a colleague asking the owner to review a document and reply to "
                      "confirm approval"),
          "facts": ["review", "document", "approval"]},
         {"needs_reply": False, "no_reply": NO_REPLY_PHRASE,
          "purpose": ("an automated notification that a document is ready in the shared "
                      "drive, with no action required"),
          "facts": ["document", "ready"]},
     ]},
]

OWNER = "priya@acme.example"


def _money(n):
    return "$%d.%02d" % (n // 100, n % 100)


def build_briefs(seed, per_family=2):
    out = []
    for fi, fam in enumerate(FAMILIES):
        variant_count = min(per_family, len(fam["variants"]))
        for k in range(variant_count):
            var = fam["variants"][k]
            w = WORLD[(seed + fi + k) % len(WORLD)]
            n = 1000 + (seed * 17 + fi * 31 + k) % 8999
            ref = "REF-%d-%s-%d" % (seed, fam["family"].upper(), k)
            facts = [f.format(n=n, amount=_money(1200 + n % 900 * 100))
                     for f in var["facts"]] + [ref]
            reply_phrase = var.get("reply")
            no_reply_phrase = var.get("no_reply")
            if reply_phrase:
                facts = facts + [reply_phrase]
            elif no_reply_phrase:
                facts = facts + [no_reply_phrase]
            out.append({
                "brief_id": "b_%s_%d_%d" % (fam["family"], seed, k),
                "pair_id": "ev_%s_%d" % (fam["family"], seed),
                "seed": seed,
                "family": fam["family"],
                "category": fam["category"],
                "variant": k,
                "needs_reply": var["needs_reply"],
                "reply_phrase": reply_phrase,
                "no_reply_phrase": no_reply_phrase,
                "action": fam["action"],
                "folder": fam["folder"],
                "purpose": var["purpose"],
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
