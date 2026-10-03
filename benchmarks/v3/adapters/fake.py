"""Offline scripted/mock adapter for benchmark v3 (WP4).

This adapter is **explicitly a mock**: it replays a caller-supplied script keyed
by ``case_id`` and never consults gold.  It is the offline driver the acceptance
flow uses, and it is disqualified as a real baseline (``mock=True``,
``qualifies_as_baseline=False``) so it can never be reported as one.

It supports all three task shapes so the offline acceptance flow can exercise
decision, full-response and workflow cases without a model or network:

* decision/full_response -- a scripted raw response string (parsed with the
  production native parser, exercising raw/native parity) or a scripted parsed
  dict;
* workflow -- a scripted list of ``{"tool", "args", "approve"}`` steps executed
  against a fresh sandbox, plus an optional scripted reply.
"""
from __future__ import annotations

import json

from ..contracts import parse_native_response, native_output, rendered_input_hash
from . import base as B


class FakeAdapter(B.Adapter):
    adapter_id = "offline-fake"
    revision = "v3.0"
    capabilities = {"decision": True, "prose": True, "tools": True,
                    "native_parse": True}
    mock = True
    qualifies_as_baseline = False
    confidence_meaning = "scripted; not a model confidence"
    prompt_revision = "scripted-v3.0"

    def __init__(self, predictions=None, workflow_script=None,
                 workflow_replies=None, model_key="offline-fake",
                 model_revision="mock-1", generation_config=None,
                 runtime_config=None):
        super().__init__(generation_config or {"temperature": 0, "deterministic": True},
                         runtime_config or {"backend": "scripted", "network": False})
        self.predictions = dict(predictions or {})
        self.workflow_script = dict(workflow_script or {})
        self.workflow_replies = dict(workflow_replies or {})
        self.model_key = model_key
        self.model_revision = model_revision

    # -------------------------------------------------------------- helpers

    def _decision_result(self, view, spec):
        semantic_hash = rendered_input_hash(view.get("rendered_input") or {},
                                            mailbox=view.get("mailbox"),
                                            tools=view.get("tools"))
        raw, parsed, error = None, None, None
        if isinstance(spec, str):
            raw = spec
            try:
                parsed = native_output(parse_native_response(raw))
            except Exception as exc:  # noqa: BLE001 - preserve the exact failure
                error = "%s: %s" % (type(exc).__name__, exc)
                return B.make_result(
                    status=B.STATUS_ERROR, raw=raw, parsed=None, error=error,
                    field_provenance=B.provenance_for(None),
                    failure_class=B.FAIL_MODEL, request_sha256=semantic_hash,
                    timings={"wall_s": 0.0, "measured": "simulated", "label": "scripted"})
        elif isinstance(spec, dict) and "raw" in spec:
            raw = spec["raw"]
            if spec.get("parsed") is not None:
                parsed = spec["parsed"]
            else:
                try:
                    parsed = native_output(parse_native_response(raw))
                except Exception as exc:  # noqa: BLE001
                    error = "%s: %s" % (type(exc).__name__, exc)
        elif isinstance(spec, dict):
            parsed = dict(spec)
            raw = json.dumps(spec, sort_keys=True)
        else:
            return B.make_result(status=B.STATUS_MISSING, raw=None, parsed=None,
                                 error="unusable script",
                                 field_provenance=B.provenance_for(None),
                                 failure_class=B.FAIL_MODEL)
        status = B.STATUS_ERROR if error else B.STATUS_OK
        return B.make_result(
            status=status, raw=raw, parsed=parsed, error=error,
            field_provenance=B.provenance_for(parsed),
            failure_class=B.FAIL_MODEL if error else None,
            request_sha256=semantic_hash,
            timings={"wall_s": 0.0, "measured": "simulated", "label": "scripted"},
            capabilities_used={"decision": True,
                               "prose": bool(parsed and parsed.get("summary") is not None)})

    def _workflow_result(self, view, sandbox):
        if sandbox is None:
            return B.make_result(status=B.STATUS_ERROR, raw=None, parsed=None,
                                 error="workflow case requires a sandbox",
                                 field_provenance=B.provenance_for(None),
                                 failure_class=B.FAIL_INFRASTRUCTURE)
        case_id = view["case_id"]
        script = self.workflow_script.get(case_id) or []
        for step in script:
            if not isinstance(step, dict) or "tool" not in step:
                continue
            sandbox.execute(step["tool"], step.get("args") or {},
                            approve=bool(step.get("approve")))
        reply = self.workflow_replies.get(case_id, "")
        raw = json.dumps({"script": script, "reply": reply}, sort_keys=True)
        parsed = {"task": "workflow", "completed": bool(script), "answer": reply}
        return B.make_result(
            status=B.STATUS_OK, raw=raw, parsed=parsed, error=None,
            field_provenance=B.provenance_for(None),
            tool_events=list(sandbox.events),
            request_sha256=rendered_input_hash(
                view.get("rendered_input") or {},
                mailbox=view.get("mailbox"), tools=view.get("tools")),
            timings={"wall_s": 0.0, "measured": "simulated",
                     "label": "scripted workflow"},
            capabilities_used={"tools": True})

    # ---------------------------------------------------------------- run

    def run_case(self, view, sandbox=None):
        task = view.get("task")
        if task == "workflow":
            return self._workflow_result(view, sandbox)
        spec = self.predictions.get(view.get("case_id"))
        if spec is None:
            return B.make_result(
                status=B.STATUS_MISSING, raw=None, parsed=None,
                error="no scripted prediction for %s" % view.get("case_id"),
                field_provenance=B.provenance_for(None),
                failure_class=B.FAIL_MODEL,
                request_sha256=rendered_input_hash(
                    view.get("rendered_input") or {}),
                timings={"wall_s": 0.0, "measured": "simulated", "label": "scripted"})
        return self._decision_result(view, spec)
