"""The single, pre-declared scoring policy (WP5).

There is exactly **one** configured statistical protocol feeding every interval
and noninferiority verdict: bootstrap count ``B``, a fixed ``seed``, ``alpha``
and a pre-declared ``margin``.  Runtime overrides are merged onto
``policy.json`` so tests can dial B down, but shipped runs use the committed
values (default B = 4000, margin = 0.03).
"""
import copy
import json
import os

from ..common.hashing import hash_obj
from .errors import PolicyError

POLICY_PATH = os.path.join(os.path.dirname(__file__), "policy.json")

_REQUIRED_BOOTSTRAP = ("B", "seed", "alpha", "margin", "min_lineages")
_REQUIRED_GATES = (
    "min_coverage", "min_category_macro_f1", "min_reply_f1", "max_missed_reply",
    "min_workflow_completion", "min_lineages", "min_cases",
)
_REQUIRED_SPLITS = ("development", "calibration", "test")
_REQUIRED_SEMANTICS = ("category_confidence", "reply_probability")


def load_default_policy():
    with open(POLICY_PATH) as f:
        return json.load(f)


_CACHED = None


def default_policy():
    """A fresh copy of the committed scoring policy."""
    global _CACHED
    if _CACHED is None:
        _CACHED = load_default_policy()
    return copy.deepcopy(_CACHED)


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def validate_policy(policy):
    """Return ``True`` or raise :class:`PolicyError` for an invalid policy."""
    if not isinstance(policy, dict):
        raise PolicyError("scoring policy must be a mapping")
    boot = policy.get("bootstrap")
    if not isinstance(boot, dict):
        raise PolicyError("policy.bootstrap must be a mapping")
    for key in _REQUIRED_BOOTSTRAP:
        if boot.get(key) is None:
            raise PolicyError("policy.bootstrap.%s is required" % key)
    if not isinstance(boot["B"], int) or boot["B"] < 1:
        raise PolicyError("policy.bootstrap.B must be a positive integer")
    if not isinstance(boot["seed"], int):
        raise PolicyError("policy.bootstrap.seed must be an integer")
    alpha = boot["alpha"]
    if not (0 < float(alpha) < 1):
        raise PolicyError("policy.bootstrap.alpha must be in (0, 1)")
    if float(boot["margin"]) < 0:
        raise PolicyError("policy.bootstrap.margin must be >= 0")

    sem = policy.get("probability_semantics")
    if not isinstance(sem, dict):
        raise PolicyError("policy.probability_semantics must be a mapping")
    for name in _REQUIRED_SEMANTICS:
        if name not in sem:
            raise PolicyError("policy.probability_semantics.%s is required" % name)
        entry = sem[name]
        if not isinstance(entry, dict):
            raise PolicyError("probability semantics %r must be a mapping" % name)
        rng = entry.get("range")
        if not (isinstance(rng, list) and len(rng) == 2
                and float(rng[0]) < float(rng[1])):
            raise PolicyError("probability semantics %r needs an ordered range" % name)

    cal = policy.get("calibration")
    if not isinstance(cal, dict) or not isinstance(cal.get("bins"), int) \
            or cal["bins"] < 1:
        raise PolicyError("policy.calibration.bins must be a positive integer")
    if cal.get("split") != "calibration":
        raise PolicyError("calibration may only be fitted from the calibration split")

    gates = policy.get("gates")
    if not isinstance(gates, dict):
        raise PolicyError("policy.gates must be a mapping")
    for key in _REQUIRED_GATES:
        if gates.get(key) is None:
            raise PolicyError("policy.gates.%s is required" % key)

    splits = policy.get("splits")
    if not isinstance(splits, dict):
        raise PolicyError("policy.splits must be a mapping")
    for key in _REQUIRED_SPLITS:
        if not splits.get(key):
            raise PolicyError("policy.splits.%s must be a non-empty list" % key)
    return True


def normalize_policy(policy=None):
    """Merge an override onto the committed default and validate the result."""
    base = default_policy()
    if policy is None:
        merged = base
    elif not isinstance(policy, dict):
        raise PolicyError("policy override must be a mapping")
    else:
        merged = _deep_merge(base, policy)
    validate_policy(merged)
    return merged


def policy_hash(policy):
    """Canonical hash of the scoring policy (reported so a protocol is pinned)."""
    return hash_obj(policy)
