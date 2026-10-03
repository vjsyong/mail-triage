"""Command-line entry point for benchmark v3 (WP4/WP7 plumbing).

Subcommands::

    build     build an offline dataset bundle        (WP2 ``build`` API)
    validate  schema/lint a dataset bundle           (WP2 ``build`` API)
    run       execute a dataset through an adapter   (this package)
    score     score a run                            (WP5 ``scoring`` API)
    compare   compare two runs                       (WP5 ``scoring`` API)
    review    export/import review worksheets        (WP2 ``build`` API)
    export    write a dataset (optionally split private material)
    import    read a dataset/archive back

The build/scoring/review calls are **lazy** imports of the owning packages so
this module imports cleanly before those packages are integrated.  JSON output
is available on every command; Markdown is used for scoring reports.

Safety defaults: no model network unless an endpoint is given, a non-loopback
endpoint additionally requires ``--allow-remote``, draft datasets require
``--allow-draft``, and credentials are never read from ``.env`` or written to
run artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


# --------------------------------------------------------------------- helpers

def _load_json(path):
    with open(path) as f:
        return json.load(f)


def _load_dataset(path):
    """Load a dataset bundle, preferring the owning build package."""
    try:
        from . import build  # type: ignore  # noqa: PLC0415
        if hasattr(build, "load_dataset"):
            return build.load_dataset(path)
    except Exception:  # noqa: BLE001 - not integrated yet; read JSON directly
        pass
    data = _load_json(path) if os.path.exists(path) else None
    if data is None:
        raise SystemExit("dataset not found: %s" % path)
    return data


def _dump(payload, as_json, markdown=None):
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(markdown if markdown is not None else json.dumps(
            payload, indent=2, sort_keys=True, default=str))


def _local_validate(bundle):
    """Fallback structural validation used when the build package is absent."""
    from .schema import validate_artifact
    errs = []
    if not isinstance(bundle, dict):
        return ["dataset must be an object"]
    if bundle.get("schema_version") != "v3.0":
        errs.append("dataset schema_version must be 'v3.0'")
    if not bundle.get("dataset_id"):
        errs.append("dataset_id is required")
    cases = bundle.get("cases")
    if not isinstance(cases, list) or not cases:
        errs.append("cases must be a non-empty list")
    else:
        seen = set()
        for i, case in enumerate(cases):
            for e in validate_artifact("case", case):
                errs.append("cases[%d]: %s" % (i, e))
            cid = case.get("case_id")
            if cid in seen:
                errs.append("duplicate case_id %r" % cid)
            seen.add(cid)
    return errs


# -------------------------------------------------------------------- adapters

def _adapter_from_args(args):
    from .adapters import (AdapterError, FakeAdapter, FusionAdapter,
                           OpenAICompatAdapter, TinyJevAdapter)
    name = args.adapter
    if name == "offline-fake":
        predictions = _load_json(args.predictions) if args.predictions else {}
        script = _load_json(args.workflow_script) if args.workflow_script else {}
        replies = _load_json(args.workflow_replies) if args.workflow_replies else {}
        return FakeAdapter(predictions=predictions, workflow_script=script,
                           workflow_replies=replies)
    if name == "generative-openai":
        if not args.endpoint:
            raise AdapterError("--endpoint is required for generative-openai")
        return OpenAICompatAdapter(base_url=args.endpoint, model=args.model or "model",
                                   allow_remote=args.allow_remote)
    if name == "tinyjev-decision":
        return TinyJevAdapter(model=args.model or "TinyJev-0.6B",
                              device=args.device or "cpu")
    if name == "fusion":
        decision = TinyJevAdapter(model=args.model or "TinyJev-0.6B",
                                  device=args.device or "cpu")
        prose = None
        if args.endpoint:
            prose = OpenAICompatAdapter(base_url=args.endpoint,
                                        model=args.prose_model or "prose",
                                        allow_remote=args.allow_remote)
        return FusionAdapter(decision_adapter=decision, prose_adapter=prose)
    raise AdapterError("unknown adapter %r" % name)


def _endpoint_class(args):
    if getattr(args, "adapter", None) == "offline-fake":
        return "in_process"
    if getattr(args, "endpoint", None):
        from urllib.parse import urlparse
        host = (urlparse(args.endpoint).hostname or "").lower()
        if host.startswith("127.") or host in ("localhost", "::1"):
            return "in_process"
        return "external_warm"
    return "in_process"


# --------------------------------------------------------------------- commands

def cmd_build(args):
    try:
        from . import build as B  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("build package is not available: %s" % exc)
    bundle = B.build_dataset(seed=args.seed, triage_roots=args.triage_roots,
                             workflow_roots=args.workflow_roots,
                             include_variants=not args.no_variants,
                             layout=args.layout, private_seed=args.private_seed)
    errs = B.validate_dataset(bundle)
    result = {"dataset_id": bundle.get("dataset_id"), "cases": len(bundle.get("cases") or []),
              "errors": errs, "path": args.out}
    if errs:
        _dump(result, args.json)
        return 1
    B.write_dataset(bundle, args.out, private_root=args.private_root)
    _dump(result, args.json,
          markdown="built %s: %d cases -> %s" % (result["dataset_id"],
                                                  result["cases"], args.out))
    return 0


def cmd_validate(args):
    bundle = _load_dataset(args.dataset)
    try:
        from . import build as B  # noqa: PLC0415
        errs = B.validate_dataset(bundle) if hasattr(B, "validate_dataset") else _local_validate(bundle)
    except Exception:  # noqa: BLE001
        errs = _local_validate(bundle)
    result = {"dataset": args.dataset, "valid": not errs, "errors": errs}
    _dump(result, args.json,
          markdown=("valid: %s" % args.dataset) if not errs
          else "invalid:\n  " + "\n  ".join(errs))
    return 0 if not errs else 1


def cmd_run(args):
    from .runner import RunnerError, run_dataset, run_summary
    bundle = _load_dataset(args.dataset)
    adapter = _adapter_from_args(args)
    preflight = {
        "dataset": args.dataset,
        "dataset_id": bundle.get("dataset_id"),
        "review_status": (bundle.get("metadata") or {}).get("review_status", "draft"),
        "allow_draft": bool(args.allow_draft),
        "adapter": adapter.adapter_id,
        "capabilities": adapter.capabilities,
        "mock": adapter.mock,
        "qualifies_as_baseline": adapter.qualifies_as_baseline,
        "model_network": adapter.adapter_id != "offline-fake" and bool(args.endpoint),
        "endpoint_class": _endpoint_class(args),
        "cases": len(bundle.get("cases") or []),
        "out": args.out,
    }
    if not args.json:
        print("preflight: " + json.dumps(preflight, sort_keys=True))
    try:
        run = run_dataset(
            bundle, adapter,
            requested_case_ids=args.cases, requested_splits=args.splits,
            requested_profiles=args.profiles, out_dir=args.out,
            resume=args.resume, allow_draft=args.allow_draft,
            scorer_revision=args.scorer_revision,
            calibrator_revision=args.calibrator_revision,
            max_cases=args.limit, endpoint_class=_endpoint_class(args))
    except RunnerError as exc:
        print("run error: %s" % exc, file=sys.stderr)
        return 1
    summary = run_summary(run)
    summary["preflight"] = preflight
    _dump(summary, args.json,
          markdown="run %s: %d cases (%d ok)" % (summary["run_id"], summary["cases"],
                                                  summary["ok"]))
    return 0


def _scoring():
    try:
        from . import scoring  # type: ignore  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("scoring package is not available: %s" % exc)
    return scoring


def cmd_score(args):
    scoring = _scoring()
    bundle = _load_dataset(args.dataset)
    from .runner import load_run
    run = load_run(args.run)
    report = scoring.score_run(bundle, run, policy=None, calibrator=None)
    if args.out:
        scoring.write_report(report, args.out)
    text = scoring.render_markdown(report) if hasattr(scoring, "render_markdown") else None
    _dump(report, args.json, markdown=text)
    return 0


def cmd_compare(args):
    scoring = _scoring()
    bundle = _load_dataset(args.dataset)
    from .runner import load_run
    comparison = scoring.compare_runs(bundle, load_run(args.baseline),
                                      load_run(args.candidate), policy=None)
    if args.out:
        scoring.write_report(comparison, args.out)
    text = scoring.render_markdown(comparison) if hasattr(scoring, "render_markdown") else None
    _dump(comparison, args.json, markdown=text)
    return 0


def cmd_review(args):
    """Export or import a review worksheet through the WP2 build package."""
    try:
        from . import build as B  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("build package is not available: %s" % exc)
    if args.review_export:
        fn = getattr(B, "review_export", None)
        if fn is None:
            raise SystemExit("build.review_export is not implemented (WP2 owns it)")
        out = fn(_load_dataset(args.dataset), args.review_export,
                 reviewer=args.reviewer, authorized=args.authorized)
        _dump({"exported": args.review_export, "result": out}, args.json)
        return 0
    if args.review_import:
        fn = getattr(B, "review_import", None)
        if fn is None:
            raise SystemExit("build.review_import is not implemented (WP2 owns it)")
        out = fn(args.review_import)
        _dump({"imported": args.review_import, "result": out}, args.json)
        return 0
    raise SystemExit("review requires --export PATH or --import PATH")


def cmd_export(args):
    try:
        from . import build as B  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("build package is not available: %s" % exc)
    fn = getattr(B, "write_dataset", None)
    if fn is None:
        raise SystemExit("build.write_dataset is not implemented (WP2 owns it)")
    result = fn(_load_dataset(args.dataset), args.out, private_root=args.private_root)
    _dump({"path": args.out, "result": result}, args.json)
    return 0


def cmd_import(args):
    bundle = _load_dataset(args.archive)
    try:
        from . import build as B  # noqa: PLC0415
        B.write_dataset(bundle, args.out, private_root=args.private_root)
    except Exception:  # noqa: BLE001 - fall back to plain JSON write
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(bundle, f, indent=1, sort_keys=True)
    _dump({"path": args.out, "cases": len(bundle.get("cases") or [])}, args.json)
    return 0


# ----------------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(prog="benchmarks.v3.cli",
                                description="benchmark v3 build/run/score tooling")
    sub = p.add_subparsers(dest="command")

    b = sub.add_parser("build", help="build an offline dataset bundle (WP2)")
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--triage-roots", type=int, default=200)
    b.add_argument("--workflow-roots", type=int, default=30)
    b.add_argument("--layout", default="pilot")
    b.add_argument("--private-seed", type=int, default=None)
    b.add_argument("--private-root", default=None)
    b.add_argument("--no-variants", action="store_true")
    b.add_argument("--out", required=True)
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("validate", help="validate a dataset bundle (WP2)")
    v.add_argument("dataset")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("run", help="run a dataset through an adapter")
    r.add_argument("dataset")
    r.add_argument("--adapter", default="offline-fake")
    r.add_argument("--out", required=True)
    r.add_argument("--case", action="append", dest="cases")
    r.add_argument("--split", action="append", dest="splits")
    r.add_argument("--profile", action="append", dest="profiles")
    r.add_argument("--resume", default=None)
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--endpoint", default=None)
    r.add_argument("--model", default=None)
    r.add_argument("--prose-model", default=None)
    r.add_argument("--device", default=None)
    r.add_argument("--allow-remote", action="store_true")
    r.add_argument("--scorer-revision", default=None)
    r.add_argument("--calibrator-revision", default=None)
    r.add_argument("--predictions", default=None,
                   help="offline-fake: JSON {case_id: response}")
    r.add_argument("--workflow-script", default=None,
                   help="offline-fake: JSON {case_id: [{tool,args,approve}]}")
    r.add_argument("--workflow-replies", default=None)
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("score", help="score a run (WP5)")
    s.add_argument("dataset")
    s.add_argument("run")
    s.add_argument("--out", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_score)

    c = sub.add_parser("compare", help="compare two runs (WP5)")
    c.add_argument("dataset")
    c.add_argument("baseline")
    c.add_argument("candidate")
    c.add_argument("--out", default=None)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_compare)

    rv = sub.add_parser("review", help="export/import a review worksheet (WP2)")
    rv.add_argument("dataset")
    rv.add_argument("--export", dest="review_export", default=None)
    rv.add_argument("--import", dest="review_import", default=None)
    rv.add_argument("--reviewer", default=None)
    rv.add_argument("--authorized", action="store_true")
    rv.add_argument("--json", action="store_true")
    rv.set_defaults(func=cmd_review)

    e = sub.add_parser("export", help="write a dataset bundle (WP2)")
    e.add_argument("dataset")
    e.add_argument("out")
    e.add_argument("--private-root", default=None)
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_export)

    i = sub.add_parser("import", help="read a dataset bundle (WP2)")
    i.add_argument("archive")
    i.add_argument("out")
    i.add_argument("--private-root", default=None)
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_import)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, str):
            print(code, file=sys.stderr)
            return 1
        return code if isinstance(code, int) else 1
    except Exception as exc:  # noqa: BLE001 - report one clear failure
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
