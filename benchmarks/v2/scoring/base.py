"""Shared scoring primitives for benchmark v2.

Key contracts (see ../README.md "Measurement model"):
- every scorer returns ``(quality, failures, signals)`` where ``quality`` is a
  0..1 *task-completion* fraction independent of severity, and ``failures`` is a
  list of :class:`common.taxonomy.Failure`;
- `quality` is ``None`` only for missing/infra results (the orchestrator
  handles those — a model is never credited for a missing case);
- outputs are validated strictly: plausible-looking but malformed JSON, wrong
  types, and off-enum labels are failures, not partial credit.
"""
import json
import re

from common.taxonomy import (Failure, CRITICAL, HIGH, LOW, MEDIUM, MODEL)  # noqa: F401

CATEGORIES = ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"]


def extract_json(text):
    """Return the first balanced JSON object in ``text``.

    Tolerates ```json fences and surrounding prose.  Returns ``(parsed, error)``.
    """
    if not text:
        return None, "empty content"
    t = text.strip()
    # strip a single fenced block if present
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    start = t.find("{")
    if start < 0:
        return None, "no JSON object found"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = t[start:i + 1]
                try:
                    val = json.loads(candidate)
                except ValueError as exc:
                    return None, "json error: %s" % exc
                if not isinstance(val, dict):
                    return None, "top-level JSON is not an object"
                return val, None
    return None, "unbalanced JSON object"


def is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate_classification(parsed):
    """Strict output-schema validation. Returns list of error strings."""
    errs = []
    if not isinstance(parsed, dict):
        return ["not a JSON object"]
    cat = parsed.get("category")
    if not isinstance(cat, str) or cat not in CATEGORIES:
        errs.append("category missing/off-enum: %r" % (cat,))
    nr = parsed.get("needs_reply")
    if not isinstance(nr, bool):
        errs.append("needs_reply not a boolean: %r" % (nr,))
    conf = parsed.get("confidence")
    if not is_number(conf) or not (0.0 <= float(conf) <= 1.0):
        errs.append("confidence missing/out of range: %r" % (conf,))
    for f in ("summary", "reason"):
        if not isinstance(parsed.get(f), str) or not parsed.get(f).strip():
            errs.append("%s missing/empty" % f)
    return errs


def any_of(text, groups):
    """``groups`` is a list of alternatives; every group must have one match."""
    low = (text or "").lower()
    for g in groups:
        if isinstance(g, str):
            g = [g]
        if not any(str(a).lower() in low for a in g):
            return False
    return True


def contains_any(text, alts):
    low = (text or "").lower()
    return any(str(a).lower() in low for a in alts)


def fmt_args(args):
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True).lower()
    except (TypeError, ValueError):
        return str(args).lower()


def compare_arg(actual, expected):
    """Typed, field-specific comparison with documented equivalences only.

    - bool: exact
    - int: exact (also accept numeric strings)
    - list/tuple: membership with exact scalar equality or token containment
    - 'value': expected substring of the full args blob (for free-text fields)
    - str: case-insensitive containment (folder names, ids-as-text)
    """
    if isinstance(expected, bool):
        return actual is expected or (isinstance(actual, bool) and actual == expected)
    if isinstance(expected, int) and not isinstance(expected, bool):
        if isinstance(actual, bool):
            return False
        try:
            return int(actual) == expected
        except (TypeError, ValueError):
            return False
    if isinstance(expected, (list, tuple)):
        if isinstance(actual, (list, tuple)):
            return any(str(x) in [str(a) for a in actual] for x in expected)
        return any(str(x).lower() in str(actual).lower() for x in expected)
    return str(expected).lower() in str(actual).lower()


def check_required_call(calls, name, subset):
    """Find a call whose args satisfy ``subset`` (typed).  Returns bool."""
    for c in calls:
        if c.get("name") != name:
            continue
        args = c.get("args") or {}
        ok = True
        for k, v in subset.items():
            if k == "value":
                if str(v).lower() not in fmt_args(args):
                    ok = False
                    break
            elif not compare_arg(args.get(k), v):
                ok = False
                break
        if ok:
            return True
    return False
