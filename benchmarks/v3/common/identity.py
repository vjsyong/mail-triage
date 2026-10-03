"""Immutable benchmark-v3 run identity (WP1).

A run identity is a canonical hash over everything that could change a number
in a report.  It is stricter than v2:

* every fingerprint field must be **non-empty** -- ``None``/``""`` cannot
  certify a run, so a manifest with a null hash can never support a resume;
* model identity is ``model_key`` plus **either** ``model_revision`` **or**
  ``model_artifact_sha256`` (a pinned artifact digest);
* the requested case/split/profile ids are part of identity, so a different
  subset cannot silently reuse a partial results directory; their elements must
  be non-empty strings (``None``/blank entries are rejected);
* ``generation_config`` / ``runtime_config`` carry decoding, calibration and
  execution settings, so changing a calibrator or a runtime blocks resume;
* a resume **revalidates and recomputes** the canonical digest from the
  identity fields of both manifests -- a stale or tampered stored hash (e.g. a
  changed body still carrying its old ``config_hash``) is rejected, as is a
  ``run_id`` that does not match its identity;
* ``run_id`` is path-safe even when the model key is a Hugging Face id with
  ``/`` or a traversal-looking string, while the original model identity values
  remain part of the hash.

Design reference: ``specs/benchmark-v3.spec.md`` §Identity and resume;
FR7 (matching dataset, model, adapter, prompt, scorer and policy identities).
"""
import re

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
# "everything" or accidentally resume another slice.  Each element must be a
# non-empty string.
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

# Characters unsafe in a path component.  Used only for ``run_id``; the hashed
# identity keeps the original values (a slashed HF id and a hyphenated id are
# different models and must hash differently).
_UNSAFE_RUN_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_RUN_DASHES = re.compile(r"-{2,}")


class IdentityError(ValueError):
    """Raised when a manifest cannot certify a run."""


def _is_nonempty_str(value):
    return isinstance(value, str) and value.strip() != ""


def _normalise_scope(fields):
    """Pull the hashed identity fields out of a manifest-like mapping."""
    body = {name: fields.get(name) for name in HASHED_FIELDS}
    for name in REQUIRED_LIST_FIELDS:
        value = body.get(name)
        if isinstance(value, tuple):
            body[name] = list(value)
    return body


def safe_run_component(value, max_len=80):
    """A filesystem-safe, non-empty id fragment derived from ``value``.

    ``openbmb/MiniCPM5-2B`` -> ``openbmb-MiniCPM5-2B``; ``../../etc`` -> ``etc``;
    empty/dot-only values -> ``model``.
    """
    text = _UNSAFE_RUN_CHARS.sub("-", str(value or "")).strip("-")
    text = _RUN_DASHES.sub("-", text)[:max_len]
    return text.strip("-") or "model"


def compute_config_hash(fields):
    """Recompute the canonical digest over the identity fields."""
    return hash_obj(_normalise_scope(fields))


def expected_run_id(fields):
    """The run_id a manifest with these identity fields must carry."""
    return "%s-%s-%s" % (
        safe_run_component(fields.get("dataset_id")),
        safe_run_component(fields.get("model_key")),
        short(compute_config_hash(fields)),
    )


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
            continue
        for i, item in enumerate(value):
            if not _is_nonempty_str(item):
                errs.append("requested scope %r[%d] must be a non-empty string"
                            % (name, i))
    return errs


def build_manifest(**fields):
    """Build a hashed manifest or raise :class:`IdentityError`.

    Keys passed in any order produce the same ``config_hash``/``run_id``.
    """
    body = _normalise_scope(fields)
    errs = validate_identity(body)
    if errs:
        raise IdentityError("invalid run identity:\n  " + "\n  ".join(errs))
    manifest = dict(body)
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["config_hash"] = compute_config_hash(body)
    manifest["run_id"] = expected_run_id(body)
    return manifest


def resolve_resume(manifest, existing_manifest):
    """Return ``"fresh"`` or ``"resume"``; raise on any identity mismatch.

    Both manifests are revalidated and their canonical digests recomputed from
    the identity fields, so a stored ``config_hash`` that no longer matches its
    own body (stale or tampered) is rejected, as is a mismatched ``run_id``.
    A prior manifest without a usable ``config_hash`` cannot support a resume --
    an unknown identity must never be continued (null hashes cannot resume).
    """
    if not existing_manifest:
        return "fresh"

    old_errs = validate_identity(existing_manifest)
    if old_errs:
        raise IdentityError(
            "cannot resume: prior manifest has an invalid identity:\n  "
            + "\n  ".join(old_errs))
    old_stored = existing_manifest.get("config_hash")
    if not _is_nonempty_str(old_stored):
        raise IdentityError(
            "cannot resume run %r: prior manifest has no identity hash"
            % existing_manifest.get("run_id"))
    old_digest = compute_config_hash(existing_manifest)
    if old_digest != old_stored:
        raise IdentityError(
            "cannot resume: prior manifest identity hash is stale or tampered "
            "(stored %s != recomputed %s)"
            % (short(old_stored), short(old_digest)))
    old_expected = expected_run_id(existing_manifest)
    if existing_manifest.get("run_id") != old_expected:
        raise IdentityError(
            "cannot resume: prior manifest run_id %r does not match its identity %r"
            % (short(existing_manifest.get("run_id")), short(old_expected)))

    new_errs = validate_identity(manifest)
    if new_errs:
        raise IdentityError(
            "cannot resume: requested manifest has an invalid identity:\n  "
            + "\n  ".join(new_errs))
    new_stored = manifest.get("config_hash")
    if not _is_nonempty_str(new_stored):
        raise IdentityError("cannot resume: requested manifest has no identity hash")
    new_digest = compute_config_hash(manifest)
    if new_digest != new_stored:
        raise IdentityError(
            "cannot resume: requested manifest identity hash is stale or tampered "
            "(stored %s != recomputed %s)" % (short(new_stored), short(new_digest)))

    if old_digest != new_digest:
        raise IdentityError(
            "config mismatch for run_id %s: existing %s != requested %s; "
            "refusing to resume -- start a fresh run_id"
            % (manifest.get("run_id"), short(old_stored), short(new_stored)))
    return "resume"


def identity_fingerprints(manifest):
    """The subset of a manifest that identifies a run (for reports/UI)."""
    return {name: manifest.get(name) for name in IDENTITY_FIELDS}
