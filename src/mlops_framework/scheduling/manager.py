"""ScheduleManager: create/list/update/delete Schedule rows.

Mirrors ``ModelManager`` / ``DatasetManager``'s shape — CRUD over one
entity, all business logic (cron validity, model/dataset existence)
enforced here rather than left to the caller. Cron *firing* is a
separate concern; see ``scheduling/runner.py``.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.model import Model
from mlops_framework.database.models.schedule import Schedule
from mlops_framework.exceptions import (
    DatasetNotFoundError,
    ModelNotFoundError,
    ScheduleNotFoundError,
)
from mlops_framework.scheduling import cron


class ScheduleManager:
    """Manages Schedule entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_schedule(
        self,
        model_id: int,
        dataset_id: int,
        pipeline_id: str,
        cron_expression: str,
        *,
        enabled: bool = True,
        parameters: dict[str, Any] | None = None,
        min_f1: float = 0.0,
        notes: str | None = None,
    ) -> Schedule:
        cron.validate(cron_expression)
        if self._session.get(Model, model_id) is None:
            raise ModelNotFoundError(f"Model with id {model_id} not found")
        if self._session.get(Dataset, dataset_id) is None:
            raise DatasetNotFoundError(f"Dataset with id {dataset_id} not found")

        schedule = Schedule(
            model_id=model_id,
            dataset_id=dataset_id,
            pipeline_id=pipeline_id,
            cron_expression=cron_expression,
            enabled=enabled,
            parameters_json=json.dumps(parameters) if parameters else None,
            min_f1=min_f1,
            notes=notes,
        )
        self._session.add(schedule)
        self._session.flush()
        return schedule

    def get_schedule(self, schedule_id: int) -> Schedule:
        schedule = self._session.get(Schedule, schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError(f"Schedule with id {schedule_id} not found")
        return schedule

    def list_schedules(
        self, *, model_id: int | None = None, enabled_only: bool = False
    ) -> list[Schedule]:
        stmt = select(Schedule).order_by(Schedule.id)
        if model_id is not None:
            stmt = stmt.where(Schedule.model_id == model_id)
        if enabled_only:
            stmt = stmt.where(Schedule.enabled.is_(True))
        return list(self._session.execute(stmt).scalars().all())

    def update_schedule(
        self,
        schedule_id: int,
        *,
        cron_expression: str | None = None,
        enabled: bool | None = None,
        parameters: dict[str, Any] | None = None,
        min_f1: float | None = None,
        notes: str | None = None,
    ) -> Schedule:
        schedule = self.get_schedule(schedule_id)
        if cron_expression is not None:
            cron.validate(cron_expression)
            schedule.cron_expression = cron_expression
        if enabled is not None:
            schedule.enabled = enabled
        if parameters is not None:
            schedule.parameters_json = json.dumps(parameters)
        if min_f1 is not None:
            schedule.min_f1 = min_f1
        if notes is not None:
            schedule.notes = notes
        self._session.flush()
        return schedule

    def record_trigger(
        self, schedule_id: int, *, triggered_at, training_run_id: int | None
    ) -> Schedule:
        """Called by the runner after firing a schedule — advances
        ``last_triggered_at`` so the same window isn't fired twice."""
        schedule = self.get_schedule(schedule_id)
        schedule.last_triggered_at = triggered_at
        schedule.last_training_run_id = training_run_id
        self._session.flush()
        return schedule

    def delete_schedule(self, schedule_id: int) -> None:
        schedule = self.get_schedule(schedule_id)
        self._session.delete(schedule)
        self._session.flush()

    def get_parameters(self, schedule_id: int) -> dict[str, Any]:
        schedule = self.get_schedule(schedule_id)
        if not schedule.parameters_json:
            return {}
        return json.loads(schedule.parameters_json)
