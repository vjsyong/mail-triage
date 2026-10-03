#!/usr/bin/env python3
"""Benchmark v2 scorer orchestrator (WP2).

Reads a completed run's resolved attempts and emits a report that keeps three
dimensions separate by construction:

1. ``quality``  — task-completion fraction (scored-only AND fixed-denominator);
2. ``failures`` — counts/rates by behaviour kind and severity, plus critical
   case ids;
3. ``cost_index`` — a documented, arbitrary severity-weighted index.

Missing / infrastructure results are never silently dropped: they make the run
ineligible for comparison (see harness/run_manager.py) and are counted as
losses in the fixed-denominator views.

Usage:
  python scoring/score.py --run <run_id> [--split dev|acceptance] [--json]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
RESULTS = os.path.join(V2, "results")
CASES = os.path.join(V2, "cases")
sys.path.insert(0, V2)

from common.taxonomy import MODEL, WEIGHT  # noqa: E402
from harness import run_manager  # noqa: E402
from scoring import suites as S  # noqa: E402
from scoring import calibrate  # noqa: E402

SCORER_REVISION = "v2.0"


def load_suite(suite, split=None):
    rows = run_manager.load_suite(suite)
    if split:
        rows = [c for c in rows if c.get("split") == split]
    return rows


def score_run(run_id, split=None, results=RESULTS):
    resolved = run_manager.resolve_attempts(run_id, results)
    cov = run_manager.coverage(run_id, results)
    manifest = run_manager.load_manifest(run_id, results) or {}
    per_case = []
    crit_cases = []
    by_sub = {}
    by_suite = {}
    calib_rows = []
    cls_conf = {}          # true -> {pred: n}
    nr_conf = {}           # true -> {pred: n} for needs_reply
    for suite in run_manager.SUITES:
        cases = load_suite(suite, split)
        s_quality_scored = []
        s_quality_fixed = []
        s_fail = 0
        for case in cases:
            a = resolved.get((suite, case["id"]))
            loss = 1.0
            status = "missing"
            qual = None
            fails = []
            signals = {}
            if a and a["status"] in ("error", "timeout"):
                status = "infra"
            elif a and a["status"] == "ok":
                status = "scored"
                output = a.get("output")
                qual, fails, signals = S.SCORERS[suite](case, output)
                model_fails = [f for f in fails if f.domain == MODEL]
                sev_sum = sum(WEIGHT.get(f.severity, 9) for f in model_fails)
                loss = min(1.0, sev_sum / 27.0)
                if qual is not None:
                    s_quality_scored.append(qual)
                s_quality_fixed.append(qual if qual is not None else 0.0)
                if any(f.severity == "CRITICAL" for f in model_fails):
                    crit_cases.append("%s/%s" % (suite, case["id"]))
                s_fail += len(model_fails)
                if suite == "classification":
                    cat = signals.get("category")
                    junk = bool(case["expect"].get("junk"))
                    true_cat = "junk" if junk else case["expect"].get("category")
                    pred_cat = cat if cat else "invalid"
                    cls_conf.setdefault(true_cat, {}).setdefault(pred_cat, 0)
                    cls_conf[true_cat][pred_cat] += 1
                    if not junk:
                        t_nr = bool(case["expect"].get("needs_reply"))
                        p_nr = signals.get("needs_reply")
                        nr_conf.setdefault(str(t_nr), {}).setdefault(str(p_nr), 0)
                        nr_conf[str(t_nr)][str(p_nr)] += 1
                    correct = junk or \
                        cat in (case["expect"].get("acceptable") or [case["expect"].get("category")])
                    conf_sig = signals.get("confidence")
                    if isinstance(conf_sig, (int, float)) and not isinstance(conf_sig, bool) \
                            and 0 <= conf_sig <= 1:
                        calib_rows.append({"confidence": conf_sig,
                                           "correct": bool(correct), "suite": "classification"})
            else:
                s_quality_fixed.append(0.0)

            sub = case.get("sub") or "?"
            agg = by_sub.setdefault(sub, [0.0, 0])
            agg[0] += (qual if qual is not None else 0.0)
            agg[1] += 1

            per_case.append({
                "suite": suite, "id": case["id"], "sub": sub,
                "family": case.get("family"), "split": case.get("split"),
                "status": status,
                "quality": round(qual, 3) if qual is not None else None,
                "loss": round(loss, 3),
                "failures": [f.as_dict() for f in fails],
                "signals": _trim_signals(signals),
            })
        n = len(cases)
        by_suite[suite] = {
            "cases": n,
            "scored": len(s_quality_scored),
            "quality_scored_only": round(100 * sum(s_quality_scored) / len(s_quality_scored), 1)
            if s_quality_scored else None,
            "quality_fixed_denominator": round(100 * sum(s_quality_fixed) / n, 1) if n else None,
        }

    scoped = per_case
    total = len(scoped)
    scored = [r for r in scoped if r["status"] == "scored"]
    qual_scored = [r["quality"] for r in scored if r["quality"] is not None]
    qual_fixed = [r["quality"] if r["status"] == "scored" and r["quality"] is not None else 0.0
                  for r in scoped]

    sev_tally = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}
    kind_tally = {}
    cases_with_model_failure = 0
    for r in scoped:
        model_fails = [f for f in r["failures"] if f["domain"] == MODEL]
        if model_fails:
            cases_with_model_failure += 1
        for f in model_fails:
            sev_tally[f["severity"]] = sev_tally.get(f["severity"], 0) + 1
            kind_tally[f["kind"]] = kind_tally.get(f["kind"], 0) + 1

    loss_mean = sum(r["loss"] for r in scoped) / total if total else 0.0
    report = {
        "run_id": run_id,
        "model": manifest.get("model_key"),
        "config_hash": manifest.get("config_hash"),
        "scorer_revision": SCORER_REVISION,
        "split": split or "all",
        "coverage": cov,
        "quality": {
            "overall_scored_only": round(100 * sum(qual_scored) / len(qual_scored), 1)
            if qual_scored else None,
            "overall_fixed_denominator": round(100 * sum(qual_fixed) / total, 1) if total else None,
            "by_suite": by_suite,
        },
        "failures": {
            "by_severity": sev_tally,
            "by_kind": dict(sorted(kind_tally.items(), key=lambda kv: -kv[1])),
            "total_model_failures": sum(sev_tally.values()),
            "cases_with_model_failure": cases_with_model_failure,
            "critical_cases": crit_cases,
            "note": ("severity counts include only MODEL failures; missing and "
                     "infrastructure results are reported under coverage"),
        },
        "cost_index": {
            "value": round(100 * (1 - loss_mean), 1),
            "fixed_denominator": True,
            "definition": ("100 * (1 - mean over scope of min(1, sum(severity weights)/27)); "
                           "missing/infra count as full loss. Arbitrary ordinal index, "
                           "not a calibrated real-world cost."),
            "weights": WEIGHT,
        },
        "calibration": calibrate.build(calib_rows),
        "classification": {
            "confusion": cls_conf,
            "per_category": _per_category(cls_conf),
            "needs_reply_confusion": nr_conf,
            "needs_reply": _binary_metrics(nr_conf),
        },
        "by_sub": {k: round(100 * v[0] / v[1], 1) for k, v in sorted(by_sub.items()) if v[1]},
    }
    return report, per_case


def _per_category(conf):
    out = {}
    for true_cat, preds in conf.items():
        tp = preds.get(true_cat, 0)
        fn = sum(n for p, n in preds.items() if p != true_cat)
        fp = sum(p.get(true_cat, 0) for t, p in conf.items() if t != true_cat)
        prec = tp / (tp + fp) if (tp + fp) else None
        rec = tp / (tp + fn) if (tp + fn) else None
        f1 = (2 * prec * rec / (prec + rec)) if prec and rec else None
        out[true_cat] = {"support": tp + fn, "precision": _r(prec), "recall": _r(rec),
                         "f1": _r(f1)}
    return out


def _binary_metrics(conf):
    keys = set(conf) | {k for v in conf.values() for k in v}
    if "True" not in keys or "False" not in keys:
        return {}
    tp = conf.get("True", {}).get("True", 0)
    fn = conf.get("True", {}).get("False", 0)
    fp = conf.get("False", {}).get("True", 0)
    tn = conf.get("False", {}).get("False", 0)
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if prec and rec else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": _r(prec), "recall": _r(rec), "f1": _r(f1)}


def _r(x):
    return round(x, 3) if x is not None else None


def _trim_signals(signals):
    out = {}
    for k, v in signals.items():
        if k in ("calls", "rules", "state"):
            out[k] = v
        elif isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
    return out


def write_report(run_id, split=None, results=RESULTS, write_cases=True, out_dir=None):
    report, per_case = score_run(run_id, split=split, results=results)
    d = out_dir or os.path.join(results, run_id)
    os.makedirs(d, exist_ok=True)
    suffix = "" if not split else "_" + split
    with open(os.path.join(d, "report%s.json" % suffix), "w") as f:
        json.dump(report, f, indent=1)
    if write_cases:
        with open(os.path.join(d, "cases_scored%s.jsonl" % suffix), "w") as f:
            for r in per_case:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return report, per_case


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--split", default=None, choices=[None, "dev", "acceptance"])
    ap.add_argument("--results", default=RESULTS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report, _ = write_report(args.run, split=args.split, results=args.results)
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print("run:", report["run_id"], "model:", report["model"])
        print("coverage complete:", report["coverage"]["_totals"]["complete"])
        print("quality scored-only: %s  fixed-denom: %s" % (
            report["quality"]["overall_scored_only"],
            report["quality"]["overall_fixed_denominator"]))
        print("cost_index:", report["cost_index"]["value"],
              "critical cases:", len(report["failures"]["critical_cases"]))
        print("by kind:", report["failures"]["by_kind"])


if __name__ == "__main__":
    main()
