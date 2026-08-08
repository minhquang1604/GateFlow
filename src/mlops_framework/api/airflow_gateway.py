"""Read-only gateway to the Airflow deployment.

Mirrors ``mlflow_gateway.py``'s shape: every Airflow-backed endpoint shares
the same two concerns (not configured, or unreachable), so that is answered
once here instead of in each router function.

Nothing here writes to Airflow — no trigger, no clear, no pause. The
adapter (``AirflowOrchestrator``) supports those for the training-service
call path; this gateway only ever calls its read methods.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

import httpx

from mlops_framework.api.schemas import ExternalPanel
from mlops_framework.config.settings import get_settings

# AirflowOrchestrator's own default (httpx.Client(timeout=30.0)) is sized
# for the training-service call path — triggering a run, checking on it —
# where a slow but eventually-successful request beats a false negative.
# These console panels are the opposite case: read-only, rendered
# asynchronously after the page has already painted, best-effort by
# design. Measured against an unreachable host, the default timeout is a
# real ceiling (not MLflow's multi-minute retry hang) but still a
# noticeably long stall for a supplementary panel — a security-group DROP
# on AWS burns the full 30s with no response at all. Bounded here to keep
# a dead Airflow from being a 30-second wait before the page says so.
_PANEL_TIMEOUT_SECONDS = 8.0


def base_url() -> Optional[str]:
    """Return the configured Airflow base URL, if any."""
    return get_settings().airflow_base_url


def client_or_reason() -> Tuple[Any, Optional[str]]:
    """Build an ``AirflowOrchestrator``, or explain why one is not available.

    Returns:
        ``(orchestrator, None)`` on success, ``(None, reason)`` otherwise.
        The caller is responsible for closing the orchestrator — ``panel()``
        does this; a caller using ``client_or_reason()`` directly (the
        artifact-style raw-response endpoints) must do the same.
    """
    settings = get_settings()
    url = settings.airflow_base_url
    if not url:
        return None, "AIRFLOW_BASE_URL is not configured"

    from mlops_framework.exceptions import OrchestratorConfigError
    from mlops_framework.orchestration.airflow import AirflowOrchestrator

    try:
        bounded_client = httpx.Client(
            base_url=url.rstrip("/"),
            auth=(settings.airflow_username, settings.airflow_password),
            timeout=_PANEL_TIMEOUT_SECONDS,
        )
        return (
            AirflowOrchestrator(base_url=url, http_client=bounded_client),
            None,
        )
    except OrchestratorConfigError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - never fail the page
        return None, f"Airflow unavailable: {exc}"


def panel(fn: Callable[[Any], Any]) -> ExternalPanel:
    """Run ``fn(orchestrator)`` and wrap the outcome in an :class:`ExternalPanel`.

    Any exception becomes ``available=False`` with the error as the reason,
    for the same reason as the MLflow gateway: a panel that fails should
    degrade on its own, not take the page down with it.
    """
    orchestrator, reason = client_or_reason()
    if orchestrator is None:
        return ExternalPanel(available=False, reason=reason)
    try:
        return ExternalPanel(available=True, data=fn(orchestrator))
    except Exception as exc:  # noqa: BLE001 - never fail the page
        return ExternalPanel(available=False, reason=f"Airflow error: {exc}")
    finally:
        orchestrator.close()
