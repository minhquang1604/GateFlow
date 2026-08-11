"""Experiment tracking abstraction and shared types.

The framework depends on this ABC; MLflow and any other tracking
backend is plugged in via an adapter. Application code calls
``tracker.log_param(...)`` etc., never ``mlflow.log_param(...)``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any


class RunStatus(str, Enum):
    """Final status of a tracker run."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExperimentTracker(ABC):
    """Abstract experiment tracker.

    Implementations must support a sequence like:

        run_id = tracker.start_run(...)
        tracker.log_param("lr", 0.01)
        tracker.log_metric("loss", 0.42, step=1)
        tracker.log_artifact("model.pkl")
        tracker.end_run(status=RunStatus.SUCCESS)
    """

    @abstractmethod
    def start_run(
        self,
        run_name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> str:
        """Begin a new tracker run and return its identifier."""
        raise NotImplementedError

    @abstractmethod
    def log_param(self, key: str, value: Any) -> None:
        """Log a single parameter."""
        raise NotImplementedError

    @abstractmethod
    def log_params(self, params: dict[str, Any]) -> None:
        """Log a batch of parameters."""
        raise NotImplementedError

    @abstractmethod
    def log_metric(
        self,
        key: str,
        value: float,
        step: int | None = None,
    ) -> None:
        """Log a single metric."""
        raise NotImplementedError

    @abstractmethod
    def log_metrics(
        self,
        metrics: dict[str, float],
        step: int | None = None,
    ) -> None:
        """Log a batch of metrics."""
        raise NotImplementedError

    @abstractmethod
    def log_artifact(self, path: str) -> None:
        """Log a single artifact (file or directory)."""
        raise NotImplementedError

    @abstractmethod
    def end_run(self, status: str | RunStatus = RunStatus.SUCCESS) -> None:
        """End the current run with a final status."""
        raise NotImplementedError
