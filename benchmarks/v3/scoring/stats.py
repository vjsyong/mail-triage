"""Lineage-clustered bootstrap, paired comparison and stability (WP5).

The bootstrap resamples **lineage clusters**, not rows, and **recomputes** the
metric (macro-F1, reply F1, ...) on each resample -- never averaging per-row
correctness as a fake F1.  A paired comparison resamples the shared clusters
once and applies the same indices to baseline and candidate.

Every entry point takes the single scoring policy, so ``B``, ``seed`` and
``alpha`` are identical across intervals and noninferiority verdicts.
"""
import random

from ..common.hashing import hash_obj


def seed_for(policy, *parts):
    boot = policy["bootstrap"]
    digest = hash_obj({"seed": boot["seed"], "parts": [str(p) for p in parts]})
    return int(digest[:16], 16)


def _resample(rng, ids):
    n = len(ids)
    return [ids[rng.randrange(n)] for _ in range(n)]


def _percentile(sorted_values, q):
    if not sorted_values:
        return None
    idx = int(q * (len(sorted_values) - 1))
    return sorted_values[idx]


def _as_float(value):
    return 0.0 if value is None else float(value)


def bootstrap_metric(clusters, metric_fn, policy, seed_parts=()):
    """Percentile CI for one metric over lineage clusters.

    ``clusters`` maps ``lineage_id -> [items]`` and ``metric_fn(items)`` returns
    the metric (recomputed on every resample).
    """
    boot = policy["bootstrap"]
    ids = sorted(clusters)
    n_roots = len(ids)
    n_cases = sum(len(v) for v in clusters.values())
    point = _as_float(metric_fn([it for cid in ids for it in clusters[cid]]))
    if not ids or n_cases == 0:
        return {"point": point, "ci_low": None, "ci_high": None, "B": boot["B"],
                "alpha": boot["alpha"], "n_roots": n_roots, "n_cases": n_cases,
                "estimable": False}
    rng = random.Random(seed_for(policy, "bootstrap", *seed_parts))
    boots = []
    for _ in range(boot["B"]):
        pick = _resample(rng, ids)
        items = [it for cid in pick for it in clusters[cid]]
        boots.append(_as_float(metric_fn(items)))
    boots.sort()
    return {
        "point": point,
        "ci_low": _percentile(boots, boot["alpha"] / 2.0),
        "ci_high": _percentile(boots, 1 - boot["alpha"] / 2.0),
        "B": boot["B"], "alpha": boot["alpha"],
        "n_roots": n_roots, "n_cases": n_cases,
        "estimable": n_roots >= int(boot["min_lineages"]),
    }


def paired_bootstrap(baseline_clusters, candidate_clusters, metric_fn, policy,
                     seed_parts=()):
    """Paired CI for the metric difference ``candidate - baseline``.

    Only clusters present in both runs are used (the same roots), and the same
    resample indices drive both sides, so the interval reflects the paired
    design.  ``metric_fn`` is recomputed per side per resample.
    """
    boot = policy["bootstrap"]
    shared = sorted(set(baseline_clusters) & set(candidate_clusters))
    margin = float(boot["margin"])
    n_cases = sum(len(baseline_clusters[c]) for c in shared)
    base_point = _as_float(metric_fn(
        [it for c in shared for it in baseline_clusters[c]])) if shared else None
    cand_point = _as_float(metric_fn(
        [it for c in shared for it in candidate_clusters[c]])) if shared else None
    result = {
        "baseline": base_point, "candidate": cand_point,
        "diff": (cand_point - base_point)
        if (base_point is not None and cand_point is not None) else None,
        "ci_low": None, "ci_high": None,
        "B": boot["B"], "alpha": boot["alpha"], "margin": margin,
        "n_roots": len(shared), "n_cases": n_cases,
        "estimable": bool(shared) and len(shared) >= int(boot["min_lineages"]),
        "noninferior": False, "verdict": "not_estimable",
    }
    if not shared:
        return result
    rng = random.Random(seed_for(policy, "paired", *seed_parts))
    diffs = []
    for _ in range(boot["B"]):
        pick = _resample(rng, shared)
        b_items = [it for c in pick for it in baseline_clusters[c]]
        c_items = [it for c in pick for it in candidate_clusters[c]]
        diffs.append(_as_float(metric_fn(c_items)) - _as_float(metric_fn(b_items)))
    diffs.sort()
    lo = _percentile(diffs, boot["alpha"] / 2.0)
    hi = _percentile(diffs, 1 - boot["alpha"] / 2.0)
    result["ci_low"] = lo
    result["ci_high"] = hi
    if result["estimable"]:
        result["noninferior"] = bool(lo is not None and lo > -margin)
        if lo is not None and lo > 0:
            result["verdict"] = "better"
        elif hi is not None and hi < 0:
            result["verdict"] = "worse"
        else:
            result["verdict"] = "inconclusive"
    return result


def stability_report(repeats, lineage_of=None, requested_ids=None):
    """Variance of per-case outcomes across repeated runs.

    ``repeats`` is a list of ``{case_id: value}``.  Returns case-level variance,
    a stability rate, and Nroots/Ncases plus requested/completed coverage.
    """
    if not repeats:
        return {"n_cases": 0, "n_roots": 0, "varying_cases": [],
                "stability": None, "requested": 0, "completed": 0,
                "coverage": None}
    shared = set(repeats[0])
    for run in repeats[1:]:
        shared &= set(run)
    shared = sorted(shared)
    varying = []
    for cid in shared:
        values = [run.get(cid) for run in repeats]
        if len(set(_freeze(v) for v in values)) > 1:
            varying.append({"case_id": cid, "values": values})
    lineage_of = lineage_of or {}
    roots = {lineage_of.get(cid, cid) for cid in shared}
    completed = max((sum(1 for run in repeats if cid in run) for cid in shared),
                    default=0)
    requested = len(requested_ids) if requested_ids is not None else len(shared)
    n = len(shared)
    return {
        "n_cases": n,
        "n_roots": len(roots),
        "varying_cases": varying,
        "stability": (1 - len(varying) / float(n)) if n else None,
        "requested": requested,
        "completed": completed,
        "coverage": (n / float(requested)) if requested else None,
    }


def _freeze(value):
    if isinstance(value, (dict, list)):
        return hash_obj(value)
    return value
