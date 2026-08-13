"""Airflow-backed views: mostly read-only, plus a small, gated write path.

The framework's own tables answer "what ran and what came of it". Airflow
holds the other half — what is scheduled to run, whether the scheduler
itself is healthy, and why a DAG might not be running at all. Every
read-only endpoint here returns :class:`ExternalPanel`, so a missing or
unreachable Airflow degrades a card instead of failing a page — the same
contract ``mlflow_views.py`` follows for MLflow.

Task control (write)
---------------------

``clear_task``/``retry_task`` are the one exception to "nothing here
writes to Airflow" — added so a stuck or failed task can be fixed from
Gateflow instead of Airflow's own UI. Both routes require
``Depends(require_write_token)`` (see ``api/security.py``): the console
still has no user/session/RBAC concept, and that dependency is the
specific, minimal thing standing between "anyone who can reach the
console" and "can act on production pipelines" that this module's
docstring used to cite as the reason these routes did not exist.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from mlops_framework.api.airflow_gateway import client_or_reason, panel
from mlops_framework.api.deps import get_db
from mlops_framework.api.schemas import ExternalPanel
from mlops_framework.api.security import require_write_token
from mlops_framework.database.models.training_run import TrainingRun
from mlops_framework.exceptions import ExecutionNotFoundError, OrchestratorConfigError

router = APIRouter()


@router.get("/airflow/health", response_model=ExternalPanel)
def get_airflow_health() -> ExternalPanel:
    """Scheduler/metadatabase health, DAG import errors, and pool usage.

    Bundled into one call because they answer the same family of question
    — "why isn't anything running" — and none of the three costs enough on
    its own to justify a separate round trip from the page that renders
    them together.
    """

    def query(orchestrator: Any) -> dict[str, Any]:
        return {
            "health": orchestrator.get_health(),
            "import_errors": orchestrator.get_import_errors(),
            "pools": orchestrator.get_pools(),
        }

    return panel(query)


@router.get("/airflow/dags", response_model=ExternalPanel)
def list_dags() -> ExternalPanel:
    """List DAGs known to the Airflow deployment."""
    return panel(lambda o: {"dags": o.list_dags()})


@router.get("/airflow/dags/{dag_id}", response_model=ExternalPanel)
def get_dag_detail(
    dag_id: str,
    limit: int = Query(default=25, ge=1, le=200, description="Recent DAG runs to include"),
    grid_runs: int = Query(
        default=10,
        ge=0,
        le=25,
        description=(
            "How many of the most recent runs to expand into per-task "
            "instance states for the console's task-history grid. Each one "
            "costs one extra Airflow REST call, so this is capped well "
            "below `limit` and separately overridable — 0 skips the grid "
            "entirely for callers that don't need it."
        ),
    ),
) -> ExternalPanel:
    """A DAG's task structure, recent run history, and a task x run grid.

    ``dag_runs`` includes runs the scheduler triggered on its own, not
    only ones this framework started — the framework's ``TrainingRun``
    table only ever learns about the latter, so a scheduled run otherwise
    has no representation anywhere in the console.

    ``grid_run_ids``/``grid_cells`` expand the newest ``grid_runs`` of
    those into full task-instance state, so the console can draw an
    Airflow-Tree-View-style grid (and colour the DAG graph by the latest
    run) without the browser making one request per run itself. This
    reuses ``get_task_instances``/``make_execution_id`` — both already
    used by the per-run task view — rather than adding a new
    orchestrator method for what is just N calls to an existing one.
    """

    def query(orchestrator: Any) -> dict[str, Any]:
        dag_runs = orchestrator.list_dag_runs(dag_id, limit=limit)
        grid_run_ids = [
            r["dag_run_id"] for r in dag_runs[:grid_runs] if r.get("dag_run_id")
        ]
        grid_cells: list[dict[str, Any]] = []
        for run_id in grid_run_ids:
            execution_id = orchestrator.make_execution_id(dag_id, run_id)
            for task_instance in orchestrator.get_task_instances(execution_id):
                grid_cells.append({"dag_run_id": run_id, **task_instance})
        return {
            "dag_id": dag_id,
            "tasks": orchestrator.get_dag_tasks(dag_id),
            "dag_runs": dag_runs,
            "grid_run_ids": grid_run_ids,
            "grid_cells": grid_cells,
        }

    return panel(query)


def _execution_id_or_error(db: Session, run_id: int) -> str:
    """Resolve a framework run to its Airflow execution id, or 404/409.

    Duplicates the resolution ``runs.get_run_tasks`` does inline rather
    than importing it, the same way ``mlflow_views._mlflow_run_id`` stands
    apart from ``runs.py`` — each router resolves its own view of a run
    rather than reaching into another router's internals.
    """
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"TrainingRun {run_id} not found")
    metadata = json.loads(run.metadata_json or "{}")
    execution_id = metadata.get("orchestrator_execution_id")
    if not execution_id or "/" not in str(execution_id):
        raise HTTPException(
            status_code=409,
            detail=f"TrainingRun {run_id} was not executed on Airflow",
        )
    return str(execution_id)


@router.get("/training-runs/{run_id}/tasks/{task_id}/log")
def get_task_log(
    run_id: int,
    task_id: str,
    try_number: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """Stream one task attempt's log as plain text.

    Not an :class:`ExternalPanel`: the browser consumes this directly as
    text, the same reasoning ``mlflow_views.get_run_artifact`` uses for
    artifact bytes rather than wrapping them in JSON.

    See ``AirflowOrchestrator``'s module docstring for why this can come
    back with Airflow's own "could not read served logs" message embedded
    in a 200 response in this deployment — that text is passed through
    unchanged rather than intercepted, because it is the accurate answer.
    """
    execution_id = _execution_id_or_error(db, run_id)

    orchestrator, reason = client_or_reason()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail=reason)
    try:
        text = orchestrator.get_task_log(execution_id, task_id, try_number=try_number)
    except ExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface as a clean error
        raise HTTPException(status_code=502, detail=f"Airflow error: {exc}") from exc
    finally:
        orchestrator.close()
    return PlainTextResponse(text)


# ---------------------------------------------------------------------- #
# Task control (write — gated, see module docstring)
# ---------------------------------------------------------------------- #


class TaskActionResult(BaseModel):
    task_id: str
    action: str
    cleared_task_instances: int


def _run_task_action(
    run_id: int, task_id: str, db: Session, *, action: str
) -> TaskActionResult:
    execution_id = _execution_id_or_error(db, run_id)

    orchestrator, reason = client_or_reason()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail=reason)
    try:
        method = orchestrator.clear_task if action == "clear" else orchestrator.retry_task
        result = method(execution_id, task_id)
    except ExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrchestratorConfigError as exc:
        raise HTTPException(status_code=502, detail=f"Airflow error: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface as a clean error
        raise HTTPException(status_code=502, detail=f"Airflow error: {exc}") from exc
    finally:
        orchestrator.close()
    return TaskActionResult(
        task_id=task_id,
        action=action,
        cleared_task_instances=len(result.get("task_instances", []) or []),
    )


@router.post(
    "/training-runs/{run_id}/tasks/{task_id}/clear",
    response_model=TaskActionResult,
    dependencies=[Depends(require_write_token)],
)
def clear_task(
    run_id: int, task_id: str, db: Session = Depends(get_db)
) -> TaskActionResult:
    """Reset one task instance so the scheduler runs it again, without
    resetting the dag run's own state — for a task in a run that has not
    reached a terminal state yet. See ``AirflowOrchestrator.clear_task``.
    """
    return _run_task_action(run_id, task_id, db, action="clear")


@router.post(
    "/training-runs/{run_id}/tasks/{task_id}/retry",
    response_model=TaskActionResult,
    dependencies=[Depends(require_write_token)],
)
def retry_task(
    run_id: int, task_id: str, db: Session = Depends(get_db)
) -> TaskActionResult:
    """Clear one task instance *and* reset the dag run's own state — for a
    dag run already in a terminal state (typically ``failed``), where
    clearing the task alone would not resume anything. See
    ``AirflowOrchestrator.retry_task``.
    """
    return _run_task_action(run_id, task_id, db, action="retry")
