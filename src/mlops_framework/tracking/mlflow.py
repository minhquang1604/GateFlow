"""MLflow implementation of :class:`ExperimentTracker`.

The MLflow SDK is imported lazily — the framework itself never imports
``mlflow``. This keeps the framework runnable in environments that do
not have MLflow installed (e.g. CI, the test suite).

If MLflow is not installed, ``MLflowTracker`` will raise a clear
:class:`ExperimentTrackingError` at construction time.

Constructor precedence: explicit kwargs > environment-driven
:class:`Settings`. This keeps tests trivial (pass the URI explicitly)
and makes the production adapters zero-config when the framework is
deployed against the bundled Compose stack.

Known limitation — one active run per process
---------------------------------------------

``mlflow.start_run`` tracks the active run in *process-global* state,
while this adapter tracks it per instance. Two ``MLflowTracker``
objects in one process therefore do not get independent runs: the
second ``start_run`` nests inside the first (or is refused), and
subsequent ``log_*`` calls from either tracker land on whichever run
MLflow considers active. Concurrent training runs in a single process
need separate processes, or a rewrite onto ``MlflowClient``, which
takes an explicit ``run_id`` per call instead of the global fluent
API. ``tests/integration/test_mlflow_live.py::TestGlobalRunState``
pins the current behaviour against a real server.
"""

from __future__ import annotations

from typing import Any, Optional

from mlops_framework.config.settings import get_settings
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


def _map_run_status(status: "str | RunStatus") -> str:
    """Map a framework :class:`RunStatus` to the MLflow run-status string.

    The framework exposes three terminal states. MLflow uses
    ``FINISHED`` for success, ``FAILED`` for failure and ``KILLED`` for
    any external termination (e.g. an orchestrator cancelling a run).
    """
    value = status.value if isinstance(status, RunStatus) else str(status)
    normalized = (value or "").upper()
    if normalized == RunStatus.SUCCESS.value:
        return "FINISHED"
    if normalized == RunStatus.FAILED.value:
        return "FAILED"
    if normalized == RunStatus.CANCELLED.value:
        return "KILLED"
    # Unknown — MLflow accepts FINISHED as a generic "done" value; we
    # be conservative and treat everything else as FAILED so the run is
    # visually flagged for review in the MLflow UI.
    return "FAILED"


class MLflowTracker(ExperimentTracker):
    """Adapter that implements the framework's tracker ABC with MLflow.

    The adapter is intentionally thin — it maps the framework's
    ``ExperimentTracker`` methods onto MLflow's API. No business logic
    lives here.

    Args:
        tracking_uri: MLflow tracking URI. Falls back to
            ``Settings.mlflow_tracking_uri`` when not provided.
        experiment_name: MLflow experiment name. Falls back to
            ``Settings.mlflow_experiment_name``.
    """

    def __init__(
        self,
        tracking_uri: Optional[str] = None,
        experiment_name: Optional[str] = None,
    ) -> None:
        mlflow = _import_mlflow()
        self._mlflow = mlflow

        settings = get_settings()
        effective_uri = tracking_uri or settings.mlflow_tracking_uri
        effective_experiment = (
            experiment_name or settings.mlflow_experiment_name or "mlops-framework"
        )

        if effective_uri:
            mlflow.set_tracking_uri(effective_uri)
        mlflow.set_experiment(effective_experiment)
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
        mlflow_status = _map_run_status(status)
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