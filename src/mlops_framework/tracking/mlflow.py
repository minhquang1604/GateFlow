"""MLflow implementation of :class:`ExperimentTracker`.

The MLflow SDK is imported lazily — the framework itself never imports
``mlflow``. This keeps the framework runnable in environments that do
not have MLflow installed (e.g. CI, the test suite).

If MLflow is not installed, ``MLflowTracker`` will raise a clear
:class:`ExperimentTrackingError` at construction time.
"""

from __future__ import annotations

from typing import Any, Optional

from mlops_framework.exceptions import ExperimentTrackingError
from mlops_framework.tracking.base import ExperimentTracker, RunStatus


def _import_mlflow():
    """Lazy import so the framework does not require MLflow."""
    try:
        import mlflow  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ExperimentTrackingError(
            "MLflowTracker requires the 'mlflow' package. "
            "Install it with `pip install mlflow`."
        ) from exc
    return mlflow


class MLflowTracker(ExperimentTracker):
    """Adapter that implements the framework's tracker ABC with MLflow.

    The adapter is intentionally thin — it maps the framework's
    ``ExperimentTracker`` methods onto MLflow's API. No business logic
    lives here.
    """

    def __init__(
        self,
        tracking_uri: Optional[str] = None,
        experiment_name: str = "mlops-framework",
    ) -> None:
        mlflow = _import_mlflow()
        self._mlflow = mlflow
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment_name)
        self._active_run = None

    # ------------------------------------------------------------------ #
    # ExperimentTracker API
    # ------------------------------------------------------------------ #

    def start_run(
        self,
        run_name: Optional[str] = None,
        tags: Optional[dict[str, str]] = None,
    ) -> str:
        if self._active_run is not None:
            # An active run already exists; return its id.
            return self._active_run.info.run_id
        run = self._mlflow.start_run(run_name=run_name, tags=tags or {})
        self._active_run = run
        return run.info.run_id

    def log_param(self, key: str, value: Any) -> None:
        self._require_active()
        self._mlflow.log_param(key, value)

    def log_params(self, params: dict[str, Any]) -> None:
        self._require_active()
        self._mlflow.log_params(params)

    def log_metric(
        self,
        key: str,
        value: float,
        step: Optional[int] = None,
    ) -> None:
        self._require_active()
        self._mlflow.log_metric(key, value, step=step)

    def log_metrics(
        self,
        metrics: dict[str, float],
        step: Optional[int] = None,
    ) -> None:
        self._require_active()
        self._mlflow.log_metrics(metrics, step=step)

    def log_artifact(self, path: str) -> None:
        self._require_active()
        self._mlflow.log_artifact(path)

    def end_run(self, status: "str | RunStatus" = RunStatus.SUCCESS) -> None:
        if self._active_run is None:
            return
        mlflow_status = "FINISHED" if str(status) == RunStatus.SUCCESS.value else "FAILED"
        self._mlflow.end_run(status=mlflow_status)
        self._active_run = None

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _require_active(self) -> None:
        if self._active_run is None:
            raise ExperimentTrackingError(
                "No active MLflow run. Call start_run() first."
            )
