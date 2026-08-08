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

Task logs and remote logging
-----------------------------

:meth:`get_task_log` proxies Airflow's own log-fetch endpoint. As of
2026-08-08 the deployment logs to S3
(``AIRFLOW__LOGGING__REMOTE_LOGGING=True``, wired in
``infrastructure/terraform/environments/prod/main.tf``'s
``local.airflow_remote_logging_env``, applied to both
airflow-webserver and airflow-scheduler), so a task's log survives its
container being redeployed. Verified end-to-end against the live
deployment: put+get+delete through the same ``S3Hook``/``aws_default``
connection Airflow's own ``S3TaskHandler`` uses, over the task role —
no explicit AWS credentials anywhere in this config, the same pattern
mlflow and the training tasks already relied on for S3 access.

Before that date this deployment had no remote logging configured
despite the bucket already existing, and this method could only serve
a task's log while the container that ran it was still alive —
Airflow's webserver tried to fetch it directly from the worker's
short-lived ECS hostname and failed DNS resolution for anything
redeployed since. That failure mode is why this method treats a 200
response as no guarantee of a real log body: Airflow reports a fetch
failure that way, embedded in the text, not as an HTTP error, and this
adapter still passes it through unchanged rather than intercepting it
— it remains the accurate answer for logs written before remote
logging existed, or for a deployment that later loses it again.
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

    def get_task_instances(self, execution_id: str) -> list[dict[str, Any]]:
        """Return the full task-instance list for a DAG run.

        Optional secondary query used by callers that want finer-grained
        reporting than the DAG-level state. Returns ``[]`` on a 404 or
        any non-200 — this query is best-effort and never raises.

        Each entry keeps the fields a task-level Gantt view needs beyond
        bare state — ``start_date``/``end_date``/``duration``,
        ``try_number``/``max_tries`` (a task mid-retry looks identical to a
        first attempt without these), and ``operator``/``pool``/``hostname``
        for "which task is slow and where did it run". An earlier version
        of this method discarded everything but ``task_id``/``state`` from
        this same response.
        """
        try:
            url = self._dag_run_url(execution_id, "/taskInstances")
            response = self._client.get(url)
        except Exception:
            return []
        if response.status_code != 200:
            return []
        payload = response.json()
        out: list[dict[str, Any]] = []
        for entry in payload.get("task_instances", []) or []:
            tid = entry.get("task_id")
            state = entry.get("state")
            if tid is None or state is None:
                continue
            out.append(
                {
                    "task_id": str(tid),
                    "state": str(state),
                    "start_date": entry.get("start_date"),
                    "end_date": entry.get("end_date"),
                    "duration": entry.get("duration"),
                    "try_number": entry.get("try_number"),
                    "max_tries": entry.get("max_tries"),
                    "operator": entry.get("operator"),
                    "pool": entry.get("pool"),
                    "queue": entry.get("queue"),
                    "hostname": entry.get("hostname"),
                }
            )
        return out

    def list_dags(self, limit: int = 100) -> list[dict[str, Any]]:
        """List DAGs known to the Airflow deployment.

        Paginates until every DAG is fetched or ``limit`` is reached,
        whichever comes first — a single Airflow response page tops out
        at 100 by default, and this framework's own DAG count is small,
        but a hardcoded single-page fetch would silently truncate a
        larger deployment.
        """
        dags: list[dict[str, Any]] = []
        offset = 0
        page_size = 100
        while len(dags) < limit:
            response = self._client.get(
                f"/api/v1/dags?limit={page_size}&offset={offset}"
            )
            if response.status_code != 200:
                break
            payload = response.json()
            page = payload.get("dags", []) or []
            for d in page:
                dags.append(
                    {
                        "dag_id": d.get("dag_id"),
                        "description": d.get("description"),
                        "is_paused": d.get("is_paused"),
                        "is_active": d.get("is_active"),
                        "schedule_interval": d.get("schedule_interval"),
                        "next_dagrun": d.get("next_dagrun"),
                        "owners": d.get("owners") or [],
                        "tags": [t.get("name") for t in (d.get("tags") or []) if t.get("name")],
                        "has_import_errors": d.get("has_import_errors"),
                        "max_active_runs": d.get("max_active_runs"),
                    }
                )
            offset += page_size
            if len(page) < page_size or offset >= payload.get("total_entries", 0):
                break
        return dags[:limit]

    def get_dag_tasks(self, dag_id: str) -> list[dict[str, Any]]:
        """Return a DAG's tasks with their downstream dependencies.

        This is the DAG's static structure — the same for every run of it
        — not a run's state, which is why it lives here rather than on
        :meth:`get_task_instances`.
        """
        response = self._client.get(f"/api/v1/dags/{quote(dag_id, safe='')}/tasks")
        if response.status_code == 404:
            raise OrchestratorConfigError(f"Airflow DAG {dag_id!r} not found")
        if response.status_code != 200:
            raise OrchestratorConfigError(
                f"Airflow task-list query failed: {response.status_code} {response.text}"
            )
        payload = response.json()
        return [
            {
                "task_id": t.get("task_id"),
                "operator_name": t.get("class_ref", {}).get("class_name"),
                "downstream_task_ids": t.get("downstream_task_ids") or [],
                "trigger_rule": t.get("trigger_rule"),
            }
            for t in payload.get("tasks", []) or []
        ]

    def list_dag_runs(self, dag_id: str, limit: int = 25) -> list[dict[str, Any]]:
        """Return recent DAG runs, newest first — not only the ones this
        framework triggered.

        ``TrainingRun`` only ever learns about a run this framework itself
        started; a run the Airflow scheduler kicked off on its own schedule
        has no corresponding row and is otherwise invisible to the console.
        """
        response = self._client.get(
            f"/api/v1/dags/{quote(dag_id, safe='')}/dagRuns"
            f"?limit={limit}&order_by=-execution_date"
        )
        if response.status_code == 404:
            raise OrchestratorConfigError(f"Airflow DAG {dag_id!r} not found")
        if response.status_code != 200:
            raise OrchestratorConfigError(
                f"Airflow dag-run query failed: {response.status_code} {response.text}"
            )
        payload = response.json()
        out = []
        for r in payload.get("dag_runs", []) or []:
            out.append(
                {
                    "dag_run_id": r.get("dag_run_id"),
                    "state": r.get("state"),
                    "run_type": r.get("run_type"),
                    "execution_date": r.get("execution_date"),
                    "start_date": r.get("start_date"),
                    "end_date": r.get("end_date"),
                    "external_trigger": r.get("external_trigger"),
                    "conf": r.get("conf") or {},
                    "note": r.get("note"),
                }
            )
        return out

    def get_task_log(
        self, execution_id: str, task_id: str, try_number: int = 1
    ) -> str:
        """Fetch one task attempt's log as plain text.

        Airflow's log endpoint answers 200 with an in-band error message
        in the body (not an HTTP error) when it cannot reach wherever the
        log actually lives — e.g. local log serving pointed at a worker
        container that no longer exists. That text is returned as-is
        rather than detected and rewritten: it is Airflow's own accurate
        account of what went wrong, and paraphrasing it would risk hiding
        the real reason (see the module docstring's note on remote
        logging — configured since 2026-08-08, but still the fallback
        path for anything logged before that, or if it is ever
        unconfigured again).
        """
        dag_id, dag_run_id = self._split_execution_id(execution_id)
        url = (
            f"/api/v1/dags/{quote(dag_id, safe='')}/dagRuns/{quote(dag_run_id, safe='')}"
            f"/taskInstances/{quote(task_id, safe='')}/logs/{try_number}"
            "?full_content=true"
        )
        response = self._client.get(url)
        if response.status_code == 404:
            raise ExecutionNotFoundError(
                f"No log for task {task_id!r} attempt {try_number} on {execution_id!r}"
            )
        if response.status_code != 200:
            raise OrchestratorConfigError(
                f"Airflow log query failed: {response.status_code} {response.text}"
            )
        return response.text

    def get_import_errors(self) -> list[dict[str, Any]]:
        """DAG files Airflow could not parse.

        A DAG-parse failure is a common, silent reason a pipeline "does
        nothing" — the framework can trigger it, and Airflow will 404 or
        hang, and nothing in this framework's own tables explains why.
        """
        response = self._client.get("/api/v1/importErrors")
        if response.status_code != 200:
            return []
        payload = response.json()
        return [
            {
                "filename": e.get("filename"),
                "stack_trace": e.get("stack_trace"),
                "timestamp": e.get("timestamp"),
            }
            for e in payload.get("import_errors", []) or []
        ]

    def get_health(self) -> dict[str, Any]:
        """Component health: scheduler heartbeat, metadatabase, triggerer.

        Explains a run stuck at PENDING that a task-instance query alone
        cannot: if the scheduler's heartbeat is stale, nothing is going to
        pick the run up regardless of what its rows say.
        """
        response = self._client.get("/api/v1/health")
        if response.status_code != 200:
            return {}
        return response.json()

    def get_pools(self) -> list[dict[str, Any]]:
        """Slot usage per pool — another PENDING explanation: a full pool
        queues new task instances behind ones already running."""
        response = self._client.get("/api/v1/pools")
        if response.status_code != 200:
            return []
        payload = response.json()
        return [
            {
                "name": p.get("name"),
                "slots": p.get("slots"),
                "running_slots": p.get("running_slots"),
                "queued_slots": p.get("queued_slots"),
                "open_slots": p.get("open_slots"),
            }
            for p in payload.get("pools", []) or []
        ]

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
