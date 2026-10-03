"""Authored recipe loading and slot/category resolution (WP2).

All authored content lives in ``fixtures/*.json`` next to this module: personas,
polices, message families, regional forms, slot vocabulary, role mappings,
corpus metadata and workflow recipes. Nothing here generates text by calling a
model; the generator only fills authored templates with deterministic slots.
"""
import json
import os

from .errors import BuildError

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

_CACHE = {}


def _load(name):
    if name not in _CACHE:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            _CACHE[name] = json.load(f)
    return _CACHE[name]


def personas():
    return _load("personas.json")["personas"]


def families():
    return _load("families.json")["families"]


def regions():
    return _load("regions.json")["regions"]


def shift_regions():
    """Regional forms reserved for the source/style shift axis (never dev/cal)."""
    return _load("regions.json").get("shift_regions", [])


def policies():
    return _load("policies.json")["policies"]


def policy_variants():
    return _load("policies.json")["variants"]


def vocab():
    return _load("vocab.json")


def situations():
    return _load("situations.json")["groups"]


def category_roles():
    return _load("roles.json")["category_roles"]


def corpora():
    return _load("corpora.json")["corpora"]


def workflow_recipes():
    return _load("workflow.json")["recipes"]


def policy_index(include_variants=True):
    records = list(policies())
    if include_variants:
        records = records + list(policy_variants())
    return {p["policy_id"]: p for p in records}


def variant_policy_for(policy_id):
    """The authored counterfactual taxonomy for a policy, or None."""
    for p in policy_variants():
        if p.get("policy_id", "").startswith(policy_id + "_"):
            return p
    return None


def category_roles_for(cat):
    """Roles for a category: an explicit ``roles`` override, else the global map."""
    if isinstance(cat, dict) and isinstance(cat.get("roles"), list):
        return list(cat["roles"])
    return list(category_roles().get(cat.get("name") if isinstance(cat, dict) else cat, []))


def resolve_category(policy, roles):
    """Resolve role preferences to a concrete policy category.

    Returns ``(name, folder)`` for the first category (in policy order) whose
    role set intersects ``roles``; otherwise the policy's declared filing
    default, then its first category. This is how a visible policy turns an
    otherwise ambiguous message into a specific filing decision.
    """
    wanted = set(roles or [])
    for cat in policy.get("categories") or []:
        name = cat.get("name")
        if wanted & set(category_roles_for(cat)):
            return name, cat.get("folder") or name
    default_folder = (policy.get("filing") or {}).get("default_folder")
    for cat in policy.get("categories") or []:
        if cat.get("folder") == default_folder or cat.get("name") == default_folder:
            return cat["name"], cat.get("folder") or cat["name"]
    cats = policy.get("categories") or []
    if not cats:
        raise BuildError("policy %r has no categories" % policy.get("policy_id"))
    return cats[0]["name"], cats[0].get("folder") or cats[0]["name"]


def policy_category_names(policy):
    return [c["name"] for c in policy.get("categories") or []]


def all_roles():
    seen = set()
    for cats in category_roles().values():
        seen.update(cats)
    return seen


def validate_recipes():
    """Fail fast when authored data is internally inconsistent."""
    problems = []
    fams = families()
    if len(personas()) != 8:
        problems.append("expected 8 personas, found %d" % len(personas()))
    for fid, fam in fams.items():
        if not fam.get("templates"):
            problems.append("family %r has no templates" % fid)
        for t in fam["templates"]:
            for slot in ("id", "subject", "body"):
                if not t.get(slot):
                    problems.append("family %r template missing %r" % (fid, slot))
    for p in policies() + policy_variants():
        if not p.get("policy_id"):
            problems.append("policy missing policy_id")
        for c in p.get("categories") or []:
            if c.get("name") not in category_roles():
                problems.append("policy %r category %r has no role mapping"
                                % (p.get("policy_id"), c.get("name")))
    for r in workflow_recipes():
        for key in ("id", "task", "trusted_system", "permissions", "mailbox", "gold"):
            if key not in r:
                problems.append("workflow %r missing %r" % (r.get("id"), key))
    if problems:
        raise BuildError("authored recipes invalid:\n  " + "\n  ".join(problems))
    return True
