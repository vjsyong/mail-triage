"""Benchmark v3 scoring, statistics and executable gates (WP5).

The package is stdlib-only and offline.  It consumes the shared dataset/run
bundle shape documented in ``README.md`` and the frozen WP0/WP1 contracts
(observability, field provenance, run identity) without importing any adapters.

Shared API::

    default_policy() -> dict
    score_run(dataset, run, policy=None, calibrator=None) -> report
    compare_runs(dataset, baseline_run, candidate_run, policy=None) -> comparison
    render_markdown(report) -> str
    write_report(report, path)

Additional helpers used by the tests and by WP6/WP7::

    fit_calibrator(dataset, run, policy=None, split="calibration") -> artifact
    apply_calibrator(artifact, confidence) -> float | None
    validate_probability(value, policy, semantics) -> float | None
"""
from . import calibration, gates, metrics, normalize, stats, workflows
from .errors import (
    CalibrationError,
    ComparisonError,
    PolicyError,
    ProbabilityError,
    ScoringError,
)
from .calibration import (
    apply_calibrator,
    build_calibrator,
    confidence_field,
    reliability,
    selective_risk,
    validate_probability,
)
from .policy import default_policy, normalize_policy, policy_hash, validate_policy
from .score import (
    compare_runs,
    fit_calibrator,
    render_markdown,
    score_run,
    write_report,
)

__all__ = [
    "default_policy", "score_run", "compare_runs", "render_markdown",
    "write_report", "fit_calibrator", "apply_calibrator", "build_calibrator",
    "validate_probability", "validate_policy", "normalize_policy",
    "policy_hash", "confidence_field", "reliability", "selective_risk",
    "ScoringError", "CalibrationError", "ProbabilityError", "ComparisonError",
    "PolicyError", "calibration", "gates", "metrics", "normalize", "stats",
    "workflows",
]
