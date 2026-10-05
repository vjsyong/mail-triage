"""CLI for the mail SFT slice.

Subcommands::

    build-slice   export paired classifier+dialogue examples into train/dev domains
    render        render one example with the MiniCPM5 native template
    verify        re-run verification on an example file

``build-slice`` is deterministic and offline.  It never relabels a record that
lacks an explicit role/domain.  When ``--sources``/``--dialogues`` are given
(the live teacher + rollout output), those records are used instead of the
authored fixtures.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ..common.hashing import hash_obj
from . import export as E
from . import minicpm, samples as S
from .messages import to_native_messages
from .taxonomies import load_taxonomy

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DEFAULT_SAMPLES = os.path.join(os.path.dirname(__file__), "samples")

IDENTITIES = {
    "model": "openbmb/MiniCPM5-2B",
    "model_revision": "f97400052a43d642bbc6e9975e2397e3ae6a6b52",
    "prompt_revision": "mail-sft-prompt2",
    "schema_revision": "mail-sft-0.1",
    "template_revision": "minicpm5-native-v1",
    "taxonomy_revision": "mail-sft-tax1",
}


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _jsonl_write(path, examples):
    with open(path, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False, sort_keys=True) + "\n")


def _tool_names(example):
    return [t["function"]["name"] for t in example.get("tools") or []]


def _assistant_json(example):
    for m in reversed(example.get("messages") or []):
        if m.get("role") == "assistant" and m.get("supervised"):
            content = m.get("content") or ""
            start = content.find("{")
            if start >= 0:
                try:
                    return json.loads(content[start:])
                except ValueError:
                    return None
    return None


def compact_sample(example):
    """A compact, committable view of an example (canonical + native render)."""
    messages = example["messages"]
    native = to_native_messages(messages)
    rendered = minicpm.render_messages(native, tools=example.get("tools") or None)
    out = {
        "example_id": example["example_id"],
        "task": example["task"],
        "role": example.get("role"),
        "generation_domain": example["generation_domain"],
        "source_id": example.get("source_id"),
        "lineage_id": example.get("lineage_id"),
        "tools": _tool_names(example),
        "messages": [{k: m[k] for k in ("role", "content", "supervised")
                      if k in m} for m in messages],
        "mask": [bool(m.get("supervised")) for m in messages],
        "native_render": rendered,
        "identities": example["identities"],
        "metadata": example["metadata"],
    }
    if example["task"] == "decision":
        out["target"] = _assistant_json(example)
    trace = example.get("trace")
    if trace:
        out["trace_summary"] = {
            "usage": trace["usage"], "usage_source": trace.get("usage_source"),
            "state_final": trace.get("state_final"),
            "turns": [{"index": t["index"], "finish_reason": t["finish_reason"],
                       "rejected": t["rejected"],
                       "tool_calls": [c["name"] for c in t["tool_calls"]],
                       "events": [{"tool": e.get("tool"), "status": e.get("status"),
                                   "mutated": e.get("mutated")}
                                  for e in t["events"]]}
                      for t in trace["turns"]],
        }
    return out


def build_slice(out_dir, *, seed=7, sources=None, dialogues=None,
                dev_sources=None, dev_dialogues=None, sample_dir=None):
    os.makedirs(out_dir, exist_ok=True)
    taxonomy = load_taxonomy()
    if sources is None:
        fix = S.load_workflow_scenarios()
        sources, dialogues = fix["sources"], fix["dialogues"]
    else:
        sources = _load_json(sources) if isinstance(sources, str) else sources
        dialogues = _load_json(dialogues) if isinstance(dialogues, str) else dialogues
    if dev_sources is None:
        fix = S.load_dev_workflow_scenarios()
        dev_sources, dev_dialogues = fix["sources"], fix["dialogues"]
    else:
        dev_sources = _load_json(dev_sources) if isinstance(dev_sources, str) else dev_sources
        dev_dialogues = _load_json(dev_dialogues) if isinstance(dev_dialogues, str) else dev_dialogues

    train = E.export_sources(sources, dialogues, taxonomy, domain="training",
                             seed=seed, identities=IDENTITIES,
                             allowed_roles=("training",))
    dev = E.export_sources(dev_sources, dev_dialogues, taxonomy,
                           domain="development", seed=seed + 101,
                           identities=IDENTITIES, allowed_roles=("development",))
    exports = [train, dev]
    contamination = E.contamination_report(exports)
    linkage = {"training": E.assert_cross_task_linkage(train),
               "development": E.assert_cross_task_linkage(dev)}

    # write per-domain train/dev/eval JSONL WITHOUT an undocumented manual split
    domains = {"training": train, "development": dev}
    written = {}
    all_examples = []
    for name, exp in domains.items():
        path = os.path.join(out_dir, "%s.jsonl" % name)
        _jsonl_write(path, exp["accepted"])
        written[name] = path
        all_examples.extend(exp["accepted"])
    _jsonl_write(os.path.join(out_dir, "examples.jsonl"), all_examples)

    sample_dir = sample_dir or os.path.join(out_dir, "samples")
    os.makedirs(sample_dir, exist_ok=True)
    committed = []
    chosen = [e for e in train["accepted"][:4]] + [e for e in dev["accepted"][:1]]
    for ex in chosen[:5]:
        compact = compact_sample(ex)
        path = os.path.join(sample_dir, "%s.json" % ex["example_id"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(compact, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        committed.append(os.path.relpath(path, REPO_ROOT))

    report = {
        "slice_revision": "mail-sft-0.2",
        "seed": seed,
        "taxonomy_revision": taxonomy["revision"],
        "domains": {exp["domain"]["domain"]: exp["domain"]["domain_sha256"]
                    for exp in exports},
        "counts": {name: {"accepted": len(exp["accepted"]),
                          "rejected": len(exp["rejected"]),
                          "rejected_reasons": sorted({r["reason"].split(":")[0]
                                                      for r in exp["rejected"]})}
                   for name, exp in domains.items()},
        "pairs": {name: len(exp["pairs"]) for name, exp in domains.items()},
        "contamination": contamination,
        "cross_task_linkage": linkage,
        "written": written,
        "committed_samples": committed,
        "review_status": "draft",
        "human_seal": False,
        "test_qualified": False,
        "content_sha256": hash_obj(all_examples),
    }
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    return report


def cmd_build_slice(args):
    report = build_slice(
        args.out, seed=args.seed, sources=args.sources, dialogues=args.dialogues,
        dev_sources=args.dev_sources, dev_dialogues=args.dev_dialogues,
        sample_dir=args.sample_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    ok = (not report["contamination"]
          and not any(report["cross_task_linkage"].values()))
    return 0 if ok else 1


def cmd_render(args):
    example = _load_json(args.example)
    native = to_native_messages(example["messages"])
    print(minicpm.render_messages(native, tools=example.get("tools") or None))
    return 0


def cmd_verify(args):
    from .verify import verify_example
    example = _load_json(args.example)
    problems = verify_example(example)
    print(json.dumps({"problems": problems}, indent=2))
    return 0 if not problems else 1


def build_parser():
    p = argparse.ArgumentParser(prog="benchmarks.v3.training.cli")
    sub = p.add_subparsers(dest="command")

    b = sub.add_parser("build-slice", help="export paired examples")
    b.add_argument("--out", required=True)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--sources", default=None)
    b.add_argument("--dialogues", default=None)
    b.add_argument("--dev-sources", default=None)
    b.add_argument("--dev-dialogues", default=None)
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
