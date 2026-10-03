"""Build-package error types (WP2/WP6).

All are explicit: the builder fails closed rather than emitting an artifact a
reviewer could mistake for reviewed, authorized or publicly reusable data.
"""


class BuildError(ValueError):
    """A dataset could not be built from the supplied inputs."""


class WriteRefused(BuildError):
    """Refusing to write a dataset (unintended overwrite / bad target)."""


class PrivateExportError(BuildError):
    """Refusing to publish private/calibration/real-mail records."""


class ReviewError(ValueError):
    """A review worksheet/import/seal request violates the review gates."""


class ImportAuthorizationError(ReviewError):
    """A real-mail import lacks the separate consent/authorization metadata."""
