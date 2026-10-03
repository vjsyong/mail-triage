"""Review worksheet, import and seal gates (WP6).

A freshly built dataset is ``draft`` with no human seal. Review happens on a
worksheet; human judgements are imported (optionally from a de-identified real-mail
file contract that demands separate consent/authorization); agreement is computed;
and sealing demands reviewed items, a named explicit reviewer and no unresolved
annotation conflict. None of it is ever automatic.
"""
import json
import os

from .errors import ImportAuthorizationError, ReviewError
from .generate import SCHEMA_VERSION

_DECISIONS = ("accept", "reject", "revise")
_RAW_MAIL_FIELDS = ("body", "raw", "headers", "snippet", "attachments", "message")


def build_review_worksheet(bundle, *, reviewer=None, splits=None):
    """Build a draft review worksheet; no item is marked reviewed or sealed."""
    wanted = set(splits) if splits else None
    case_by_id = {c["case_id"]: c for c in bundle.get("cases", [])}
    items = []
    for gold in bundle.get("gold", []):
        case = case_by_id.get(gold.get("case_id"), {})
        if wanted is not None and case.get("split") not in wanted:
            continue
        rendered = case.get("rendered_input") or {}
        items.append({
            "gold_id": gold.get("gold_id"),
            "case_id": gold.get("case_id"),
            "split": case.get("split"),
            "task": case.get("task"),
            "input_profile": case.get("input_profile"),
            "source": gold.get("source"),
            "observable": gold.get("observable"),
            "answer": gold.get("answer"),
            "hidden_evidence": gold.get("hidden_evidence"),
            "excerpt": (rendered.get("user") or "")[:400],
            "review_status": "draft",
            "human_seal": False,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": bundle.get("dataset_id"),
        "review_status": "draft",
        "human_seal": False,
        "reviewer": reviewer,
        "items": items,
        "notes": ("Fresh worksheet. Every row is draft and unsealed; a named "
                  "human reviewer must import judgements before anything is reviewed."),
    }


def validate_review_worksheet(worksheet):
    """Return worksheet problems ([] when honest)."""
    errs = []
    if not isinstance(worksheet, dict):
        return ["worksheet must be a mapping"]
    if not worksheet.get("dataset_id"):
        errs.append("worksheet.dataset_id must be non-empty")
    if worksheet.get("review_status") != "draft":
        errs.append("a fresh worksheet must be draft")
    if worksheet.get("human_seal"):
        errs.append("a worksheet is never born sealed")
    if not isinstance(worksheet.get("items"), list):
        errs.append("worksheet.items must be a list")
        return errs
    for item in worksheet["items"]:
        if item.get("review_status") != "draft" or item.get("human_seal"):
            errs.append("worksheet item %r is not draft/unsealed" % item.get("gold_id"))
    return errs


def write_review_worksheet(worksheet, path):
    """Export a validated worksheet for a reviewer; refuses a dishonest one."""
    errs = validate_review_worksheet(worksheet)
    if errs:
        raise ReviewError("refusing to export an invalid worksheet:\n  "
                          + "\n  ".join(errs))
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(worksheet, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    return path


def load_review_worksheet(path):
    """Load and validate an exported worksheet; raise when it is not honest."""
    with open(path, encoding="utf-8") as f:
        worksheet = json.load(f)
    errs = validate_review_worksheet(worksheet)
    if errs:
        raise ReviewError("loaded worksheet is invalid:\n  " + "\n  ".join(errs))
    return worksheet


def _normalise_judgements(judgements):
    if isinstance(judgements, dict):
        out = []
        for gold_id, verdict in judgements.items():
            entries = verdict if isinstance(verdict, list) else [verdict]
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ReviewError("judgement for %r must be a mapping" % gold_id)
                merged = dict(entry)
                merged["gold_id"] = gold_id
                out.append(merged)
        return out
    return [dict(j) for j in (judgements or [])]


def _require_reviewer(verdict):
    reviewer = verdict.get("reviewer")
    if not (isinstance(reviewer, str) and reviewer.strip()):
        raise ReviewError("every judgement needs a named human reviewer")
    if verdict.get("decision") not in _DECISIONS:
        raise ReviewError("decision %r must be one of %s"
                          % (verdict.get("decision"), _DECISIONS))
    return reviewer


def agreement_report(judgements):
    """Percentage agreement across multi-reviewed items ([] when none)."""
    by_gold = {}
    for verdict in _normalise_judgements(judgements):
        by_gold.setdefault(verdict.get("gold_id"), []).append(verdict)
    multi = {g: v for g, v in by_gold.items() if len(v) >= 2}
    agree = 0
    conflicts = []
    for gold_id, verdicts in multi.items():
        decisions = {v.get("decision") for v in verdicts}
        if len(decisions) == 1:
            agree += 1
        else:
            conflicts.append({"gold_id": gold_id,
                              "decisions": sorted(d for d in decisions if d)})
    total = len(multi)
    return {"multi_reviewed": total, "agreed": agree,
            "agreement": (agree / total) if total else None,
            "conflicts": conflicts}


def import_review(bundle, worksheet, judgements, *, authorization=None):
    """Import human judgements over a worksheet.

    Returns an updated *reviewed* bundle plus the agreement report and the list
    of conflicts. A conflict or a rejection leaves the gold in ``draft`` -- it is
    never promoted to reviewed. Real-mail judgements require the separate
    authorization metadata.
    """
    errs = validate_review_worksheet(worksheet)
    if errs:
        raise ReviewError("invalid review worksheet:\n  " + "\n  ".join(errs))
    if worksheet.get("dataset_id") != bundle.get("dataset_id"):
        raise ReviewError("worksheet does not belong to this dataset")
    verdicts = _normalise_judgements(judgements)
    by_gold = {}
    for verdict in verdicts:
        _require_reviewer(verdict)
        if verdict.get("gold_id") not in \
                {i["gold_id"] for i in worksheet["items"]}:
            raise ReviewError("judgement for unknown worksheet item %r"
                              % verdict.get("gold_id"))
        by_gold.setdefault(verdict["gold_id"], []).append(verdict)

    worksheet_item = {i["gold_id"]: i for i in worksheet["items"]}
    updated = {g["gold_id"]: dict(g) for g in bundle["gold"]}
    conflicts, rejected, reviewed, unreviewed = [], [], [], []
    for gold_id, item in worksheet_item.items():
        gold = updated.get(gold_id)
        if gold is None:
            continue
        gold_verdicts = by_gold.get(gold_id, [])
        if not gold_verdicts:
            unreviewed.append(gold_id)
            continue
        decisions = {v["decision"] for v in gold_verdicts}
        if len(decisions) > 1:
            conflicts.append({"gold_id": gold_id, "decisions": sorted(decisions)})
            continue
        decision = decisions.pop()
        if item.get("source") == "real_mail":
            _require_real_authorization(authorization, gold)
        if decision in ("accept", "revise"):
            gold["review_status"] = "reviewed"
            reviewed.append(gold_id)
        else:
            rejected.append(gold_id)
    result_bundle = dict(bundle)
    result_bundle["gold"] = [updated[g["gold_id"]] for g in bundle["gold"]]
    result_bundle["metadata"] = dict(bundle.get("metadata") or {})
    result_bundle["metadata"]["review_status"] = (
        "reviewed" if reviewed and not conflicts and not unreviewed else "draft")
    return {"bundle": result_bundle,
            "worksheet": worksheet,
            "review_status": result_bundle["metadata"]["review_status"],
            "reviewed": reviewed, "rejected": rejected,
            "conflicts": conflicts, "unreviewed": unreviewed,
            "agreement": agreement_report(verdicts)}


def _require_real_authorization(authorization, gold):
    if not isinstance(authorization, dict):
        raise ImportAuthorizationError(
            "real-mail judgement for %s needs separate consent/authorization"
            % gold.get("gold_id"))
    consent = authorization.get("consent")
    if not isinstance(consent, dict) or not consent.get("consent_ref"):
        raise ImportAuthorizationError("real-mail import needs an explicit consent_ref")
    if not authorization.get("authorization_ref") or \
            not authorization.get("authorized_by"):
        raise ImportAuthorizationError(
            "real-mail import needs authorization_ref and authorized_by")


def validate_real_import(records, *, authorization):
    """Validate a de-identified real-mail import file contract (fail closed).

    Only de-identified judgement rows are accepted; any raw message field makes
    the import invalid, and authorization/consent metadata is mandatory.
    """
    if not isinstance(records, list) or not records:
        raise ReviewError("real-mail import records must be a non-empty list")
    for row in records:
        if not isinstance(row, dict):
            raise ReviewError("each real-mail import record must be a mapping")
        for forbidden in _RAW_MAIL_FIELDS:
            if forbidden in row:
                raise ReviewError(
                    "real-mail import must be de-identified; found raw field %r"
                    % forbidden)
        for required in ("gold_id", "decision", "reviewer"):
            if not row.get(required):
                raise ReviewError("real-mail import record missing %r" % required)
        if row["decision"] not in _DECISIONS:
            raise ReviewError("real-mail import decision %r invalid" % row["decision"])
    _require_real_authorization(authorization, {"gold_id": "import"})
    return _normalise_judgements(records)


def seal_bundle(bundle, *, reviewer, conflicts=None):
    """Seal a fully reviewed bundle.

    Refuses unless a non-blank named reviewer is supplied, every gold is already
    ``reviewed``/``sealed``, and there is no unresolved annotation conflict.
    Nothing is auto-sealed.
    """
    if not (isinstance(reviewer, str) and reviewer.strip()):
        raise ReviewError("cannot seal without a named human reviewer")
    if conflicts:
        raise ReviewError("cannot seal with unresolved annotation conflicts: %s"
                          % conflicts)
    sealed_golds = []
    for gold in bundle.get("gold", []):
        if gold.get("review_status") not in ("reviewed", "sealed"):
            raise ReviewError(
                "cannot seal: gold %s is %r, not reviewed"
                % (gold.get("gold_id"), gold.get("review_status")))
        sealed_golds.append(dict(gold))
    from .. import schema
    out = []
    for gold in sealed_golds:
        if gold.get("review_status") == "sealed":
            out.append(gold)
            continue
        authorized = bool(gold.get("authorized"))
        out.append(schema.seal_gold(gold, reviewer, authorized=authorized))
    result = dict(bundle)
    result["gold"] = out
    result["metadata"] = dict(bundle.get("metadata") or {})
    result["metadata"]["review_status"] = "sealed"
    result["metadata"]["reviewer"] = reviewer
    return result
