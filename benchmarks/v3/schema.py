"""Schema-versioned v3.0 artifacts, validation, and gold/review gates (WP1).

Every scenario/case/policy/gold/lineage/provenance/attempt/run record carries
``"schema_version": "v3.0"``.  The committed JSON Schemas under
``benchmarks/v3/schemas/`` are the source of truth; :func:`validate_artifact`
applies them with the stdlib validator from :mod:`common.validation`.

Review honesty (FR/acceptance): a freshly built gold record is always
``draft``, ``human_seal`` is never auto-asserted, and real-mail material needs
**both** an explicit human seal and a separate authorization -- the two gates
are independent and neither is inferred.
"""
import os

from .common import identity
from .common.validation import ValidationError, load_schema, validate, validate_or_raise

SCHEMA_VERSION = "v3.0"

ARTIFACT_KINDS = {
    "scenario": "scenario.schema.json",
    "policy": "policy.schema.json",
    "case": "case.schema.json",
    "lineage": "lineage.schema.json",
    "gold": "gold.schema.json",
    "provenance": "provenance.schema.json",
    "attempt": "attempt.schema.json",
    "run_manifest": "run_manifest.schema.json",
}

OBSERVABILITY = ("visible", "retrievable", "full_context", "ambiguous", "unavailable")
FIELD_PROVENANCE = ("produced", "derived", "missing")
REVIEW_STATUS = ("draft", "reviewed", "sealed")
PROVENANCE_SOURCES = ("synthetic", "real_mail", "public_corpus")
PROFILES = ("native", "policy_conditioned", "full_context", "workflow")
SPLITS = ("development", "calibration", "private_test", "private_shift", "real_holdout")

__all__ = [
    "SCHEMA_VERSION", "ARTIFACT_KINDS", "OBSERVABILITY", "FIELD_PROVENANCE",
    "REVIEW_STATUS", "PROVENANCE_SOURCES", "PROFILES", "SPLITS",
    "ValidationError", "load_schema", "validate", "validate_or_raise",
    "validate_artifact", "validate_artifact_or_raise", "validate_run_manifest",
    "new_gold", "seal_gold", "assert_draft_honest", "can_seal", "SealError",
]


class SealError(ValueError):
    """Raised when a gold record is sealed without the required gates."""


def _nonblank(value):
    return isinstance(value, str) and value.strip() != ""


def _review_gate_errors(gold):
    """Review-honesty problems for a gold record ([] when consistent)."""
    errs = []
    status = gold.get("review_status")
    human = gold.get("human_seal")
    reviewer = gold.get("reviewer")
    source = gold.get("source")
    if human and status != "sealed":
        errs.append("$.human_seal: true requires review_status=sealed "
                    "(a draft cannot carry a seal)")
    if status == "sealed" and not human:
        errs.append("$.review_status: sealed requires human_seal=true")
    if status == "sealed" and not _nonblank(reviewer):
        errs.append("$.reviewer: sealed requires a non-empty reviewer")
    if reviewer is not None and not _nonblank(reviewer):
        errs.append("$.reviewer: must be a non-empty string or null")
    if source == "real_mail" and (status == "sealed" or human) and not gold.get("authorized"):
        errs.append("$.authorized: real-mail gold must be explicitly authorized "
                    "before seal")
    return errs


def validate_artifact(kind, instance):
    """Validate one v3.0 artifact; return a list of error strings."""
    if kind not in ARTIFACT_KINDS:
        raise KeyError("unknown artifact kind %r (expected one of %s)"
                       % (kind, ", ".join(sorted(ARTIFACT_KINDS))))
    schema = load_schema(ARTIFACT_KINDS[kind])
    errs = validate(instance, schema)
    if isinstance(instance, dict):
        if kind == "gold":
            observable = instance.get("observable")
            if observable is not None and not isinstance(observable, dict):
                errs.append("$.observable: expected object, got %s"
                            % type(observable).__name__)
            elif isinstance(observable, dict):
                for field, level in observable.items():
                    if level not in OBSERVABILITY:
                        errs.append("$.observable.%s: %r not in %s"
                                    % (field, level, list(OBSERVABILITY)))
            errs = errs + _review_gate_errors(instance)
        if kind == "attempt":
            provenance = instance.get("field_provenance")
            if provenance is not None and not isinstance(provenance, dict):
                errs.append("$.field_provenance: expected object, got %s"
                            % type(provenance).__name__)
            elif isinstance(provenance, dict):
                for field, value in provenance.items():
                    if value not in FIELD_PROVENANCE:
                        errs.append("$.field_provenance.%s: %r not in %s"
                                    % (field, value, list(FIELD_PROVENANCE)))
    if kind == "run_manifest":
        errs = errs + identity.validate_identity(instance)
    return errs


def validate_artifact_or_raise(kind, instance, label=None):
    errs = validate_artifact(kind, instance)
    if errs:
        raise ValidationError("%s invalid:\n  " % (label or kind) + "\n  ".join(errs))
    return True


def validate_run_manifest(manifest):
    """Validate a hashed run manifest (schema + identity fingerprints)."""
    return validate_artifact_or_raise("run_manifest", manifest, label="run manifest")


# ----------------------------------------------------------------- gold / review

def new_gold(case_id, gold_id, **fields):
    """Build a gold record that is honestly still a draft.

    ``review_status`` is ``draft`` and ``human_seal`` is ``False`` by default;
    caller-supplied values for those keys are ignored so a draft cannot be
    silently born sealed.
    """
    record = {
        "schema_version": SCHEMA_VERSION,
        "gold_id": gold_id,
        "case_id": case_id,
        "review_status": "draft",
        "human_seal": False,
        "reviewer": None,
        "authorized": False,
        "observable": {},
        "hidden_evidence": [],
        "answer": {},
    }
    for k, v in fields.items():
        if k in ("review_status", "human_seal"):
            continue
        record[k] = v
    return record


def can_seal(gold):
    """Whether the record may be sealed: an explicit, non-blank human reviewer,
    and for real-mail material a separate authorization."""
    if not _nonblank(gold.get("reviewer")):
        return False
    if gold.get("source") == "real_mail" and not gold.get("authorized"):
        return False
    return True


def seal_gold(gold, reviewer, authorized=False):
    """Mark a gold record sealed only when the gates are explicitly supplied.

    Raises :class:`SealError` when no named human reviewer is provided, or when
    real-mail material is sealed without a separate authorization.  Authorization
    is never inferred from downloadability or from the presence of the data.
    """
    if not _nonblank(reviewer):
        raise SealError("cannot seal gold without a named human reviewer")
    if gold.get("source") == "real_mail" and not authorized:
        raise SealError("cannot seal real-mail gold without explicit authorization")
    sealed = dict(gold)
    sealed["review_status"] = "sealed"
    sealed["human_seal"] = True
    sealed["reviewer"] = reviewer
    sealed["authorized"] = bool(authorized)
    assert_draft_honest(sealed)
    return sealed


def assert_draft_honest(gold):
    """Reject contradictory review state.

    A sealed record must carry a non-blank human reviewer; a record with
    ``human_seal=true`` must actually be ``sealed`` (a draft cannot carry a
    seal); and real-mail material must be explicitly authorized before any seal
    is accepted.  This is the same gate
    :func:`validate_artifact` applies when validating a gold record.
    """
    errs = _review_gate_errors(gold)
    if errs:
        raise SealError("; ".join(errs))
    return True


def schema_dir():
    return os.path.join(os.path.dirname(__file__), "schemas")
