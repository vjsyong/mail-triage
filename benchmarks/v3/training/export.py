"""Fail-closed SFT export over real source records (AC2/M2/M3).

Every source record must declare an explicit ``role`` and ``domain``; the
exporter never relabels an absent-role v3 development record.  A hard denylist
rejects calibration/private/test/real/ambiguous/unobservable material **before**
any caller-supplied allow-list is consulted.  Each accepted source yields a
classifier example **and** a bounded tool dialogue over the same email, and the
pair must share ``source_id``/``lineage_id`` (cross-task lineage).

``contamination_report`` checks source ids, lineage ids and normalised per-email
text across **both** tasks and across domains.
"""
from __future__ import annotations

import re

from ..common.hashing import hash_obj
from .reply_gold import reply_problems
from .schema import validate_sft_example
from .samples import (ROLE_DEV, ROLE_TRAIN, build_decision_example,
                      build_dialogue_example)
from .verify import verify_example

EXPORT_REVISION = "mail-sft-export2"

GENERATION_DOMAINS = ("training", "development", "evaluation")
ROLES = (ROLE_TRAIN, ROLE_DEV)

# Splits/sources that are never training material, regardless of any override.
DENIED_SPLITS = ("calibration", "private_test", "private_shift", "test",
                 "real_holdout")
DENIED_SOURCES = ("real_mail",)
AMBIGUOUS_OBS = ("ambiguous", "unavailable")
ACTIONABLE = ("move", "propose")


class ExportError(ValueError):
    """Raised when an export cannot be built."""


def domain_spec(domain, seed, revision=EXPORT_REVISION):
    if domain not in GENERATION_DOMAINS:
        raise ExportError("unknown generation domain %r" % domain)
    return {"domain": domain, "seed": int(seed),
            "domain_sha256": hash_obj({"domain": domain, "seed": int(seed),
                                       "revision": revision})}


def _norm_text(text):
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _email_text(example):
    e = example.get("source_email") or {}
    return _norm_text(" ".join([str(e.get(k) or "") for k in
                                ("from_addr", "to_addr", "subject", "body")]))


def _reject(rejected, sid, reason):
    rejected.append({"source_id": sid, "reason": reason})


def verify_source(source, taxonomy):
    """Authored-intent problems justified by the **visible** email (S1)."""
    problems = []
    intent = source.get("intent") or {}
    names = [c.get("name") for c in taxonomy["categories"]]
    if intent.get("category") not in names:
        problems.append("category_not_in_taxonomy")
    needs_reply = intent.get("needs_reply")
    if not isinstance(needs_reply, bool):
        problems.append("needs_reply_not_boolean")
    else:
        email = source.get("email") or {}
        text = "%s\n%s" % (email.get("subject", ""), email.get("body", ""))
        for p in reply_problems(text, needs_reply):
            problems.append("reply_gold:%s" % p)
    return problems


def export_sources(sources, dialogues, taxonomy, *, domain, seed,
                   identities=None, allowed_roles=(ROLE_TRAIN,)):
    """Export paired classifier + dialogue examples for one generation domain."""
    spec = domain_spec(domain, seed)
    dialog_by_source = {}
    for d in dialogues or []:
        dialog_by_source.setdefault(d.get("source_id"), d)
    accepted, rejected, pairs = [], [], []
    for source in sources or []:
        sid = source.get("source_id")
        role = source.get("role")
        dom = source.get("domain")
        if not role:
            _reject(rejected, sid, "missing_role"); continue
        if not dom:
            _reject(rejected, sid, "missing_domain"); continue
        if role not in allowed_roles:
            _reject(rejected, sid, "role_not_allowed:%s" % role); continue
        if dom != domain:
            _reject(rejected, sid, "mixed_generation_domain:%s" % dom); continue
        # hard denylist before any allow-list
        if source.get("split") in DENIED_SPLITS:
            _reject(rejected, sid, "disallowed_split:%s" % source.get("split")); continue
        if source.get("source_kind") in DENIED_SOURCES:
            _reject(rejected, sid, "real_mail_material"); continue
        intent = source.get("intent") or {}
        obs = intent.get("observable", "visible")
        if obs in AMBIGUOUS_OBS or not intent.get("category"):
            _reject(rejected, sid, "unobservable_decision_gold"); continue
        if not source.get("lineage_id"):
            _reject(rejected, sid, "missing_lineage"); continue
        src_problems = verify_source(source, taxonomy)
        if src_problems:
            _reject(rejected, sid,
                    "source_verification_failed:" + "; ".join(src_problems))
            continue

        dialogue = dialog_by_source.get(sid)
        actionable = source.get("action") in ACTIONABLE
        if actionable and dialogue is None:
            _reject(rejected, sid, "no_dialogue"); continue
        if dialogue is not None:
            if dialogue.get("lineage_id") != source.get("lineage_id"):
                _reject(rejected, sid, "dialogue_lineage_mismatch"); continue
            if dialogue.get("domain") != domain:
                _reject(rejected, sid, "dialogue_domain_mismatch"); continue

        dec = build_decision_example(source, taxonomy, domain=domain,
                                     identities=identities)
        dec["generation_domain_sha256"] = spec["domain_sha256"]
        examples = [dec]
        problems = (validate_sft_example(dec)
                    + verify_example(dec, source=source, taxonomy=taxonomy))
        if dialogue is not None:
            dlg = build_dialogue_example(dialogue, taxonomy, domain=domain,
                                         identities=identities)
            dlg["generation_domain_sha256"] = spec["domain_sha256"]
            examples.append(dlg)
            problems += (validate_sft_example(dlg)
                         + verify_example(dlg, source=source,
                                          gold=dialogue.get("gold"),
                                          taxonomy=taxonomy))
        else:
            dlg = None
        if problems:
            _reject(rejected, sid, "verification_failed:%s" % "; ".join(problems))
            continue
        accepted.extend(examples)
        if dlg is not None:
            pairs.append({"source_id": sid, "lineage_id": source["lineage_id"],
                          "decision_example": dec["example_id"],
                          "dialogue_example": dlg["example_id"]})
    return {"domain": spec, "accepted": accepted, "rejected": rejected,
            "examples": accepted, "pairs": pairs}


def contamination_report(exports):
    """Cross-domain contamination for source ids, lineage ids and email text."""
    problems = []
    hashes = {}
    for exp in exports:
        dom = (exp.get("domain") or {}).get("domain")
        h = (exp.get("domain") or {}).get("domain_sha256")
        if h in hashes and hashes[h] != dom:
            problems.append("domain hash collision between %r and %r"
                            % (hashes[h], dom))
        hashes[h] = dom

    seen_sid, seen_lin, seen_text = {}, {}, {}
    for exp in exports:
        dom = (exp.get("domain") or {}).get("domain")
        for ex in exp.get("accepted") or []:
            sid, lin = ex.get("source_id"), ex.get("lineage_id")
            if sid and sid in seen_sid and seen_sid[sid] != dom:
                problems.append("source_id %r shared across %r and %r"
                                % (sid, seen_sid[sid], dom))
            if sid:
                seen_sid[sid] = dom
            if lin and lin in seen_lin and seen_lin[lin] != dom:
                problems.append("lineage_id %r shared across %r and %r"
                                % (lin, seen_lin[lin], dom))
            if lin:
                seen_lin[lin] = dom
            text = _email_text(ex)
            if text and text in seen_text and seen_text[text] != dom:
                problems.append("email text shared across %r and %r"
                                % (seen_text[text], dom))
            if text:
                seen_text[text] = dom
    return problems


def assert_cross_task_linkage(export):
    """Every dialogue must be paired with a decision of the same lineage.

    Decision-only sources are allowed; a dialogue without its classifier twin (or
    with a different lineage) is a linkage failure.
    """
    problems = []
    by_source = {}
    for ex in export.get("accepted") or []:
        by_source.setdefault(ex["source_id"], {}).setdefault(ex["task"], []).append(ex)
    for sid, tasks in by_source.items():
        if "workflow" in tasks and "decision" not in tasks:
            problems.append("source %r has a dialogue but no decision example" % sid)
        if "workflow" in tasks and "decision" in tasks:
            dec_lin = tasks["decision"][0]["lineage_id"]
            dlg_lin = tasks["workflow"][0]["lineage_id"]
            if dec_lin != dlg_lin:
                problems.append("source %r decision/dialogue lineage differ "
                                "(%r/%r)" % (sid, dec_lin, dlg_lin))
    return problems
