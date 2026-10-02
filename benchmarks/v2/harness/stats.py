#!/usr/bin/env python3
"""Paired comparison, uncertainty, noninferiority, and stability (WP5).

Design goals from the assessment:
- compare models on *paired* cases, with uncertainty clustered by scenario
  family (cases within a thread are correlated, so treating them as independent
  would understate the interval);
- never turn "a better point estimate" into "equivalent": a noninferiority
  verdict requires the lower confidence bound to clear a margin declared in
  advance;
- rescore complete stability repeats (quality, not just labels).

Pure stdlib (seeded bootstrap), so it runs anywhere.
"""
import hashlib
import json
import os
import random

DEFAULT_B = 2000


def _seed(diffs):
    h = hashlib.sha256()
    for k in sorted(diffs):
        h.update(("%s=%s;" % (k, diffs[k])).encode())
    return int(h.hexdigest()[:8], 16)


def load_rows(path):
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def by_id(rows):
    return {r["id"]: r for r in rows}


def paired_compare(rows_a, rows_b, key="quality", B=DEFAULT_B, alpha=0.05):
    """Paired diff (b - a) over cases present and scored in both runs.

    Resamples *families* with replacement, so the CI reflects scenario-level
    clustering.  ``rows_*`` are per-case dicts with id, family, and ``key``.
    """
    a, b = by_id(rows_a), by_id(rows_b)
    shared = [cid for cid in a if cid in b
              and a[cid].get(key) is not None and b[cid].get(key) is not None]
    diffs = {cid: (b[cid][key] - a[cid][key]) for cid in shared}
    if not shared:
        return {"n": 0, "error": "no shared scored cases"}
    fam = {}
    for cid in shared:
        fam.setdefault(a[cid].get("family") or "?", []).append(cid)
    fam_ids = sorted(fam)
    mean = sum(diffs.values()) / len(shared)
    rng = random.Random(_seed(diffs))
    boots = []
    for _ in range(B):
        pick = [rng.choice(fam_ids) for _ in fam_ids]
        vals = [diffs[cid] for f in pick for cid in fam[f]]
        boots.append(sum(vals) / len(vals))
    boots.sort()
    lo = boots[int((alpha / 2) * B)]
    hi = boots[int((1 - alpha / 2) * B) - 1]
    # win/loss/tie
    wins = sum(1 for d in diffs.values() if d > 1e-9)
    losses = sum(1 for d in diffs.values() if d < -1e-9)
    return {"n": len(shared), "families": len(fam_ids),
            "mean_diff": round(mean, 4),
            "ci_low": round(lo, 4), "ci_high": round(hi, 4),
            "alpha": alpha, "B": B,
            "wins": wins, "losses": losses, "ties": len(shared) - wins - losses,
            "verdict": _verdict(mean, lo, hi)}


def _verdict(mean, lo, hi):
    if lo > 0:
        return "better"
    if hi < 0:
        return "worse"
    return "inconclusive"


def noninferiority(rows_a, rows_b, margin, key="quality", B=DEFAULT_B, alpha=0.05):
    """Is B noninferior to A by at most ``margin`` (a positive number)?"""
    cmp = paired_compare(rows_a, rows_b, key=key, B=B, alpha=alpha)
    if cmp.get("error"):
        return dict(cmp, noninferior=False, margin=margin)
    noninf = cmp["ci_low"] > -abs(margin)
    return dict(cmp, margin=margin, noninferior=bool(noninf),
                verdict_ni="noninferior" if noninf else "inferior")


def stability_report(repeats, key="quality"):
    """``repeats``: list of lists of per-case rows (one list per repeat).

    Rescores complete outcomes (quality + failure kinds), not just labels.
    """
    runs = [by_id(r) for r in repeats]
    ids = set.intersection(*[set(r) for r in runs]) if runs else set()
    varying = []
    for cid in sorted(ids):
        vals = [r[cid].get(key) for r in runs]
        if None in vals:
            continue
        if max(vals) - min(vals) > 1e-9:
            varying.append({"id": cid, "values": vals})
    kind_sets = []
    for r in runs:
        kind_sets.append({cid: frozenset(f["kind"] for f in r.get(cid, {}).get("failures", []))
                          for cid in ids})
    kind_variance = []
    for cid in sorted(ids):
        sets = [k.get(cid, frozenset()) for k in kind_sets]
        if len(set(sets)) > 1:
            kind_variance.append({"id": cid,
                                  "kinds": [sorted(s) for s in sets]})
    n = len(ids)
    return {"n_shared": n, "repeats": len(runs),
            "quality_variance_cases": varying,
            "quality_variance_rate": round(len(varying) / n, 3) if n else None,
            "failure_kind_variance_cases": kind_variance,
            "failure_kind_variance_rate": round(len(kind_variance) / n, 3) if n else None}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="cases_scored.jsonl for model A")
    ap.add_argument("--b", required=True, help="cases_scored.jsonl for model B")
    ap.add_argument("--margin", type=float, default=None)
    ap.add_argument("--key", default="quality")
    args = ap.parse_args()
    a, b = load_rows(args.a), load_rows(args.b)
    out = paired_compare(a, b, key=args.key)
    if args.margin is not None:
        out = noninferiority(a, b, args.margin, key=args.key)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
