"""In-memory tracker for tests and lightweight environments.

Records everything in process memory. The framework tests use this
instead of a real MLflow instance so they do not require the mlflow
package or a tracking server.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from mlops_framework.tracking.base import ExperimentTracker, RunStatus


class InMemoryTracker(ExperimentTracker):
    """Tracker that records calls in a list — useful for tests."""

    def __init__(self) -> None:
        self.params: list[tuple[str, Any]] = []
        self.metrics: list[tuple[str, float, Optional[int]]] = []
        self.artifacts: list[str] = []
        self.runs: list[dict[str, Any]] = []
        self._active_id: Optional[str] = None

    def start_run(
        self,
        run_name: Optional[str] = None,
        tags: Optional[dict[str, str]] = None,
    ) -> str:
        if self._active_id is not None:
            return self._active_id
        run_id = uuid.uuid4().hex
        self._active_id = run_id
        self.runs.append(
            {"run_id": run_id, "run_name": run_name, "tags": tags or {}, "ended": False}
        )
        return run_id

    def log_param(self, key: str, value: Any) -> None:
        self.params.append((key, value))

    def log_params(self, params: dict[str, Any]) -> None:
        for k, v in params.items():
            self.log_param(k, v)

    def log_metric(
        self,
        key: str,
        value: float,
        step: Optional[int] = None,
    ) -> None:
        self.metrics.append((key, float(value), step))

    def log_metrics(
        self,
        metrics: dict[str, float],
        step: Optional[int] = None,
    ) -> None:
        for k, v in metrics.items():
            self.log_metric(k, v, step=step)

    def log_artifact(self, path: str) -> None:
        self.artifacts.append(path)

    def end_run(self, status: "str | RunStatus" = RunStatus.SUCCESS) -> None:
        if self._active_id is None:
            return
        status_value = status.value if isinstance(status, RunStatus) else str(status)
        for run in self.runs:
            if run["run_id"] == self._active_id:
                run["ended"] = True
                run["end_status"] = status_value
                break
        self._active_id = None
