"""Tiny dependency-free JSON-Schema-subset validator for v3 artifacts.

The committed JSON Schemas under ``benchmarks/v3/schemas/`` are the single
source of truth; this validator exists only so v3 build/scoring code can run in
a bare stdlib venv without pinning ``jsonschema``.  It supports the subset used
by the v3 schemas: type, required, properties, additionalProperties, items,
enum, const, pattern, minLength, minItems, maxItems, minimum, maximum, oneOf,
anyOf.

Adapted from ``benchmarks/v2/common/validation.py`` (extended with ``const``
and ``anyOf``); no v2 runtime import.
"""
import json
import os
import re

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "..", "schemas")


class ValidationError(ValueError):
    pass


def load_schema(name):
    path = os.path.join(SCHEMA_DIR, name if name.endswith(".json")
                        else name + ".schema.json")
    with open(path) as f:
        return json.load(f)


def _type_ok(value, t):
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return True


def validate(instance, schema, path="$"):
    errs = []
    if "const" in schema and instance != schema["const"]:
        return ["%s: expected const %r, got %r" % (path, schema["const"], instance)]
    if "anyOf" in schema:
        ok = False
        first = ""
        for sub in schema["anyOf"]:
            if not validate(instance, sub, path):
                ok = True
                break
            if not first:
                first = sub
        if not ok:
            return ["%s: matched no anyOf alternative (%s)" % (path, first)]
        return errs
    if "oneOf" in schema:
        matches = 0
        first_errs = []
        for sub in schema["oneOf"]:
            if not validate(instance, sub, path):
                matches += 1
            elif not first_errs:
                first_errs = validate(instance, sub, path)
        if matches != 1:
            return errs + ["%s: expected exactly one of oneOf (%d matched): %s"
                           % (path, matches, first_errs[0] if first_errs else "")]
        return errs

    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(instance, x) for x in types):
            return ["%s: expected %s, got %s"
                    % (path, t, type(instance).__name__)]

    if "enum" in schema and instance not in schema["enum"]:
        errs.append("%s: %r not in enum %r" % (path, instance, schema["enum"]))
    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errs.append("%s: %r does not match %r" % (path, instance, schema["pattern"]))
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errs.append("%s: shorter than minLength %d" % (path, schema["minLength"]))
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errs.append("%s: %r < minimum %r" % (path, instance, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errs.append("%s: %r > maximum %r" % (path, instance, schema["maximum"]))

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errs.append("%s: missing required property %r" % (path, req))
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and props:
            for k in sorted(set(instance) - set(props)):
                errs.append("%s: unexpected property %r" % (path, k))
        for k, v in instance.items():
            if k in props:
                errs.extend(validate(v, props[k], "%s.%s" % (path, k)))
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errs.append("%s: %d items < minItems %d" % (path, len(instance), schema["minItems"]))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errs.append("%s: %d items > maxItems %d" % (path, len(instance), schema["maxItems"]))
        items = schema.get("items")
        if items:
            for i, v in enumerate(instance):
                errs.extend(validate(v, items, "%s[%d]" % (path, i)))
    return errs


def validate_or_raise(instance, schema, label="value"):
    errs = validate(instance, schema)
    if errs:
        raise ValidationError("%s invalid:\n  " % label + "\n  ".join(errs))
    return True
