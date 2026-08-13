"""Schedules router — CRUD over cron-triggered automatic retraining.

Mutations go through :class:`ScheduleManager`; ``POST
/schedules/{id}/run-now`` goes through
``scheduling.runner.run_schedule_now`` — the same function the
background loop (``api/app.py``'s ``_start_scheduler``) calls when a
schedule is actually due, so a manual trigger fires exactly the same
way a cron-triggered one does, just without waiting on the clock.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_actor, get_audit_manager, get_db, get_schedule_manager
from mlops_framework.api.schemas import ScheduleOut
from mlops_framework.audit.manager import AuditManager
from mlops_framework.config.settings import get_settings
from mlops_framework.exceptions import (
    DatasetNotFoundError,
    InvalidCronExpressionError,
    ModelNotFoundError,
    ScheduleNotFoundError,
)
from mlops_framework.scheduling.manager import ScheduleManager
from mlops_framework.scheduling.runner import run_schedule_now

router = APIRouter()


class CreateScheduleRequest(BaseModel):
    model_id: int
    dataset_id: int
    pipeline_id: str
    cron_expression: str
    enabled: bool = True
    parameters: dict[str, Any] | None = None
    min_f1: float = 0.0
    notes: str | None = None


class UpdateScheduleRequest(BaseModel):
    cron_expression: str | None = None
    enabled: bool | None = None
    parameters: dict[str, Any] | None = None
    min_f1: float | None = None
    notes: str | None = None


class RunNowResponse(BaseModel):
    schedule_id: int
    fired: bool
    skipped_reason: str | None = None
    training_run_id: int | None = None
    promoted: bool | None = None
    blocked_reason: str | None = None


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
def create_schedule(
    request: CreateScheduleRequest,
    sm: ScheduleManager = Depends(get_schedule_manager),
    am: AuditManager = Depends(get_audit_manager),
    actor: str = Depends(get_actor),
) -> ScheduleOut:
    try:
        schedule = sm.create_schedule(
            model_id=request.model_id,
            dataset_id=request.dataset_id,
            pipeline_id=request.pipeline_id,
            cron_expression=request.cron_expression,
            enabled=request.enabled,
            parameters=request.parameters,
            min_f1=request.min_f1,
            notes=request.notes,
        )
    except InvalidCronExpressionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ModelNotFoundError, DatasetNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    am.record(
        actor=actor,
        action="SCHEDULE_CREATED",
        entity_type="Schedule",
        entity_id=schedule.id,
        metadata={
            "model_id": schedule.model_id,
            "dataset_id": schedule.dataset_id,
            "cron_expression": schedule.cron_expression,
        },
    )
    return ScheduleOut.from_schedule(schedule)


@router.get("/schedules", response_model=list[ScheduleOut])
def list_schedules(
    model_id: int | None = None,
    enabled_only: bool = False,
    sm: ScheduleManager = Depends(get_schedule_manager),
) -> list[ScheduleOut]:
    schedules = sm.list_schedules(model_id=model_id, enabled_only=enabled_only)
    return [ScheduleOut.from_schedule(s) for s in schedules]


@router.get("/schedules/{schedule_id}", response_model=ScheduleOut)
def get_schedule(
    schedule_id: int, sm: ScheduleManager = Depends(get_schedule_manager)
) -> ScheduleOut:
    try:
        schedule = sm.get_schedule(schedule_id)
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ScheduleOut.from_schedule(schedule)


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut)
def update_schedule(
    schedule_id: int,
    request: UpdateScheduleRequest,
    sm: ScheduleManager = Depends(get_schedule_manager),
    am: AuditManager = Depends(get_audit_manager),
    actor: str = Depends(get_actor),
) -> ScheduleOut:
    try:
        schedule = sm.update_schedule(
            schedule_id,
            cron_expression=request.cron_expression,
            enabled=request.enabled,
            parameters=request.parameters,
            min_f1=request.min_f1,
            notes=request.notes,
        )
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidCronExpressionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    am.record(
        actor=actor,
        action="SCHEDULE_UPDATED",
        entity_type="Schedule",
        entity_id=schedule.id,
        metadata=request.model_dump(exclude_none=True),
    )
    return ScheduleOut.from_schedule(schedule)


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(
    schedule_id: int,
    sm: ScheduleManager = Depends(get_schedule_manager),
    am: AuditManager = Depends(get_audit_manager),
    actor: str = Depends(get_actor),
) -> None:
    try:
        sm.delete_schedule(schedule_id)
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    am.record(
        actor=actor,
        action="SCHEDULE_DELETED",
        entity_type="Schedule",
        entity_id=schedule_id,
    )


@router.post("/schedules/{schedule_id}/run-now", response_model=RunNowResponse)
def trigger_schedule_now(
    schedule_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(get_actor),
) -> RunNowResponse:
    """Fire a schedule immediately, bypassing the cron check.

    Runs synchronously — the request blocks until training (a real
    local subprocess, see ``runner.py``'s module docstring on why
    ``LocalDockerOrchestrator``) finishes, same as
    ``POST /internal/models/{name}/promote`` already does for a
    manually-triggered run. A slow pipeline makes this endpoint slow;
    that is the honest answer, not a reason to fake an async response
    this API doesn't otherwise have infrastructure to poll.
    """
    try:
        result = run_schedule_now(
            db, schedule_id, mlflow_tracking_uri=get_settings().mlflow_tracking_uri
        )
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    outcome = result.outcome
    AuditManager(db).record(
        actor=actor,
        action="SCHEDULE_RUN_NOW",
        entity_type="Schedule",
        entity_id=schedule_id,
        metadata={
            "fired": result.fired,
            "skipped_reason": result.skipped_reason,
            "training_run_id": outcome.training_run_id if outcome else None,
            "promoted": outcome.promoted if outcome else None,
        },
    )
    return RunNowResponse(
        schedule_id=result.schedule_id,
        fired=result.fired,
        skipped_reason=result.skipped_reason,
        training_run_id=outcome.training_run_id if outcome else None,
        promoted=outcome.promoted if outcome else None,
        blocked_reason=outcome.blocked_reason if outcome else None,
    )
