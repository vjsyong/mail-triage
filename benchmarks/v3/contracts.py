"""Frozen application contracts for benchmark v3 (WP0/WP1).

This module is the executable interface later disjoint packages build against:

* **Native triage** reconstructs the pinned ``engine.LLMClient.classify``
  request (system prompt, header/``snippet[:1500]`` user body, MIME salvage)
  and its JSON parsing exactly.  Parity is certified by
  ``tests/test_contracts.py`` via isolated AST extraction of the live engine --
  no ``.env`` load, no database, no worker, no network, and no improved parser
  ever receives "native" credit.
* **Policy-conditioned** and **full-context** are separately named profiles and
  are never silently folded into native.
* Adapters declare field provenance (``produced`` / ``derived`` / ``missing``);
  decision-only adapters never fabricate prose.

Function/data-shape contract (frozen here, see ``README.md`` for the summary):

``build_native_request(msg, categories=None, owner="") -> dict``
``build_policy_request(msg, policy, categories=None, owner="") -> dict``
``build_full_context_request(msg, full_body, categories=None, owner="") -> dict``
``parse_native_response(content) -> dict``
``native_output(result) -> dict``
``decision_only_provenance(...) -> dict``
``find_gold_leakage(model_input, gold) -> list[str]``
``observable_in(level, profile) -> bool``
"""
import json
import re

from .common.hashing import canonical
from .common.mime import clean_snippet

# ----------------------------------------------------------------- profiles

NATIVE_PROFILE = "native"
POLICY_PROFILE = "policy_conditioned"
FULL_CONTEXT_PROFILE = "full_context"
WORKFLOW_PROFILE = "workflow"
TRIAGE_PROFILES = (NATIVE_PROFILE, POLICY_PROFILE, FULL_CONTEXT_PROFILE)
BENCHMARK_PROFILES = TRIAGE_PROFILES + (WORKFLOW_PROFILE,)
# Retrieval only exists inside the workflow sandbox; the triage profiles never
# claim it.  This package defines the name but ships no workflow renderer.
WORKFLOW_PROFILES = (WORKFLOW_PROFILE,)

# Native input contract (must match engine.LLMClient.classify).
SNIPPET_LIMIT = 1500
# Full-context diagnostic is deliberately richer than the native boundary.
FULL_CONTEXT_LIMIT = 6000
DEFAULT_CATEGORIES = ("Action", "Notification", "Newsletter", "Receipt",
                      "Personal", "Promo")
NATIVE_HEADER_FIELDS = ("from_addr", "to_addr", "subject", "date")
NATIVE_OUTPUT_FIELDS = ("category", "needs_reply", "confidence", "summary", "reason")

# Request parameters the pinned classifier uses (frozen for adapters).
NATIVE_MAX_TOKENS = 4096
NATIVE_RETRY_MAX_TOKENS = 8192
NATIVE_JSON_MODE = True
NATIVE_FULL = True

# ----------------------------------------------------------------- provenance

FIELD_PRODUCED = "produced"
FIELD_DERIVED = "derived"
FIELD_MISSING = "missing"
FIELD_PROVENANCE_VALUES = (FIELD_PRODUCED, FIELD_DERIVED, FIELD_MISSING)

# Observability of a decision's supporting evidence.
OBSERVABILITY_VISIBLE = "visible"
OBSERVABILITY_RETRIEVABLE = "retrievable"
OBSERVABILITY_FULL_CONTEXT = "full_context"
OBSERVABILITY_AMBIGUOUS = "ambiguous"
OBSERVABILITY_UNAVAILABLE = "unavailable"
OBSERVABILITY_VALUES = (OBSERVABILITY_VISIBLE, OBSERVABILITY_RETRIEVABLE,
                        OBSERVABILITY_FULL_CONTEXT, OBSERVABILITY_AMBIGUOUS,
                        OBSERVABILITY_UNAVAILABLE)


class ContractError(ValueError):
    """Raised when a request violates the frozen application contract."""


class NativeParseError(RuntimeError):
    """Raised when native JSON parsing fails the way ``classify`` fails.

    Subclasses ``RuntimeError`` because the pinned engine raises
    ``RuntimeError`` for both "no JSON object" and "missing category".  A
    genuinely unparseable object (e.g. two concatenated JSON objects) raises
    :class:`json.JSONDecodeError`, exactly as the engine does -- that behaviour
    is preserved, not "fixed".
    """


class GoldLeakageError(ValueError):
    """Raised when gold/adjudication material appears in model-facing input."""


# ----------------------------------------------------------------- native prompt

def native_system_prompt(categories=None, owner=""):
    """Reproduce ``classify``'s system prompt with a dynamic category enum."""
    cats = ", ".join(categories) if categories else ", ".join(DEFAULT_CATEGORIES)
    return ("You triage incoming email for %s. Reply with a single JSON object "
            "and nothing else. Shape: {\"category\": one of [%s], "
            "\"needs_reply\": true|false, \"confidence\": 0.0-1.0, "
            "\"summary\": \"one short sentence saying what the email is\", "
            "\"reason\": \"why that category, max 15 words\"}"
            % (owner or "the account owner", cats))


def native_user_body(msg):
    """Header block + cleaned ``snippet[:1500]``, exactly as ``classify``."""
    body = clean_snippet(msg.get("snippet"), limit=SNIPPET_LIMIT)
    return ("From: %s\nTo: %s\nSubject: %s\nDate: %s\n\n%s"
            % (msg.get("from_addr", ""), msg.get("to_addr", ""),
               msg.get("subject", ""), msg.get("date", ""),
               body[:SNIPPET_LIMIT]))


def build_native_request(msg, categories=None, owner=""):
    """The native triage request through the current application boundary."""
    cats = list(categories) if categories else list(DEFAULT_CATEGORIES)
    return {
        "profile": NATIVE_PROFILE,
        "owner": owner,
        "categories": cats,
        "system": native_system_prompt(cats, owner),
        "user": native_user_body(msg),
        "params": {"json_mode": NATIVE_JSON_MODE,
                   "max_tokens": NATIVE_MAX_TOKENS,
                   "retry_max_tokens": NATIVE_RETRY_MAX_TOKENS,
                   "full": NATIVE_FULL},
    }


def policy_category_names(policy):
    """Category names from a policy card (objects ``{name, ...}`` or strings)."""
    names = []
    for c in (policy or {}).get("categories") or []:
        if isinstance(c, dict):
            name = str(c.get("name") or "")
        else:
            name = str(c)
        if name:
            names.append(name)
    return names


def build_policy_request(msg, policy, categories=None, owner=""):
    """Policy-conditioned triage: same information budget plus a trusted card.

    The policy card is an explicit extension to the native prompt; the returned
    ``profile`` is ``policy_conditioned`` so it can never be pooled with native.
    """
    if not isinstance(policy, dict) or not policy.get("policy_id"):
        raise ContractError("policy card requires a non-empty policy_id")
    cats = list(categories) if categories else policy_category_names(policy)
    if not cats:
        cats = list(DEFAULT_CATEGORIES)
    base = build_native_request(msg, cats, owner)
    return {
        "profile": POLICY_PROFILE,
        "owner": owner,
        "categories": base["categories"],
        "system": base["system"],
        "user": base["user"],
        "policy_id": policy["policy_id"],
        "policy": policy,
        "params": base["params"],
    }


def build_full_context_request(msg, full_body, categories=None, owner=""):
    """Full-context diagnostic: same prompt/headers, richer body, separate name.

    The body is not clipped at the native 1500-character boundary; it uses the
    declared ``FULL_CONTEXT_LIMIT`` so the diagnostic can show what clipping
    costs.  Results are reported separately from native triage.
    """
    base = build_native_request(msg, categories, owner)
    return {
        "profile": FULL_CONTEXT_PROFILE,
        "owner": owner,
        "categories": base["categories"],
        "system": base["system"],
        "user": ("From: %s\nTo: %s\nSubject: %s\nDate: %s\n\n%s"
                 % (msg.get("from_addr", ""), msg.get("to_addr", ""),
                    msg.get("subject", ""), msg.get("date", ""),
                    (full_body or "")[:FULL_CONTEXT_LIMIT])),
        "params": base["params"],
    }


# ----------------------------------------------------------------- native parsing

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def parse_native_response(content):
    """Parse a native model response the way ``classify`` does.

    Reproduces the greedy ``re.search(r"\\{.*\\}", content, re.S)`` + strict
    ``json.loads`` + category presence check.  Two concatenated JSON objects
    therefore raise ``json.JSONDecodeError`` (greedy match is not valid JSON),
    matching production rather than papering over it.
    """
    m = _JSON_OBJECT.search(content or "")
    if not m:
        raise NativeParseError("LLM returned no JSON: %r" % (content or "")[:200])
    result = json.loads(m.group(0))  # JSONDecodeError propagates, as in engine
    if not isinstance(result, dict) or not result.get("category"):
        raise NativeParseError("LLM JSON missing category: %r" % result)
    return result


def native_output(result):
    """Keep only the declared native fields present in a parsed result."""
    if not isinstance(result, dict):
        return {}
    return {k: result[k] for k in NATIVE_OUTPUT_FIELDS if k in result}


# ----------------------------------------------------------------- provenance

def decision_only_provenance(category=FIELD_PRODUCED, needs_reply=FIELD_PRODUCED,
                             confidence=FIELD_MISSING):
    """Field provenance for a decision-only adapter (e.g. TinyJev).

    Prose fields are always ``missing`` -- a decision-only head cannot produce
    summaries or reasons, and must not be credited as if it had.
    """
    return {"category": category, "needs_reply": needs_reply,
            "confidence": confidence,
            "summary": FIELD_MISSING, "reason": FIELD_MISSING}


def validate_provenance(provenance):
    """Return a list of provenance problems ([] when valid)."""
    errs = []
    if not isinstance(provenance, dict):
        return ["provenance must be a mapping"]
    for field in NATIVE_OUTPUT_FIELDS:
        if provenance.get(field) not in FIELD_PROVENANCE_VALUES:
            errs.append("field %r must be one of %s"
                        % (field, "/".join(FIELD_PROVENANCE_VALUES)))
    return errs


def has_fabricated_prose(provenance):
    """True when prose fields are claimed by an adapter that cannot produce them."""
    return (provenance.get("summary") in (FIELD_PRODUCED, FIELD_DERIVED)
            or provenance.get("reason") in (FIELD_PRODUCED, FIELD_DERIVED))


# ----------------------------------------------------------------- observability

def observable_in(level, profile):
    """Whether gold evidence at ``level`` is answerable in ``profile``."""
    if level == OBSERVABILITY_VISIBLE:
        return True
    if level == OBSERVABILITY_RETRIEVABLE:
        # Retrieval only exists in the workflow sandbox, not in triage profiles.
        return profile in WORKFLOW_PROFILES
    if level == OBSERVABILITY_FULL_CONTEXT:
        return profile == FULL_CONTEXT_PROFILE
    return False


def project_observable_fields(observable, profile):
    """Map ``{field: observability}`` to ``{field: answerable?}`` for a profile."""
    return {field: observable_in(level, profile)
            for field, level in (observable or {}).items()}


# ----------------------------------------------------------------- gold isolation

MIN_LEAK_LEN = 4


def _as_text(value):
    if isinstance(value, str):
        return value
    return canonical(value)


def gold_leakage_terms(gold):
    """Strings in a gold record that must never reach model-facing input."""
    terms = []
    if isinstance(gold, dict):
        for s in gold.get("hidden_evidence") or []:
            if isinstance(s, str) and len(s.strip()) >= MIN_LEAK_LEN:
                terms.append(s.strip())
        for key in ("gold_id",):
            v = gold.get(key)
            if isinstance(v, str) and len(v) >= MIN_LEAK_LEN:
                terms.append(v)
    return terms


def find_gold_leakage(model_input, gold):
    """Return the gold terms found in model-facing input ([] when clean)."""
    text = _as_text(model_input)
    if not text:
        return []
    hits = []
    if isinstance(gold, dict):
        blob = canonical(gold)
        if blob and len(blob) <= 4000 and blob in text:
            hits.append("<serialized-gold>")
    low = text.lower()
    for term in gold_leakage_terms(gold):
        if term.lower() in low:
            hits.append(term)
    return sorted(set(hits))


def assert_no_gold_leakage(model_input, gold):
    """Raise :class:`GoldLeakageError` if gold material is in model input."""
    hits = find_gold_leakage(model_input, gold)
    if hits:
        raise GoldLeakageError("gold leaked into model-facing input: %s"
                               % "; ".join(hits))
    return True
