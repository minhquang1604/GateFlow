"""Read-only gateway to the MLflow tracking server.

Every MLflow-backed endpoint shares two concerns: MLflow may not be
installed or configured at all, and the tracking server may be down. This
module answers both once so the routers stay about their own data.

Nothing here writes to MLflow. The framework logs runs through
``mlops_framework.tracking.mlflow.MLflowTracker`` and syncs the model
registry through ``mlops_framework.tracking.mlflow_registry``; these
views only read what is already there. ``client_or_reason`` itself now
lives in ``mlops_framework.tracking.mlflow_client`` (re-exported below)
so both the read and write sides share one client constructor without
the write side having to depend on this ``api`` package.

The ``mlflow`` import stays inside the functions on purpose. It costs
~14s of CPU (it drags in pandas, sqlalchemy, alembic), and the framework
is meant to stay usable without MLflow installed — see
``_warm_mlflow_import`` in ``api/app.py``, which pre-warms it in the
background when a tracking URI is configured.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mlops_framework.api.schemas import ExternalPanel
from mlops_framework.tracking.mlflow_client import client_or_reason, tracking_uri

__all__ = ["client_or_reason", "tracking_uri", "panel"]


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
