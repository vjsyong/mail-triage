#!/usr/bin/env python3
"""Analyze 3x stability repeats on the hard subset for a model.
Usage: stability_report.py <model_key>
Reads results/<key>/stability/r{1,2,3}/*.jsonl
"""
import json
import os
import sys
from collections import Counter

R = os.path.expanduser("~/mail-triage-bench/benchmarks/results")
key = sys.argv[1] if len(sys.argv) > 1 else "qwen9b"
base = os.path.join(R, key, "stability")

def load(rel):
    p = os.path.join(base, rel)
    if not os.path.exists(p):
        return {}
    return {json.loads(l)["id"]: json.loads(l) for l in open(p)}

runs = [load(f"r{i}/classification.jsonl") for i in (1, 2, 3)]
asst = [load(f"r{i}/assistant.jsonl") for i in (1, 2, 3)]

print(f"### {key} — classification stability (labels across 3 runs)")
for cid in sorted(set().union(*[set(r) for r in runs])):
    cats = []
    for r in runs:
        rec = r.get(cid)
        if not rec:
            cats.append(None); continue
        cats.append((rec.get("parsed") or {}).get("category"))
    cons = len(set(cats)) == 1 and cats[0] is not None
    print(f"  {cid:28s} {'STABLE' if cons else 'VARIES'} -> {cats}")

print(f"\n### {key} — assistant stability (tool choice / empties across runs)")
for cid in sorted(set().union(*[set(r) for r in asst])):
    firsts, calls_n, empty = [], [], []
    for r in asst:
        rec = r.get(cid)
        if not rec:
            firsts.append(None); calls_n.append(None); empty.append(None); continue
        calls = rec.get("calls") or []
        firsts.append(calls[0]["name"] if calls else "(none)")
        calls_n.append(len(calls))
        empty.append(not (rec.get("reply") or "").strip() and not calls)
    cons = len(set(firsts)) == 1
    print(f"  {cid:28s} {'STABLE' if cons else 'VARIES'} first={firsts} calls={calls_n} empty_reply={empty}")
