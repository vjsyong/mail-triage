"""Fusion adapter for benchmark v3 (WP4).

Composes a decision component (TinyJev category + needs_reply) with a prose
component (a generative summary/reason), recording **which component produced
each field** and each component's timing.  A component that is unavailable or
unsupported leaves its fields ``missing`` -- prose is never invented to fill a
schema, and a facade read is explicitly labelled as a read-only existing
service rather than an independent measurement.
"""
from __future__ import annotations

from . import base as B


class FusionAdapter(B.Adapter):
    adapter_id = "fusion"
    revision = "v3.0"
    capabilities = {"decision": True, "prose": True, "tools": False,
                    "native_parse": False}
    mock = False
    qualifies_as_baseline = True
    confidence_meaning = "from the decision component; see components.decision"
    prompt_revision = "fusion-v3.0"

    def __init__(self, decision_adapter=None, prose_adapter=None, facade=None,
                 allow_remote=False, model_key="fusion", model_revision="v3.0",
                 model_artifact_sha256=None):
        caps = {"decision": True, "prose": bool(prose_adapter or facade),
                "tools": False, "native_parse": False}
        super().__init__(
            generation_config={"composition": "decision+prose",
                               "decision": getattr(decision_adapter, "adapter_id", None),
                               "prose": getattr(prose_adapter, "adapter_id", None)},
            runtime_config={"backend": "fusion", "facade": bool(facade),
                            "read_only_facade": bool(facade)})
        self.capabilities = caps
        self.decision_adapter = decision_adapter
        self.prose_adapter = prose_adapter
        self.facade = facade
        self.allow_remote = allow_remote
        self.model_key = model_key
        self.model_revision = model_revision
        self.model_artifact_sha256 = model_artifact_sha256

    def _components_provenance(self, decision_prov, prose_prov, prose_ok):
        def field(name):
            if name in ("category", "needs_reply"):
                return decision_prov.get(name, "missing")
            if name == "confidence":
                return decision_prov.get("confidence", "missing")
            if name in ("summary", "reason"):
                return prose_prov.get(name, "missing") if prose_ok else "missing"
            return "missing"
        return {name: field(name) for name in
                ("category", "needs_reply", "confidence", "summary", "reason")}

    def _run_facade(self, view):
        data = self.facade(view)
        if not isinstance(data, dict):
            raise B.AdapterError("facade returned a non-mapping")
        parsed = {k: data.get(k) for k in
                  ("category", "needs_reply", "confidence", "summary", "reason")}
        prose_ok = parsed.get("summary") is not None or parsed.get("reason") is not None
        provenance = self._components_provenance(
            {"category": "produced", "needs_reply": "produced",
             "confidence": "produced"},
            {"summary": "produced", "reason": "produced"}, prose_ok)
        return B.make_result(
            status=B.STATUS_OK, raw=data, parsed=parsed, error=None,
            field_provenance=provenance,
            timings={"measured": "warm", "label": "read-only existing facade"},
            output_extra={"components": {"facade": data},
                          "read_only_facade": True},
            capabilities_used={"decision": True, "prose": prose_ok})

    def run_case(self, view, sandbox=None):
        if view.get("task") == "workflow":
            return B.make_result(
                status=B.STATUS_SKIPPED, raw=None, parsed=None,
                error="fusion adapter does not drive tools",
                field_provenance=self._components_provenance({}, {}, False))

        if self.facade is not None:
            try:
                return self._run_facade(view)
            except Exception as exc:  # noqa: BLE001
                return B.make_result(
                    status=B.STATUS_ERROR, raw=None, parsed=None, error=repr(exc),
                    field_provenance=self._components_provenance({}, {}, False),
                    failure_class=B.FAIL_INFRASTRUCTURE,
                    timings={"measured": "warm", "label": "fusion facade"})

        if self.decision_adapter is None:
            return B.make_result(
                status=B.STATUS_ERROR, raw=None, parsed=None,
                error="fusion has no decision component",
                field_provenance=self._components_provenance({}, {}, False),
                failure_class=B.FAIL_INFRASTRUCTURE)

        dec = self.decision_adapter.run_case(view, sandbox)
        decision_parsed = dec.get("output", {}).get("parsed") or {}
        category = decision_parsed.get("category")
        needs_reply = decision_parsed.get("needs_reply")
        confidence = decision_parsed.get("confidence")

        prose_parsed, prose_prov, prose_ok, prose_error = {}, {}, False, None
        if self.prose_adapter is not None:
            prose = self.prose_adapter.run_case(view, sandbox)
            p_out = prose.get("output", {})
            prose_parsed = p_out.get("parsed") or {}
            prose_prov = prose.get("field_provenance") or {}
            prose_ok = (prose.get("status") == B.STATUS_OK
                        and (prose_parsed.get("summary") is not None
                             or prose_parsed.get("reason") is not None))
            prose_error = p_out.get("error")

        if category is None:
            return B.make_result(
                status=B.STATUS_ERROR, raw=dec.get("output", {}).get("raw"),
                parsed=None, error=decision_parsed.get("error") or "no category",
                field_provenance=self._components_provenance(
                    dec.get("field_provenance") or {}, prose_prov, prose_ok),
                failure_class=B.FAIL_MODEL,
                output_extra={"components": {"decision": dec,
                                             "prose_error": prose_error}})

        parsed = {"category": category, "needs_reply": needs_reply,
                  "confidence": confidence,
                  "summary": prose_parsed.get("summary") if prose_ok else None,
                  "reason": prose_parsed.get("reason") if prose_ok else None}
        provenance = self._components_provenance(
            dec.get("field_provenance") or {}, prose_prov, prose_ok)
        return B.make_result(
            status=B.STATUS_OK, raw={"decision": dec.get("output", {}).get("raw"),
                                     "prose": (prose_parsed or None)},
            parsed=parsed, error=None, field_provenance=provenance,
            timings={"measured": "warm", "label": "fusion decision+prose"},
            request_sha256=dec.get("request_sha256", ""),
            output_extra={
                "components": {
                    "decision": {"adapter_id": getattr(self.decision_adapter,
                                                       "adapter_id", None),
                                 "provenance": dec.get("field_provenance"),
                                 "timings": dec.get("timings")},
                    "prose": {"adapter_id": getattr(self.prose_adapter,
                                                    "adapter_id", None),
                              "provenance": prose_prov, "ok": prose_ok,
                              "error": prose_error}},
                "component_provenance": {
                    "category": "decision", "needs_reply": "decision",
                    "confidence": "decision",
                    "summary": "prose" if prose_ok else "missing",
                    "reason": "prose" if prose_ok else "missing"},
            },
            capabilities_used={"decision": True, "prose": prose_ok})
