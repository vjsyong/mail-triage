"""Configurable taxonomy for the mail SFT slice (acceptance AC1).

Design:

* A taxonomy is authored as stable category **ids** plus a public *definition*.
  Semantic resolution is keyed to authored ``covers`` (intent ids), **never** to
  the display ``name`` or to the filing ``folder`` -- so a renamed, reordered or
  remapped taxonomy resolves an email identically.
* The model-facing projection (:func:`public_projection`) strips every
  authoring-only fact (``covers``, ``roles``, ``authoring_notes``); a public
  prompt built from it cannot leak gold or role hints.
* Transformations (:func:`rename`, :func:`reorder`, :func:`remap_folders`) are
  explicit and reversible views over the same semantic core.

This module is stdlib-only and import-safe (no network, no GPU, no DB).
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

# Keys that exist only for authoring and must never reach a public projection.
HIDDEN_CATEGORY_KEYS = ("covers", "roles", "authoring_notes", "intents")
PUBLIC_CATEGORY_KEYS = ("id", "name", "folder", "definition")

# Resolution outcomes.
VISIBLE = "visible"
AMBIGUOUS = "ambiguous"
UNAVAILABLE = "unavailable"

AUTHORING_PROFILES = ("policy_conditioned",)


class TaxonomyError(ValueError):
    """Raised when a taxonomy is malformed or a transform is inconsistent."""


def load_taxonomy(path=None):
    """Load the authored taxonomy fixture (default: the bundled one)."""
    path = path or os.path.join(FIXTURES, "taxonomy.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return _normalize(raw)


def _normalize(raw):
    tax = {
        "revision": raw.get("revision") or "unknown",
        "intents": dict(raw.get("intents") or {}),
        "categories": [dict(c) for c in raw.get("categories") or []],
        "families": {k: list(v) for k, v in (raw.get("families") or {}).items()},
    }
    ids = [c.get("id") for c in tax["categories"]]
    if any(not i for i in ids):
        raise TaxonomyError("every category needs a stable id")
    if len(set(ids)) != len(ids):
        raise TaxonomyError("duplicate category id in taxonomy")
    for c in tax["categories"]:
        for intent in c.get("covers") or []:
            if intent not in tax["intents"]:
                raise TaxonomyError("category %r covers unknown intent %r"
                                    % (c["id"], intent))
    for family, intents in tax["families"].items():
        for intent in intents:
            if intent not in tax["intents"]:
                raise TaxonomyError("family %r names unknown intent %r"
                                    % (family, intent))
    return tax


# --------------------------------------------------------------------- transforms

def _copy(tax):
    return {
        "revision": tax.get("revision"),
        "intents": {k: dict(v) for k, v in tax["intents"].items()},
        "categories": [dict(c) for c in tax["categories"]],
        "families": {k: list(v) for k, v in tax["families"].items()},
        "transforms": list(tax.get("transforms") or []),
    }


def rename(tax, name_map):
    """Return a copy with display names replaced (``{id: new_name}``)."""
    out = _copy(tax)
    unknown = [i for i in name_map if i not in {c["id"] for c in out["categories"]}]
    if unknown:
        raise TaxonomyError("rename targets unknown category id(s): %s"
                            % ", ".join(sorted(unknown)))
    for c in out["categories"]:
        if c["id"] in name_map:
            c["name"] = name_map[c["id"]]
    out["transforms"].append({"op": "rename", "map": dict(name_map)})
    return out


def reorder(tax, order):
    """Return a copy with categories ordered by the given id list."""
    out = _copy(tax)
    ids = {c["id"] for c in out["categories"]}
    if set(order) != ids:
        raise TaxonomyError("reorder must name every category exactly once")
    by_id = {c["id"]: c for c in out["categories"]}
    out["categories"] = [by_id[i] for i in order]
    out["transforms"].append({"op": "reorder", "order": list(order)})
    return out


def remap_folders(tax, folder_map):
    """Return a copy with filing folders replaced (``{id: folder}``)."""
    out = _copy(tax)
    unknown = [i for i in folder_map if i not in {c["id"] for c in out["categories"]}]
    if unknown:
        raise TaxonomyError("remap targets unknown category id(s): %s"
                            % ", ".join(sorted(unknown)))
    for c in out["categories"]:
        if c["id"] in folder_map:
            c["folder"] = folder_map[c["id"]]
    out["transforms"].append({"op": "remap_folders", "map": dict(folder_map)})
    return out


# ------------------------------------------------------------------ projections

def public_projection(tax):
    """The model-facing view: ids/names/folders/definitions only.

    ``covers``, ``roles``, ``authoring_notes`` and any authoring intent map are
    removed so a prompt built from this projection cannot leak hidden facts.
    """
    return {
        "revision": tax.get("revision"),
        "categories": [
            {k: c.get(k) for k in PUBLIC_CATEGORY_KEYS}
            for c in tax["categories"]
        ],
    }


def hidden_category_keys(public_cat):
    """Any authoring-only key still present in a supposed public category."""
    return sorted(set(public_cat) - set(PUBLIC_CATEGORY_KEYS))


def assert_public_clean(public):
    """Raise if a public projection still carries any hidden authoring key."""
    problems = []
    for cat in public.get("categories") or []:
        extra = hidden_category_keys(cat)
        if extra:
            problems.append("category %r leaks %s"
                            % (cat.get("id") or cat.get("name"), ", ".join(extra)))
    if "intents" in public or "families" in public or "covers" in public:
        problems.append("public projection leaks an authoring map")
    if problems:
        raise TaxonomyError("public projection is not clean: " + "; ".join(problems))
    return True


def render_classifier_prompt(public, owner="", include_definitions=True):
    """Render a classifier system prompt from a **public** projection only."""
    assert_public_clean(public)
    names = [c.get("name") for c in public["categories"] if c.get("name")]
    lines = [
        "You triage incoming email for %s." % (owner or "the account owner"),
        "Choose exactly one category from this list:",
    ]
    if include_definitions:
        for c in public["categories"]:
            lines.append("- %s: %s" % (c.get("name"), c.get("definition")))
    else:
        lines.append(", ".join(names))
    lines.append("Reply with a single JSON object: "
                 '{"category": <name>, "needs_reply": true|false, '
                 '"summary": "<one sentence>", "reason": "<why, max 15 words>"}.')
    return "\n".join(lines)


# ------------------------------------------------------------------- resolution

def _categories_covering(tax, intents):
    wanted = set(intents)
    out = []
    for c in tax["categories"]:
        if wanted & set(c.get("covers") or []):
            out.append(c)
    return out


def resolve_semantics(tax, family_id, profile="policy_conditioned",
                      by_name=None):
    """Resolve an authored family to a category by semantic coverage.

    Returns ``{category, acceptable, observable, reason}``:

    * ``visible`` with one concrete category when exactly one category covers the
      family's intents and either the policy card is visible or the definition is
      self-evident (``by_name``);
    * ``ambiguous`` with the acceptable set when several categories or several
      truly intended purposes fit, or a native run cannot see the definition;
    * ``unavailable`` with ``reason`` ``taxonomy_gap`` when nothing covers it.

    Resolution consults only the authored ``covers`` (keyed by intent), so a
    renamed or reordered taxonomy returns the same category.
    """
    intents = list(tax["families"].get(family_id) or [])
    if not intents:
        return {"category": None, "acceptable": [],
                "observable": UNAVAILABLE, "reason": "unknown_family"}
    matches = _categories_covering(tax, intents)
    if not matches:
        return {"category": None, "acceptable": [],
                "observable": UNAVAILABLE, "reason": "taxonomy_gap"}
    names = [c["name"] for c in matches]
    if len(matches) > 1 or len(intents) > 1:
        return {"category": None, "acceptable": names,
                "observable": AMBIGUOUS, "reason": "multiple_fitting_categories"}
    cat = matches[0]
    by_name = bool(by_name) if by_name is not None else True
    if profile in AUTHORING_PROFILES or by_name:
        return {"category": cat["name"], "acceptable": [cat["name"]],
                "observable": VISIBLE, "reason": "mapped"}
    return {"category": None, "acceptable": [cat["name"]],
            "observable": AMBIGUOUS, "reason": "requires_definition"}


def category_by_id(tax, cid):
    for c in tax["categories"]:
        if c["id"] == cid:
            return dict(c)
    raise TaxonomyError("unknown category id %r" % cid)


# --------------------------------------------------------------- cross-task link

def cross_task_lineage(source_id, cases, relation="same_source"):
    """The lineage shared by the classifier and workflow views of one email.

    A source message must be traceable to both a triage case and a workflow case
    under the SAME ``source_id``/``lineage_id`` (never a re-authored look-alike).
    """
    if not cases:
        raise TaxonomyError("cross-task lineage needs at least one case")
    lineage_id = "lin_%s" % source_id
    for case in cases:
        case["source_id"] = source_id
        case["lineage_id"] = lineage_id
    tasks = sorted({c.get("task") for c in cases if c.get("task")})
    if len(tasks) < 2:
        raise TaxonomyError("cross-task lineage needs >=2 distinct tasks, got %r"
                            % tasks)
    return {
        "lineage_id": lineage_id,
        "source_id": source_id,
        "relation_type": relation,
        "tasks": tasks,
        "members": [c.get("case_id") for c in cases],
    }
