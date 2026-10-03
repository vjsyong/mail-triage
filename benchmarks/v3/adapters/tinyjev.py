"""TinyJev typed-decision adapter for benchmark v3 (WP4).

Wraps the installed TinyJev API (:mod:`tinyjev`, lazily imported) described in
``/home/xrim/tinyjev-spike/run_tinyjev.py`` and ``fusion/server.py``:
``agent.predict({"state", "questions"})`` returns typed answers where a
``choice`` question yields ``{"choice", "confidence", "probabilities"}`` and a
``boolean``/``noul`` question yields ``{"p_true", ...}``.

Contract care:

* the category enum is built from the **case's declared categories** (falling
  back to the trusted policy card's categories, then the app defaults), and the
  exact option order is recorded so a report can map a probability back to its
  option;
* the two uncertainty values have **separately declared meanings**; the native
  single ``confidence`` field is a declared combination and is never treated as
  two calibrated signals;
* the head is decision-only: ``summary`` and ``reason`` are recorded ``missing``
  and are **never fabricated**;
* ``raw`` preserves the actual model answer returned by the head (not the
  request payload); the request payload digest is recorded separately as
  ``wire_sha256`` and the semantic rendered-input hash as ``request_sha256``.
"""
from __future__ import annotations

import json

from ..common.hashing import canonical, sha256_text
from ..contracts import DEFAULT_CATEGORIES, decision_only_provenance, \
    rendered_input_hash
from . import base as B

DEFAULT_DESCRIPTIONS = {
    "Action": "Needs the owner to do something: reply, decide, submit, pay, or act",
    "Notification": "Automatic status updates and notices that need no action",
    "Newsletter": "Recurring editorial or digest content the owner subscribed to",
    "Receipt": "Order confirmations, invoices, and payment receipts",
    "Personal": "Mail from friends, family, or personal contacts",
    "Promo": "Marketing, offers, and sales promotions",
}

CATEGORY_INSTRUCTIONS = "Which category does this email belong to?"
REPLY_INSTRUCTIONS = ("Does this email need a reply from the account owner? "
                      "Answer for {owner}.")

# The category answer and the reply answer are two separate signals.  The native
# ``confidence`` field is a declared combination of them, NOT a calibrated pair.
CONFIDENCE_MEANING = {
    "category": "choice-answer confidence over the recorded option order",
    "needs_reply": "p_true from the boolean/noul answer",
    "native_confidence": "min(category_confidence, max(p_true, 1-p_true)): a "
                         "declared single uncertainty, not both signals and not "
                         "a calibrated probability",
}


class TinyJevAdapter(B.Adapter):
    adapter_id = "tinyjev-decision"
    revision = "v3.0"
    capabilities = {"decision": True, "prose": False, "tools": False,
                    "native_parse": False}
    mock = False
    qualifies_as_baseline = True
    confidence_meaning = json.dumps(CONFIDENCE_MEANING, sort_keys=True)
    prompt_revision = "tinyjev-typed-v1"

    def __init__(self, model="TinyJev-0.6B", device="cpu", agent=None,
                 categories=None, descriptions=None, model_revision=None,
                 model_artifact_sha256=None, temperature=1.0):
        cats = list(categories) if categories else list(DEFAULT_CATEGORIES)
        desc = dict(DEFAULT_DESCRIPTIONS)
        desc.update(descriptions or {})
        self.categories = cats
        self.descriptions = {c: desc.get(c, "") for c in cats}
        super().__init__(
            generation_config={"temperature": temperature, "option_order": cats,
                               "confidence_meaning": CONFIDENCE_MEANING},
            runtime_config={"backend": "tinyjev", "device": device})
        self.model = model
        self.device = device
        self.model_key = model
        self.model_revision = model_revision
        self.model_artifact_sha256 = model_artifact_sha256
        self.temperature = temperature
        self._agent = agent

    def _load_agent(self):
        """Load and **cache** the TinyJev agent (never reload per case)."""
        if self._agent is None:
            try:
                import tinyjev  # noqa: PLC0415 - optional dependency, lazy by design
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise B.AdapterError("TinyJev is not installed: %s" % exc) from exc
            self._agent = tinyjev.load(self.model, device=self.device)
        return self._agent

    def _render_context(self, view):
        rendered = view.get("rendered_input") or {}
        policy = view.get("policy") or {}
        cats = list(rendered.get("categories") or []) or \
            list(self.categories)
        if not cats:
            for entry in policy.get("categories") or []:
                name = entry.get("name") if isinstance(entry, dict) else entry
                if name:
                    cats.append(str(name))
        if not cats:
            cats = list(DEFAULT_CATEGORIES)
        desc = dict(DEFAULT_DESCRIPTIONS)
        for entry in policy.get("categories") or []:
            if isinstance(entry, dict) and entry.get("name"):
                desc[str(entry["name"])] = str(entry.get("description") or
                                              desc.get(str(entry["name"]), ""))
        descriptions = {c: desc.get(c, "") for c in cats}
        owner = rendered.get("owner") or policy.get("owner") or ""
        return cats, descriptions, owner

    def _payload(self, user, cats, descriptions, owner):
        return {
            "state": user,
            "questions": {
                "category": {"type": "choice",
                             "instructions": CATEGORY_INSTRUCTIONS,
                             "criteria": dict(descriptions)},
                "needs_reply": {"type": "noul",
                                "instructions": REPLY_INSTRUCTIONS.format(
                                    owner=owner or "the account owner")},
            },
        }

    def run_case(self, view, sandbox=None):
        if view.get("task") != "decision":
            return B.make_result(
                status=B.STATUS_SKIPPED, raw=None, parsed=None,
                error="tinyjev adapter is decision-only",
                field_provenance=decision_only_provenance(confidence="missing"),
                capabilities_used={"decision": True})
        rendered = view.get("rendered_input") or {}
        user = rendered.get("user") or ""
        cats, descriptions, owner = self._render_context(view)
        payload = self._payload(user, cats, descriptions, owner)
        semantic_hash = rendered_input_hash(rendered,
                                            mailbox=view.get("mailbox"),
                                            tools=view.get("tools"))
        wire_hash = sha256_text(canonical(payload))
        try:
            agent = self._load_agent()
            out = agent.predict(payload, temperature=self.temperature)
            answers = out["states"][0]["answers"]
            choice = answers["category"]
            cat = choice["choice"]
            cat_conf = float(choice["confidence"])
            p_true = float(answers["needs_reply"]["p_true"])
        except Exception as exc:  # noqa: BLE001
            return B.make_result(
                status=B.STATUS_ERROR, raw=canonical(payload), parsed=None,
                error="%s: %s" % (type(exc).__name__, exc),
                field_provenance=decision_only_provenance(confidence="missing"),
                failure_class=B.FAIL_MODEL, request_sha256=semantic_hash,
                output_extra={"wire_sha256": wire_hash})

        combined = round(min(cat_conf, max(p_true, 1.0 - p_true)), 3)
        parsed = {"category": cat, "needs_reply": p_true >= 0.5,
                  "confidence": combined}
        provenance = decision_only_provenance(confidence="derived")
        output_extra = {
            "wire_sha256": wire_hash,
            "tinyjev": {
                "category_probabilities": choice.get("probabilities"),
                "category_confidence": cat_conf,
                "needs_reply_p_true": p_true,
                "option_order": list(cats),
                "confidence_meaning": CONFIDENCE_MEANING,
                "model_ms": (out.get("execution") or {}).get("model_ms"),
                "backend": (out.get("model") or {}).get("backend"),
                "synthesized_summary_reason": False,
            },
        }
        return B.make_result(
            status=B.STATUS_OK, raw=canonical(out), parsed=parsed, error=None,
            field_provenance=provenance,
            timings={"measured": "warm", "label": "tinyjev typed decision"},
            request_sha256=semantic_hash,
            output_extra=output_extra,
            capabilities_used={"decision": True, "prose": False})
