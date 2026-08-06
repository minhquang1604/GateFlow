"""Read-only gateway to the MLflow tracking server.

Every MLflow-backed endpoint shares two concerns: MLflow may not be
installed or configured at all, and the tracking server may be down. This
module answers both once so the routers stay about their own data.

Nothing here writes to MLflow. The framework logs through
``mlops_framework.tracking.mlflow.MLflowTracker``; these views only read
what is already there.

The ``mlflow`` import stays inside the functions on purpose. It costs
~14s of CPU (it drags in pandas, sqlalchemy, alembic), and the framework
is meant to stay usable without MLflow installed — see
``_warm_mlflow_import`` in ``api/app.py``, which pre-warms it in the
background when a tracking URI is configured.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional, Tuple

from mlops_framework.api.schemas import ExternalPanel
from mlops_framework.config.settings import get_settings

# MLflow's client defaults to a 120s timeout and 7 retries with an
# exponential backoff, which is sized for a training job that must not lose
# its metrics. These endpoints are the opposite case: they render
# supplementary panels while someone waits on a page. Measured against a
# tracking server that was simply down, the defaults left a request hanging
# for over three minutes — worse than an error, because it pins a worker and
# the browser just spins.
#
# These are applied as *defaults*: an operator who exports the variables
# keeps their own values.
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
        reason is user-facing text that lands in
        :attr:`ExternalPanel.reason`.
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
    except Exception as exc:  # noqa: BLE001 - never fail the page
        return None, f"MLflow unavailable: {exc}"


def panel(fn: Callable[[Any], Any]) -> ExternalPanel:
    """Run ``fn(client)`` and wrap the outcome in an :class:`ExternalPanel`.

    Any exception becomes ``available=False`` with the error as the reason.
    That is deliberate: these panels are supplementary, and a tracking
    server that is slow, unreachable, or returning something unexpected
    should degrade one card rather than 500 the page around it.
    """
    client, reason = client_or_reason()
    if client is None:
        return ExternalPanel(available=False, reason=reason)
    try:
        return ExternalPanel(available=True, data=fn(client))
    except Exception as exc:  # noqa: BLE001 - never fail the page
        return ExternalPanel(available=False, reason=f"MLflow error: {exc}")
