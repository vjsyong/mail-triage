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
from . import minicpm, samples as S, taxonomies
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


def full_sample(example, source=None, gold=None, taxonomy=None):
    """A full, auditable committed sample.

    Keeps every message field (including ``think`` and tool calls) so that
    ``render(example.messages) == native_render``, plus a ``verification_context``
    sidecar (source record, gold, taxonomy revision/public view) matching the
    example's ``source_id`` so ``cli verify`` works offline without pretending
    self-gold.
    """
    messages = example["messages"]
    native = to_native_messages(messages)
    rendered = minicpm.render_messages(native, tools=example.get("tools") or None)
    context = {
        "source_id": example.get("source_id"),
        "taxonomy_revision": (taxonomy or {}).get("revision"),
        "public_taxonomy": (taxonomies.public_prompt_view(
            taxonomies.public_projection(taxonomy))
            if taxonomy is not None else None),
        "source": source,
        "gold": gold if gold is not None else example.get("gold"),
    }
    return {
        "schema_version": example["schema_version"],
        "example": example,
        "native_render": rendered,
        "verification_context": context,
    }


def _choose_samples(train, sources):
    """Pick a reply/no-reply counterfactual pair and its dialogues (auditable)."""
    by_id = {s["source_id"]: s for s in sources}
    by_source = {}
    for ex in train.get("accepted") or []:
        by_source.setdefault(ex["source_id"], {})[ex["task"]] = ex
    pair_groups = {}
    for sid, src in by_id.items():
        if sid in by_source:
            pair_groups.setdefault(src.get("pair_id") or src["source_id"],
                                   []).append((src, by_source[sid]))
    chosen = []
    for group in pair_groups.values():
        values = {s["intent"]["needs_reply"] for s, _ in group}
        if len(values) == 2:
            for src, tasks in sorted(group, key=lambda t: t[0]["intent"]["needs_reply"]):
                if "decision" in tasks:
                    chosen.append((tasks["decision"], src))
                if "workflow" in tasks:
                    chosen.append((tasks["workflow"], src))
            break
    if not chosen:
        for ex in (train.get("accepted") or [])[:4]:
            chosen.append((ex, by_id.get(ex["source_id"])))
    return chosen[:4]


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
    chosen = _choose_samples(train, sources)
    for ex, src in chosen:
        gold = ex.get("gold")
        sample = full_sample(ex, source=src, gold=gold, taxonomy=taxonomy)
        path = os.path.join(sample_dir, "%s.json" % ex["example_id"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sample, f, ensure_ascii=False, indent=1, sort_keys=True)
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
    """Verify an example; accept explicit or sidecar source/gold context."""
    from .verify import verify_example
    obj = _load_json(args.example)
    context = {}
    if isinstance(obj, dict) and "example" in obj:
        context = obj.get("verification_context") or {}
        example = obj["example"]
    else:
        example = obj
    source = None
    if args.sources:
        for s in _load_json(args.sources):
            if s.get("source_id") == example.get("source_id"):
                source = s
                break
    if source is None and context.get("source") \
            and context["source"].get("source_id") == example.get("source_id"):
        source = context["source"]
    tax = _load_json(args.taxonomy) if args.taxonomy else load_taxonomy()
    gold = example.get("gold")
    if gold is None and context.get("gold") is not None:
        gold = context["gold"]
    problems = verify_example(example, source=source, gold=gold, taxonomy=tax)
    payload = {"example_id": example.get("example_id"),
               "source_id": example.get("source_id"),
               "resolved_source": source is not None,
               "problems": problems}
    print(json.dumps(payload, indent=2))
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

    v = sub.add_parser("verify", help="verify one example (uses --sources/sidecar)")
    v.add_argument("example")
    v.add_argument("--sources", default=None,
                   help="JSON list of source records to resolve by source_id")
    v.add_argument("--taxonomy", default=None, help="taxonomy JSON path")
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
