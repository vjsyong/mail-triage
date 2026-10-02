#!/usr/bin/env python3
"""Summarize unmodified probes + adapted runs per candidate for the report."""
import json
import os
import statistics
import sys

R = os.path.expanduser("~/mail-triage-bench/benchmarks/results")
keys = sys.argv[1:] or ["qwen4b", "qwen9b", "gemma4e4b", "granite3b", "lfm8b"]

out = {}
for key in keys:
    d = {}
    probe_dir = os.path.join(R, key, "unmodified_probe")
    for suite in ("classification", "assistant", "drafting"):
        p = os.path.join(probe_dir, suite + ".jsonl")
        if not os.path.exists(p):
            continue
        rows = [json.loads(l) for l in open(p)]
        ok = err = 0
        walls = []
        for r in rows:
            bad = bool(r.get("error")) or bool(r.get("parse_error")) or \
                  (suite == "classification" and not (r.get("parsed") or {}).get("category"))
            ok += (not bad)
            err += bad
            if r.get("wall_s"):
                walls.append(r["wall_s"])
        d[suite] = {"n": len(rows), "ok": ok, "bad": err,
                    "median_wall": round(statistics.median(walls), 1) if walls else None,
                    "max_wall": max(walls) if walls else None}
    out[key] = d

print(json.dumps(out, indent=1))
with open(os.path.join(R, "probes_summary.json"), "w") as f:
    json.dump(out, f, indent=1)
