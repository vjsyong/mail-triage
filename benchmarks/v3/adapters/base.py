"""Adapter base contract for benchmark v3 (WP4).

An adapter turns a case's **declared visible input** into an attempt result.  It
never receives the dataset bundle, never receives gold, and may only touch the
sandbox it is handed.  It reports:

* the raw model response verbatim, the parsed decision, and any error;
* explicit per-field provenance (``produced`` / ``derived`` / ``missing``) so a
  typed decision head can never be credited with prose it cannot produce;
* a capability declaration, a fingerprint used for immutable run identity, and
  ``mock`` / ``qualifies_as_baseline`` flags so a scripted fake can never stand
  in as a real baseline.

The runner assembles the final foundation-schema attempt from the result dict
returned by :meth:`Adapter.run_case`.
"""
from __future__ import annotations

import os
import sys

from ..common.hashing import sha256_file, sha256_text, short
from ..contracts import FIELD_MISSING, FIELD_PRODUCED, NATIVE_OUTPUT_FIELDS

# Result statuses (attempt schema enum plus an internal ``skipped``).
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_MISSING = "missing"
STATUS_SKIPPED = "skipped"

# Failure classes: a model/quality failure is not an infrastructure failure and
# neither is a budget failure.
FAIL_MODEL = "model"
FAIL_INFRASTRUCTURE = "infrastructure"
FAIL_BUDGET = "budget"

# Capabilities a task can require.
TASK_CAPABILITIES = {
    "decision": ("decision",),
    "full_response": ("decision", "prose"),
    "workflow": ("tools",),
}


class AdapterError(Exception):
    """Raised for adapter-level misconfiguration."""


class UnsupportedCapability(AdapterError):
    """Raised when an adapter is asked for a capability it does not declare."""


def provenance_for(parsed):
    """Provenance for the declared native fields from an actual parsed result.

    A field that the adapter did not produce is ``missing`` -- it is never
    inferred, and prose is never synthesized for a typed decision head.
    """
    parsed = parsed if isinstance(parsed, dict) else {}
    return {field: (FIELD_PRODUCED if parsed.get(field) is not None else FIELD_MISSING)
            for field in NATIVE_OUTPUT_FIELDS}


def make_result(status=STATUS_OK, raw=None, parsed=None, error=None,
                field_provenance=None, tool_events=None, timings=None,
                resources=None, request_sha256="", failure_class=None,
                capabilities_used=None, output_extra=None):
    """Build the adapter result dict the runner turns into an attempt."""
    output = {"raw": raw, "parsed": parsed, "error": error}
    if output_extra:
        output.update(output_extra)
    return {
        "status": status,
        "output": output,
        "field_provenance": field_provenance or {},
        "tool_events": list(tool_events or []),
        "timings": dict(timings or {}),
        "resources": dict(resources or {}),
        "request_sha256": str(request_sha256 or ""),
        "failure_class": failure_class,
        "capabilities_used": dict(capabilities_used or {}),
    }


class Adapter(object):
    """Base adapter.  Subclasses implement :meth:`run_case` and identity."""

    adapter_id = "base"
    revision = "v3.0"
    capabilities = {"decision": False, "prose": False, "tools": False,
                    "native_parse": False}
    mock = False
    qualifies_as_baseline = True
    confidence_meaning = ""

    model_key = ""
    model_revision = None
    model_artifact_sha256 = None
    prompt_revision = ""
    # "pinned" (a real revision/artifact digest) / "observed" / "unverified".
    model_identity_source = None

    def __init__(self, generation_config=None, runtime_config=None):
        self.generation_config = dict(generation_config or {})
        self.runtime_config = dict(runtime_config or {})

    # ------------------------------------------------------------- identity

    def _source_sha256(self):
        module = sys.modules.get(type(self).__module__)
        path = getattr(module, "__file__", None)
        if path and os.path.exists(path):
            return sha256_file(path)
        return sha256_text(type(self).__module__ + ":" + type(self).__qualname__)

    def model_identity(self):
        """Whether the model identity is a real pin, observed, or unverified.

        A literal placeholder (``installed``/``unverified``) or an absent
        revision/artifact is **not** a pinned identity and must never qualify a
        run as a real baseline.
        """
        if self.model_identity_source:
            return self.model_identity_source
        revision = (self.model_revision or "").strip()
        artifact = (self.model_artifact_sha256 or "").strip()
        if artifact:
            return "pinned"
        if revision and revision not in ("installed", "unverified", "unknown"):
            return "pinned"
        return "unverified"

    def component_fingerprints(self):
        """Optional per-component identity for composed adapters (e.g. fusion)."""
        return {}

    def fingerprint(self):
        src = self._source_sha256()
        return {
            "adapter_id": self.adapter_id,
            "adapter_revision": "%s+%s" % (self.revision, short(src)),
            "adapter_source_sha256": src,
            "capabilities": dict(self.capabilities),
            "mock": bool(self.mock),
            "qualifies_as_baseline": bool(self.qualifies_as_baseline),
            "confidence_meaning": self.confidence_meaning,
            "generation_config": dict(self.generation_config),
            "runtime_config": dict(self.runtime_config),
            "prompt_revision": self.prompt_revision or self.revision,
            "model_key": self.model_key or self.adapter_id,
            "model_revision": self.model_revision,
            "model_artifact_sha256": self.model_artifact_sha256,
            "model_identity_source": self.model_identity(),
            "component_fingerprints": self.component_fingerprints(),
        }

    # ------------------------------------------------------------ capability

    def supports(self, task):
        required = TASK_CAPABILITIES.get(task, ())
        return all(self.capabilities.get(c) for c in required)

    def run_case(self, view, sandbox=None):
        raise NotImplementedError
