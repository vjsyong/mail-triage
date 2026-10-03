"""Calibration, declared probability semantics and selective risk (WP5).

Two rules are enforced by construction:

* the native ``confidence`` value is a **category confidence**.  It is used only
  for category calibration and never repurposed as a reply probability unless
  the policy explicitly declares a field for ``reply_probability`` (the shipped
  policy declares ``null``);
* a probability signal must be a finite number inside its declared range.  NaN,
  infinity, strings and booleans are rejected, not coerced.

Calibrators are fitted **only** from the ``calibration`` split and carry an
artifact hash and revision so development/test scoring reuses a frozen artifact.
"""
import math

from ..common.hashing import hash_obj
from .errors import CalibrationError, ProbabilityError


def probability_spec(policy, semantics):
    return (policy.get("probability_semantics") or {}).get(semantics)


def confidence_field(policy, semantics="category_confidence"):
    """The declared field for a semantics, or raise if none is declared."""
    spec = probability_spec(policy, semantics)
    if not isinstance(spec, dict) or not spec.get("field"):
        raise ProbabilityError(
            "no field is declared for probability semantics %r; native "
            "confidence must not be repurposed" % semantics)
    return spec["field"]


def validate_probability(value, policy, semantics):
    """Return a finite ``float`` or raise :class:`ProbabilityError`.

    ``None`` means "not produced" and is allowed; anything non-numeric,
    boolean, NaN or infinite, or outside the declared range is rejected.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbabilityError("probability %r for %r is not numeric"
                               % (value, semantics))
    value = float(value)
    if not math.isfinite(value):
        raise ProbabilityError("probability %r for %r is not finite"
                               % (value, semantics))
    spec = probability_spec(policy, semantics)
    if not isinstance(spec, dict):
        raise ProbabilityError("unknown probability semantics %r" % semantics)
    lo, hi = spec.get("range", [0.0, 1.0])
    if value < lo or value > hi:
        raise ProbabilityError("probability %r for %r outside [%s, %s]"
                               % (value, semantics, lo, hi))
    return value


def _finite_records(records):
    return [r for r in records if r.get("confidence") is not None]


def reliability(records, bins):
    """Reliability table + expected calibration error over finite confidences.

    Returns ``{"n", "coverage", "ece", "bins": [...]}``.  ``coverage`` is the
    fraction of all records that carried a usable confidence.
    """
    finite = _finite_records(records)
    total = len(records)
    if not finite:
        return {"n": 0, "coverage": 0.0 if total else None, "ece": None,
                "bins": []}
    buckets = [[] for _ in range(bins)]
    for record in finite:
        conf = float(record["confidence"])
        idx = min(bins - 1, max(0, int(conf * bins)))
        buckets[idx].append((conf, bool(record["correct"])))
    out_bins = []
    ece = 0.0
    n = len(finite)
    for i, bucket in enumerate(buckets):
        lo, hi = i / float(bins), (i + 1) / float(bins)
        if not bucket:
            out_bins.append({"lo": lo, "hi": hi, "n": 0, "mean_conf": None,
                             "accuracy": None})
            continue
        mean_conf = sum(c for c, _ in bucket) / len(bucket)
        acc = sum(1 for _, ok in bucket if ok) / float(len(bucket))
        out_bins.append({"lo": lo, "hi": hi, "n": len(bucket),
                         "mean_conf": mean_conf, "accuracy": acc})
        ece += (len(bucket) / float(n)) * abs(mean_conf - acc)
    return {"n": n, "coverage": n / float(total) if total else None,
            "ece": ece, "bins": out_bins}


def selective_risk(records, threshold):
    """Coverage and accepted error rate at a confidence threshold.

    The denominator is *all* observable records: a record with no confidence
    (or confidence below the threshold) is deferred and stays in the base.  A
    threshold that defers everything yields zero coverage -- which is exactly
    why an always-deferring system cannot qualify.
    """
    total = len(records)
    if threshold is None:
        accepted = []
    else:
        accepted = [r for r in records
                    if r.get("confidence") is not None
                    and float(r["confidence"]) >= threshold]
    errors = sum(1 for r in accepted if not r["correct"])
    return {
        "threshold": threshold,
        "base": total,
        "accepted": len(accepted),
        "deferred": total - len(accepted),
        "coverage": (len(accepted) / float(total)) if total else None,
        "accepted_errors": errors,
        "accepted_error_rate": (errors / float(len(accepted))) if accepted else None,
    }


def _choose_threshold(records, policy):
    """Lowest threshold whose accepted set meets the target precision."""
    cal = policy["calibration"]
    target = float(cal["target_precision"])
    min_accepted = int(cal["min_accepted"])
    candidates = sorted({float(r["confidence"]) for r in _finite_records(records)})
    for threshold in candidates:
        accepted = [r for r in records if float(r["confidence"]) >= threshold]
        if len(accepted) < min_accepted:
            continue
        acc = sum(1 for r in accepted if r["correct"]) / float(len(accepted))
        if acc >= target:
            return threshold, True
    return float(cal["coverage_threshold"]), False


def build_calibrator(records, policy, source_case_ids, fit_split="calibration",
                     binding=None):
    """Fit a reproducible bin calibrator from calibration records only.

    ``records`` is a list of ``{"confidence","correct","case_id"}``.  ``binding``
    is the immutable inference context the artifact is valid for (dataset /
    model / adapter / prompt / generation / scorer / eval policy).  It is folded
    into the artifact hash so a stale or tampered artifact can be refused, and so
    a calibrator fitted for one model/adapter can never be silently applied to
    another.  Raises :class:`CalibrationError` when asked to fit from anything but
    the declared calibration split, or when no finite confidence exists.
    """
    declared = policy["calibration"]["split"]
    if fit_split != declared:
        raise CalibrationError(
            "calibrator may only be fitted from split %r, not %r"
            % (declared, fit_split))
    finite = _finite_records(records)
    if not finite:
        raise CalibrationError("no finite confidence signal to calibrate")
    table = reliability(records, policy["calibration"]["bins"])
    threshold, target_met = _choose_threshold(records, policy)
    payload = {
        "schema_version": "v3.0",
        "fit_split": fit_split,
        "n": table["n"],
        "bins": table["bins"],
        "ece": table["ece"],
        "threshold": threshold,
        "target_precision": float(policy["calibration"]["target_precision"]),
        "target_met": target_met,
        "estimable": table["n"] >= int(policy["calibration"]["min_calibration_cases"]),
        "source_case_ids": sorted(source_case_ids),
    }
    if binding is not None:
        payload["binding"] = binding
        payload["binding_sha256"] = hash_obj(binding)
    digest = hash_obj(payload)
    artifact = dict(payload)
    artifact["artifact_sha256"] = digest
    artifact["revision"] = "cal3.0-" + digest[:12]
    return artifact


def _artifact_payload(artifact):
    return {k: v for k, v in artifact.items()
            if k not in ("artifact_sha256", "revision")}


def verify_calibrator(artifact, expected_binding=None, require_identity=True):
    """Refuse a stale, tampered or mismatched calibrator; returns ``True``.

    The digest is **recomputed** over the artifact body (never trusted from the
    stored field).  When ``expected_binding`` is supplied the artifact's binding
    must equal it exactly, so a calibrator fitted for one model / adapter /
    prompt / generation cannot be applied to a run with a different identity.
    """
    if not isinstance(artifact, dict):
        raise CalibrationError("calibrator artifact must be a mapping")
    digest = artifact.get("artifact_sha256")
    if not digest:
        if require_identity:
            raise CalibrationError("calibrator artifact has no artifact_sha256")
        return False
    recomputed = hash_obj(_artifact_payload(artifact))
    if recomputed != digest:
        raise CalibrationError(
            "calibrator artifact hash is stale or tampered (stored %s != "
            "recomputed %s)" % (str(digest)[:12], recomputed[:12]))
    if artifact.get("revision") != "cal3.0-" + digest[:12]:
        raise CalibrationError("calibrator revision does not match its hash")
    if expected_binding is not None:
        binding = artifact.get("binding")
        if binding != expected_binding:
            raise CalibrationError(
                "calibrator binding does not match the run identity; refusing "
                "to apply a calibrator fitted for a different dataset/model/"
                "adapter/prompt/generation")
        if artifact.get("binding_sha256") != hash_obj(expected_binding):
            raise CalibrationError("calibrator binding hash is stale or tampered")
    return True


def apply_calibrator(artifact, confidence):
    """Map a raw confidence to its bin's empirical accuracy (frozen artifact)."""
    if confidence is None:
        return None
    if not artifact:
        return confidence
    bins = artifact.get("bins") or []
    if not bins:
        return confidence
    width = len(bins)
    idx = min(width - 1, max(0, int(float(confidence) * width)))
    entry = bins[idx]
    if entry.get("n") and entry.get("accuracy") is not None:
        return float(entry["accuracy"])
    return confidence
