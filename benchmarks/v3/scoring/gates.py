"""Executable qualification gates (WP5, FR8).

A failed gate marks a run **ineligible** with an explicit reason list; it is
never reduced to a prose warning.  Gates are evaluated per input profile so a
strong native result cannot mask a failed workflow or a failed safety gate.

The rules that matter most:

* an always-deferring system has zero coverage and fails the coverage gate;
* draft/unreviewed data may produce exploratory development metrics but can
  never be ``final_test_qualified``;
* real-mail material without explicit authorization fails;
* CPU qualification requires a verified hardware receipt -- configured
  thread/memory numbers in a config file are not a receipt.
"""


def _eligible(reasons):
    return not reasons


def _profile_reasons(report, profile, prof, policy):
    gates = policy["gates"]
    reasons = []
    if not report["integrity"]["ok"]:
        reasons.append("run integrity problems: %s"
                       % "; ".join(report["integrity"]["problems"])[:200])
    lint = report["dataset"].get("lint_problems") or []
    if lint:
        reasons.append("dataset lint: %d problem(s)" % len(lint))

    cov = prof["coverage"]
    observable = cov["observable"]
    if observable == 0:
        reasons.append("no observable decisions")
    else:
        rate = cov["produced"] / float(observable)
        if rate < gates["min_coverage"]:
            reasons.append("coverage %.3f below minimum %.3f"
                           % (rate, gates["min_coverage"]))

    workflow = prof.get("workflow") or {}
    is_workflow = profile == "workflow" and bool(workflow.get("cases"))
    if is_workflow:
        completion = workflow.get("task_completion_rate")
        if completion is not None and completion < gates["min_workflow_completion"]:
            reasons.append("workflow completion %.3f below %.3f"
                           % (completion, gates["min_workflow_completion"]))
        if workflow.get("safety_violations"):
            reasons.append("workflow safety violations: %d"
                           % workflow["safety_violations"])
        if workflow.get("compliance_violations"):
            reasons.append("workflow compliance violations: %d"
                           % workflow["compliance_violations"])
        unsupported = workflow.get("unsupported_assertions") or []
        if unsupported:
            reasons.append("unsupported workflow assertions: %s"
                           % ", ".join(sorted(unsupported)))
    else:
        cat = prof.get("category") or {}
        if cat.get("n"):
            if cat.get("macro_f1") is not None \
                    and cat["macro_f1"] < gates["min_category_macro_f1"]:
                reasons.append("category macro-F1 %.3f below %.3f"
                               % (cat["macro_f1"], gates["min_category_macro_f1"]))
        else:
            reasons.append("no single-label category gold")

        reply = prof.get("needs_reply") or {}
        if reply.get("denominator"):
            if reply["f1"] < gates["min_reply_f1"]:
                reasons.append("reply F1 %.3f below %.3f"
                               % (reply["f1"], gates["min_reply_f1"]))
            if reply.get("missed_reply_rate") is not None \
                    and reply["missed_reply_rate"] > gates["max_missed_reply"]:
                reasons.append("missed-reply rate %.3f above %.3f"
                               % (reply["missed_reply_rate"], gates["max_missed_reply"]))
        else:
            reasons.append("no reply gold")

    if not report["scope"]["complete"]:
        reasons.append("requested scope incomplete")
    requested_cases = report["scope"].get("requested_case_ids") or []
    if len(requested_cases) < gates["min_cases"]:
        reasons.append("fewer than %d requested cases" % gates["min_cases"])

    dataset = report["dataset"]
    if report["scope"]["is_test"]:
        if gates["require_reviewed_for_test"] \
                and dataset.get("review_status") not in ("reviewed", "sealed"):
            reasons.append("test scope requires reviewed/sealed data "
                           "(review_status=%r)" % dataset.get("review_status"))
        if dataset.get("has_real_mail") and not dataset.get("real_mail_authorized") \
                and gates["require_authorization_for_real_mail"]:
            reasons.append("real-mail material is not authorized")
    return reasons


def qualify(report, policy):
    """Attach per-profile eligibility, final-test and deployment verdicts."""
    profiles = {}
    for profile, prof in report["profiles"].items():
        reasons = _profile_reasons(report, profile, prof, policy)
        profiles[profile] = {"eligible": _eligible(reasons), "reasons": reasons}

    scope = report["scope"]
    all_eligible = bool(profiles) and all(v["eligible"] for v in profiles.values())
    final_test_qualified = bool(scope["is_test"] and all_eligible)
    exploratory_development = bool(
        not final_test_qualified
        and (scope["is_development"] or not scope["is_test"]))

    deployment = report.get("deployment") or {}
    cpu = qualify_cpu(deployment, policy)
    return {
        "profiles": profiles,
        "final_test_qualified": final_test_qualified,
        "exploratory_development": exploratory_development,
        "deployment": cpu,
    }


def qualify_cpu(deployment, policy):
    """CPU qualification requires a verified hardware receipt, never config."""
    deployment = deployment or {}
    reasons = []
    receipt = deployment.get("hardware_receipt")
    if not policy["gates"].get("require_hardware_receipt_for_cpu"):
        return {"cpu_qualified": True, "reasons": [],
                "state": deployment.get("state", "not_required")}
    if not isinstance(receipt, dict):
        reasons.append("no hardware receipt; configured numbers are not evidence")
    else:
        if not receipt.get("verified"):
            reasons.append("hardware receipt is not verified")
        if not receipt.get("cpu_model"):
            reasons.append("hardware receipt lacks a CPU model")
        if receipt.get("shared") or receipt.get("warm_endpoint"):
            reasons.append("receipt describes shared/warm infrastructure, "
                           "not a qualified CPU")
    return {
        "cpu_qualified": _eligible(reasons),
        "reasons": reasons,
        "state": deployment.get("state", "unverified"),
    }


def _run_quality_reasons(role, meta):
    """Per-run reasons a comparison cannot make a qualified claim."""
    reasons = []
    meta = meta or {}
    if meta.get("mock") is True:
        reasons.append("%s run used a mock adapter" % role)
    elif meta.get("mock") is None:
        reasons.append("%s run mock status is unknown" % role)
    source = meta.get("model_identity_source")
    if source == "unverified":
        reasons.append("%s model identity is unverified" % role)
    elif source is None:
        reasons.append("%s model identity is unknown" % role)
    return reasons


def qualify_comparison(comparison, policy):
    """Per-dimension comparison eligibility (quality / test / deployment).

    Statistical calculations stay usable (``estimable``) but a **qualified**
    NI/ranking claim (``eligible`` / ``quality_eligible``) requires both runs to
    be real (non-mock, verified model identity) over reviewed/sealed data with a
    complete paired scope.  A GPU quality reference that is not CPU-qualified may
    still compare for *quality*; it simply cannot approve a CPU *deployment*.
    Missing or draft evidence is fail-closed, never a free pass.
    """
    scope = comparison.get("scope") or {}
    runs = comparison.get("runs") or {}
    data = comparison.get("dataset") or {}
    profiles = comparison.get("profiles") or {}

    scope_reasons = []
    if not scope.get("complete"):
        scope_reasons.append("paired scope incomplete")
    if scope.get("n_roots", 0) < int(policy["bootstrap"]["min_lineages"]):
        scope_reasons.append("too few shared lineage roots (%d < %d)"
                             % (scope.get("n_roots", 0),
                                policy["bootstrap"]["min_lineages"]))

    quality_reasons = list(scope_reasons)
    for role in ("baseline", "candidate"):
        quality_reasons.extend(_run_quality_reasons(role, runs.get(role)))
    review = data.get("review_status")
    if review not in ("reviewed", "sealed"):
        quality_reasons.append("dataset review_status is %r (not reviewed/sealed)"
                               % review)
    if data.get("has_real_mail") and not data.get("real_mail_authorized"):
        quality_reasons.append("real-mail material is not authorized")

    # Per-profile scientific eligibility propagates the same two-run gates and
    # additionally requires at least one estimable paired metric for that
    # profile.
    for profile, metrics in profiles.items():
        profile_reasons = list(quality_reasons)
        metric_list = [m for m in metrics.values() if isinstance(m, dict)]
        if not metric_list:
            profile_reasons.append("profile %r has no paired metrics" % profile)
        elif not any(m.get("estimable") for m in metric_list):
            profile_reasons.append(
                "profile %r has no estimable paired metric" % profile)
        if isinstance(metrics, dict):
            metrics["eligibility"] = {
                "quality_eligible": _eligible(profile_reasons),
                "reasons": profile_reasons,
            }

    quality_eligible = _eligible(quality_reasons)
    estimable = _eligible(scope_reasons)

    test_reasons = list(quality_reasons)
    if not scope.get("is_test"):
        test_reasons.append("scope is not a test split")
    test_eligible = _eligible(test_reasons)

    deployment_reasons = []
    candidate = runs.get("candidate") or {}
    if candidate.get("cpu_qualified") is not True:
        deployment_reasons.append(
            "candidate CPU hardware receipt is not verified/qualified")
    baseline_meta = runs.get("baseline") or {}
    if baseline_meta.get("cpu_qualified") is False:
        deployment_reasons.append("baseline is not CPU-qualified (quality "
                                  "comparison unaffected)")
    deployment_eligible = _eligible(deployment_reasons)

    state = ("qualified" if quality_eligible
             else "exploratory" if estimable else "not_estimable")
    return {
        "eligible": quality_eligible,
        "estimable": estimable,
        "quality_eligible": quality_eligible,
        "test_eligible": test_eligible,
        "deployment_eligible": deployment_eligible,
        "exploratory": bool(estimable and not quality_eligible),
        "state": state,
        # Flat list kept for report/markdown compatibility (quality reasons).
        "reasons": quality_reasons,
        "reasons_by_dimension": {
            "quality": quality_reasons,
            "test": test_reasons,
            "deployment": deployment_reasons,
        },
    }
