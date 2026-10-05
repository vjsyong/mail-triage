"""CLI for the mail SFT slice.

Subcommands::

    build-slice   build domain-separated examples from a small v3 bundle
    render        render one example with the MiniCPM5 native template
    verify        re-run verification on an example file

``build-slice`` is deterministic and offline (no model, no network).  It writes
an examples JSONL, a report with accepted/rejected counts and domain hashes, and
a small set of compact committed samples.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ..common.hashing import hash_obj
from .. import build as B
from . import export as E
from . import minicpm, samples as S, verify as V
from .messages import to_native_messages

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DEFAULT_SAMPLES = os.path.join(os.path.dirname(__file__), "samples")

IDENTITIES = {
    "model": "openbmb/MiniCPM5-2B",
    "model_revision": "f97400052a43d642bbc6e9975e2397e3ae6a6b52",
    "prompt_revision": "mail-sft-prompt1",
    "schema_revision": "mail-sft-0.1",
    "template_revision": "minicpm5-native-v1",
    "taxonomy_revision": "mail-sft-tax1",
}


def _jsonl_write(path, examples):
    with open(path, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False, sort_keys=True) + "\n")


def _tool_names(example):
    return [t["function"]["name"] for t in example.get("tools") or []]


def compact_sample(example):
    """A compact, committable view of an example (canonical + native render)."""
    messages = example["messages"]
    native = to_native_messages(messages)
    rendered = minicpm.render_messages(native, tools=example.get("tools") or None)
    out = {
        "example_id": example["example_id"],
        "task": example["task"],
        "generation_domain": example["generation_domain"],
        "source_id": example.get("source_id"),
        "lineage_id": example.get("lineage_id"),
        "tools": _tool_names(example),
        "messages": [
            {k: m[k] for k in ("role", "content", "supervised")
             if k in m}
            for m in messages
        ],
        "mask": [bool(m.get("supervised")) for m in messages],
        "native_render": rendered,
        "identities": example["identities"],
        "metadata": example["metadata"],
    }
    if example.get("decision") is not None:
        out["decision"] = example["decision"]
    trace = example.get("trace")
    if trace:
        out["trace_summary"] = {
            "usage": trace["usage"],
            "usage_source": trace.get("usage_source", "model"),
            "state_final_folders": (trace.get("state_final") or {}).get("folders"),
            "turns": [
                {"index": t["index"], "finish_reason": t["finish_reason"],
                 "rejected": t["rejected"],
                 "tool_calls": [c["name"] for c in t["tool_calls"]],
                 "events": [{"tool": e.get("tool"), "status": e.get("status"),
                             "mutated": e.get("mutated"), "permission": (
                                 e.get("permission") or {}).get("decision")}
                            for e in t["events"]]}
                for t in trace["turns"]
            ],
        }
    return out


def build_slice(out_dir, *, seed=7, triage_roots=8, sample_dir=None):
    os.makedirs(out_dir, exist_ok=True)
    taxonomy = None
    from .taxonomies import load_taxonomy
    taxonomy = load_taxonomy()

    bundle = B.build_dataset(seed=seed, triage_roots=triage_roots,
                             workflow_roots=0, include_variants=False)
    scenarios = S.load_workflow_scenarios()["scenarios"]

    train = E.export_decision_cases(
        bundle["cases"], bundle["gold"], domain="training", seed=seed,
        allowed_splits=E.TRAINING_ALLOWED, identities=IDENTITIES,
        taxonomy=taxonomy)
    train_wf = E.export_workflow_scenarios(
        scenarios, domain="training", seed=seed, identities=IDENTITIES)

    dev_bundle = B.build_dataset(seed=seed + 101, triage_roots=triage_roots,
                                 workflow_roots=0, include_variants=False)
    dev = E.export_decision_cases(
        dev_bundle["cases"], dev_bundle["gold"], domain="development",
        seed=seed + 101, allowed_splits=E.DEVELOPMENT_ALLOWED,
        identities=IDENTITIES, taxonomy=taxonomy)

    ev_bundle = B.build_dataset(seed=seed + 202, triage_roots=triage_roots,
                                workflow_roots=0, include_variants=False)
    ev = E.export_decision_cases(
        ev_bundle["cases"], ev_bundle["gold"], domain="evaluation",
        seed=seed + 202, allowed_splits=E.DEVELOPMENT_ALLOWED,
        identities=IDENTITIES, taxonomy=taxonomy)

    exports = [train, train_wf, dev, ev]
    contamination = E.contamination_report(exports)

    all_examples = []
    for exp in exports:
        all_examples.extend(exp["accepted"])
    _jsonl_write(os.path.join(out_dir, "examples.jsonl"), all_examples)

    sample_dir = sample_dir or os.path.join(out_dir, "samples")
    os.makedirs(sample_dir, exist_ok=True)
    committed = []
    chosen = ((train["accepted"][:2] if train["accepted"] else [])
              + train_wf["accepted"])
    for ex in chosen[:5]:
        compact = compact_sample(ex)
        path = os.path.join(sample_dir, "%s.json" % ex["example_id"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(compact, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        committed.append(os.path.relpath(path, REPO_ROOT))

    report = {
        "slice_revision": "mail-sft-0.1",
        "seed": seed,
        "triage_roots": triage_roots,
        "taxonomy_revision": taxonomy["revision"],
        "domains": {exp["domain"]["domain"]: exp["domain"]["domain_sha256"]
                    for exp in exports},
        "counts": {exp["domain"]["domain"]: {
            "accepted": len(exp["accepted"]),
            "rejected": len(exp["rejected"]),
            "rejected_reasons": sorted({r["reason"].split(":")[0]
                                        for r in exp["rejected"]}),
        } for exp in exports},
        "contamination": contamination,
        "committed_samples": committed,
        "review_status": "draft",
        "human_seal": False,
        "test_qualified": False,
        "notes": ("Synthetic-only authored slice. No real model was run to build "
                  "these examples; workflow traces carry placeholder usage. "
                  "No test-set qualification is claimed."),
        "content_sha256": hash_obj(all_examples),
    }
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    return report


def cmd_build_slice(args):
    report = build_slice(args.out, seed=args.seed, triage_roots=args.triage_roots,
                         sample_dir=args.sample_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not report["contamination"] else 1


def cmd_render(args):
    with open(args.example, encoding="utf-8") as f:
        example = json.load(f)
    native = to_native_messages(example["messages"])
    print(minicpm.render_messages(native, tools=example.get("tools") or None))
    return 0


def cmd_verify(args):
    with open(args.example, encoding="utf-8") as f:
        example = json.load(f)
    gold = None
    problems = V.verify_example(example, gold, trace=example.get("trace"))
    print(json.dumps({"problems": problems}, indent=2))
    return 0 if not problems else 1


def build_parser():
    p = argparse.ArgumentParser(prog="benchmarks.v3.training.cli")
    sub = p.add_subparsers(dest="command")

    b = sub.add_parser("build-slice", help="build domain-separated examples")
    b.add_argument("--out", required=True)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--triage-roots", type=int, default=8)
    b.add_argument("--sample-dir", default=None)
    b.set_defaults(func=cmd_build_slice)

    r = sub.add_parser("render", help="render one example natively")
    r.add_argument("example")
    r.set_defaults(func=cmd_render)

    v = sub.add_parser("verify", help="verify one example")
    v.add_argument("example")
    v.set_defaults(func=cmd_verify)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
