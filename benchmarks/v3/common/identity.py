"""Immutable benchmark-v3 run identity (WP1).

A run identity is a canonical hash over everything that could change a number
in a report.  It is stricter than v2:

* every fingerprint field must be **non-empty** -- ``None``/``""`` cannot
  certify a run, so a manifest with a null hash can never support a resume;
* model identity is ``model_key`` plus **either** ``model_revision`` **or**
  ``model_artifact_sha256`` (a pinned artifact digest);
* the requested case/split/profile ids are part of identity, so a different
  subset cannot silently reuse a partial results directory;
* ``generation_config`` / ``runtime_config`` carry decoding, calibration and
  execution settings, so changing a calibrator or a runtime blocks resume.

Design reference: ``specs/benchmark-v3.spec.md`` §Identity and resume;
FR7 (matching dataset, model, adapter, prompt, scorer and policy identities).
"""
from .hashing import hash_obj, short

# Artifact schema version stamped onto every built manifest.
SCHEMA_VERSION = "v3.0"

# Fingerprint strings that must be present and non-empty.
REQUIRED_NONEMPTY_FIELDS = (
    "dataset_id",
    "dataset_sha256",
    "case_manifest_sha256",
    "prompt_revision",
    "prompt_sha256",
    "model_key",
    "adapter_id",
    "adapter_revision",
    "scorer_revision",
    "policy_revision",
    "policy_sha256",
    "engine_contract_sha256",
    "calibrator_revision",
)

# Structured config blocks that must be present (may be empty mappings).
REQUIRED_CONFIG_FIELDS = ("generation_config", "runtime_config")

# Requested scope: must be non-empty so an empty subset cannot masquerade as
# "everything" or accidentally resume another slice.
REQUIRED_LIST_FIELDS = ("requested_case_ids", "requested_splits", "requested_profiles")

# Model identity is satisfied by a revision string OR an artifact digest.
MODEL_IDENTITY_FIELDS = ("model_revision", "model_artifact_sha256")

# The manifest hashed by ``build_manifest`` (config_hash/run_id are derived and
# therefore excluded from the hashed body).
HASHED_FIELDS = (
    REQUIRED_NONEMPTY_FIELDS
    + REQUIRED_CONFIG_FIELDS
    + REQUIRED_LIST_FIELDS
    + ("model_revision", "model_artifact_sha256")
)

# Fields named in the schema/identity handoff for later writers.
IDENTITY_FIELDS = tuple(HASHED_FIELDS)


class IdentityError(ValueError):
    """Raised when a manifest cannot certify a run."""


def _is_nonempty_str(value):
    return isinstance(value, str) and value.strip() != ""


def validate_identity(fields):
    """Return a list of human-readable identity problems ([] when valid)."""
    errs = []
    for name in REQUIRED_NONEMPTY_FIELDS:
        if not _is_nonempty_str(fields.get(name)):
            errs.append("missing/empty fingerprint field %r" % name)
    if not (_is_nonempty_str(fields.get("model_revision"))
            or _is_nonempty_str(fields.get("model_artifact_sha256"))):
        errs.append("model identity requires model_revision or model_artifact_sha256")
    for name in REQUIRED_CONFIG_FIELDS:
        if not isinstance(fields.get(name), dict):
            errs.append("config field %r must be a mapping" % name)
    for name in REQUIRED_LIST_FIELDS:
        value = fields.get(name)
        if not isinstance(value, (list, tuple)) or len(value) == 0:
            errs.append("requested scope %r must be a non-empty list" % name)
    return errs


def build_manifest(**fields):
    """Build a hashed manifest or raise :class:`IdentityError`.

    Keys passed in any order produce the same ``config_hash``/``run_id``.
    """
    body = {name: fields.get(name) for name in HASHED_FIELDS}
    # normalise list-like scope to plain lists so the canonical hash is stable
    for name in REQUIRED_LIST_FIELDS:
        value = body.get(name)
        if isinstance(value, tuple):
            body[name] = list(value)
    errs = validate_identity(body)
    if errs:
        raise IdentityError("invalid run identity:\n  " + "\n  ".join(errs))
    config_hash = hash_obj(body)
    manifest = dict(body)
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["config_hash"] = config_hash
    manifest["run_id"] = "%s-%s-%s" % (body["dataset_id"], body["model_key"],
                                       short(config_hash))
    return manifest


def resolve_resume(manifest, existing_manifest):
    """Return ``"fresh"`` or ``"resume"``; raise on any identity mismatch.

    A prior manifest without a usable ``config_hash`` cannot support a resume
    -- an unknown identity must never be continued (null hashes cannot resume).
    """
    if not existing_manifest:
        return "fresh"
    old = existing_manifest.get("config_hash")
    if not _is_nonempty_str(old):
        raise IdentityError(
            "cannot resume run %r: prior manifest has no identity hash"
            % existing_manifest.get("run_id"))
    new = manifest.get("config_hash")
    if not _is_nonempty_str(new):
        raise IdentityError("cannot resume: requested manifest has no identity hash")
    if old != new:
        raise IdentityError(
            "config mismatch for run_id %s: existing %s != requested %s; "
            "refusing to resume -- start a fresh run_id"
            % (manifest.get("run_id"), short(old), short(new)))
    return "resume"


def identity_fingerprints(manifest):
    """The subset of a manifest that identifies a run (for reports/UI)."""
    return {name: manifest.get(name) for name in IDENTITY_FIELDS}
