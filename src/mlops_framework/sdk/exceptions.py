"""SDK-level exceptions.

These wrap the underlying framework exceptions so SDK users can catch one
type per concern without importing the internals.
"""

from __future__ import annotations


class MLOpsError(Exception):
    """Base class for all SDK errors."""


class NotFoundError(MLOpsError):
    """Raised when a requested entity (dataset, model, run, …) does not exist."""


class AlreadyExistsError(MLOpsError):
    """Raised when attempting to create a duplicate entity."""


class PipelineNotRegisteredError(MLOpsError):
    """Raised when ``project.train(pipeline=...)`` references an unknown name."""


class TrainingError(MLOpsError):
    """Raised when a training run fails or the run lifecycle is invalid."""


class GovernanceError(MLOpsError):
    """Raised when governance (readiness / eligibility / promotion) rejects a request."""
