"""Build a raw ``MlflowClient``, or explain why one is not available.

Framework-layer (not ``api/``) so both sides of the MLflow boundary can
share it without an inverted dependency:

* ``mlops_framework.api.mlflow_gateway`` — read-only views, wraps the
  result in an :class:`~mlops_framework.api.schemas.ExternalPanel`.
* ``mlops_framework.tracking.mlflow_registry`` — the write path (model
  registry sync), used from both ``api/routers/internal.py`` (the
  Airflow-DAG path) and ``workflow/retraining.py`` (the in-process
  retrain path). The latter must not depend on ``mlops_framework.api``
  — nothing else under the framework's core layers does, and importing
  ``mlops_framework.api`` pulls in the whole FastAPI app (every router)
  just to reach one client constructor.
"""

from __future__ import annotations

import os
from typing import Any, Optional, Tuple

from mlops_framework.config.settings import get_settings

# MLflow's client defaults to a 120s timeout and 7 retries with an
# exponential backoff, which is sized for a training job that must not
# lose its metrics. Callers of this module are typically answering an
# interactive request (a console page, a promotion API call) — someone
# or something is waiting synchronously. Measured against a tracking
# server that was simply down, the defaults left a request hanging for
# over three minutes — worse than an error, because it pins a worker.
#
# Applied as *defaults*: an operator who exports the variables keeps
# their own values.
_HTTP_LIMITS = {
    "MLFLOW_HTTP_REQUEST_TIMEOUT": "6",
    "MLFLOW_HTTP_REQUEST_MAX_RETRIES": "1",
    "MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR": "1",
}


def _apply_http_limits() -> None:
    for key, value in _HTTP_LIMITS.items():
        os.environ.setdefault(key, value)


def tracking_uri() -> Optional[str]:
    """Return the configured tracking URI, if any."""
    return get_settings().mlflow_tracking_uri


def client_or_reason() -> Tuple[Any, Optional[str]]:
    """Build an ``MlflowClient``, or explain why one is not available.

    Returns:
        ``(client, None)`` on success, ``(None, reason)`` otherwise. The
        reason is short, user-facing text.
    """
    uri = tracking_uri()
    if not uri:
        return None, "MLFLOW_TRACKING_URI is not configured"
    _apply_http_limits()
    try:
        from mlflow.tracking import MlflowClient
    except Exception as exc:  # noqa: BLE001 - optional dependency
        return None, f"mlflow is not installed: {exc}"
    try:
        return MlflowClient(tracking_uri=uri), None
    except Exception as exc:  # noqa: BLE001 - never fail the caller
        return None, f"MLflow unavailable: {exc}"
