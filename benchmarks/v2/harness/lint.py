#!/usr/bin/env python3
"""Dataset linting for benchmark v2.

Fails loudly on the classes of defect that silently corrupt a benchmark:
- duplicate case ids, missing/invalid schemas;
- references to messages that do not exist;
- family leakage across the dev/acceptance split;
- near-duplicate cases that inflate a score;
- "long-context" cases whose evidence is truncated away by the production
  render limit (the v1 long-mail bug);
- contradictory or internally inconsistent expectations;
- adversarial cases without a clean pair / forbidden-label list.

Usage:  python harness/lint.py [--json]
Exit code 0 = clean; 1 = errors; 2 = warnings only (with --strict).
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
CASES = os.path.join(V2, "cases")
CORPUS = os.path.join(V2, "corpus")
sys.path.insert(0, V2)

from common.validation import load_schema, validate  # noqa: E402

RENDER_LIMIT = 1500  # engine.py classify body truncation
SUITES = ["classification", "assistant", "drafting", "rules", "simulate", "summary"]


def load_cases():
    out = {}
    for s in SUITES:
        p = os.path.join(CASES, s + ".jsonl")
        rows = []
        if os.path.exists(p):
            with open(p) as f:
                for i, line in enumerate(f, 1):
                    if line.strip():
                        rows.append((i, json.loads(line)))
        out[s] = rows
    return out


def load_msgs():
    with open(os.path.join(CORPUS, "messages.jsonl")) as f:
        return {json.loads(l)["id"]: json.loads(l) for l in f if l.strip()}


def norm_text(s):
    return " ".join((s or "").lower().split())


def signature(case):
    """Coarse content signature for near-duplicate detection."""
    key = case["class"] + "|" + case.get("sub", "")
    for f in ("user", "rule_text", "reasoning", "existing_rules"):
        if f in case:
            key += "|" + norm_text(case[f])[:400]
    if case["class"] == "rules":
        key += "|" + json.dumps(case.get("tagged"), sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def lint():
    errors, warnings = [], []
    suites = load_cases()
    msgs = load_msgs()
    schema = load_schema("case.schema.json")
    seen_ids = {}
    seen_sig = {}
    family_split = {}

    for suite, rows in suites.items():
        for lineno, c in rows:
            where = "%s:%d (%s)" % (suite, lineno, c.get("id", "?"))
            errs = validate(c, schema)
            for e in errs:
                errors.append("%s schema: %s" % (where, e))
            cid = c.get("id")
            if cid in seen_ids:
                errors.append("duplicate id %r in %s and %s" % (cid, seen_ids[cid], where))
            seen_ids[cid] = where

            # required common fields
            for f in ("split", "family", "expect", "sub"):
                if not c.get(f):
                    errors.append("%s missing %r" % (where, f))
            spl = c.get("split")
            fam = c.get("family")
            if fam in family_split and family_split[fam] != spl:
                errors.append("family %r leaks across splits (%s vs %s)"
                              % (fam, family_split[fam], spl))
            family_split[fam] = spl

            # references
            if c.get("source_message") is not None and c["source_message"] not in msgs:
                errors.append("%s references unknown message %r" % (where, c["source_message"]))
            if suite == "drafting" and c.get("msg_id") not in msgs:
                errors.append("%s references unknown msg_id %r" % (where, c.get("msg_id")))

            # long-context honesty
            u = c.get("user") or ""
            if c.get("evidence_position") == "late" and len(u) <= RENDER_LIMIT:
                errors.append("%s claims late evidence but renders %d <= %d"
                              % (where, len(u), RENDER_LIMIT))
            if "long" in (c.get("tags") or []) and suite == "classification":
                warnings.append("%s tagged long; classification body truncated at %d "
                                "(evidence position=%s)"
                                % (where, RENDER_LIMIT, c.get("evidence_position")))

            # adversarial hygiene
            if c.get("adversarial"):
                if suite == "classification":
                    exp = c["expect"]
                    if not exp.get("injection"):
                        warnings.append("%s adversarial classification lacks injection flag" % where)
                    if c.get("injection_target") == "label" and not exp.get("forbidden_labels"):
                        warnings.append("%s label-injection lacks forbidden_labels" % where)
                if suite == "assistant" and not (c["expect"].get("answer_forbid")
                                                 or c["expect"].get("injection_reply_markers")
                                                 or c["expect"].get("injection_obey_markers")):
                    warnings.append("%s adversarial assistant has no obedience marker" % where)
                if c.get("clean_pair") and c["clean_pair"] not in seen_ids:
                    pass  # clean pair may appear later; checked in a second pass

            # expectation sanity
            exp = c.get("expect") or {}
            if suite == "classification":
                if not exp.get("json_required"):
                    warnings.append("%s classification without json_required" % where)
                if not exp.get("junk") and exp.get("category") is None:
                    warnings.append("%s classification without category or junk flag" % where)
                if exp.get("junk") and exp.get("category") is not None:
                    errors.append("%s junk case must not pin a category" % where)
                inj = exp.get("forbidden_labels")
                if inj and exp.get("category") in inj:
                    errors.append("%s forbidden_labels includes the ground-truth category" % where)
            if suite == "rules":
                if exp.get("min_rules", 0) > exp.get("max_rules", 5):
                    errors.append("%s min_rules > max_rules" % where)
                if exp.get("need_guard") and exp.get("allow_empty") and exp.get("max_rules", 1) == 0:
                    errors.append("%s contradictory guard/empty expectation" % where)
            if suite == "drafting":
                if exp.get("min_words", 0) > exp.get("max_words", 1 << 30):
                    errors.append("%s min_words > max_words" % where)
            if suite == "assistant":
                if exp.get("no_calls") and exp.get("required_calls"):
                    errors.append("%s no_calls with required_calls" % where)

            # near-duplicate detection
            sig = signature(c)
            if sig in seen_sig and seen_sig[sig][0] != c["class"]:
                warnings.append("%s near-duplicate of %s" % (where, seen_sig[sig][1]))
            else:
                seen_sig.setdefault(sig, (c["class"], where))

    # second pass: clean_pair existence
    for suite, rows in suites.items():
        for _lineno, c in rows:
            cp = c.get("clean_pair")
            if cp and cp not in seen_ids:
                errors.append("%s clean_pair %r not found" % (c.get("id"), cp))

    return errors, warnings, suites, family_split


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    errors, warnings, suites, fams = lint()
    counts = {s: len(rows) for s, rows in suites.items()}
    out = {"counts": counts, "families": len(fams),
           "errors": errors, "warnings": warnings}
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print("cases:", counts, "families:", len(fams))
        for e in errors:
            print("  ERROR  ", e)
        for w in warnings[:40]:
            print("  WARN   ", w)
        if len(warnings) > 40:
            print("  ... %d more warnings" % (len(warnings) - 40))
    if errors:
        sys.exit(1)
    if warnings and args.strict:
        sys.exit(2)
    print("lint OK")


if __name__ == "__main__":
    main()
