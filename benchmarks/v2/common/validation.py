"""Tiny dependency-free JSON-Schema-subset validator.

The v2 harness and scorer must run inside the app venv, the GPU bench venv, and
(eventually) the in-app plugin sandbox.  Rather than depend on ``jsonschema``
being installed in all three, we validate against the committed schemas with
this small subset: type, required, properties, items, enum, pattern,
additionalProperties, minItems, maxItems, minimum, maximum, oneOf.

The JSON Schema files remain the single source of truth for both this
validator and external tooling.
"""
import json
import os
import re

_HERE = os.path.dirname(__file__)
SCHEMA_DIR = os.path.join(_HERE, "..", "schemas")


def load_schema(name):
    path = os.path.join(SCHEMA_DIR, name if name.endswith(".json") else name + ".schema.json")
    with open(path) as f:
        return json.load(f)


class ValidationError(ValueError):
    pass


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
    if "oneOf" in schema:
        matches = 0
        first_errs = []
        for sub in schema["oneOf"]:
            try:
                validate(instance, sub, path)
                matches += 1
            except ValidationError as exc:
                if not first_errs:
                    first_errs = [str(exc)]
        if matches != 1:
            return errs + ["%s: expected exactly one of oneOf (%d matched): %s"
                           % (path, matches, first_errs[0] if first_errs else "")]
        return errs

    t = schema.get("type")
    if t and not _type_ok(instance, t):
        return ["%s: expected %s, got %s" % (path, t, type(instance).__name__)]

    if "enum" in schema and instance not in schema["enum"]:
        errs.append("%s: %r not in enum %r" % (path, instance, schema["enum"]))
    if isinstance(instance, str) and "pattern" in schema:
        if not re.search(schema["pattern"], instance):
            errs.append("%s: %r does not match %r" % (path, instance, schema["pattern"]))
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
            extra = set(instance) - set(props)
            for k in sorted(extra):
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
