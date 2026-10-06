"""Authoritative production assistant contract, extracted without importing engine.

The production surface is `engine.ASSISTANT_TOOLS` plus `_fn` and
`_wrap_untrusted` (and the rule validators `_validate_rule`/`_rule_brief`).  This
module compiles **only those functions and constants** out of the live
``engine.py`` by isolated AST extraction -- exactly the technique
``tests/test_contracts.py`` uses for ``classify`` -- so no ``.env``, database,
worker, network or config is touched and the contract can never silently drift.

A "partial" surface is a subset of tool **names**; every schema entry returned
here is the production entry verbatim (byte/structurally identical), never a
sandbox look-alike and never annotated with extra keys.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ENGINE_PATH = os.path.join(REPO_ROOT, "engine.py")

# Tools whose execution and argument validation the training slice implements.
BOUNDED_SURFACE = ("search_messages", "read_message", "move_message",
                   "list_rules", "propose_rule")

_WANT_FUNCS = ("_fn", "_wrap_untrusted", "_safe_json", "_truncate", "_as_bool",
               "_rule_brief", "_validate_rule", "_rules_to_text", "_cond_norm",
               "rule_similarity")
_WANT_ASSIGNS = ("UNTRUSTED_TAG", "ALLOWED_FIELDS", "ALLOWED_OPS",
                 "ASSISTANT_TOOLS")

_CACHE = {}


class EngineContractError(RuntimeError):
    """Raised when the production assistant contract cannot be extracted."""


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract():
    if _CACHE:
        return _CACHE
    src = _read(ENGINE_PATH)
    tree = ast.parse(src)
    chunks = []
    seen = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            chunks.append(ast.get_source_segment(src, node))
            seen.add(node.name)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in _WANT_ASSIGNS:
            chunks.append(ast.get_source_segment(src, node))
            seen.add(node.targets[0].id)
    missing = (set(_WANT_FUNCS) | set(_WANT_ASSIGNS)) - seen
    if missing:
        raise EngineContractError(
            "engine.py is missing extracted symbols: %s" % ", ".join(sorted(missing)))
    ns = {"re": re, "json": json}
    exec("\n\n".join(chunks), ns)  # noqa: S102 - trusted in-repo source, no import
    _CACHE.update(ns)
    return _CACHE


def engine_sha256():
    from ..common.hashing import sha256_file
    return sha256_file(ENGINE_PATH)


def assistant_tools():
    """The full production ASSISTANT_TOOLS list (deep copy)."""
    return copy.deepcopy(_extract()["ASSISTANT_TOOLS"])


def assistant_tool_names():
    return [t["function"]["name"] for t in assistant_tools()]


def bounded_schemas(names=None):
    """Production schema entries for the bounded surface, verbatim.

    An unexposed name raises (a bounded surface limits tool names, never rewrites
    or annotates the production schema).
    """
    wanted = list(BOUNDED_SURFACE if names is None else names)
    by_name = {t["function"]["name"]: t for t in assistant_tools()}
    out = []
    for name in wanted:
        if name not in by_name:
            raise EngineContractError(
                "tool %r is not in engine.ASSISTANT_TOOLS" % name)
        out.append(copy.deepcopy(by_name[name]))
    return out


def wrap_untrusted(value):
    """The production ``_wrap_untrusted`` (delimiter neutralisation included)."""
    return _extract()["_wrap_untrusted"](value)


def truncate(text, limit):
    return _extract()["_truncate"](text, limit)


def as_bool(value):
    return _extract()["_as_bool"](value)


def rule_brief(rule):
    return _extract()["_rule_brief"](rule)


def validate_rule(proposal):
    """Production ``_validate_rule`` -> ``(normalized | None, [errors])``."""
    return _extract()["_validate_rule"](proposal)


def rules_to_text(rules):
    return _extract()["_rules_to_text"](rules)


def rule_similarity(new_rule, rules):
    return _extract()["rule_similarity"](new_rule, rules)


def untrusted_tag():
    return _extract()["UNTRUSTED_TAG"]


def allowed_fields():
    return tuple(_extract()["ALLOWED_FIELDS"])


def allowed_ops():
    return tuple(_extract()["ALLOWED_OPS"])


def schema_types(schema):
    """A compact ``{prop: (type, enum)}`` view of a parameters object."""
    params = schema["function"].get("parameters") or {}
    props = params.get("properties") or {}
    out = {}
    for name, spec in props.items():
        out[name] = (spec.get("type"), tuple(spec.get("enum") or ()))
    return out


def validate_arguments(schema, args):
    """Reject production schema type/enum violations before any execution.

    Returns ``(ok, problems)``.  Nested array items are checked for their item
    type/enum where the production schema declares them.  Unknown keys are
    allowed (production schemas do not set ``additionalProperties``), matching
    engine behaviour.
    """
    problems = []
    if not isinstance(args, dict):
        return False, ["arguments must be an object"]
    params = schema["function"].get("parameters") or {}
    props = params.get("properties") or {}
    for key, spec in props.items():
        if key not in args or args[key] is None:
            continue
        val = args[key]
        typ = spec.get("type")
        if typ == "integer":
            if isinstance(val, bool) or not isinstance(val, int):
                if not (isinstance(val, str) and val.strip().lstrip("-").isdigit()):
                    problems.append("%s must be an integer" % key)
        elif typ == "string":
            if not isinstance(val, str):
                problems.append("%s must be a string" % key)
        elif typ == "boolean":
            if not isinstance(val, bool):
                problems.append("%s must be a boolean" % key)
        elif typ == "array":
            if not isinstance(val, list):
                problems.append("%s must be an array" % key)
            else:
                items = spec.get("items") or {}
                for i, item in enumerate(val):
                    if items.get("type") == "object" and not isinstance(item, dict):
                        problems.append("%s[%d] must be an object" % (key, i))
                        continue
                    for iprop, ispec in (items.get("properties") or {}).items():
                        if iprop not in item:
                            continue
                        iv = item[iprop]
                        ityp = ispec.get("type")
                        ienum = ispec.get("enum")
                        if ityp == "string" and not isinstance(iv, str):
                            problems.append("%s[%d].%s must be a string" % (key, i, iprop))
                        elif ityp == "integer" and (isinstance(iv, bool)
                                                    or not isinstance(iv, int)):
                            problems.append("%s[%d].%s must be an integer" % (key, i, iprop))
                        elif ityp == "boolean" and not isinstance(iv, bool):
                            problems.append("%s[%d].%s must be a boolean" % (key, i, iprop))
                        if ienum and iv not in ienum:
                            problems.append("%s[%d].%s=%r not in %s"
                                            % (key, i, iprop, iv, list(ienum)))
        enum = spec.get("enum")
        if enum and val not in enum:
            problems.append("%s=%r not in %s" % (key, val, list(enum)))
    for req in params.get("required") or []:
        if args.get(req) in (None, ""):
            problems.append("%s is required" % req)
    return (not problems), problems
