#!/usr/bin/env python3
"""Run integrity for benchmark v2 (WP1).

Responsibilities:
- validate a run manifest against the committed schema;
- create an immutable ``results/<run_id>/`` directory;
- refuse to resume when the configuration hash changed;
- append (never overwrite) attempt records with explicit retry numbering;
- compute coverage and classify every case as scored / missing / infra-failed;
- preflight the case set against its manifest so a stale suite cannot be scored.

No GPU or network here — this is pure accounting, unit-testable with fakes.
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
CASES = os.path.join(V2, "cases")
RESULTS = os.path.join(V2, "results")
sys.path.insert(0, V2)

from common.identity import resolve_resume  # noqa: E402
from common.validation import validate_or_raise, load_schema  # noqa: E402

SUITES = ["classification", "assistant", "drafting", "rules", "simulate", "summary"]


def run_dir(run_id, results=RESULTS):
    return os.path.join(results, run_id)


def load_manifest(run_id, results=RESULTS):
    p = os.path.join(run_dir(run_id, results), "run_manifest.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def init_run(manifest, results=RESULTS):
    """Validate + create the run dir, guarding against a config mismatch."""
    validate_or_raise(manifest, load_schema("run_manifest.schema.json"), "run manifest")
    d = run_dir(manifest["run_id"], results)
    os.makedirs(d, exist_ok=True)
    existing = load_manifest(manifest["run_id"], results)
    mode = resolve_resume(manifest, existing)
    with open(os.path.join(d, "run_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    if not os.path.exists(os.path.join(d, "attempts.jsonl")):
        open(os.path.join(d, "attempts.jsonl"), "w").close()
    return d, mode


def attempts_path(run_id, results=RESULTS):
    return os.path.join(run_dir(run_id, results), "attempts.jsonl")


def record_attempt(run_id, suite, attempt, results=RESULTS):
    """Append one attempt record. ``attempt`` must match attempt.schema.json."""
    validate_or_raise(attempt, load_schema("attempt.schema.json"), "attempt")
    with open(attempts_path(run_id, results), "a") as f:
        f.write(json.dumps(attempt, ensure_ascii=False) + "\n")


def load_attempts(run_id, results=RESULTS):
    p = attempts_path(run_id, results)
    rows = []
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def resolve_attempts(run_id, results=RESULTS):
    """Collapse attempts to the winning record per (suite, case_id).

    Rules: any successful attempt wins (latest success); otherwise the latest
    attempt is kept so infrastructure errors remain visible.  Nothing is
    silently dropped — the raw attempts file is preserved.
    """
    best = {}
    for a in load_attempts(run_id, results):
        key = (a["suite"], a["case_id"])
        cur = best.get(key)
        if cur is None:
            best[key] = a
            continue
        if a["status"] == "ok" and cur["status"] != "ok":
            best[key] = a
        elif a["status"] == "ok" and cur["status"] == "ok":
            best[key] = a if a["attempt"] >= cur["attempt"] else cur
        elif cur["status"] != "ok":
            best[key] = a if a["attempt"] >= cur["attempt"] else cur
    return best


def load_suite(suite, cases_dir=CASES):
    p = os.path.join(cases_dir, suite + ".jsonl")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def preflight(run_id, results=RESULTS, cases_dir=CASES):
    """Verify the on-disk case set matches the frozen manifest and the run.

    Returns (ok, report).  A mismatch means the suite moved under the run.
    """
    problems = []
    manifest_path = os.path.join(cases_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return False, {"error": "cases/manifest.json missing"}
    with open(manifest_path) as f:
        case_manifest = json.load(f)
    run_manifest = load_manifest(run_id, results)
    if run_manifest and run_manifest.get("case_manifest_sha256"):
        from common.hashing import sha256_file
        actual = sha256_file(manifest_path)
        if actual != run_manifest["case_manifest_sha256"]:
            problems.append("case manifest changed since run init (%s != %s)"
                            % (actual[:12], run_manifest["case_manifest_sha256"][:12]))
    total = 0
    for suite in SUITES:
        rows = load_suite(suite, cases_dir)
        total += len(rows)
        declared = (case_manifest.get(suite) or {}).get("count")
        if declared is not None and declared != len(rows):
            problems.append("%s: manifest says %s cases, found %d" % (suite, declared, len(rows)))
        ids = [c["id"] for c in rows]
        if len(set(ids)) != len(ids):
            problems.append("%s: duplicate case ids" % suite)
    if (case_manifest.get("total") or {}).get("count") not in (None, total):
        problems.append("total case count %s != %d" % (case_manifest["total"].get("count"), total))
    return (not problems), {"cases": total, "problems": problems}


def coverage(run_id, results=RESULTS, cases_dir=CASES):
    """Per-suite scored / missing / infra counts for a run."""
    resolved = resolve_attempts(run_id, results)
    out = {}
    for suite in SUITES:
        rows = load_suite(suite, cases_dir)
        scored = missing = infra = 0
        missing_ids, infra_ids = [], []
        for c in rows:
            a = resolved.get((suite, c["id"]))
            if a is None or a["status"] == "missing":
                missing += 1
                missing_ids.append(c["id"])
            elif a["status"] in ("error", "timeout"):
                infra += 1
                infra_ids.append(c["id"])
            else:
                scored += 1
        out[suite] = {"total": len(rows), "scored": scored, "missing": missing,
                      "infra": infra, "missing_ids": missing_ids[:20],
                      "infra_ids": infra_ids[:20]}
    out["_totals"] = {
        "total": sum(v["total"] for v in out.values()),
        "scored": sum(v["scored"] for v in out.values()),
        "missing": sum(v["missing"] for v in out.values()),
        "infra": sum(v["infra"] for v in out.values()),
    }
    out["_totals"]["complete"] = out["_totals"]["missing"] == 0
    return out


def eligible_for_comparison(cov):
    """A run may only be ranked against others if it is complete."""
    return bool(cov.get("_totals", {}).get("complete"))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--coverage", action="store_true")
    args = ap.parse_args()
    if args.coverage:
        cov = coverage(args.run)
        print(json.dumps(cov, indent=1))
        if not eligible_for_comparison(cov):
            print("RUN INELIGIBLE FOR COMPARISON (incomplete)", file=sys.stderr)
            sys.exit(1)
    else:
        ok, rep = preflight(args.run)
        print(json.dumps(rep, indent=1))
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
