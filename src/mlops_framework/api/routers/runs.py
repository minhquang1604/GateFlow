"""Training runs router.

Beyond the framework's own rows, two endpoints here reach out to the
systems a run was executed on — Airflow for per-task state, MLflow for
what it recorded about the run. Both are strictly best-effort: the UI
renders without them, so a tracking server being down degrades a panel
rather than failing the page.

The rest of the MLflow surface — experiments, artifacts, model signature
— lives in ``mlflow_views.py``; only the per-run view is here, next to
the run it belongs to.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.api import airflow_gateway
from mlops_framework.api.deps import get_db, get_db_manager_dep
from mlops_framework.api.mlflow_gateway import panel, tracking_uri
from mlops_framework.api.schemas import ExternalPanel, TrainingRunOut
from mlops_framework.database.models.training_run import TrainingRun
from mlops_framework.database.session import DatabaseManager

router = APIRouter()


@router.get("/training-runs", response_model=list[TrainingRunOut])
def list_runs(
    status: str | None = Query(default=None, description="Filter by run status"),
    dataset_version_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[TrainingRunOut]:
    """List training runs, newest first.

    Supports filtering by ``status`` and ``dataset_version_id``.
    """
    stmt = select(TrainingRun).order_by(TrainingRun.id.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(TrainingRun.status == status)
    if dataset_version_id is not None:
        stmt = stmt.where(TrainingRun.dataset_version_id == dataset_version_id)
    runs = list(db.execute(stmt).scalars().all())
    return [TrainingRunOut.from_orm_with_json(r) for r in runs]


@router.get("/training-runs/{run_id}", response_model=TrainingRunOut)
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
) -> TrainingRunOut:
    """Return a single training run by id."""
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"TrainingRun {run_id} not found")
    return TrainingRunOut.from_orm_with_json(run)


# ---------------------------------------------------------------------- #
# Live status — Server-Sent Events
# ---------------------------------------------------------------------- #

# Polled *inside the server*, not by the browser — see run_detail.html's
# subscribeToRunEvents(). The alternative (the browser itself polling
# GET /training-runs/{id} on a setInterval) works too, but every open tab
# would independently hit the database on its own timer; this way there
# is exactly one query per tick per open stream, and the query itself
# never touches the request-scoped session (see _read_status below) so a
# slow client holding a connection open can't pin a pooled session for
# the run's entire lifetime.
_SSE_POLL_SECONDS = 2.0
# Bounds a stream against a run that enters RUNNING and then never
# reaches a terminal state (an orchestrator that died without ever
# calling back /internal/training-runs/{id}/finish) — without this, a
# forgotten browser tab holds its connection (and the asyncio task behind
# it) open forever.
_SSE_MAX_SECONDS = 1800.0


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _read_run_status(db: DatabaseManager, run_id: int) -> TrainingRunOut | None:
    """A standalone read, deliberately not using the request's own
    ``Depends(get_db)`` session — that session lives and dies with the
    request, but this function is called repeatedly across a stream that
    can run for many minutes. Opens and closes its own short-lived
    session per call, same pattern as ``api/app.py``'s scheduler loop.

    ``db`` still comes from ``Depends(get_db_manager_dep)`` in the route
    below, not the global ``get_db_manager()`` directly — the former is
    what ``app.dependency_overrides`` (every test's isolated in-memory
    database) actually replaces; calling the global getter here would
    silently read past a test's override and hit whatever `DATABASE_URL`
    happens to be set to."""
    with db.get_session() as session:
        run = session.get(TrainingRun, run_id)
        return TrainingRunOut.from_orm_with_json(run) if run is not None else None


@router.get("/training-runs/{run_id}/events")
async def stream_run_events(
    run_id: int,
    request: Request,
    db: DatabaseManager = Depends(get_db_manager_dep),
) -> StreamingResponse:
    """Server-Sent Events stream of one TrainingRun's status.

    Emits a ``status`` event every time the status changes (including
    once immediately on connect, whatever the current status is), then
    closes the stream itself once that status is terminal
    (SUCCESS/FAILED/CANCELLED) — a run already finished when the client
    connects gets exactly one event and an immediate close, not a
    dangling open connection. Also closes on ``_SSE_MAX_SECONDS`` (a
    ``timeout`` event first) and as soon as the client disconnects.
    """
    initial = await asyncio.to_thread(_read_run_status, db, run_id)
    if initial is None:
        raise HTTPException(status_code=404, detail=f"TrainingRun {run_id} not found")

    async def event_stream() -> AsyncIterator[str]:
        last_status: str | None = None
        elapsed = 0.0
        while True:
            if await request.is_disconnected():
                return
            run = await asyncio.to_thread(_read_run_status, db, run_id)
            if run is None:
                yield _sse_event("error", {"detail": f"TrainingRun {run_id} not found"})
                return
            if run.status != last_status:
                last_status = run.status
                yield _sse_event("status", json.loads(run.model_dump_json()))
            if run.status in ("SUCCESS", "FAILED", "CANCELLED"):
                return
            if elapsed >= _SSE_MAX_SECONDS:
                yield _sse_event("timeout", {"status": run.status})
                return
            await asyncio.sleep(_SSE_POLL_SECONDS)
            elapsed += _SSE_POLL_SECONDS

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx (and similar proxies) buffer a proxied response by
            # default, which would hold every event until the buffer
            # filled or the stream closed — defeating the point. Not
            # meaningful outside such a proxy, but harmless to always send.
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------- #
# External-system views. Best-effort by design — see the module docstring.
# ---------------------------------------------------------------------- #


# MLflow's system-metrics collector prefixes everything it records with
# this; see mlflow.system_metrics.
SYSTEM_METRIC_PREFIX = "system/"


def _dataset_inputs(mlflow_run: Any) -> list[dict[str, Any]]:
    """Summarise the datasets a run declared it trained on.

    MLflow records these through ``mlflow.log_input``. The digest is the
    interesting field: it is content-derived, so it can be held against the
    framework's own dataset-version checksum to catch a run that trained on
    something other than what the lineage claims.

    Returns an empty list when the run logged no inputs, which is the
    common case — the framework does not call ``log_input`` today.
    """
    inputs = getattr(mlflow_run, "inputs", None)
    entries = getattr(inputs, "dataset_inputs", None) or []
    out: list[dict[str, Any]] = []
    for entry in entries:
        dataset = getattr(entry, "dataset", None)
        if dataset is None:
            continue
        schema = getattr(dataset, "schema", None)
        out.append(
            {
                "name": getattr(dataset, "name", None),
                "digest": getattr(dataset, "digest", None),
                "source_type": getattr(dataset, "source_type", None),
                "source": getattr(dataset, "source", None),
                # Left as the raw JSON string MLflow stores. Its shape
                # varies by dataset flavour (tensorspec vs colspec), and
                # guessing wrong would be worse than showing it verbatim.
                "schema": schema,
                "tags": {
                    t.key: t.value for t in (getattr(entry, "tags", None) or [])
                },
            }
        )
    return out


def _run_or_404(db: Session, run_id: int) -> TrainingRun:
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"TrainingRun {run_id} not found")
    return run


@router.get("/training-runs/{run_id}/tasks", response_model=ExternalPanel)
def get_run_tasks(run_id: int, db: Session = Depends(get_db)) -> ExternalPanel:
    """Airflow's view of a run: the DAG run's own state plus per-task detail.

    ``tasks`` is now a list of full task-instance records (state, timing,
    retry count, operator, pool, hostname — see
    ``AirflowOrchestrator.get_task_instances``), not the bare
    ``{task_id: state}`` map this endpoint used to return; a task mid-retry
    was indistinguishable from a first attempt without ``try_number``.
    ``dag_run`` adds the run-level state/dates/**conf** from
    ``get_execution_status`` — the same call ``sync_from_orchestrator``
    already makes elsewhere, now surfaced here too since a run's own
    parameters (``conf``) are worth showing next to its tasks.
    """
    run = _run_or_404(db, run_id)
    metadata = json.loads(run.metadata_json or "{}")
    execution_id = metadata.get("orchestrator_execution_id")
    if not execution_id:
        return ExternalPanel(available=False, reason="run has no orchestrator execution id")
    if "/" not in str(execution_id):
        return ExternalPanel(
            available=False,
            reason="run was not executed on Airflow (local orchestrator)",
        )
    execution_id = str(execution_id)

    def query(orchestrator: Any) -> dict[str, Any]:
        status = orchestrator.get_execution_status(execution_id)
        return {
            "execution_id": execution_id,
            "dag_run": {
                "state": status.state.value,
                "started_at": status.started_at.isoformat() if status.started_at else None,
                "finished_at": status.finished_at.isoformat() if status.finished_at else None,
                "conf": (status.metadata or {}).get("conf") or {},
            },
            "tasks": orchestrator.get_task_instances(execution_id),
        }

    return airflow_gateway.panel(query)


def _run_mlflow_panel(mlflow_run_id: str) -> ExternalPanel:
    """Everything MLflow holds about a run, in one call.

    Renamed from ``/metric-history``: that endpoint already fetched the
    whole run via ``get_run`` and then used only the metrics, throwing away
    the tags and info alongside them. Those carry the run's provenance —
    the git commit, the source file, the user — which is exactly what a
    governance-oriented console should show. Widening the payload costs
    nothing extra on the wire to MLflow and saves the browser a round trip.

    Metric *history* is the part the framework cannot reproduce on its own:
    it stores each metric's final value, while MLflow keeps the whole
    series, which is what makes a training curve renderable.

    Shared by both routes below — the framework-run-scoped
    ``/training-runs/{run_id}/mlflow`` and ``/mlflow/runs/{mlflow_run_id}``,
    which takes the raw MLflow run id directly for a run this framework
    has no TrainingRun row for at all (a sweep's child run, a run
    started outside this framework) — every such run in the Experiments
    list was previously a dead end with nothing to click into.
    """

    def query(client: Any) -> dict[str, Any]:
        mlflow_run = client.get_run(mlflow_run_id)
        series = {
            key: [
                {"step": m.step, "value": m.value, "timestamp": m.timestamp}
                for m in client.get_metric_history(mlflow_run_id, key)
            ]
            for key in mlflow_run.data.metrics
        }
        # MLflow's own resource collector namespaces what it records under
        # "system/". Those series answer a different question from the
        # model's metrics — how hard the box was working, not how well the
        # model did — so they are split out here rather than left to mix
        # into the training charts.
        system = {k: v for k, v in series.items() if k.startswith(SYSTEM_METRIC_PREFIX)}
        model_series = {k: v for k, v in series.items() if k not in system}
        info = mlflow_run.info
        return {
            "mlflow_run_id": mlflow_run_id,
            "tracking_uri": tracking_uri(),
            "params": dict(mlflow_run.data.params),
            "metrics": {
                k: v
                for k, v in mlflow_run.data.metrics.items()
                if not k.startswith(SYSTEM_METRIC_PREFIX)
            },
            "history": model_series,
            "system_history": system,
            "dataset_inputs": _dataset_inputs(mlflow_run),
            "tags": dict(mlflow_run.data.tags or {}),
            "info": {
                # experiment_id is what makes the "open in MLflow" deep link
                # correct; it used to be hardcoded to 0 in the UI, which is
                # wrong for every run outside the Default experiment.
                "experiment_id": info.experiment_id,
                "run_name": info.run_name,
                "status": info.status,
                "start_time": info.start_time,
                "end_time": info.end_time,
                "artifact_uri": info.artifact_uri,
                "user_id": info.user_id,
                "lifecycle_stage": info.lifecycle_stage,
            },
        }

    return panel(query)


@router.get("/training-runs/{run_id}/mlflow", response_model=ExternalPanel)
def get_run_mlflow(run_id: int, db: Session = Depends(get_db)) -> ExternalPanel:
    """Everything MLflow holds about a run, in one call — by framework
    run id."""
    run = _run_or_404(db, run_id)
    if not run.mlflow_run_id:
        return ExternalPanel(available=False, reason="run has no MLflow run id")
    return _run_mlflow_panel(str(run.mlflow_run_id))


@router.get("/mlflow/runs/{mlflow_run_id}", response_model=ExternalPanel)
def get_mlflow_run(mlflow_run_id: str) -> ExternalPanel:
    """Everything MLflow holds about a run, in one call — by raw MLflow
    run id, for a run with no framework TrainingRun row at all."""
    return _run_mlflow_panel(mlflow_run_id)
