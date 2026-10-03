"""Exception hierarchy for v3 scoring (WP5).

Scoring refuses malformed integrity rather than silently swallowing it.  The
base class carries all scoring failures so callers can catch one type while the
subclasses keep the *why* explicit for the tests and the report.
"""


class ScoringError(ValueError):
    """A dataset/run bundle is structurally malformed and cannot be scored."""


class CalibrationError(ScoringError):
    """A calibrator was fitted from the wrong split or with invalid inputs."""


class ProbabilityError(ScoringError):
    """A declared probability signal is non-finite, out of range or misused."""


class ComparisonError(ScoringError):
    """Two runs cannot be compared (different dataset, scope or protocol)."""


class PolicyError(ScoringError):
    """The scoring policy config is missing a required declaration."""
