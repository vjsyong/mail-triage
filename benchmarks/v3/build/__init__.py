"""Benchmark v3 dataset builder (WP2/WP6).

Public API (other work packages target these, never the internals):

``build_dataset(*, seed=0, triage_roots=200, workflow_roots=30,
include_variants=True, layout='pilot', private_seed=None) -> bundle``
``validate_dataset(bundle) -> [problem, ...]``
``load_dataset(path) -> bundle`` (raises on validation errors)
``write_dataset(bundle, path, *, private_root=None) -> artifact paths``

A bundle is ``{"schema_version": "v3.0", "dataset_id", "cases", "gold",
"scenarios", "policies", "lineage", "provenance", "metadata"}`` and every
individual record satisfies the frozen v3.0 schemas. Fixture and assertion
shapes for collaborators are documented in ``build/README.md``.
"""
import json
import os

from ..common.hashing import hash_obj
from ..common.validation import ValidationError
from .errors import BuildError, PrivateExportError, ReviewError, WriteRefused
from .generate import (FULL_LAYOUT, PRIVATE_SPLITS, PUBLIC_SPLITS, SCHEMA_VERSION,
                       build_dataset, summarize_components)
from .lint import validate_dataset
from .review import (agreement_report, build_review_worksheet, import_review,
                     load_review_worksheet, seal_bundle, validate_real_import,
                     validate_review_worksheet, write_review_worksheet)

__all__ = [
    "SCHEMA_VERSION", "FULL_LAYOUT", "PUBLIC_SPLITS", "PRIVATE_SPLITS",
    "build_dataset", "validate_dataset", "load_dataset", "write_dataset",
    "public_export", "build_review_worksheet", "validate_review_worksheet",
    "write_review_worksheet", "load_review_worksheet", "import_review",
    "agreement_report", "seal_bundle", "validate_real_import",
    "BuildError", "WriteRefused", "PrivateExportError", "ReviewError",
]

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MANIFEST = "manifest.json"
BUNDLE_FILE = "bundle.json"

_SUBSET_MAP = {"cases": "case_id", "gold": "gold_id", "scenarios": "scenario_id",
               "lineage": "lineage_id"}


def _subset_bundle(bundle, splits):
    """A validated bundle containing only the requested splits.

    Policies and provenance are shared, so they are carried whole; cases, gold,
    scenarios and lineage are filtered consistently, and the metadata
    visibility/contains_private flags are recomputed so a public export can
    never carry a private record.
    """
    splits = set(splits)
    keep_cases = [c for c in bundle["cases"] if c.get("split") in splits]
    case_ids = {c["case_id"] for c in keep_cases}
    keep_golds = [g for g in bundle["gold"] if g.get("case_id") in case_ids]
    scn_ids = {c["scenario_id"] for c in keep_cases}
    keep_scenarios = [s for s in bundle["scenarios"] if s.get("scenario_id") in scn_ids]
    lin_ids = {c["lineage_id"] for c in keep_cases}
    keep_lineage = [l for l in bundle["lineage"] if l.get("lineage_id") in lin_ids]

    metadata = dict(bundle.get("metadata") or {})
    private_present = any(c.get("split") in PRIVATE_SPLITS for c in keep_cases)
    metadata["contains_private"] = private_present
    metadata["visibility"] = "private" if private_present else "public"
    if not private_present:
        # Never leak the secret private seed through a public artifact.
        metadata.pop("private_seed_used", None)
        metadata["private_seed_used"] = None
    components = summarize_components(keep_cases, keep_scenarios)
    counts = dict(metadata.get("counts") or {})
    counts.update({
        "cases": len(keep_cases), "gold": len(keep_golds),
        "scenarios": len(keep_scenarios), "lineages": len(keep_lineage),
        "triage_roots": components["triage"]["roots"],
        "workflow_roots": components["workflow"]["roots"],
        "triage_components": components["triage"]["total"],
        "workflow_components": components["workflow"]["total"],
        "duplicate_groups": (components["triage"]["duplicate_groups"]
                             + components["workflow"]["duplicate_groups"]),
        "grouped_roots": (components["triage"]["grouped_roots"]
                          + components["workflow"]["grouped_roots"]),
    })
    metadata["counts"] = counts
    metadata["components"] = components
    metadata["split_counts"] = {
        "triage_components": components["triage"]["by_split"],
        "workflow_components": components["workflow"]["by_split"],
    }
    coverage = dict(metadata.get("coverage") or {})
    coverage["components"] = components
    metadata["coverage"] = coverage
    subset = dict(bundle)
    subset.update({
        "cases": keep_cases, "gold": keep_golds, "scenarios": keep_scenarios,
        "lineage": keep_lineage, "metadata": metadata,
    })
    return subset


def _target_is_intended(path, dataset_id):
    if not os.path.exists(path):
        return True
    if os.path.isdir(path) and not os.listdir(path):
        return True
    manifest_path = os.path.join(path, MANIFEST)
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError):
            return False
        return existing.get("dataset_id") == dataset_id
    return False


def _assert_private_root(private_root, public_path):
    real = os.path.realpath(private_root)
    public_real = os.path.realpath(public_path)
    if real == public_real:
        raise PrivateExportError("private_root and public path must differ")
    if real == os.path.realpath(REPO_ROOT) or real.startswith(
            os.path.realpath(REPO_ROOT) + os.sep):
        raise PrivateExportError(
            "private_root %r is inside the public checkout; private records must "
            "live outside it" % private_root)
    if real.startswith(public_real + os.sep) or public_real.startswith(real + os.sep):
        raise PrivateExportError("private_root and public path must not nest")


def _write_dir(bundle, path, dataset_id):
    if not _target_is_intended(path, dataset_id):
        raise WriteRefused(
            "refusing to overwrite %r: it holds a different or unrecognised dataset"
            % path)
    os.makedirs(path, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "visibility": (bundle.get("metadata") or {}).get("visibility"),
        "review_status": (bundle.get("metadata") or {}).get("review_status"),
        "counts": (bundle.get("metadata") or {}).get("counts"),
        "content_sha256": hash_obj(bundle),
    }
    bundle_path = os.path.join(path, BUNDLE_FILE)
    manifest_path = os.path.join(path, MANIFEST)
    with open(bundle_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    return {"bundle": bundle_path, "manifest": manifest_path}


def write_dataset(bundle, path, *, private_root=None):
    """Write a bundle to ``path``; private records go only to ``private_root``.

    Refuses an unintended overwrite and refuses to publish any private,
    calibration or real-mail record through the public path.
    """
    problems = validate_dataset(bundle)
    if problems:
        raise ValidationError("refusing to write an invalid bundle:\n  "
                              + "\n  ".join(problems[:20]))
    private_cases = [c for c in bundle["cases"]
                     if c.get("split") in PRIVATE_SPLITS]
    if private_cases and private_root is None:
        raise PrivateExportError(
            "bundle contains %d private/calibration records; pass an explicit "
            "private_root outside the checkout" % len(private_cases))
    if private_root is None:
        return {"public": _write_dir(bundle, path, bundle["dataset_id"])}
    _assert_private_root(private_root, path)
    public_bundle = _subset_bundle(bundle, PUBLIC_SPLITS)
    paths = {"public": _write_dir(public_bundle, path, bundle["dataset_id"])}
    if private_cases:
        private_bundle = _subset_bundle(bundle, PRIVATE_SPLITS)
        paths["private"] = _write_dir(private_bundle, private_root,
                                      bundle["dataset_id"])
    return paths


def public_export(bundle):
    """A public (development, synthetic) subset for future SFT/export paths.

    Fails closed: private, calibration and real-mail records can never be
    consumed by a public export, even if they were generated in the same build.
    """
    problems = validate_dataset(bundle)
    if problems:
        raise ValidationError("refusing to export an invalid bundle:\n  "
                              + "\n  ".join(problems[:20]))
    subset = _subset_bundle(bundle, PUBLIC_SPLITS)
    for case in subset["cases"]:
        if case.get("split") not in PUBLIC_SPLITS:
            raise PrivateExportError("public export contains a private split")
    for gold in subset["gold"]:
        if gold.get("source") == "real_mail":
            raise PrivateExportError("public export cannot include real-mail gold")
    return subset


def load_dataset(path):
    """Load and validate a written bundle; raise on any validation error."""
    bundle_path = os.path.join(path, BUNDLE_FILE)
    manifest_path = os.path.join(path, MANIFEST)
    if not os.path.isfile(bundle_path):
        raise ValidationError("no %s under %r" % (BUNDLE_FILE, path))
    with open(bundle_path, encoding="utf-8") as f:
        bundle = json.load(f)
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        if manifest.get("content_sha256") != hash_obj(bundle):
            raise ValidationError("bundle content does not match its manifest hash")
    problems = validate_dataset(bundle)
    if problems:
        raise ValidationError("loaded bundle is invalid:\n  "
                              + "\n  ".join(problems[:20]))
    return bundle
