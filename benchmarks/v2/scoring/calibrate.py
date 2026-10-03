#!/usr/bin/env python3
"""Confidence calibration and selective risk (WP2).

Classification confidence is only useful if it is calibrated enough to route
uncertain mail to a fallback.  Given scored classification signals, produce:

- a reliability curve (bin -> coverage, mean confidence, accuracy);
- expected calibration error (ECE);
- a selective-risk curve: for each confidence threshold, what fraction of cases
  the model would auto-accept and how accurate those are.

No scipy dependency — plain Python.
"""
import json
import os


def _load_signals(rows):
    """Keep only finite numeric confidences in [0,1]; models sometimes echo the
    schema string (e.g. "0.0 - 1.0") and that must not break the report."""
    out = []
    for r in rows:
        c = r.get("confidence")
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            continue
        if 0 <= c <= 1:
            out.append(r)
    return out


def calibration(records, bins=10):
    """``records``: iterable of {'confidence': float, 'correct': bool}."""
    recs = _load_signals(records)
    if not recs:
        return {"n": 0, "ece": None, "bins": []}
    buckets = [[] for _ in range(bins)]
    for r in recs:
        c = float(r["confidence"])
        idx = min(bins - 1, max(0, int(c * bins)))
        buckets[idx].append((c, bool(r["correct"])))
    out_bins = []
    ece = 0.0
    n = len(recs)
    for i, b in enumerate(buckets):
        if not b:
            out_bins.append({"lo": i / bins, "hi": (i + 1) / bins, "n": 0,
                             "mean_conf": None, "accuracy": None})
            continue
        mean_conf = sum(c for c, _ in b) / len(b)
        acc = sum(1 for _, ok in b if ok) / len(b)
        out_bins.append({"lo": i / bins, "hi": (i + 1) / bins, "n": len(b),
                         "mean_conf": round(mean_conf, 3), "accuracy": round(acc, 3)})
        ece += (len(b) / n) * abs(mean_conf - acc)
    return {"n": n, "ece": round(ece, 4), "bins": out_bins}


def selective_risk(records, thresholds=(0.5, 0.6, 0.7, 0.8, 0.9, 0.95)):
    """For each confidence threshold, coverage + accuracy of accepted cases."""
    recs = _load_signals(records)
    out = []
    for t in thresholds:
        acc = [r for r in recs if float(r["confidence"]) >= t]
        if not acc:
            out.append({"threshold": t, "coverage": 0.0, "n": 0, "accuracy": None,
                        "errors": 0})
            continue
        correct = sum(1 for r in acc if r["correct"])
        out.append({"threshold": t,
                    "coverage": round(len(acc) / len(recs), 3),
                    "n": len(acc),
                    "accuracy": round(correct / len(acc), 3),
                    "errors": len(acc) - correct})
    return out


def build(per_case_rows):
    """per_case_rows: list of {'confidence','correct','suite'}."""
    cls = [r for r in per_case_rows if r.get("suite") == "classification"]
    return {"reliability": calibration(cls),
            "selective_risk": selective_risk(cls)}


if __name__ == "__main__":
    import sys
    rows = [json.loads(l) for l in open(sys.argv[1])] if len(sys.argv) > 1 else []
    print(json.dumps(build(rows), indent=1))
