#!/usr/bin/env python3
"""Report generator for benchmark v2 (WP6).

Combines one or more run reports into a markdown report that keeps the three
measurement dimensions separate and adds paired, family-clustered comparisons
against a declared baseline using pre-registered margins from
``policy/acceptance.json``.

Usage:
  python harness/report.py --baseline <run_id> --candidate <run_id> \
      [--split acceptance] [--out report.md]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
RESULTS = os.path.join(V2, "results")
POLICY = os.path.join(V2, "policy", "acceptance.json")
sys.path.insert(0, V2)

from harness import stats  # noqa: E402


def load_report(run_id, split=None, results=RESULTS):
    suffix = "" if not split else "_" + split
    p = os.path.join(results, run_id, "report%s.json" % suffix)
    with open(p) as f:
        return json.load(f)


def load_cases(run_id, split=None, results=RESULTS):
    suffix = "" if not split else "_" + split
    p = os.path.join(results, run_id, "cases_scored%s.jsonl" % suffix)
    return stats.load_rows(p)


def render(baseline, candidates, split=None, results=RESULTS):
    with open(POLICY) as f:
        policy = json.load(f)
    base_rep = load_report(baseline, split, results)
    base_cases = load_cases(baseline, split, results)
    lines = []
    lines.append("# Model benchmark v2 — comparison report\n")
    lines.append("Split: **%s** · scorer %s · policy %s\n"
                 % (split or "all", base_rep.get("scorer_revision"),
                    policy.get("policy_revision")))
    lines.append("## Measurement model\n")
    lines.append("Quality (task completion), failure counts by behaviour, and the "
                 "severity cost index are reported separately; missing/infra results "
                 "disqualify a comparison rather than being silently dropped.\n")

    # headline table
    lines.append("## Headline (baseline = %s)\n" % baseline)
    lines.append("| run | complete | quality (fixed denom) | quality (scored only) | "
                 "cost index | critical cases |")
    lines.append("|---|---|---|---|---|---|")
    reps = [base_rep] + [load_report(c, split, results) for c in candidates]
    for r in reps:
        lines.append("| %s | %s | %s | %s | %s | %d |" % (
            r["run_id"], r["coverage"]["_totals"]["complete"],
            r["quality"]["overall_fixed_denominator"],
            r["quality"]["overall_scored_only"],
            r["cost_index"]["value"], len(r["failures"]["critical_cases"])))

    # failure taxonomy
    lines.append("\n## Failure taxonomy (MODEL failures)\n")
    lines.append("| run | " + " | ".join(["CRITICAL", "HIGH", "MEDIUM", "LOW"]) + " |")
    lines.append("|---" * 5 + "|")
    for r in reps:
        sev = r["failures"]["by_severity"]
        lines.append("| %s | %d | %d | %d | %d |" % (
            r["run_id"], sev.get("CRITICAL", 0), sev.get("HIGH", 0),
            sev.get("MEDIUM", 0), sev.get("LOW", 0)))
    kinds = {}
    for r in reps:
        for k, v in r["failures"]["by_kind"].items():
            kinds[k] = kinds.get(k, 0) + v
    if kinds:
        lines.append("\nTop failure kinds across runs: " +
                     ", ".join("`%s`×%d" % (k, v) for k, v in
                               sorted(kinds.items(), key=lambda kv: -kv[1])[:12]) + "\n")

    # paired comparisons
    lines.append("\n## Paired comparisons vs baseline (family-clustered bootstrap)\n")
    for c in candidates:
        cc = load_cases(c, split, results)
        cmp = stats.paired_compare(base_cases, cc, B=policy["sample"]["paired_bootstrap_B"],
                                   alpha=policy["sample"]["alpha"])
        lines.append("### %s" % c)
        if cmp.get("error"):
            lines.append("- _%s_" % cmp["error"])
            continue
        lines.append("- mean quality diff: **%+.3f** (95%% CI %+.3f … %+.3f), n=%d "
                     "families=%d" % (cmp["mean_diff"], cmp["ci_low"], cmp["ci_high"],
                                      cmp["n"], cmp["families"]))
        lines.append("- verdict: **%s** (wins %d / losses %d / ties %d)"
                     % (cmp["verdict"], cmp["wins"], cmp["losses"], cmp["ties"]))
        margins = policy["per_task_noninferiority_margin"]
        for suite, margin in margins.items():
            sub_a = [r for r in base_cases if r["suite"] == suite and r.get("quality") is not None]
            sub_b = [r for r in cc if r["suite"] == suite and r.get("quality") is not None]
            ni = stats.noninferiority(sub_a, sub_b, margin,
                                      B=min(1000, policy["sample"]["paired_bootstrap_B"]))
            if ni.get("error"):
                continue
            lines.append("  - %s noninferiority (margin %.2f): **%s** (CI low %+.3f)"
                         % (suite, margin, "PASS" if ni["noninferior"] else "FAIL",
                            ni["ci_low"]))

    # calibration
    lines.append("\n## Calibration (classification)\n")
    for r in reps:
        rel = (r.get("calibration") or {}).get("reliability") or {}
        lines.append("- %s: n=%s ECE=%s" % (r["run_id"], rel.get("n"), rel.get("ece")))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--candidate", action="append", default=[])
    ap.add_argument("--split", default="acceptance", choices=["all", "dev", "acceptance"])
    ap.add_argument("--results", default=RESULTS)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    split = None if args.split == "all" else args.split
    md = render(args.baseline, args.candidate, split=split, results=args.results)
    if args.out:
        with open(args.out, "w") as f:
            f.write(md)
        print("wrote", args.out)
    else:
        print(md)


if __name__ == "__main__":
    main()
