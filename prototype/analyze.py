#!/usr/bin/env python3
"""Coverage-matched cross-stack analysis: same queries, same coverage, both stacks.

    python prototype/analyze.py --prototype bench/results_phase1_48.json \
        --live bench/live_live48.json --out bench/compare_48.json
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import raglite as R  # noqa: E402

ROOT = R.ROOT


def covered_ids_live(qfile):
    """Of the queries in qfile, which have their expected subject present among
    live-indexed messages (chunks join messages)?"""
    queries = json.load(open(qfile))
    src = sqlite3.connect("file:%s?mode=ro" % R.LIVE_DB, uri=True)
    covered = []
    for item in queries:
        ok = False
        for exp in item["expect"]:
            n = src.execute(
                "SELECT 1 FROM messages m WHERE EXISTS "
                "(SELECT 1 FROM chunks c WHERE c.message_id = m.id) "
                "AND lower(m.subject) LIKE ? LIMIT 1", ("%" + exp.lower() + "%",)).fetchone()
            if n:
                ok = True
                break
        covered.append(ok)
    return {i: c for i, c in enumerate(covered)}


def metrics(rows, subset=None):
    """rows: [{'q','rank'}] -> metrics dict; subset: set of indexes to include."""
    n = h1 = h5 = h10 = 0
    rr = 0.0
    for i, r in enumerate(rows):
        if subset is not None and i not in subset:
            continue
        n += 1
        rank = r.get("rank")
        if rank == 1:
            h1 += 1
        if rank and rank <= 5:
            h5 += 1
        if rank and rank <= 10:
            h10 += 1
        if rank:
            rr += 1.0 / rank
    n = max(1, n)
    return {"n": n, "R@1": round(h1 / n * 100, 1), "R@5": round(h5 / n * 100, 1),
            "R@10": round(h10 / n * 100, 1), "MRR": round(rr / n, 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prototype", required=True)
    ap.add_argument("--live", required=True)
    ap.add_argument("--qfile", default=os.path.join(ROOT, "tests", "eval_queries.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "prototype", "bench", "compare.json"))
    args = ap.parse_args()

    proto = json.load(open(args.prototype))["results"]
    live = json.load(open(args.live))
    covered = covered_ids_live(args.qfile)
    subset = {i for i, c in enumerate(covered.values()) if c}
    total = len(covered)
    print("coverage: %d/%d queries have their target message inside the live index"
          % (len(subset), total))

    out = {"coverage": {"covered": len(subset), "total": total},
           "live_full": {}, "live_covered": {}, "prototype_full": {}, "prototype_covered": {}}
    print("\n== LIVE stack (production index) ==")
    for name, blob in live.items():
        if not isinstance(blob, dict) or "rows" not in blob:
            continue
        mf = metrics(blob["rows"])
        mc = metrics(blob["rows"], subset)
        out["live_full"][name] = mf
        out["live_covered"][name] = mc
        print("%-15s full: R@1 %5.1f R@5 %5.1f MRR %.3f | covered: R@1 %5.1f R@5 %5.1f MRR %.3f"
              % (name, mf["R@1"], mf["R@5"], mf["MRR"], mc["R@1"], mc["R@5"], mc["MRR"]))

    print("\n== Prototype stack (same corpus; covered queries) ==")
    for name, blob in proto.items():
        rows = blob.get("rows")
        if not rows:
            continue
        mf = metrics(rows)
        mc = metrics(rows, subset)
        out["prototype_full"][name] = mf
        out["prototype_covered"][name] = mc
        print("%-18s full: R@1 %5.1f R@5 %5.1f MRR %.3f | covered: R@1 %5.1f R@5 %5.1f MRR %.3f"
              % (name, mf["R@1"], mf["R@5"], mf["MRR"], mc["R@1"], mc["R@5"], mc["MRR"]))

    json.dump(out, open(args.out, "w"), indent=1)
    print("\nwrote", args.out)


if __name__ == "__main__":
    main()
