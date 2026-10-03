"""Command-line entry point for benchmark v3 (WP2/WP4/WP5/WP7 plumbing).

Subcommands::

    build      build an offline dataset bundle        (WP2 ``build`` API)
    validate   schema/lint a dataset bundle           (WP2 ``build`` API)
    run        execute a dataset through an adapter   (WP4 runner)
    score      score a run, optionally fit a calibrator (WP5 ``scoring`` API)
    compare    compare two runs                       (WP5 ``scoring`` API)
    review     export/import a review worksheet, or seal (WP2 ``build`` API)
    export     write a dataset (optionally split private material)
    import     read a dataset bundle back (dataset import, not real-mail intake)

Fail-closed policy: dataset load/validate/export/import go through the owning
``build`` package; a rejected private/real/invalid bundle never creates or
overwrites a destination.  There is no weaker local fallback.

Safety defaults: no model network unless an endpoint is given; a non-loopback
endpoint additionally requires ``--allow-remote``; every externally managed
endpoint (including a loopback GPU server) is recorded as external/warm, never
as an in-process CPU.  Draft datasets require ``--allow-draft``; a model with no
pinned/observed identity requires ``--allow-unverified-model`` (development
probe only).  Credentials are never read from ``.env`` or written to artifacts.
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
    """Load a written dataset bundle through the owning build package.

    Raises ``ValidationError``/``BuildError`` on any problem -- never falls back
    to reading raw JSON, so an invalid bundle cannot masquerade as valid.
    """
    from . import build as B  # noqa: PLC0415
    return B.load_dataset(path)


def _dump(payload, as_json, markdown=None):
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(markdown if markdown is not None else json.dumps(
            payload, indent=2, sort_keys=True, default=str))


def _endpoint_class(args):
    """Any configured endpoint is external/warm; only offline-fake is in-process.

    A loopback HTTP endpoint may be a GPU server; it is externally managed and
    is never evidence of in-process CPU resource limits or cold startup.
    """
    if getattr(args, "adapter", None) == "offline-fake":
        return "in_process"
    if getattr(args, "endpoint", None):
        from urllib.parse import urlparse
        host = (urlparse(args.endpoint).hostname or "").lower()
        if host.startswith("127.") or host in ("localhost", "::1"):
            return "external_warm"
        return "external_warm"
    return "in_process"


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
        return OpenAICompatAdapter(
            base_url=args.endpoint, model=args.model or "model",
            model_revision=args.model_revision,
            model_artifact_sha256=args.model_artifact,
            allow_remote=args.allow_remote)
    if name == "tinyjev-decision":
        return TinyJevAdapter(model=args.model or "TinyJev-0.6B",
                              device=args.device or "cpu",
                              model_revision=args.model_revision,
                              model_artifact_sha256=args.model_artifact)
    if name == "fusion":
        decision = TinyJevAdapter(model=args.model or "TinyJev-0.6B",
                                  device=args.device or "cpu",
                                  model_revision=args.model_revision,
                                  model_artifact_sha256=args.model_artifact)
        prose = None
        if args.endpoint:
            prose = OpenAICompatAdapter(base_url=args.endpoint,
                                        model=args.prose_model or "prose",
                                        model_revision=args.prose_model_revision,
                                        model_artifact_sha256=args.prose_model_artifact,
                                        allow_remote=args.allow_remote)
        return FusionAdapter(decision_adapter=decision, prose_adapter=prose)
    raise AdapterError("unknown adapter %r" % name)


# --------------------------------------------------------------------- commands

def cmd_build(args):
    from . import build as B  # noqa: PLC0415
    bundle = B.build_dataset(seed=args.seed, triage_roots=args.triage_roots,
                             workflow_roots=args.workflow_roots,
                             include_variants=not args.no_variants,
                             layout=args.layout, private_seed=args.private_seed)
    errs = B.validate_dataset(bundle)
    result = {"dataset_id": bundle.get("dataset_id"),
              "cases": len(bundle.get("cases") or []), "errors": errs,
              "path": args.out}
    if errs:
        _dump(result, args.json)
        return 1
    paths = B.write_dataset(bundle, args.out, private_root=args.private_root)
    result["written"] = paths
    _dump(result, args.json,
          markdown="built %s: %d cases -> %s" % (result["dataset_id"],
                                                  result["cases"], args.out))
    return 0


def cmd_validate(args):
    from . import build as B  # noqa: PLC0415
    from .common.validation import ValidationError
    try:
        bundle = B.load_dataset(args.dataset)
    except ValidationError as exc:
        _dump({"dataset": args.dataset, "valid": False, "errors": [str(exc)]},
              args.json, markdown="invalid: %s" % exc)
        return 1
    errs = B.validate_dataset(bundle)
    _dump({"dataset": args.dataset, "valid": not errs, "errors": errs},
          args.json,
          markdown=("valid: %s" % args.dataset) if not errs
          else "invalid:\n  " + "\n  ".join(errs))
    return 0 if not errs else 1


def cmd_run(args):
    from .runner import RunnerError, run_dataset, run_summary
    bundle = _load_dataset(args.dataset)
    adapter = _adapter_from_args(args)
    endpoint_class = _endpoint_class(args)
    preflight = {
        "dataset": args.dataset,
        "dataset_id": bundle.get("dataset_id"),
        "review_status": (bundle.get("metadata") or {}).get("review_status", "draft"),
        "allow_draft": bool(args.allow_draft),
        "adapter": adapter.adapter_id,
        "capabilities": adapter.capabilities,
        "mock": adapter.mock,
        "qualifies_as_baseline": adapter.qualifies_as_baseline,
        "model_key": adapter.fingerprint().get("model_key"),
        "model_identity_source": adapter.fingerprint().get("model_identity_source"),
        "model_unverified_allowed": bool(args.allow_unverified_model),
        "model_network": adapter.adapter_id != "offline-fake" and bool(args.endpoint),
        "endpoint_class": endpoint_class,
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
            max_cases=args.limit, endpoint_class=endpoint_class,
            allow_unverified_model=args.allow_unverified_model)
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
    from . import scoring  # type: ignore  # noqa: PLC0415
    return scoring


def cmd_score(args):
    scoring = _scoring()
    bundle = _load_dataset(args.dataset)
    from .runner import load_run
    run = load_run(args.run)

    calibrator = None
    if args.calibrator:
        calibrator = _load_json(args.calibrator)
    if args.fit_calibrator:
        if args.calibrator:
            raise SystemExit("pass either --calibrator or --fit-calibrator, not both")
        calibrator = scoring.fit_calibrator(bundle, run, policy=None,
                                            split=args.fit_split)
        with open(args.fit_calibrator, "w") as f:
            json.dump(calibrator, f, indent=1, sort_keys=True)

    report = scoring.score_run(bundle, run, policy=None, calibrator=calibrator)
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
    """Bridge to the WP2 review gates: worksheet, import, seal.

    Dataset import and real-mail intake are separate: a de-identified real-mail
    import requires ``--real-mail`` **and** an ``--authorization`` file; nothing
    is auto-sealed and no authorization is synthesized.
    """
    from . import build as B  # noqa: PLC0415
    modes = [bool(args.export_worksheet), bool(args.import_judgements),
             bool(args.seal)]
    if sum(modes) != 1:
        raise SystemExit("review requires exactly one of --export-worksheet, "
                         "--import, --seal")
    bundle = _load_dataset(args.dataset)

    if args.export_worksheet:
        worksheet = B.build_review_worksheet(bundle, reviewer=args.reviewer,
                                             splits=args.splits)
        B.write_review_worksheet(worksheet, args.export_worksheet)
        _dump({"worksheet": args.export_worksheet,
               "items": len(worksheet["items"]),
               "review_status": worksheet["review_status"]}, args.json)
        return 0

    if args.import_judgements:
        if not args.worksheet:
            raise SystemExit("--import requires --worksheet PATH")
        worksheet = B.load_review_worksheet(args.worksheet)
        judgements = _load_json(args.import_judgements)
        authorization = _load_json(args.authorization) if args.authorization else None
        if args.real_mail:
            records = judgements if isinstance(judgements, list) else \
                judgements.get("judgements", [])
            judgements = B.validate_real_import(records,
                                                authorization=authorization)
        result = B.import_review(bundle, worksheet, judgements,
                                 authorization=authorization)
        if args.out:
            B.write_dataset(result["bundle"], args.out,
                            private_root=args.private_root)
        _dump({k: result[k] for k in ("review_status", "reviewed", "rejected",
                                      "conflicts", "unreviewed", "agreement")},
              args.json)
        return 0

    # seal
    if not args.out:
        raise SystemExit("--seal requires --out DIR")
    conflicts = _load_json(args.conflicts) if args.conflicts else None
    sealed = B.seal_bundle(bundle, reviewer=args.reviewer, conflicts=conflicts)
    B.write_dataset(sealed, args.out, private_root=args.private_root)
    _dump({"sealed": sealed.get("dataset_id"),
           "review_status": sealed["metadata"].get("review_status"),
           "out": args.out}, args.json)
    return 0


def cmd_export(args):
    from . import build as B  # noqa: PLC0415
    result = B.write_dataset(_load_dataset(args.dataset), args.out,
                             private_root=args.private_root)
    _dump({"path": args.out, "result": result}, args.json)
    return 0


def cmd_import(args):
    """Dataset import: load + validate + write. No fallback, no raw JSON write."""
    from . import build as B  # noqa: PLC0415
    bundle = _load_dataset(args.archive)
    result = B.write_dataset(bundle, args.out, private_root=args.private_root)
    _dump({"path": args.out, "cases": len(bundle.get("cases") or []),
           "result": result}, args.json)
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
    r.add_argument("--allow-unverified-model", action="store_true")
    r.add_argument("--endpoint", default=None)
    r.add_argument("--model", default=None)
    r.add_argument("--model-revision", default=None)
    r.add_argument("--model-artifact", default=None)
    r.add_argument("--prose-model", default=None)
    r.add_argument("--prose-model-revision", default=None)
    r.add_argument("--prose-model-artifact", default=None)
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

    s = sub.add_parser("score", help="score a run, optionally fit a calibrator (WP5)")
    s.add_argument("dataset")
    s.add_argument("run")
    s.add_argument("--out", default=None)
    s.add_argument("--calibrator", default=None,
                   help="apply a fitted calibrator artifact (JSON)")
    s.add_argument("--fit-calibrator", default=None,
                   help="fit a calibrator from the calibration split and write it")
    s.add_argument("--fit-split", default="calibration")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_score)

    c = sub.add_parser("compare", help="compare two runs (WP5)")
    c.add_argument("dataset")
    c.add_argument("baseline")
    c.add_argument("candidate")
    c.add_argument("--out", default=None)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_compare)

    rv = sub.add_parser("review", help="review worksheet / import / seal (WP2)")
    rv.add_argument("dataset")
    rv.add_argument("--export-worksheet", default=None)
    rv.add_argument("--worksheet", default=None,
                    help="input worksheet for --import")
    rv.add_argument("--import", dest="import_judgements", default=None,
                    help="judgements JSON for --import")
    rv.add_argument("--seal", action="store_true")
    rv.add_argument("--out", default=None)
    rv.add_argument("--private-root", default=None)
    rv.add_argument("--reviewer", default=None)
    rv.add_argument("--splits", action="append", default=None)
    rv.add_argument("--conflicts", default=None)
    rv.add_argument("--real-mail", action="store_true",
                    help="de-identified real-mail import (needs --authorization)")
    rv.add_argument("--authorization", default=None)
    rv.add_argument("--json", action="store_true")
    rv.set_defaults(func=cmd_review)

    e = sub.add_parser("export", help="write a dataset bundle (WP2)")
    e.add_argument("dataset")
    e.add_argument("out")
    e.add_argument("--private-root", default=None)
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_export)

    i = sub.add_parser("import", help="read a dataset bundle back (WP2)")
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
