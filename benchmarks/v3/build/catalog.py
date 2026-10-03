"""Metadata-only source catalog and reuse gates (WP2 / spec §4.4, §10).

v3 never downloads or extracts an external corpus. These records describe
existing releases (including known release duplication and stale access paths)
so later authors cannot mistake public downloadability for a redistribution
right. Any use of a ``public_corpus`` record without an explicit authorization
fails closed.
"""
from ..common.hashing import hash_obj
from ..common.validation import ValidationError
from . import recipes
from .errors import BuildError

# Synthetic authoring provenance is the only provenance v3 can grant itself;
# it makes no claim about human review or real-mail authorization.
SYNTHETIC_RELEASE = "v3-synth-1"
SYNTHETIC_LICENSE = "CC0-1.0-authored"
SYNTHETIC_RETRIEVAL = "synthetic-authoring"

REAL_MAIL_RETRIEVAL = "authorized-export"
CATALOG_RETRIEVAL = "metadata_only"


def catalog():
    return recipes.corpora()


def catalog_entry(corpus_id):
    for entry in catalog():
        if entry["corpus_id"] == corpus_id:
            return entry
    raise BuildError("unknown corpus %r" % corpus_id)


def synthetic_provenance(provenance_id, content_material):
    """Provenance for authored synthetic material (the only kind v3 ships)."""
    return {
        "schema_version": "v3.0",
        "provenance_id": provenance_id,
        "source": "synthetic",
        "source_release": SYNTHETIC_RELEASE,
        "license": SYNTHETIC_LICENSE,
        "retrieval": SYNTHETIC_RETRIEVAL,
        "content_sha256": hash_obj(content_material),
        "reference_style": "synthetic-authored",
        "authorization": {"authorized": True,
                          "authorized_by": "synthetic-authoring",
                          "authorization_ref": SYNTHETIC_RELEASE},
        "notes": ("Authored for the v3 benchmark from templates and vocabulary; "
                  "not derived from, and not authorized for reuse of, any real "
                  "mailbox or public corpus."),
    }


def catalog_provenance(corpus_id):
    """A metadata-only provenance record for a catalogued corpus.

    ``authorized`` is always False: cataloguing a corpus is not a reuse right.
    """
    entry = catalog_entry(corpus_id)
    material = {"corpus_id": corpus_id, "releases": entry["releases"],
                "license": entry["license"], "retrieval": entry["retrieval"]}
    return {
        "schema_version": "v3.0",
        "provenance_id": "prov_catalog_%s" % corpus_id,
        "source": "public_corpus",
        "source_release": entry["releases"][0],
        "license": entry["license"],
        "retrieval": CATALOG_RETRIEVAL,
        "content_sha256": hash_obj(material),
        "reference_style": "metadata-only",
        "authorization": {"authorized": False, "authorized_by": None,
                          "authorization_ref": None},
        "notes": entry["notes"],
    }


def assert_reusable(provenance):
    """Fail closed when a provenance record carries no reuse authorization.

    A ``public_corpus`` or ``real_mail`` record is only usable with an explicit
    authorization object; a synthetic self-authored record is usable by
    construction.
    """
    if not isinstance(provenance, dict):
        raise BuildError("provenance must be a mapping")
    source = provenance.get("source")
    auth = provenance.get("authorization") or {}
    if source == "synthetic":
        return True
    if not auth.get("authorized"):
        raise BuildError(
            "source %r (%s) is not authorized for reuse; refusing to build from it"
            % (provenance.get("provenance_id"), source))
    if source == "real_mail" and not auth.get("authorization_ref"):
        raise BuildError("real_mail provenance needs an explicit authorization_ref")
    return True


def validate_provenance_record(provenance):
    from ..schema import validate_artifact
    errs = validate_artifact("provenance", provenance)
    if errs:
        raise ValidationError("catalog provenance invalid:\n  " + "\n  ".join(errs))
    return True
