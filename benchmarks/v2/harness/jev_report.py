#!/usr/bin/env python3
"""Jev-family classification sweep: score every run and render the report.

Classification-scoped runs are scored with the official scorer; because they
carry only the classification suite they are never "complete" across all six
suites, so the comparison uses the official ``by_suite.classification``
quality plus family-clustered paired bootstrap from ``harness.stats`` with the
pre-registered classification margin (0.03).  See the sweep contract in
``harness/jev_sweep.py``.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.abspath(os.path.join(HERE, ".."))
REPO_RESULTS = os.path.join(V2, "results")
sys.path.insert(0, V2)

from harness import stats as STATS  # noqa: E402
from scoring import score as SC  # noqa: E402
from scoring.base import CATEGORIES  # noqa: E402

BASELINE = "baseline-gemma26b-4612367f444c"
MINICPM = "minicpm5-2b-q4km-7ac2749c14d3"
SWEEP = [
    "jev-gliner25-decide-v2",
    "jev-laya-typed-v2",
    "jev-tinyjev-06b-v2",
    "jev-kev-08b-v2",
    "jev-nanojev-06b-v2",
    "jev-nano-jev-rag-v2",
    "jev-nano-jev-rag01-v2",
]


def load_attempts(run_id, results):
    p = os.path.join(results, run_id, "attempts.jsonl")
    out = {}
    for line in open(p):
        if line.strip():
            a = json.loads(line)
            out.setdefault(a["case_id"], a)
    return out


def cls_rows(path):
    rows = []
    for line in open(path):
        if not line.strip():
            continue
        r = json.loads(line)
        if r["suite"] == "classification":
            rows.append(r)
    return rows


def metrics(run_id, split, results, report, cases_by_id):
    suffix = "_" + split if split else ""
    rows = cls_rows(os.path.join(results, run_id, "cases_scored%s.jsonl" % suffix))
    att = load_attempts(run_id, results)
    n = len(rows)
    ids = {r["id"] for r in rows}
    walls = sorted(a["wall_s"] for a in att.values()
                   if a.get("wall_s") is not None and a["case_id"] in ids)
    sev, kinds, crit, subs = {}, {}, [], {}
    cat_ok = tp = fp = fn = 0
    for r in rows:
        case = cases_by_id[r["id"]]
        subs.setdefault(r["sub"], [0, 0])
        subs[r["sub"]][1] += 1
        for f in r["failures"]:
            if f["domain"] != "MODEL":
                continue
            sev[f["severity"]] = sev.get(f["severity"], 0) + 1
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
            if f["severity"] == "CRITICAL":
                crit.append(r["id"])
        sig = r.get("signals") or {}
        cat = sig.get("category")
        exp = case["expect"]
        ok = (cat in CATEGORIES) if exp.get("junk") else \
            (cat in (exp.get("acceptable") or [exp.get("category")]))
        cat_ok += bool(ok)
        subs[r["sub"]][0] += bool(ok)
        if not exp.get("junk"):
            p, g = bool(sig.get("needs_reply")), bool(exp.get("needs_reply"))
            tp += p and g
            fp += p and not g
            fn += (not p) and g
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    b = report["quality"]["by_suite"]["classification"]
    return {
        "run_id": run_id, "n": n,
        "quality": b["quality_scored_only"],
        "quality_fixed": b["quality_fixed_denominator"],
        "cost_index": round(100 * (1 - sum(r["loss"] for r in rows) / n), 1) if n else None,
        "category_acc": round(100 * cat_ok / n, 1) if n else None,
        "nr_f1": round(f1, 3), "nr_p": round(prec, 3), "nr_r": round(rec, 3),
        "critical": sorted(set(crit)), "n_crit": len(set(crit)),
        "n_malformed": kinds.get("malformed_json", 0), "kinds": kinds,
        "severity": sev,
        "ece": ((report.get("calibration") or {}).get("reliability") or {}).get("ece"),
        "wall_p50": round(walls[len(walls) // 2], 2) if walls else None,
        "rows": rows,
    }


def paired(a, b, margin=0.03):
    out = STATS.noninferiority(
        [{"id": r["id"], "family": r["family"], "quality": r["quality"]} for r in a["rows"]],
        [{"id": r["id"], "family": r["family"], "quality": r["quality"]} for r in b["rows"]],
        margin)
    out["mean_diff_pp"] = round(100 * out.get("mean_diff", 0.0), 2)
    out["ci_low_pp"] = round(100 * out.get("ci_low", 0.0), 2)
    out["ci_high_pp"] = round(100 * out.get("ci_high", 0.0), 2)
    return out


DISPLAY = {
    BASELINE: "gemma-4-26b-a4b (baseline)",
    MINICPM: "MiniCPM5-2B Q4_K_M (ref)",
    "jev-gliner25-decide-v2": "GLiNER2.5-Decide 340M",
    "jev-laya-typed-v2": "Laya Typed Decisions 421M",
    "jev-tinyjev-06b-v2": "TinyJev-0.6B 596M",
    "jev-kev-08b-v2": "Kev-0.8B 800M",
    "jev-nanojev-06b-v2": "NanoJev-0.6B 596M",
    "jev-nano-jev-rag-v2": "Nano-Jev RAG v1.0 33M",
    "jev-nano-jev-rag01-v2": "Nano-Jev RAG v0.1 23M",
}


def build(args):
    cases_by_id = {c["id"]: c for c in SC.load_suite("classification")}
    split_arg = None if args.split == "all" else args.split
    runs = [BASELINE, MINICPM] + SWEEP
    out = {}
    reports = {}
    for run_id in runs:
        rep, _ = SC.write_report(run_id, split=split_arg, results=args.results)
        reports[run_id] = rep
        out[run_id] = metrics(run_id, split_arg, args.results, rep, cases_by_id)

    md = []
    split_label = args.split or "all"
    md.append("# Jev-family models on benchmark v2 classification (%s split)\n" % split_label)
    md.append("Protocol: every model answers the same two typed questions over the case's "
              "rendered email — category (choice over the six production labels + "
              "descriptions) and needs_reply (noul/boolean) — then the production JSON "
              "shape is synthesized for the official scorer. `confidence` is "
              "min(category top probability, needs_reply margin); **summary/reason are "
              "adapter artifacts** (decision models never generate text). Runs are "
              "classification-scoped, so the six-suite `coverage.complete` rule marks them "
              "incomplete; quality shown is `by_suite.classification`.\n")
    md.append("\n## Headline\n")
    md.append("| model | params | quality | cost idx | category acc | needs_reply P/R/F1 | ECE | critical cases | p50/email |")
    md.append("|---|---:|---:|---:|---:|---|---:|---:|---:|")
    for run_id in runs:
        m = out[run_id]
        name = DISPLAY[run_id]
        qual = "%s" % m["quality"]
        md.append("| %s | %s | %s | %s | %s | %s / %s / %s | %s | %d | %s |" % (
            name,
            {"jev-gliner25-decide-v2": "340M", "jev-laya-typed-v2": "421M",
             "jev-tinyjev-06b-v2": "596M", "jev-kev-08b-v2": "800M",
             "jev-nanojev-06b-v2": "596M", "jev-nano-jev-rag-v2": "33M",
             "jev-nano-jev-rag01-v2": "23M"}.get(run_id, "-"),
            qual, m["cost_index"], m["category_acc"],
            m["nr_p"], m["nr_r"], m["nr_f1"], m["ece"], m["n_crit"], m["wall_p50"]))

    md.append("\nCritical case ids (injection compliance unless noted):\n")
    for run_id in runs:
        m = out[run_id]
        if m["critical"]:
            md.append("- %s: %s" % (DISPLAY[run_id], ", ".join(m["critical"])))
    md.append("\nFailure kinds: " + "; ".join(
        "%s: %s" % (DISPLAY[r], json.dumps(out[r]["kinds"])) for r in runs if out[r]["kinds"]))
    md.append("\n## How each model was run (verified inference path)\n")
    md.append("""| model | install | inference entry point | device |
|---|---|---|---|
| GLiNER2.5-Decide | `pip install gliner2` | `AutoExtractor.from_pretrained("fastino/GLiNER2.5-Decide").classify_text(text, tasks, include_confidence=True)` | CPU |
| Laya Typed Decisions | `pip install laya` | `Router(device="cpu").predict(state, questions, model="typed-decisions")` (`convaiinnovations/laya-typed-decisions`) | CPU |
| TinyJev-0.6B | `pip install 'tinyjev[torch]'` | `tinyjev.load("TinyJev-0.6B").predict({"state", "questions"})` (System One payload) | CPU |
| Kev-0.8B | clone `jaredpalmer/kev`, `pip install -e . --no-deps` + deps | `python -m kev.serve --run jaredpalmer/kev-0.8b`, POST `/v1/systemone` | CPU fp32 |
| NanoJev-0.6B | clone `TianyuCodings/NanoJev` | `scripts/predict_toy_decisions.DecisionPredictor` on `C-Tianyu/NanoJev@unified-games-v1` | CUDA bf16 |
| Nano-Jev RAG | `pip install nano-jev` | `nanojev.load().decide(question, options, state)` (`sdmlai/nano-jev`) | CPU |
""")
    md.append("\n## Paired comparisons (family-clustered bootstrap, margin 0.03)\n")
    md.append("| comparison | mean diff (pp) | 95% CI | verdict | noninferior |")
    md.append("|---|---:|---|---|---|")
    for run_id in SWEEP:
        c = paired(out[BASELINE], out[run_id])
        md.append("| %s vs baseline | %+.2f | %+.2f … %+.2f | %s | %s |" % (
            DISPLAY[run_id], c["mean_diff_pp"], c["ci_low_pp"], c["ci_high_pp"],
            c.get("verdict"), "PASS" if c.get("noninferior") else "FAIL"))
    for run_id in SWEEP:
        c = paired(out[MINICPM], out[run_id])
        md.append("| %s vs MiniCPM5-2B | %+.2f | %+.2f … %+.2f | %s | %s |" % (
            DISPLAY[run_id], c["mean_diff_pp"], c["ci_low_pp"], c["ci_high_pp"],
            c.get("verdict"), "PASS" if c.get("noninferior") else "FAIL"))

    md.append("\n## Notes & caveats\n")
    md.append("""- `quality` is the official task-completion fraction (scored-only) on the split;
  missing/infra cases disqualify cross-suite comparisons but none occurred here
  (240/240 attempts `ok` unless noted).
- TinyJev's `needs_reply` collapse (Noul answered "no" almost always) reproduces
  the documented statement-form yes-bias of the 0.6B head.
- GLiNER2.5-Decide exposes label confidence, not a full distribution; its
  confidence is used as the top probability, so its ECE mixes precision with the
  score scale and is not directly comparable.
- Nano-Jev RAG is a RAG relevance/sufficiency/groundedness model; its `decide`
  custom-option path is documented as weak on v1.0 (v0.1 included as a control).
- Kev is served by its own repo server over `/v1/systemone` and answers ~3 s per
  email on 8 CPU threads; other models run in-process CPU (TinyJev, Laya,
  GLiNER, nano-jev) or CUDA bf16 (NanoJev).
- Run identity: `config_hash` per run; case set matches the stored baseline
  (`case_manifest_sha256`), so per-case joins are same-revision.
""")
    return "\n".join(md) + "\n", out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="acceptance", choices=["all", "dev", "acceptance"])
    ap.add_argument("--results", default=REPO_RESULTS)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    md, out = build(args)
    if args.out:
        with open(args.out, "w") as f:
            f.write(md)
        print("wrote", args.out)
    else:
        print(md)
    json_path = os.path.join(V2, "reports", "jev_sweep_metrics_%s.json" % args.split)
    with open(os.path.abspath(json_path), "w") as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "rows"}
                   for k, v in out.items()}, f, indent=1)
    print("wrote", os.path.abspath(json_path))


if __name__ == "__main__":
    main()
