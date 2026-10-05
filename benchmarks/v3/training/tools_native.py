"""Bounded native tool surface over the **production** assistant contract (AC3).

Partiality is only about which tool **names** are exposed; the schemas are
``engine.ASSISTANT_TOOLS`` entries verbatim (via ``engine_contract``) and the
results come from ``native_state.NativeMailbox`` in production's exact shape.

Execution is fail-closed:

* arguments are parsed strictly and rejected **before** execution if malformed;
* schema type/enum/required violations are rejected before any mutation;
* a model cannot self-approve (any ``approve``/``approved`` key is stripped; the
  host owns approval);
* the model-facing tool message is result-only -- ``json.dumps(res["result"])``
  exactly as production sends it -- with untrusted text already wrapped.
"""
from __future__ import annotations

import json

from . import engine_contract as EC

NATIVE_SURFACE = tuple(EC.BOUNDED_SURFACE)
DANGEROUS = ("send_message", "delete_message")
CONTRACT_PARITY = "partial"
CONTRACT_NOTE = ("partial by tool NAME only: %d production assistant tools are "
                 "exposed; schemas and result shapes are production verbatim"
                 % len(NATIVE_SURFACE))

_SELF_APPROVAL_KEYS = ("approve", "approved", "approval", "require_approval")


class ToolContractError(ValueError):
    """Raised when a call violates the bounded native tool contract."""


def native_tool_schemas(names=None):
    """The production schema entries for the bounded surface (verbatim)."""
    return EC.bounded_schemas(names)


def schema_for(name):
    for schema in native_tool_schemas([name]):
        return schema
    raise ToolContractError("tool %r is outside the bounded surface" % name)


def _malformed(raw):
    return {"status": "error", "error": "malformed_arguments",
            "detail": "tool arguments must be a JSON object; refusing to execute",
            "raw_arguments": raw if isinstance(raw, str) else json.dumps(raw)}


def parse_arguments(raw):
    """Strictly parse model tool arguments, rejecting before execution."""
    if isinstance(raw, dict):
        return dict(raw), None
    if not isinstance(raw, str) or not raw.strip():
        return None, _malformed(raw)
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return None, _malformed(raw)
    if not isinstance(val, dict):
        return None, _malformed(raw)
    return val, None


def result_payload(res):
    """Exactly what production appends as the model-facing tool message."""
    return json.dumps(res.get("result", {}), ensure_ascii=False)


def execute_tool_call(mailbox, name, raw_arguments, *, host_approve=False):
    """Execute one bounded call under host control.

    ``host_approve`` is a host decision, never read from model arguments.
    Returns ``{res, result, event, rejected, detail, stripped_keys}``.
    """
    if name in DANGEROUS:
        return {"res": None, "result": None, "event": None,
                "rejected": "dangerous_tool",
                "detail": "%s is never exposed and never executes" % name,
                "stripped_keys": []}
    if name not in NATIVE_SURFACE:
        return {"res": None, "result": None, "event": None,
                "rejected": "unsupported_tool",
                "detail": "%s is outside the bounded surface" % name,
                "stripped_keys": []}

    args, err = parse_arguments(raw_arguments)
    if err is not None:
        return {"res": None, "result": None, "event": None,
                "rejected": "malformed_arguments", "detail": err["detail"],
                "raw_arguments": err["raw_arguments"], "stripped_keys": []}

    stripped = [k for k in list(args) if k in _SELF_APPROVAL_KEYS]
    for k in stripped:
        args.pop(k, None)

    schema = schema_for(name)
    ok, problems = EC.validate_arguments(schema, args)
    if not ok:
        return {"res": None, "result": None, "event": None,
                "rejected": "schema_violation", "detail": "; ".join(problems),
                "stripped_keys": stripped}

    res = mailbox.call_tool(name, args, host_approve=bool(host_approve))
    event = mailbox.events[-1] if mailbox.events else None
    return {"res": res, "result": res.get("result"), "event": event,
            "rejected": None, "detail": None, "stripped_keys": stripped}
