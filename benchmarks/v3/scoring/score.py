"""Scoring orchestration, calibration fitting, comparison and reports (WP5).

``score_run`` reports each input profile separately and keeps every dimension
separate: category (single gold), acceptable-set accuracy, needs-reply,
confidence/calibration, format, prose, relations, workflow and deployment.
There is deliberately **no** global "quality" number that lets schema validity
or a single dimension dominate.

``compare_runs`` uses the lineage-clustered paired bootstrap from
:mod:`benchmarks.v3.scoring.stats`, recomputing each metric on every resample
and refusing to compare runs with different datasets, scopes or protocols.
"""
import json
import os

from ..common.hashing import hash_obj
from ..contracts import (DEFAULT_CATEGORIES, observable_in)
from . import calibration as calmod
from . import metrics as M
from . import stats
from . import workflows as W
from .errors import CalibrationError, ComparisonError, ProbabilityError
from .gates import qualify, qualify_comparison
from .normalize import (
    WORKFLOW_TASK, attempt_field, attempt_parsed, attempt_provenance,
    attempt_status, case_lineage, case_profile, case_relation, case_split,
    case_task, declared_fields, final_attempt, lint_dataset, normalize_dataset,
    normalize_run, observable_level, validate_scope,
)
from .policy import normalize_policy, policy_hash

REPORT_VERSION = "3.0"
COMPARISON_VERSION = "3.0"


# ------------------------------------------------------------------ helpers

def _nonempty(value):
    return isinstance(value, str) and value.strip() != ""


def _allowed_categories(ds, case):
    policy = ds["policies_by_id"].get(case.get("policy_id"))
    if policy:
        names = []
        for entry in policy.get("categories") or []:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if _nonempty(str(name or "")):
                names.append(str(name))
        if names:
            return names
    return list(DEFAULT_CATEGORIES)


def _field_state(gold, field, profile, declared):
    if not declared:
        return "not_applicable"
    level = observable_level(gold, field)
    if level is None:
        return "metadata_missing"
    if observable_in(level, profile):
        return "scored"
    return "context_limited"


def _percentage(part, whole):
    return (part / float(whole)) if whole else None


# ---------------------------------------------------------------- calibration

def calibration_binding(rn, ds, policy):
    """The immutable inference context a calibrator is valid for.

    This is the `raw run config + scorer/eval policy` identity, deliberately
    **excluding** the run_id and attempt set: a calibrator is fitted on one run
    and may be applied to another run of the same model/adapter/prompt/
    generation over the same dataset, but never across any of those.
    """
    manifest = rn["manifest"]
    return {
        "dataset_id": manifest.get("dataset_id"),
        "dataset_sha256": manifest.get("dataset_sha256"),
        "model_key": manifest.get("model_key"),
        "model_revision": manifest.get("model_revision"),
        "model_artifact_sha256": manifest.get("model_artifact_sha256"),
        "adapter_id": manifest.get("adapter_id"),
        "adapter_revision": manifest.get("adapter_revision"),
        "prompt_revision": manifest.get("prompt_revision"),
        "prompt_sha256": manifest.get("prompt_sha256"),
        "generation_config_sha256": hash_obj(manifest.get("generation_config") or {}),
        "scorer_revision": manifest.get("scorer_revision"),
        "eval_policy_sha256": policy_hash(policy),
    }


def evaluation_identity(rn, ds, policy, calibrator=None):
    """Immutable report/evaluation identity for an **applied** scoring run.

    Keeps the raw ``run_id`` (unmodified) but names a distinct evaluation: the
    raw run config + the scorer/eval policy + the applied calibrator content.
    Applying a different calibrator yields a different ``evaluation_id`` with no
    claim that the raw run itself used it.
    """
    manifest = rn["manifest"]
    raw_config = {k: manifest.get(k) for k in (
        "dataset_id", "dataset_sha256", "case_manifest_sha256", "prompt_revision",
        "prompt_sha256", "model_key", "model_revision", "model_artifact_sha256",
        "adapter_id", "adapter_revision", "scorer_revision", "policy_revision",
        "policy_sha256", "engine_contract_sha256", "calibrator_revision",
        "generation_config", "runtime_config", "requested_case_ids",
        "requested_splits", "requested_profiles", "config_hash")}
    return hash_obj({
        "raw_run_id": rn["run_id"],
        "raw_config": raw_config,
        "eval_policy_sha256": policy_hash(policy),
        "calibrator_sha256": (calibrator or {}).get("artifact_sha256"),
    })


# ---------------------------------------------------------------- one case

def _score_case(ds, case, attempt, policy, calibrator, integrity):
    cid = case["case_id"]
    gold = ds["gold_by_case"][cid]
    answer = gold.get("answer") or {}
    task = case_task(case)
    profile = case_profile(case)
    declared = declared_fields(gold, task)

    status = attempt_status(attempt)
    parsed = attempt_parsed(attempt)
    provenance = attempt_provenance(attempt)

    raw_category = attempt_field(attempt, "category")
    pred_category = raw_category if _nonempty(raw_category) else None
    raw_reply = attempt_field(attempt, "needs_reply")
    pred_reply = raw_reply if isinstance(raw_reply, bool) else None
    allowed = _allowed_categories(ds, case)

    conf_field = calmod.confidence_field(policy, "category_confidence")
    confidence = None
    raw_conf = attempt_field(attempt, conf_field)
    if raw_conf is not None:
        try:
            confidence = calmod.validate_probability(raw_conf, policy,
                                                     "category_confidence")
        except ProbabilityError as exc:
            integrity.append("case %s: %s" % (cid, exc))
    if calibrator is not None:
        confidence = calmod.apply_calibrator(calibrator, confidence)

    states = {
        "category": _field_state(gold, "category", profile, "category" in declared),
        "needs_reply": _field_state(gold, "needs_reply", profile,
                                    "needs_reply" in declared),
        "workflow": _field_state(gold, "workflow", profile, "workflow" in declared),
    }

    gold_category = answer.get("category")
    acceptable = list(answer.get("acceptable_categories") or [])
    category_correct = None
    if states["category"] == "scored" and gold_category is not None:
        category_correct = pred_category is not None and pred_category == gold_category

    gold_reply = answer.get("needs_reply")
    reply_scored = states["needs_reply"] == "scored" and isinstance(gold_reply, bool)

    result = {
        "case_id": cid,
        "lineage_id": case_lineage(case),
        "profile": profile,
        "task": task,
        "split": case_split(case),
        "status": status,
        "states": states,
        "category": {
            "declared": "category" in declared,
            "scored": states["category"] == "scored",
            "gold": gold_category,
            "pred": pred_category,
            "correct": category_correct,
            "acceptable": acceptable,
            "acceptable_correct": (pred_category in acceptable)
            if (acceptable and states["category"] == "scored") else None,
        },
        "needs_reply": {
            "declared": "needs_reply" in declared,
            "scored": reply_scored,
            "gold": gold_reply if isinstance(gold_reply, bool) else None,
            "pred": pred_reply,
        },
        "confidence": confidence,
        "format": {
            "status": status,
            "parse_ok": status == "ok" and isinstance(parsed, dict) and bool(parsed),
            "category_enum_ok": pred_category is None or pred_category in allowed,
            "needs_reply_type_ok": raw_reply is None or isinstance(raw_reply, bool),
        },
        "prose": None,
        "workflow": None,
        "relation": case_relation(case),
    }

    if task == "full_response":
        result["prose"] = _score_prose(provenance, parsed)
    if task == WORKFLOW_TASK or states["workflow"] == "scored":
        recipient = ds["policies_by_id"].get(case.get("policy_id"))
        result["workflow"] = W.score_workflow(case, gold, attempt, recipient)
    return result


def _score_prose(provenance, parsed):
    """Full-response completeness; never a token-overlap semantic score."""
    problems = []
    fabricated = False
    for field in ("summary", "reason"):
        prov = provenance.get(field)
        value = parsed.get(field)
        if prov not in ("produced", "derived"):
            problems.append("%s missing" % field)
        elif not (_nonempty(value) if isinstance(value, str) else bool(value)):
            problems.append("%s claimed but empty" % field)
            fabricated = True
    return {"complete": not problems, "problems": problems,
            "fabricated": fabricated}


# ------------------------------------------------------------- aggregation

def _collect(results):
    clusters = {}
    for r in results:
        clusters.setdefault(r["lineage_id"], []).append(r)
    return clusters


def _category_pairs(results):
    return [(r["category"]["gold"], r["category"]["pred"])
            for r in results
            if r["category"]["scored"] and r["category"]["gold"] is not None]


def _acceptable_items(results):
    return [(r["category"]["pred"], r["category"]["acceptable"])
            for r in results
            if r["category"]["scored"] and r["category"]["acceptable"]]


def _reply_pairs(results):
    return [(r["needs_reply"]["gold"], r["needs_reply"]["pred"])
            for r in results if r["needs_reply"]["scored"]]


def _relation_metrics(results, parent_of, dataset_ids=None, requested_ids=None):
    """Relation pairs within the scored scope.

    A variant whose parent is **in the dataset but outside the requested run
    scope** is *not evaluated* (reported as relation coverage), never an
    integrity failure -- so a profile-limited run is not failed for a parent it
    never asked for.  A parent that is missing from the dataset, or that was
    requested but has no attempt, is still a real problem.
    """
    stable_total = stable_preserved = 0
    change_total = change_changed = 0
    pairs = 0
    problems = []
    not_evaluated = []
    by_id = {r["case_id"]: r for r in results}
    dataset_ids = set(dataset_ids) if dataset_ids is not None else set(by_id)
    requested_ids = set(requested_ids or ())
    for r in results:
        relation = r["relation"] or {}
        rtype = relation.get("relation_type")
        if rtype in (None, "root"):
            continue
        parent_id = parent_of.get(r["case_id"])
        if not parent_id:
            not_evaluated.append({"case_id": r["case_id"], "relation_type": rtype,
                                  "reason": "no derived parent"})
            continue
        if parent_id not in dataset_ids:
            problems.append("case %s: relation parent %r is not in the dataset"
                            % (r["case_id"], parent_id))
            continue
        parent = by_id.get(parent_id)
        if parent is None or parent.get("status") == "missing":
            if parent_id in requested_ids:
                problems.append(
                    "case %s: relation parent %r was requested but has no attempt"
                    % (r["case_id"], parent_id))
            else:
                not_evaluated.append({
                    "case_id": r["case_id"], "parent_case_id": parent_id,
                    "relation_type": rtype,
                    "reason": "parent is outside the requested scope"})
            continue
        pairs += 1
        for field in relation.get("stable_fields") or []:
            stable_total += 1
            a = _field_pred(parent, field)
            b = _field_pred(r, field)
            if a is not None and b is not None and a == b:
                stable_preserved += 1
        for field in relation.get("changing_fields") or []:
            change_total += 1
            a = _field_pred(parent, field)
            b = _field_pred(r, field)
            if a is not None and b is not None and a != b:
                change_changed += 1
    out = {
        "pairs": pairs,
        "invariance": {"fields": stable_total, "preserved": stable_preserved,
                       "rate": _percentage(stable_preserved, stable_total)},
        "counterfactual": {"fields": change_total, "changed": change_changed,
                           "rate": _percentage(change_changed, change_total)},
        "not_evaluated": not_evaluated,
    }
    return out, problems


def _field_pred(result, field):
    if field == "category":
        return result["category"]["pred"]
    if field == "needs_reply":
        return result["needs_reply"]["pred"]
    return None


def _aggregate(results, policy, calibrator):
    category_pairs = _category_pairs(results)
    acceptable = _acceptable_items(results)
    reply_pairs = _reply_pairs(results)

    coverage = {
        "cases": len(results),
        "scored_categories": sum(1 for r in results if r["category"]["scored"]),
        "context_limited": sum(1 for r in results
                               if "context_limited" in r["states"].values()),
        "metadata_missing": sum(1 for r in results
                                if "metadata_missing" in r["states"].values()),
        "errored": sum(1 for r in results if r["status"] in ("error", "timeout")),
        "missing": sum(1 for r in results if r["status"] == "missing"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
    }
    observable_decisions = 0
    produced_decisions = 0
    abstained = 0
    for r in results:
        for field in ("category", "needs_reply", "workflow"):
            if r["states"].get(field) != "scored":
                continue
            observable_decisions += 1
            if field == "category":
                produced = r["category"]["pred"] is not None
            elif field == "needs_reply":
                produced = r["needs_reply"]["pred"] is not None
            else:
                produced = (r["workflow"] or {}).get("status") == "ok"
            if produced:
                produced_decisions += 1
            else:
                abstained += 1
    coverage.update({
        "observable": observable_decisions,
        "produced": produced_decisions,
        "abstained": abstained,
    })

    category = {
        "n": len(category_pairs),
        "accuracy": M.accuracy(category_pairs),
        "macro_f1": M.macro_f1(category_pairs),
        "per_category": M.per_category(category_pairs),
        "acceptable_n": len(acceptable),
        "acceptable_accuracy": M.acceptable_accuracy(acceptable),
    }
    reply = M.binary_metrics(reply_pairs)
    reply["n"] = len(reply_pairs)

    fmt = {"ok": 0, "error": 0, "timeout": 0, "missing": 0, "skipped": 0,
           "parse_failures": 0, "enum_failures": 0, "reply_type_failures": 0}
    for r in results:
        status = r["format"]["status"]
        if status in fmt:
            fmt[status] += 1
        if not r["format"]["parse_ok"] and status not in ("missing", "skipped"):
            fmt["parse_failures"] += 1
        if not r["format"]["category_enum_ok"]:
            fmt["enum_failures"] += 1
        if not r["format"]["needs_reply_type_ok"]:
            fmt["reply_type_failures"] += 1

    records = [{"confidence": r["confidence"],
                "correct": r["category"]["correct"],
                "case_id": r["case_id"]}
               for r in results
               if r["category"]["scored"] and r["category"]["correct"] is not None]
    threshold = (calibrator or {}).get("threshold") \
        if calibrator else policy["calibration"]["coverage_threshold"]
    confidence = {
        "records": len(records),
        "reliability": calmod.reliability(records, policy["calibration"]["bins"]),
        "selective": calmod.selective_risk(records, threshold),
        "calibrated": calibrator is not None,
        "calibrator_revision": (calibrator or {}).get("revision"),
        "calibrator_sha256": (calibrator or {}).get("artifact_sha256"),
    }

    prose = {"cases": 0, "complete": 0, "missing": 0, "fabricated": 0,
             "review": {"status": "pending_human_adjudication",
                        "summary_factuality": None, "reason_consistency": None}}
    for r in results:
        if r["prose"] is None:
            continue
        prose["cases"] += 1
        if r["prose"]["complete"]:
            prose["complete"] += 1
        else:
            prose["missing"] += 1
        if r["prose"]["fabricated"]:
            prose["fabricated"] += 1

    wf_cases = [r for r in results if r["workflow"] is not None]
    workflow = None
    if wf_cases:
        completed = sum(1 for r in wf_cases if r["workflow"]["task_complete"])
        safety = sum(r["workflow"]["unauthorized_mutations"]
                     + len(r["workflow"]["forbidden_violations"])
                     for r in wf_cases)
        # A pending approval is safe progress, NOT a compliance violation; only
        # an attempt on an ``off`` capability is a compliance failure.
        compliance = sum(r["workflow"]["attempted_disabled"] for r in wf_cases)
        pending = sum(r["workflow"].get("approval_pending", 0) for r in wf_cases)
        safe_progress = sum(1 for r in wf_cases if r["workflow"].get("safe_progress"))
        claimed = sum(1 for r in wf_cases if r["workflow"].get("claimed_outcomes"))
        grounded = [r for r in wf_cases if r["workflow"]["grounded_ok"] is not None]
        workflow = {
            "cases": len(wf_cases),
            "task_complete": completed,
            "task_completion_rate": _percentage(completed, len(wf_cases)),
            "safety_violations": safety,
            "compliance_violations": compliance,
            "approval_pending": pending,
            "safe_progress_cases": safe_progress,
            "claimed_complete_cases": claimed,
            "grounded_cases": len(grounded),
            "grounding_rate": _percentage(
                sum(1 for r in grounded if r["workflow"]["grounded_ok"]),
                len(grounded)),
            "required_missing": sum(len(r["workflow"]["required_missing"])
                                    for r in wf_cases),
            "assertions_total": sum(r["workflow"].get("assertions_total", 0)
                                    for r in wf_cases),
            "assertions_passed": sum(r["workflow"].get("assertions_passed", 0)
                                     for r in wf_cases),
            "assertion_failures": sum(len(r["workflow"].get("assertion_failures") or [])
                                      for r in wf_cases),
            "unsupported_assertions": sorted(
                {kind for r in wf_cases
                 for kind in (r["workflow"].get("unsupported_assertions") or [])}),
        }

    loss = {
        "category": (1.0 - category["accuracy"])
        if category["accuracy"] is not None else None,
        "reply": (1.0 - reply["f1"]) if reply["denominator"] else None,
        "abstention_rate": _percentage(abstained, observable_decisions),
    }
    return {
        "cases": len(results),
        "coverage": coverage,
        "category": category,
        "needs_reply": reply,
        "loss": loss,
        "format": fmt,
        "confidence": confidence,
        "relations": None,
        "workflow": workflow,
        "full_response": prose,
        "prose_review": prose["review"],
    }


# ---------------------------------------------------------------- lineage

def _lineage_parents(ds):
    parent = {}
    # An explicit ``relation.parent_case_id`` always wins.
    for case in ds["cases"]:
        rel = case.get("relation") or {}
        pid = rel.get("parent_case_id")
        if pid:
            parent[case["case_id"]] = pid
    for group in ds["lineage_by_id"].values():
        members = [m for m in (group.get("members") or [])
                   if m in ds["case_by_id"]]
        if not members:
            continue
        root = _pick_root(ds, members, group.get("root_id"))
        for member in members:
            if member != root and member not in parent:
                parent[member] = root
    by_lineage = {}
    for case in ds["cases"]:
        by_lineage.setdefault(case_lineage(case), []).append(case["case_id"])
    for lineage_id, members in by_lineage.items():
        if lineage_id in ds["lineage_by_id"]:
            continue
        root = _pick_root(ds, members, None)
        for member in members:
            if member != root and member not in parent:
                parent[member] = root
    return parent


def _pick_root(ds, members, declared_root):
    for member in members:
        relation = ds["case_by_id"][member].get("relation") or {}
        if relation.get("relation_type") == "root":
            return member
    if declared_root:
        for member in members:
            if ds["case_by_id"][member].get("scenario_id") == declared_root:
                return member
    return members[0]


# ------------------------------------------------------------- evaluate

def _evaluate(dataset, run, policy, calibrator=None):
    ds = normalize_dataset(dataset)
    rn = normalize_run(run)
    integrity = list(validate_scope(ds, rn))
    parent_of = _lineage_parents(ds)

    manifest = rn["manifest"]
    requested_cases = [c for c in (manifest.get("requested_case_ids") or [])
                       if c in ds["case_by_id"]]
    if not requested_cases:
        requested_cases = sorted(rn["attempts_by_case"])
    # Lint only the requested scope: an unrequested profile must not disqualify
    # a run that never claimed it.
    lint = lint_dataset(ds, requested_cases)

    results = []
    for cid in requested_cases:
        case = ds["case_by_id"][cid]
        attempt = final_attempt(rn["attempts_by_case"].get(cid))
        results.append(_score_case(ds, case, attempt, policy, calibrator,
                                   integrity))

    profiles = {}
    for result in results:
        profiles.setdefault(result["profile"], []).append(result)
    agg = {name: _aggregate(group, policy, calibrator)
           for name, group in profiles.items()}

    all_relation, relation_problems = _relation_metrics(
        results, parent_of, set(ds["case_by_id"]), requested_cases)
    integrity.extend(relation_problems)
    for name, group in profiles.items():
        relations, _ = _relation_metrics(
            group, parent_of, set(ds["case_by_id"]), requested_cases)
        agg[name]["relations"] = relations

    requested = list(manifest.get("requested_case_ids") or [])
    present = [c for c in requested if c in rn["attempts_by_case"]]
    scope = {
        "requested_case_ids": requested,
        "requested_profiles": list(manifest.get("requested_profiles") or []),
        "requested_splits": list(manifest.get("requested_splits") or []),
        "completed_cases": len(present),
        "coverage": _percentage(len(present), len(requested)),
        "n_roots": len({r["lineage_id"] for r in results}),
        "n_cases": len(results),
        "complete": bool(requested) and len(present) == len(requested)
        and not integrity,
        "is_test": bool(set(scope_splits(manifest))
                        & set(policy["splits"]["test"])),
        "is_development": bool(set(scope_splits(manifest))
                               & set(policy["splits"]["development"]))
        or not bool(set(scope_splits(manifest)) & set(policy["splits"]["test"])),
    }

    golds = [ds["gold_by_case"][c] for c in requested_cases]
    has_real = any(g.get("source") == "real_mail" for g in golds)
    real_authorized = all(g.get("authorized") for g in golds
                          if g.get("source") == "real_mail") if has_real else True
    dataset_info = {
        "dataset_id": ds.get("dataset_id"),
        "review_status": ds["metadata"].get("review_status") or "draft",
        "has_real_mail": has_real,
        "real_mail_authorized": real_authorized,
        "lint_problems": lint,
        "splits": sorted({result["split"] for result in results}),
    }
    deployment = _deployment(rn)

    report = {
        "report_version": REPORT_VERSION,
        "scorer_revision": manifest.get("scorer_revision"),
        "dataset_id": ds.get("dataset_id"),
        "run_id": rn["run_id"],
        "manifest": rn["manifest"],
        "scoring_policy": {"revision": policy.get("revision"),
                           "policy_id": policy.get("policy_id"),
                           "sha256": policy_hash(policy)},
        "bootstrap": dict(policy["bootstrap"]),
        "scope": scope,
        "integrity": {"ok": not integrity, "problems": integrity},
        "dataset": dataset_info,
        "profiles": agg,
        "relations_overall": all_relation,
        "relations_not_evaluated": all_relation.get("not_evaluated") or [],
        "deployment": deployment,
        "dimensions_note": (
            "category and acceptable-set accuracy are single-gold vs "
            "acceptable-set; needs_reply is binary; format, prose, calibration, "
            "relations and workflow are reported separately and never pooled "
            "into one quality score"),
    }
    report["gates"] = qualify(report, policy)
    return {"report": report, "cases": {r["case_id"]: r for r in results},
            "profiles": profiles}


def scope_splits(manifest):
    return list(manifest.get("requested_splits") or [])


def _deployment(rn):
    meta = rn["metadata"] or {}
    deployment = meta.get("deployment")
    if not isinstance(deployment, dict):
        return {"state": "not_measured", "metrics": {},
                "hardware_receipt": None}
    return deployment


# ------------------------------------------------------------- public API

def score_run(dataset, run, policy=None, calibrator=None):
    """Score one run against one dataset; returns the report mapping.

    A supplied calibrator is verified against the run's immutable inference
    binding before it is applied; a stale, tampered or mismatched artifact is
    refused.  The report keeps the raw ``run_id`` and adds a distinct
    ``evaluation_id`` that folds in the applied calibrator content.
    """
    resolved = normalize_policy(policy)
    ds = normalize_dataset(dataset)
    rn = normalize_run(run)
    if calibrator is not None:
        calmod.verify_calibrator(
            calibrator, expected_binding=calibration_binding(rn, ds, resolved))
    report = _evaluate(dataset, run, resolved, calibrator)["report"]
    report["raw_run_id"] = rn["run_id"]
    report["evaluation_id"] = evaluation_identity(rn, ds, resolved, calibrator)
    report["calibrator"] = {
        "applied": calibrator is not None,
        "revision": (calibrator or {}).get("revision"),
        "artifact_sha256": (calibrator or {}).get("artifact_sha256"),
        "binding": (calibrator or {}).get("binding"),
        "binding_sha256": (calibrator or {}).get("binding_sha256"),
    }
    return report


def fit_calibrator(dataset, run, policy=None, split="calibration"):
    """Fit a frozen calibrator from the calibration split only.

    The artifact is bound to the immutable inference context (dataset/model/
    adapter/prompt/generation/scorer/eval-policy), so it cannot later be applied
    to a different run identity.
    """
    resolved = normalize_policy(policy)
    declared = resolved["calibration"]["split"]
    if split != declared:
        raise CalibrationError("calibrator may only be fitted from split %r, not %r"
                               % (declared, split))
    ds = normalize_dataset(dataset)
    rn = normalize_run(run)
    source_ids = []
    records = []
    evaluated = _evaluate(dataset, run, resolved, None)
    for cid, result in evaluated["cases"].items():
        case = ds["case_by_id"][cid]
        if case_split(case) != split:
            continue
        gold = ds["gold_by_case"][cid]
        if gold.get("source") == "real_mail" and not gold.get("authorized"):
            raise CalibrationError("cannot fit a calibrator on unauthorized "
                                   "real-mail material (case %s)" % cid)
        if result["confidence"] is None or result["category"]["correct"] is None:
            continue
        source_ids.append(cid)
        records.append({"confidence": result["confidence"],
                        "correct": result["category"]["correct"],
                        "case_id": cid})
    return calmod.build_calibrator(
        records, resolved, source_ids, fit_split=split,
        binding=calibration_binding(rn, ds, resolved))


def _ensure_comparable(base, cand, policy):
    problems = []
    for field in ("dataset_id", "dataset_sha256", "scorer_revision"):
        if base["manifest"].get(field) != cand["manifest"].get(field):
            problems.append("%s differs (%r vs %r)"
                            % (field, base["manifest"].get(field),
                               cand["manifest"].get(field)))
    for field in ("requested_case_ids", "requested_profiles", "requested_splits"):
        if set(base["manifest"].get(field) or []) \
                != set(cand["manifest"].get(field) or []):
            problems.append("%s differs" % field)
    if problems:
        raise ComparisonError("runs are not comparable: " + "; ".join(problems))


def _run_meta(rn):
    """The eligibility-relevant metadata of one run (fail-closed on absence)."""
    manifest = rn["manifest"]
    meta = rn["metadata"] or {}
    mock = meta.get("mock")
    if mock is None:
        mock = manifest.get("mock")
    source = meta.get("model_identity_source")
    if source is None:
        source = manifest.get("model_identity_source")
    if source is None:
        rev = (manifest.get("model_revision") or "").strip()
        art = (manifest.get("model_artifact_sha256") or "").strip()
        source = "pinned" if (art or (rev and rev not in
                                      ("installed", "unverified", "unknown"))) \
            else "unverified"
    cpu = meta.get("cpu_qualification") or {}
    return {
        "run_id": rn["run_id"],
        "mock": mock,
        "model_identity_source": source,
        "qualifies_as_baseline": (meta.get("qualifies_as_baseline")
                                  if meta.get("qualifies_as_baseline") is not None
                                  else manifest.get("qualifies_as_baseline")),
        "dataset_review_status": meta.get("dataset_review_status"),
        "cpu_qualified": cpu.get("cpu_qualified"),
    }


def compare_runs(dataset, baseline_run, candidate_run, policy=None):
    """Paired, lineage-clustered comparison of candidate vs baseline."""
    resolved = normalize_policy(policy)
    ds = normalize_dataset(dataset)
    base = normalize_run(baseline_run)
    cand = normalize_run(candidate_run)
    _ensure_comparable(base, cand, resolved)

    base_eval = _evaluate(dataset, baseline_run, resolved)
    cand_eval = _evaluate(dataset, candidate_run, resolved)

    shared_profiles = sorted(set(base_eval["profiles"]) & set(cand_eval["profiles"]))
    profiles = {}
    shared_ids = set(base_eval["cases"]) & set(cand_eval["cases"])
    roots = {base_eval["cases"][cid]["lineage_id"] for cid in shared_ids}
    for profile in shared_profiles:
        base_cases = base_eval["profiles"][profile]
        cand_cases = cand_eval["profiles"][profile]
        profiles[profile] = {
            "category_macro_f1": _paired_metric(
                base_cases, cand_cases, M.macro_f1, resolved, profile,
                "category_macro_f1"),
            "category_accuracy": _paired_metric(
                base_cases, cand_cases, M.accuracy, resolved, profile,
                "category_accuracy"),
            "reply_f1": _paired_metric(
                base_cases, cand_cases,
                lambda pairs: M.binary_metrics(pairs)["f1"], resolved, profile,
                "reply_f1"),
        }
    requested = set(base["manifest"].get("requested_case_ids") or [])
    requested_splits = set(base["manifest"].get("requested_splits") or [])
    scope = {
        "requested_profiles": sorted(shared_profiles),
        "requested_cases": len(requested),
        "completed_cases": len(shared_ids),
        "shared_cases": len(shared_ids),
        "n_roots": len(roots),
        "complete": bool(shared_ids)
        and len(shared_ids) == len(requested)
        and base_eval["report"]["scope"]["complete"]
        and cand_eval["report"]["scope"]["complete"],
        "is_test": bool(requested_splits & set(resolved["splits"]["test"])),
        "is_development": bool(requested_splits
                               & set(resolved["splits"]["development"])),
    }
    comparison = {
        "comparison_version": COMPARISON_VERSION,
        "dataset_id": ds.get("dataset_id"),
        "baseline_run_id": base["run_id"],
        "candidate_run_id": cand["run_id"],
        "scoring_policy": resolved.get("policy_id"),
        "protocol": dict(resolved["bootstrap"]),
        "scope": scope,
        "profiles": profiles,
        "runs": {"baseline": _run_meta(base), "candidate": _run_meta(cand)},
        "dataset": {
            "review_status": ds["metadata"].get("review_status") or "draft",
            "has_real_mail": base_eval["report"]["dataset"].get("has_real_mail"),
            "real_mail_authorized":
                base_eval["report"]["dataset"].get("real_mail_authorized"),
        },
    }
    comparison["eligibility"] = qualify_comparison(comparison, resolved)

    # A qualified NI/ranking claim is only allowed when the gates pass; the raw
    # statistics remain available (descriptive) for exploratory reading.
    if comparison["eligibility"]["estimable"] \
            and not comparison["eligibility"]["quality_eligible"]:
        for metrics in profiles.values():
            for metric in metrics.values():
                if isinstance(metric, dict) and "noninferior" in metric:
                    metric["statistical_noninferior"] = metric.get("noninferior")
                    metric["statistical_verdict"] = metric.get("verdict")
                    metric["noninferior"] = False
                    metric["verdict"] = "descriptive_not_qualified"
    return comparison


def _paired_metric(base_cases, cand_cases, metric_fn, policy, profile, metric):
    key = metric
    base_clusters = {}
    cand_clusters = {}
    for result in base_cases:
        item = _metric_item(result, key)
        if item is not None:
            base_clusters.setdefault(result["lineage_id"], []).append(item)
    for result in cand_cases:
        item = _metric_item(result, key)
        if item is not None:
            cand_clusters.setdefault(result["lineage_id"], []).append(item)
    return stats.paired_bootstrap(base_clusters, cand_clusters, metric_fn,
                                  policy, seed_parts=(profile, metric))


def _metric_item(result, metric):
    if metric == "reply_f1":
        if result["needs_reply"]["scored"]:
            return (result["needs_reply"]["gold"], result["needs_reply"]["pred"])
        return None
    if result["category"]["scored"] and result["category"]["gold"] is not None:
        return (result["category"]["gold"], result["category"]["pred"])
    return None


# --------------------------------------------------------------- reports

def _render_comparison(comparison):
    lines = ["# Benchmark v3 run comparison", ""]
    lines.append("- dataset: `%s`" % comparison.get("dataset_id"))
    lines.append("- baseline: `%s`" % comparison.get("baseline_run_id"))
    lines.append("- candidate: `%s`" % comparison.get("candidate_run_id"))
    lines.append("- scoring policy: `%s`"
                 % comparison.get("scoring_policy"))
    boot = comparison.get("protocol") or {}
    lines.append("- bootstrap: B=%s seed=%s alpha=%s margin=%s"
                 % (boot.get("B"), boot.get("seed"), boot.get("alpha"),
                    boot.get("margin")))
    scope = comparison.get("scope") or {}
    lines.append("- scope: shared=%s/%s complete=%s roots=%s test=%s"
                 % (scope.get("shared_cases"), scope.get("requested_cases"),
                    scope.get("complete"), scope.get("n_roots"),
                    scope.get("is_test")))
    elig = comparison.get("eligibility") or {}
    lines.append("- comparison state: %s (quality_eligible=%s estimable=%s "
                 "test_eligible=%s deployment_eligible=%s)"
                 % (elig.get("state"), elig.get("quality_eligible"),
                    elig.get("estimable"), elig.get("test_eligible"),
                    elig.get("deployment_eligible")))
    for dimension in ("quality", "test", "deployment"):
        reasons = (elig.get("reasons_by_dimension") or {}).get(dimension) or []
        lines.append("  - %s: %s" % (dimension, "ok" if not reasons
                                     else "; ".join(reasons)))
    runs = comparison.get("runs") or {}
    for role in ("baseline", "candidate"):
        meta = runs.get(role) or {}
        lines.append("  - %s run: mock=%s model_identity=%s cpu_qualified=%s"
                     % (role, meta.get("mock"), meta.get("model_identity_source"),
                        meta.get("cpu_qualified")))
    lines.append("")
    for profile, metrics in sorted((comparison.get("profiles") or {}).items()):
        lines.append("## profile `%s`" % profile)
        for name, metric in sorted(metrics.items()):
            if name == "eligibility" or not isinstance(metric, dict):
                continue
            lines.append("- %s: diff=%s ci=[%s, %s] verdict=%s"
                         % (name, _fmt(metric.get("diff")),
                            _fmt(metric.get("ci_low")), _fmt(metric.get("ci_high")),
                            metric.get("verdict")))
        lines.append("")
    return "\n".join(lines)


def render_markdown(report):
    """Render a truthful Markdown report (absent states are named)."""
    if "comparison_version" in report:
        return _render_comparison(report)
    lines = []
    lines.append("# Benchmark v3 scoring report")
    lines.append("")
    lines.append("- dataset: `%s`" % report.get("dataset_id"))
    lines.append("- run: `%s`" % report.get("run_id"))
    lines.append("- scorer revision: `%s`" % report.get("scorer_revision"))
    lines.append("- scoring policy: `%s` (sha256 `%s`)"
                 % (report["scoring_policy"].get("policy_id"),
                    (report["scoring_policy"].get("sha256") or "")[:12]))
    boot = report.get("bootstrap") or {}
    lines.append("- bootstrap: B=%s seed=%s alpha=%s margin=%s"
                 % (boot.get("B"), boot.get("seed"), boot.get("alpha"),
                    boot.get("margin")))
    scope = report.get("scope") or {}
    lines.append("- scope: %s/%s cases complete=%s roots=%s"
                 % (scope.get("completed_cases"),
                    len(scope.get("requested_case_ids") or []),
                    scope.get("complete"), scope.get("n_roots")))
    lines.append("- integrity: %s" % ("ok" if report["integrity"]["ok"] else "FAILED"))
    for problem in report["integrity"]["problems"]:
        lines.append("  - %s" % problem)
    lines.append("- dataset review: %s; lint problems: %d"
                 % (report["dataset"].get("review_status"),
                    len(report["dataset"].get("lint_problems") or [])))
    lines.append("")

    for profile, prof in sorted(report["profiles"].items()):
        lines.append("## profile `%s`" % profile)
        cov = prof["coverage"]
        lines.append("- coverage: produced=%s/%s abstained=%s context-limited=%s "
                     "metadata-missing=%s errored=%s missing=%s"
                     % (cov["produced"], cov["observable"], cov["abstained"],
                        cov["context_limited"], cov["metadata_missing"],
                        cov["errored"], cov["missing"]))
        cat = prof["category"]
        lines.append("- category: n=%s accuracy=%s macro-F1=%s acceptable-set=%s"
                     % (cat["n"], _fmt(cat["accuracy"]), _fmt(cat["macro_f1"]),
                        _fmt(cat["acceptable_accuracy"])))
        reply = prof["needs_reply"]
        lines.append("- reply: n=%s P=%s R=%s F1=%s missed-reply=%s"
                     % (reply["n"], _fmt(reply["precision"]), _fmt(reply["recall"]),
                        _fmt(reply["f1"]), _fmt(reply["missed_reply_rate"])))
        fmt = prof["format"]
        lines.append("- format (separate, never pooled): ok=%s error=%s timeout=%s "
                     "missing=%s skipped=%s parse-failures=%s enum-failures=%s"
                     % (fmt["ok"], fmt["error"], fmt["timeout"], fmt["missing"],
                        fmt["skipped"], fmt["parse_failures"], fmt["enum_failures"]))
        conf = prof["confidence"]["reliability"]
        sel = prof["confidence"]["selective"]
        lines.append("- confidence: n=%s ECE=%s; selective threshold=%s coverage=%s "
                     "accepted-error=%s (calibrated=%s)"
                     % (conf.get("n"), _fmt(conf.get("ece")), sel.get("threshold"),
                        _fmt(sel.get("coverage")), _fmt(sel.get("accepted_error_rate")),
                        prof["confidence"]["calibrated"]))
        fr = prof["full_response"]
        lines.append("- full-response: complete=%s/%s missing=%s fabricated=%s; "
                     "prose review=%s"
                     % (fr["complete"], fr["cases"], fr["missing"], fr["fabricated"],
                        fr["review"]["status"]))
        rel = prof["relations"] or {}
        lines.append("- relations: pairs=%s invariance=%s counterfactual=%s"
                     % (rel.get("pairs"), _fmt((rel.get("invariance") or {}).get("rate")),
                        _fmt((rel.get("counterfactual") or {}).get("rate"))))
        wf = prof.get("workflow")
        if wf:
            lines.append("- workflow: cases=%s complete=%s safety-violations=%s "
                         "compliance-violations=%s grounding=%s"
                         % (wf["cases"], _fmt(wf["task_completion_rate"]),
                            wf["safety_violations"], wf["compliance_violations"],
                            _fmt(wf["grounding_rate"])))
        gate = report["gates"]["profiles"].get(profile) or {}
        lines.append("- eligibility: %s" % ("ELIGIBLE" if gate.get("eligible")
                                             else "INELIGIBLE"))
        for reason in gate.get("reasons") or []:
            lines.append("  - %s" % reason)
        lines.append("")
    lines.append("## qualification")
    lines.append("- final test qualified: %s" % report["gates"]["final_test_qualified"])
    lines.append("- exploratory development: %s"
                 % report["gates"]["exploratory_development"])
    lines.append("- cpu qualified: %s (%s)"
                 % (report["gates"]["deployment"]["cpu_qualified"],
                    "; ".join(report["gates"]["deployment"]["reasons"]) or "ok"))
    lines.append("")
    return "\n".join(lines)


def _fmt(value):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return "%.3f" % value
    return str(value)


def write_report(report, path):
    """Write JSON (default) or Markdown (`.md` suffix); returns the path."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if str(path).endswith(".md"):
        with open(path, "w") as handle:
            handle.write(render_markdown(report))
    else:
        with open(path, "w") as handle:
            json.dump(report, handle, indent=1, sort_keys=True, default=str)
    return path
