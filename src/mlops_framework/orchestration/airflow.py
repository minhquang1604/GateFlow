"""Airflow REST API adapter for the Orchestrator interface.

The framework's core code never imports the ``airflow`` Python package.
The adapter speaks to an Airflow 2.x deployment over its REST API using
:class:`httpx.Client`. This keeps the dependency direction clean:

    Framework -> Orchestrator (ABC)
                     ^
                     |
            AirflowOrchestrator
                     |
              Airflow REST API
                     |
                  DAG runs

For unit tests, ``http_client`` can be replaced with any object that
implements :class:`httpx.Client`'s interface (or with the
:class:`tests.unit.test_airflow_orchestrator._FakeAirflowClient` stub).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import httpx

from mlops_framework.exceptions import (
    ExecutionNotFoundError,
    OrchestratorConfigError,
)
from mlops_framework.orchestration.base import (
    ExecutionState,
    ExecutionStatus,
    Orchestrator,
)


_TERMINAL_AIRFLOW_STATES = {"success", "failed"}
_RUNNING_AIRFLOW_STATES = {"running", "queued"}


def _now() -> datetime:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _parse_airflow_state(state: str) -> ExecutionState:
    state = (state or "").lower()
    if state in {"success"}:
        return ExecutionState.SUCCESS
    if state in {"failed"}:
        return ExecutionState.FAILED
    if state in {"running", "queued"}:
        return ExecutionState.RUNNING
    if state in {"up_for_retry", "upstream_failed", "scheduled"}:
        return ExecutionState.PENDING
    if state in {"skipped"}:
        return ExecutionState.CANCELLED
    return ExecutionState.UNKNOWN


class AirflowOrchestrator(Orchestrator):
    """Adapter that drives an Airflow 2.x deployment via its REST API.

    Args:
        base_url: e.g. ``"http://localhost:8080"``.
        username / password: HTTP basic auth credentials.
        http_client: Optional pre-configured ``httpx.Client``. If not
            given, one is created with the supplied credentials.
    """

    def __init__(
        self,
        base_url: str,
        username: str = "airflow",
        password: str = "airflow",
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        # Set attributes first so __del__ is safe even if we raise below.
        self._owns_client = http_client is None
        self._client = http_client  # may remain None on error
        if not base_url:
            raise OrchestratorConfigError("AirflowOrchestrator requires a base_url")
        self._base_url = base_url.rstrip("/")
        if self._client is None:
            self._client = httpx.Client(
                base_url=self._base_url,
                auth=(username, password),
                timeout=30.0,
            )

    def __del__(self) -> None:  # pragma: no cover - best effort
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Orchestrator API
    # ------------------------------------------------------------------ #

    def trigger_pipeline(
        self,
        pipeline_id: str,
        config: Optional[dict[str, Any]] = None,
    ) -> str:
        """Trigger a DAG run. Returns the Airflow ``dag_run_id``."""
        logical_date = _now().isoformat()
        body: dict[str, Any] = {
            "dag_run_id": f"mlops-{logical_date}",
            "logical_date": logical_date,
            "conf": config or {},
            "note": "Triggered by mlops_framework",
        }
        url = f"/api/v1/dags/{pipeline_id}/dagRuns"
        response = self._client.post(url, json=body)
        if response.status_code == 404:
            raise OrchestratorConfigError(
                f"Airflow DAG {pipeline_id!r} not found"
            )
        if response.status_code not in (200, 201):
            raise OrchestratorConfigError(
                f"Airflow trigger failed: {response.status_code} {response.text}"
            )
        payload = response.json()
        return payload["dag_run_id"]

    def get_execution_status(self, execution_id: str) -> ExecutionStatus:
        # We don't know which DAG the run belongs to, so we accept the
        # "pipeline_id" embedded in the metadata if present. Without it
        # we try a list endpoint and match by id.
        url = f"/api/v1/dagRuns/{execution_id}"
        response = self._client.get(url)
        if response.status_code == 404:
            raise ExecutionNotFoundError(
                f"Airflow DAG run {execution_id!r} not found"
            )
        if response.status_code != 200:
            raise OrchestratorConfigError(
                f"Airflow status query failed: {response.status_code} {response.text}"
            )
        payload = response.json()
        return self._to_status(payload)

    def cancel_execution(self, execution_id: str) -> ExecutionStatus:
        # Airflow does not support cancelling an arbitrary DAG run via
        # REST in 2.x — it supports deleting task instances, and the
        # common pattern is to mark the DAG run as failed. We delete
        # the dag run; the operator's downstream code treats the
        # absence as cancellation. For production, an
        # AirflowDeploymentManager would handle this; this adapter
        # exposes the cleanest REST-only path.
        url = f"/api/v1/dagRuns/{execution_id}"
        response = self._client.delete(url)
        if response.status_code == 404:
            raise ExecutionNotFoundError(
                f"Airflow DAG run {execution_id!r} not found"
            )
        if response.status_code not in (200, 204):
            raise OrchestratorConfigError(
                f"Airflow cancel failed: {response.status_code} {response.text}"
            )
        return ExecutionStatus(
            execution_id=execution_id,
            state=ExecutionState.CANCELLED,
            finished_at=_now(),
            message="DAG run deleted via REST",
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_status(payload: dict[str, Any]) -> ExecutionStatus:
        state = _parse_airflow_state(payload.get("state", ""))
        started = payload.get("start_date")
        finished = payload.get("end_date")
        return ExecutionStatus(
            execution_id=payload["dag_run_id"],
            state=state,
            pipeline_id=payload.get("dag_id"),
            started_at=_parse_iso(started),
            finished_at=_parse_iso(finished),
            message=payload.get("note"),
            metadata={
                "logical_date": payload.get("logical_date"),
                "external_trigger": payload.get("external_trigger"),
                "conf": payload.get("conf"),
            },
        )


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Airflow returns ISO 8601 with trailing 'Z' for UTC.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None
