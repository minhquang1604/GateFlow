"""``run_due_schedules()`` — what actually happens when a Schedule fires.

Called from two places, both of which must fire schedules exactly the
same way:

* the background loop in ``api/app.py`` (gated behind
  ``settings.scheduler_enabled`` — off by default, see that module),
  ticking every ``settings.scheduler_poll_seconds``;
* ``POST /schedules/{id}/run-now`` (``api/routers/schedules.py``), for
  a manual/deterministic trigger — the same code path a test can call
  directly without waiting on a real clock.

A fired schedule goes through the exact same governance chain a
drift-triggered retrain does — :class:`RetrainingWorkflow` — with
``force=True``: the cron expression *is* the eligibility decision here,
the same way an operator passing ``force=True`` is elsewhere (see
``EligibilityConfig``'s ``force`` field). It does not evaluate drift
(no ``reference_data``/``current_data``), so ``TrainingRun.trigger_type``
comes out ``SCHEDULED`` rather than ``DRIFT`` — see
``RetrainingWorkflow.run()``.

Uses :class:`LocalDockerOrchestrator`, not :class:`AirflowOrchestrator`.
``RetrainingWorkflow.run()`` can drive either now — pass
``training_entrypoint="module:callable"`` alongside an Airflow
``pipeline_id`` (the dag_id) to use the real orchestrator instead; see
its docstring and "AirflowOrchestrator vs LocalDockerOrchestrator" in
the README. Local was kept here because a scheduled retrain has no
operator watching it fire the way the demo scripts' human-driven runs
do, and the local orchestrator's subprocess result is available
synchronously — switching this loop to Airflow is a separate,
deliberate change, not a gap being worked around.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.framework_settings.manager import FrameworkSettingsManager
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.scheduling import cron
from mlops_framework.scheduling.manager import ScheduleManager
from mlops_framework.tracking.mlflow import MLflowTracker
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService
from mlops_framework.workflow.retraining import RetrainingOutcome, RetrainingWorkflow

_log = logging.getLogger("mlops_framework.scheduling.runner")


@dataclass
class ScheduleFireResult:
    schedule_id: int
    fired: bool
    skipped_reason: str | None = None
    outcome: RetrainingOutcome | None = None


def run_due_schedules(
    session: Session,
    *,
    mlflow_tracking_uri: str | None = None,
    now: datetime | None = None,
) -> list[ScheduleFireResult]:
    """Fire every enabled schedule that is due, in one pass.

    One result per *enabled* schedule (fired or not, with why);
    disabled schedules aren't reported at all — "disabled" isn't an
    interesting outcome to surface on every tick.
    """
    now = now or datetime.now(UTC)
    sm = ScheduleManager(session)
    results: list[ScheduleFireResult] = []

    for schedule in sm.list_schedules(enabled_only=True):
        if not cron.is_due(
            schedule.cron_expression,
            last_triggered_at=schedule.last_triggered_at,
            created_at=schedule.created_at,
            now=now,
        ):
            results.append(
                ScheduleFireResult(schedule.id, fired=False, skipped_reason="not due")
            )
            continue
        results.append(_fire(session, schedule, mlflow_tracking_uri, now))
    return results


def run_schedule_now(
    session: Session,
    schedule_id: int,
    *,
    mlflow_tracking_uri: str | None = None,
) -> ScheduleFireResult:
    """Fire one schedule immediately, bypassing the cron check.

    What ``POST /schedules/{id}/run-now`` calls — "run it now" means
    now, not "now if it happens to be due".
    """
    schedule = ScheduleManager(session).get_schedule(schedule_id)
    return _fire(session, schedule, mlflow_tracking_uri, datetime.now(UTC))


def _fire(session: Session, schedule: Any, mlflow_tracking_uri: str | None, now: datetime) -> ScheduleFireResult:
    dm = DatasetManager(session)
    version = dm.get_latest_version(schedule.dataset_id)
    if version is None:
        _log.warning(
            "schedule %s: dataset %s has no versions yet, skipping",
            schedule.id, schedule.dataset_id,
        )
        return ScheduleFireResult(
            schedule.id, fired=False, skipped_reason="dataset has no versions"
        )

    model = session.get(ModelRow, schedule.model_id)
    if model is None:  # pragma: no cover - FK guarantees this; defensive only
        return ScheduleFireResult(
            schedule.id, fired=False, skipped_reason="model no longer exists"
        )

    tm = TrainingManager(session, dm)
    tracker = MLflowTracker(tracking_uri=mlflow_tracking_uri, experiment_name="mlops-scheduled")
    orchestrator = LocalDockerOrchestrator()
    service = TrainingService(training_manager=tm, orchestrator=orchestrator, tracker=tracker)

    workflow = RetrainingWorkflow(
        session, training_service=service, actor=f"schedule:{schedule.id}"
    )
    # Persisted Settings (see FrameworkSettingsManager) is the base;
    # schedule.min_f1 and the two flags below are this call site's own
    # per-schedule overrides layered on top — identical to today's
    # bare-default behaviour for anyone who has never customized
    # Settings (an empty framework_settings table makes
    # get_eligibility_config()/get_promotion_config() return exactly
    # EligibilityConfig()/PromotionConfig()).
    settings_mgr = FrameworkSettingsManager(session)
    eligibility_config = settings_mgr.get_eligibility_config()
    promotion_config = dataclasses.replace(
        settings_mgr.get_promotion_config(),
        min_metrics={"f1": schedule.min_f1},
        must_beat_production=False,
        allow_cold_start=True,
    )
    try:
        outcome = workflow.run(
            dataset_version=version,
            model=model,
            eligibility_config=eligibility_config,
            promotion_config=promotion_config,
            pipeline_id=schedule.pipeline_id,
            force=True,  # the cron IS the eligibility decision
        )
    finally:
        orchestrator.shutdown()

    ScheduleManager(session).record_trigger(
        schedule.id, triggered_at=now, training_run_id=outcome.training_run_id
    )
    session.commit()
    _log.info(
        "schedule %s fired: training_run_id=%s promoted=%s",
        schedule.id, outcome.training_run_id, outcome.promoted,
    )
    return ScheduleFireResult(schedule.id, fired=True, outcome=outcome)
