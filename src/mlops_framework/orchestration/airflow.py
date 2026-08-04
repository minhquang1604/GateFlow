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

Execution ids
-------------

Every DAG-run endpoint in Airflow 2.x is nested under its DAG:
``/api/v1/dags/{dag_id}/dagRuns/{dag_run_id}``. There is no endpoint
that resolves a bare ``dag_run_id`` across DAGs, so the ``execution_id``
this adapter hands back — and that callers persist and pass to
:meth:`get_execution_status` / :meth:`cancel_execution` — is the
composite ``"{dag_id}/{dag_run_id}"``. Airflow restricts ``dag_id`` to
``[A-Za-z0-9_.-]``, so the first ``/`` is an unambiguous separator.

Live coverage
-------------

``tests/unit/test_airflow_orchestrator.py`` covers this adapter against
a fake client, and ``tests/integration/test_airflow_live.py`` runs the
same flows against a real Airflow (opt-in via ``AIRFLOW_BASE_URL``).
The fake alone is not enough: the URL bug this module carried until
recently — DAG-run reads and cancels sent to ``/api/v1/dagRuns/{id}``,
which Airflow does not serve — was invisible because the fake answered
200 to any URL it did not recognise.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

from mlops_framework.config.settings import get_settings
from mlops_framework.exceptions import (
    ExecutionNotFoundError,
    OrchestratorConfigError,
)
from mlops_framework.orchestration.base import (
    ExecutionState,
    ExecutionStatus,
    Orchestrator,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_airflow_state(state: str) -> ExecutionState:
    state = (state or "").lower()
    if state == "success":
        return ExecutionState.SUCCESS
    if state == "failed":
        return ExecutionState.FAILED
    if state in {"running", "queued"}:
        return ExecutionState.RUNNING
    if state in {"up_for_retry", "upstream_failed", "scheduled"}:
        return ExecutionState.PENDING
    if state == "skipped":
        return ExecutionState.CANCELLED
    return ExecutionState.UNKNOWN


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


class AirflowOrchestrator(Orchestrator):
    """Adapter that drives an Airflow 2.x deployment via its REST API.

    Constructor precedence: explicit kwargs > environment-driven
    :class:`Settings`. This keeps tests trivial (use the existing
    ``_FakeAirflowClient`` pattern) and makes the production adapter
    zero-config when the framework is deployed against the bundled
    Compose stack.

    Args:
        base_url: e.g. ``"http://airflow.internal:8080"``. Falls back
            to ``Settings.airflow_base_url``.
        username / password: HTTP basic auth credentials. Fall back to
            ``Settings.airflow_{username,password}``.
        http_client: Optional pre-configured ``httpx.Client``. If not
            given, one is created with the supplied credentials.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        # Set attributes first so __del__ is safe even if we raise below.
        self._owns_client = http_client is None
        self._client = http_client  # may remain None on error
        settings = get_settings()
        effective_url = base_url or settings.airflow_base_url
        if not effective_url:
            raise OrchestratorConfigError(
                "AirflowOrchestrator requires a base_url "
                "(explicit or AIRFLOW_BASE_URL env var)"
            )
        self._base_url = effective_url.rstrip("/")
        effective_user = username or settings.airflow_username or "airflow"
        effective_pass = password or settings.airflow_password or "airflow"
        if self._client is None:
            self._client = httpx.Client(
                base_url=self._base_url,
                auth=(effective_user, effective_pass),
                timeout=30.0,
            )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Close the HTTP client if this adapter created it."""
        if self._owns_client and self._client is not None:
            self._client.close()

    def __enter__(self) -> "AirflowOrchestrator":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Execution id handling
    # ------------------------------------------------------------------ #

    @staticmethod
    def make_execution_id(dag_id: str, dag_run_id: str) -> str:
        """Compose the adapter's execution id from its two Airflow parts."""
        return f"{dag_id}/{dag_run_id}"

    @staticmethod
    def _split_execution_id(execution_id: str) -> tuple[str, str]:
        """Split ``"{dag_id}/{dag_run_id}"`` back into its two parts.

        Raises:
            ExecutionNotFoundError: if the id is not in composite form.
                Every DAG-run endpoint is nested under its DAG and
                Airflow offers no lookup by bare run id, so a bare id
                is not addressable — failing here is clearer than
                sending a request that can only 404.
        """
        dag_id, sep, dag_run_id = execution_id.partition("/")
        if not sep or not dag_id or not dag_run_id:
            raise ExecutionNotFoundError(
                f"Execution id {execution_id!r} is not addressable: Airflow "
                "DAG runs live at /api/v1/dags/{dag_id}/dagRuns/{dag_run_id}, "
                "so the id must be '<dag_id>/<dag_run_id>' (see "
                "AirflowOrchestrator.make_execution_id)."
            )
        return dag_id, dag_run_id

    def _dag_run_url(self, execution_id: str, suffix: str = "") -> str:
        dag_id, dag_run_id = self._split_execution_id(execution_id)
        return (
            f"/api/v1/dags/{quote(dag_id, safe='')}"
            f"/dagRuns/{quote(dag_run_id, safe='')}{suffix}"
        )

    # ------------------------------------------------------------------ #
    # Orchestrator API
    # ------------------------------------------------------------------ #

    def trigger_pipeline(
        self,
        pipeline_id: str,
        config: Optional[dict[str, Any]] = None,
    ) -> str:
        """Trigger a DAG run.

        Returns:
            The composite execution id ``"{dag_id}/{dag_run_id}"``.
        """
        logical_date = _now().isoformat()
        # A uuid suffix, not the timestamp alone: Airflow rejects a
        # duplicate dag_run_id with 409, and two runs triggered inside
        # the same clock resolution would collide.
        body: dict[str, Any] = {
            "dag_run_id": f"mlops-{uuid.uuid4().hex[:12]}",
            "logical_date": logical_date,
            "conf": config or {},
            "note": "Triggered by mlops_framework",
        }
        url = f"/api/v1/dags/{quote(pipeline_id, safe='')}/dagRuns"
        response = self._client.post(url, json=body)
        if response.status_code == 404:
            raise OrchestratorConfigError(f"Airflow DAG {pipeline_id!r} not found")
        if response.status_code == 409:
            raise OrchestratorConfigError(
                f"Airflow rejected the run as a duplicate for DAG "
                f"{pipeline_id!r}: {response.text}"
            )
        if response.status_code not in (200, 201):
            raise OrchestratorConfigError(
                f"Airflow trigger failed: {response.status_code} {response.text}"
            )
        payload = response.json()
        return self.make_execution_id(payload["dag_id"], payload["dag_run_id"])

    def get_execution_status(self, execution_id: str) -> ExecutionStatus:
        url = self._dag_run_url(execution_id)
        response = self._client.get(url)
        if response.status_code == 404:
            raise ExecutionNotFoundError(
                f"Airflow DAG run {execution_id!r} not found"
            )
        if response.status_code != 200:
            raise OrchestratorConfigError(
                f"Airflow status query failed: {response.status_code} {response.text}"
            )
        return self._to_status(response.json())

    def get_task_instance_states(self, execution_id: str) -> dict[str, str]:
        """Return a ``{task_id: state}`` map for a DAG run.

        Optional secondary query used by callers that want finer-grained
        reporting than the DAG-level state. Returns ``{}`` on a 404 or
        any non-200 — this query is best-effort and never raises.
        """
        try:
            url = self._dag_run_url(execution_id, "/taskInstances")
            response = self._client.get(url)
        except Exception:
            return {}
        if response.status_code != 200:
            return {}
        payload = response.json()
        out: dict[str, str] = {}
        for entry in payload.get("task_instances", []) or []:
            tid = entry.get("task_id")
            state = entry.get("state")
            if tid is not None and state is not None:
                out[str(tid)] = str(state)
        return out

    def cancel_execution(self, execution_id: str) -> ExecutionStatus:
        """Cancel a DAG run by marking it failed.

        Airflow has no "cancelled" DAG-run state and no cancel endpoint;
        ``PATCH`` with ``{"state": "failed"}`` is the supported way to
        stop a run, and it stops queued task instances. The previous
        implementation issued ``DELETE``, which erases the run from
        Airflow's metadata database entirely — the run disappears from
        the UI and from any later audit, which is the opposite of what
        a governance-oriented framework wants. The framework's own
        TrainingRun still records CANCELLED; only Airflow's view says
        failed.
        """
        url = self._dag_run_url(execution_id)
        response = self._client.patch(url, json={"state": "failed"})
        if response.status_code == 404:
            raise ExecutionNotFoundError(
                f"Airflow DAG run {execution_id!r} not found"
            )
        if response.status_code != 200:
            raise OrchestratorConfigError(
                f"Airflow cancel failed: {response.status_code} {response.text}"
            )
        status = self._to_status(response.json())
        return ExecutionStatus(
            execution_id=status.execution_id,
            state=ExecutionState.CANCELLED,
            pipeline_id=status.pipeline_id,
            started_at=status.started_at,
            finished_at=status.finished_at or _now(),
            message="DAG run marked failed via REST (Airflow has no cancelled state)",
            metadata=status.metadata,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @classmethod
    def _to_status(cls, payload: dict[str, Any]) -> ExecutionStatus:
        state = _parse_airflow_state(payload.get("state", ""))
        return ExecutionStatus(
            execution_id=cls.make_execution_id(
                payload.get("dag_id", ""), payload["dag_run_id"]
            ),
            state=state,
            pipeline_id=payload.get("dag_id"),
            started_at=_parse_iso(payload.get("start_date")),
            finished_at=_parse_iso(payload.get("end_date")),
            message=payload.get("note"),
            metadata={
                "logical_date": payload.get("logical_date"),
                "external_trigger": payload.get("external_trigger"),
                "conf": payload.get("conf"),
            },
        )
