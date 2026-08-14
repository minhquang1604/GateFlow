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

from mlops_framework.database.models.governance_event import GovernanceEventSeverity
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.events.publisher import ScheduleFailedEvent
from mlops_framework.events.store import GovernanceEventStore
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
    # Set when the schedule was due and attempted, but firing raised.
    # Distinct from skipped_reason ("we chose not to run it") — this is
    # "we ran it and it blew up". ``fired`` stays False either way.
    error: str | None = None


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

    A schedule that raises is contained here rather than allowed out:
    it is recorded, its trigger time is advanced, and the loop moves on
    to the next schedule. Without that containment one broken schedule
    took the whole tick with it — every schedule after it in the list
    never ran, and because ``record_trigger`` was only reached on the
    success path the broken one stayed due and re-fired on every tick
    forever, minting a TrainingRun row each time. ``api/app.py``'s
    scheduler loop catches at the *tick* level, which kept the loop
    alive but could not give the other schedules their turn; that is
    what its docstring's "one broken schedule must not silently stop
    every other schedule" needs from this function to be true.
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
        try:
            results.append(_fire(session, schedule, mlflow_tracking_uri, now))
        except Exception as exc:  # noqa: BLE001 - one schedule must not end the pass
            results.append(_record_failure(session, schedule, exc))
    return results


def _record_failure(session: Session, schedule: Any, exc: Exception) -> ScheduleFireResult:
    """Absorb a schedule that blew up: surface it, then let it wait.

    Three things have to happen, in this order:

    1. The session is rolled back. Whatever ``_fire`` was part-way
       through is unusable, and the two writes below need a working
       transaction.
    2. A CRITICAL GovernanceEvent is written, so the failure shows up on
       Gateflow's Alerts tab instead of only in a container log.
    3. ``record_trigger`` advances ``last_triggered_at``. This is the
       half that stops the retry storm: the schedule waits for its next
       natural occurrence rather than being due again on the very next
       tick. A schedule failing every time therefore fails at its own
       cadence — once an hour for an hourly cron — not every
       ``scheduler_poll_seconds``.
    """
    session.rollback()
    _log.exception("schedule %s failed to fire", schedule.id)

    detail = f"{type(exc).__name__}: {exc}"
    GovernanceEventStore(session).record(
        ScheduleFailedEvent(schedule_id=schedule.id, error_message=detail),
        message=f"Schedule #{schedule.id} failed to fire — {detail}",
        severity=GovernanceEventSeverity.CRITICAL,
        entity_type="Schedule",
        entity_id=schedule.id,
    )
    try:
        ScheduleManager(session).record_trigger(
            schedule.id, triggered_at=datetime.now(UTC), training_run_id=None
        )
        session.commit()
    except Exception:  # noqa: BLE001 - nothing further to fall back on
        _log.exception(
            "schedule %s: could not record the failed trigger; it may re-fire "
            "on the next tick",
            schedule.id,
        )
        session.rollback()
    return ScheduleFireResult(schedule.id, fired=False, error=detail)


def run_schedule_now(
    session: Session,
    schedule_id: int,
    *,
    mlflow_tracking_uri: str | None = None,
) -> ScheduleFireResult:
    """Fire one schedule immediately, bypassing the cron check.

    What ``POST /schedules/{id}/run-now`` calls — "run it now" means
    now, not "now if it happens to be due".

    A failure here propagates, unlike in :func:`run_due_schedules`: this
    path has a caller waiting on the response, so the error belongs in
    it rather than in an alert row they would have to go looking for.
    Nor does a failed manual run advance ``last_triggered_at`` — that
    would silently consume the schedule's next automatic occurrence.
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

    # Anchored at the moment training *finished*, not the moment the tick
    # started. ``is_due`` measures the next occurrence from
    # ``last_triggered_at``, so recording the start time means a run that
    # outlasts its own cron interval is already overdue the instant it
    # returns — an every-minute schedule whose training takes 90s fires
    # again immediately, back to back, forever. Recording the end instead
    # gives the documented "wait for the next occurrence" semantics
    # whatever the run cost. ``now`` still decides *whether* to fire; it
    # just no longer decides when the next window opens.
    ScheduleManager(session).record_trigger(
        schedule.id,
        triggered_at=datetime.now(UTC),
        training_run_id=outcome.training_run_id,
    )
    session.commit()
    _log.info(
        "schedule %s fired: training_run_id=%s promoted=%s",
        schedule.id, outcome.training_run_id, outcome.promoted,
    )
    return ScheduleFireResult(schedule.id, fired=True, outcome=outcome)
