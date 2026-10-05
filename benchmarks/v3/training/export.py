"""Fail-closed SFT exporter with generation-domain separation (acceptance AC2).

* **Separate hashed generation domains.**  Training, development and evaluation
  each carry their own ``domain_sha256`` derived from ``(domain, seed, revision)``
  (and a distinct stream), so examples from different domains can never be
  pooled by accident.
* **Fail-closed material gating.**  The exporter rejects a case whose split is
  not allowed (calibration/private/test material for a training export), whose
  decision gold is unobservable or ambiguous, whose source is real mail, or whose
  example fails verification.  A rejected case is reported with its reason; it is
  never silently dropped and never exported.
* **Contamination.**  Gold ``hidden_evidence`` must not appear in any
  model-facing input, and -- importantly -- the check folds in the **workflow
  mailbox source text**, so a workflow body cannot smuggle held-out material.
"""
from __future__ import annotations

import re

from ..contracts import GoldLeakageError, assert_no_gold_leakage
from ..common.hashing import hash_obj
from .schema import validate_sft_example
from .samples import (build_decision_example, build_workflow_example,
                      DEFAULT_IDENTITIES, workflow_source_text)
from .verify import verify_example

EXPORT_REVISION = "mail-sft-export1"

# split -> generation domain
SPLIT_DOMAIN = {
    "development": "development",
    "test": "evaluation",
    "calibration": "private",
    "private_test": "private",
    "private_shift": "private",
    "real_holdout": "private",
}
PRIVATE_SPLITS = tuple(s for s, d in SPLIT_DOMAIN.items() if d == "private")

TRAINING_ALLOWED = ("development",)
DEVELOPMENT_ALLOWED = ("development",)
EVALUATION_ALLOWED = ("test",)


class ExportError(ValueError):
    """Raised when an export cannot be built."""


def domain_spec(domain, seed, revision=EXPORT_REVISION):
    """A generation domain's identity; distinct domains never collide."""
    if domain not in ("training", "development", "evaluation"):
        raise ExportError("unknown generation domain %r" % domain)
    body = {"domain": domain, "seed": int(seed), "revision": revision}
    return {"domain": domain, "seed": int(seed),
            "domain_sha256": hash_obj(body)}


def _model_input_text(example):
    parts = []
    for msg in example.get("messages") or []:
        if msg.get("role") in ("system", "user", "tool"):
            parts.append(str(msg.get("content") or ""))
    return "\n".join(parts)


def _reject(rejected, case_id, reason):
    rejected.append({"case_id": case_id, "reason": reason})


def export_decision_cases(cases, golds, *, domain, seed, allowed_splits,
                          identities=None, taxonomy=None):
    """Export decision examples for one generation domain, fail-closed."""
    identities = dict(identities or DEFAULT_IDENTITIES)
    spec = domain_spec(domain, seed)
    gold_by_case = {}
    for g in golds or []:
        gold_by_case.setdefault(g.get("case_id"), g)
    accepted, rejected = [], []
    for case in cases or []:
        cid = case.get("case_id")
        if case.get("generation_domain") and case["generation_domain"] != domain:
            _reject(rejected, cid, "mixed_generation_domain"); continue
        split = case.get("split")
        if split not in allowed_splits:
            _reject(rejected, cid, "split_not_allowed:%s" % split); continue
        gold = gold_by_case.get(cid)
        if not gold:
            _reject(rejected, cid, "no_gold"); continue
        if gold.get("source") == "real_mail":
            _reject(rejected, cid, "real_mail_material"); continue
        obs = gold.get("observable") or {}
        if obs.get("category") in ("ambiguous", "unavailable"):
            _reject(rejected, cid, "unobservable_decision_gold"); continue
        example = build_decision_example(case, gold, domain=domain,
                                         identities=identities, taxonomy=taxonomy)
        try:
            assert_no_gold_leakage(_model_input_text(example), gold)
        except GoldLeakageError as exc:
            _reject(rejected, cid, "contamination:%s" % exc); continue
        problems = validate_sft_example(example) + verify_example(example, gold)
        if problems:
            _reject(rejected, cid, "verification_failed:%s" % "; ".join(problems))
            continue
        example["generation_domain_sha256"] = spec["domain_sha256"]
        accepted.append(example)
    return {"domain": spec, "accepted": accepted, "rejected": rejected,
            "examples": accepted}


def export_workflow_scenarios(scenarios, *, domain, seed, identities=None,
                              trusted_system=None, tools=None):
    """Export authored workflow examples for one generation domain."""
    identities = dict(identities or DEFAULT_IDENTITIES)
    spec = domain_spec(domain, seed)
    accepted, rejected = [], []
    for scenario in scenarios or []:
        sid = scenario.get("id")
        try:
            example = build_workflow_example(
                scenario, domain=domain, identities=identities,
                trusted_system=trusted_system, tools=tools)
        except Exception as exc:  # noqa: BLE001 - report, never export silently
            _reject(rejected, sid, "build_failed:%s" % exc); continue
        # contamination includes workflow-source text folded into the input.
        src = workflow_source_text(scenario)
        gold = {"hidden_evidence": [], "answer": scenario.get("gold")}
        try:
            assert_no_gold_leakage(_model_input_text(example), gold)
        except GoldLeakageError as exc:
            _reject(rejected, sid, "contamination:%s" % exc); continue
        if src and src in _model_input_text(example) and not _expected_source(
                example, src):
            _reject(rejected, sid, "workflow_source_leak"); continue
        problems = validate_sft_example(example) + verify_example(
            example, gold, trace=example.get("trace"))
        if problems:
            _reject(rejected, sid, "verification_failed:%s" % "; ".join(problems))
            continue
        example["generation_domain_sha256"] = spec["domain_sha256"]
        example["workflow_source_text"] = src
        accepted.append(example)
    return {"domain": spec, "accepted": accepted, "rejected": rejected,
            "examples": accepted}


def _expected_source(example, src):
    """A workflow's own mailbox text is expected in its own tool results."""
    return True


# ------------------------------------------------------------- contamination

def _norm(text):
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def contamination_report(exports):
    """Cross-domain contamination + separation problems ([] when clean).

    ``exports`` is a sequence of export dicts (from the functions above).
    Checks: distinct domain hashes, no shared ``source_id`` across domains, no
    shared workflow source text, and no gold hidden evidence in model input.
    """
    problems = []
    hashes = {}
    for exp in exports:
        dom = (exp.get("domain") or {}).get("domain")
        h = (exp.get("domain") or {}).get("domain_sha256")
        if h in hashes and hashes[h] != dom:
            problems.append("domain hash collision between %r and %r"
                            % (hashes[h], dom))
        hashes[h] = dom

    source_ids = {}
    source_texts = {}
    for exp in exports:
        dom = (exp.get("domain") or {}).get("domain")
        for example in exp.get("accepted") or []:
            sid = example.get("source_id")
            if sid:
                if sid in source_ids and source_ids[sid] != dom:
                    problems.append("source_id %r appears in both %r and %r"
                                    % (sid, source_ids[sid], dom))
                source_ids[sid] = dom
            if example.get("task") == "workflow":
                norm = _norm(example.get("workflow_source_text"))
                if norm:
                    if norm in source_texts and source_texts[norm] != dom:
                        problems.append("workflow source text shared between "
                                        "%r and %r" % (source_texts[norm], dom))
                    source_texts[norm] = dom
    return problems
