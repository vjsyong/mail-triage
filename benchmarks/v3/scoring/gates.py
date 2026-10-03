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

    workflow = prof.get("workflow") or {}
    if profile == "workflow" and workflow.get("cases"):
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


def qualify_comparison(comparison, policy):
    """A comparison is usable only when its scope is complete and clustered."""
    scope = comparison["scope"]
    reasons = []
    if not scope.get("complete"):
        reasons.append("paired scope incomplete")
    if scope.get("n_roots", 0) < int(policy["bootstrap"]["min_lineages"]):
        reasons.append("too few shared lineage roots (%d < %d)"
                       % (scope.get("n_roots", 0),
                          policy["bootstrap"]["min_lineages"]))
    return {"eligible": _eligible(reasons), "reasons": reasons,
            "estimable": _eligible(reasons)}
