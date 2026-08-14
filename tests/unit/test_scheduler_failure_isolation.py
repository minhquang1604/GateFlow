"""Regression tests: one broken Schedule must not break the tick.

``run_due_schedules`` used to call ``_fire`` with no per-schedule
try/except, and only reached ``record_trigger`` on the success path.
Together that produced two failures from one broken schedule:

* every schedule *after* it in the list never ran that tick — the
  exception unwound the whole pass;
* the broken one kept ``last_triggered_at = None``, so it stayed due and
  re-fired on every tick (60s by default) forever, creating a
  TrainingRun row each time.

``api/app.py``'s scheduler loop catches at the tick level, which kept
the loop alive but could not give the other schedules their turn — its
docstring's promise that "one broken schedule must not silently stop
every other schedule from ever firing again" needed this half to be
true.

Unit-level on purpose: ``RetrainingWorkflow.run`` is patched to raise,
because what is under test is the containment, not the governance chain
(``tests/integration/test_scheduling_runner.py`` covers that for real).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models.governance_event import (
    GovernanceEvent,
    GovernanceEventSeverity,
)
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.scheduling.manager import ScheduleManager
from mlops_framework.scheduling.runner import run_due_schedules

PIPELINE_ID = "tests._pipelines.e2e_training:main"


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def two_due_schedules(session):
    """Two enabled, immediately-due schedules — 'broken' fires first."""
    dm = DatasetManager(session)
    mm = ModelManager(session)
    ds = dm.create_dataset(name="churn")
    dm.create_version(dataset_id=ds.id, storage_uri="s3://b/v1.csv", row_count=1000)
    broken = mm.create_model(name="broken")
    healthy = mm.create_model(name="healthy")
    session.commit()

    sm = ScheduleManager(session)
    schedules = [
        sm.create_schedule(
            model_id=m.id, dataset_id=ds.id,
            pipeline_id=PIPELINE_ID, cron_expression="* * * * *",
        )
        for m in (broken, healthy)
    ]
    # Anchor in the past so an every-minute cron is due without sleeping.
    for s in schedules:
        s.created_at = datetime.now(UTC) - timedelta(minutes=2)
    session.commit()
    return schedules


@pytest.fixture()
def no_tracker():
    """MLflowTracker reaches out to a tracking server at construction —
    irrelevant here and slow, so it is stubbed out."""
    with patch("mlops_framework.scheduling.runner.MLflowTracker", lambda **kw: None):
        yield


def _workflow_raises_for(model_name: str, attempted: list[str]):
    """Patch RetrainingWorkflow.run: record the attempt, blow up for one
    model, return a benign outcome for the rest."""

    def _run(self, **kwargs):
        name = kwargs["model"].name
        attempted.append(name)
        if name == model_name:
            raise RuntimeError("simulated MLflow outage")
        from mlops_framework.workflow.retraining import RetrainingOutcome

        return RetrainingOutcome(
            dataset_version_id=kwargs["dataset_version"].id,
            model_id=kwargs["model"].id,
            training_run_id=None,
            model_version_id=None,
            promotion_event_id=None,
            steps=[],
            promoted=False,
        )

    return patch("mlops_framework.workflow.retraining.RetrainingWorkflow.run", _run)


class TestOneFailureDoesNotEndTheTick:
    def test_the_pass_does_not_raise(self, session, two_due_schedules, no_tracker):
        attempted: list[str] = []
        with _workflow_raises_for("broken", attempted):
            results = run_due_schedules(session)
        assert len(results) == 2

    def test_the_next_schedule_still_fires(self, session, two_due_schedules, no_tracker):
        attempted: list[str] = []
        with _workflow_raises_for("broken", attempted):
            results = run_due_schedules(session)

        assert attempted == ["broken", "healthy"], "the healthy schedule never ran"
        by_id = {r.schedule_id: r for r in results}
        broken, healthy = two_due_schedules
        assert by_id[broken.id].fired is False
        assert by_id[healthy.id].fired is True

    def test_the_failure_is_reported_not_swallowed(
        self, session, two_due_schedules, no_tracker
    ):
        attempted: list[str] = []
        with _workflow_raises_for("broken", attempted):
            results = run_due_schedules(session)

        broken = two_due_schedules[0]
        result = next(r for r in results if r.schedule_id == broken.id)
        assert result.error is not None
        assert "simulated MLflow outage" in result.error
        # …and "we ran it and it blew up" stays distinct from "we chose
        # not to run it".
        assert result.skipped_reason is None

    def test_a_critical_alert_is_recorded(self, session, two_due_schedules, no_tracker):
        attempted: list[str] = []
        with _workflow_raises_for("broken", attempted):
            run_due_schedules(session)

        broken = two_due_schedules[0]
        events = (
            session.query(GovernanceEvent)
            .filter_by(event_type="SCHEDULE_FAILED", entity_id=broken.id)
            .all()
        )
        assert len(events) == 1
        assert events[0].severity == GovernanceEventSeverity.CRITICAL
        assert events[0].entity_type == "Schedule"
        assert "simulated MLflow outage" in events[0].message


class TestNoRetryStorm:
    def test_a_failed_schedule_records_its_trigger(
        self, session, two_due_schedules, no_tracker
    ):
        attempted: list[str] = []
        with _workflow_raises_for("broken", attempted):
            run_due_schedules(session)

        broken = ScheduleManager(session).get_schedule(two_due_schedules[0].id)
        assert broken.last_triggered_at is not None, "would re-fire on every tick"
        assert broken.last_training_run_id is None

    def test_it_does_not_fire_again_on_the_next_tick(
        self, session, two_due_schedules, no_tracker
    ):
        attempted: list[str] = []
        with _workflow_raises_for("broken", attempted):
            run_due_schedules(session)
            attempted.clear()
            # The very next tick, one second later — an every-minute cron
            # is not due again yet.
            results = run_due_schedules(session, now=datetime.now(UTC) + timedelta(seconds=1))

        assert attempted == [], "the broken schedule re-fired immediately"
        broken = two_due_schedules[0]
        result = next(r for r in results if r.schedule_id == broken.id)
        assert result.fired is False
        assert result.skipped_reason == "not due"


class TestSuccessfulFireAnchorsAtCompletion:
    """``last_triggered_at`` records when the run *finished*, so a run
    that outlasts its own cron interval does not come back overdue the
    instant it returns."""

    def test_a_slow_run_is_not_immediately_due_again(self, session, two_due_schedules, no_tracker):
        from mlops_framework.workflow.retraining import RetrainingOutcome

        tick_start = datetime.now(UTC)

        def _slow_run(self, **kwargs):
            # Stand in for training that outlasts an every-minute cron.
            import time

            time.sleep(0.05)
            return RetrainingOutcome(
                dataset_version_id=kwargs["dataset_version"].id,
                model_id=kwargs["model"].id,
                training_run_id=None, model_version_id=None,
                promotion_event_id=None, steps=[], promoted=False,
            )

        with patch("mlops_framework.workflow.retraining.RetrainingWorkflow.run", _slow_run):
            run_due_schedules(session, now=tick_start)

        fired = ScheduleManager(session).get_schedule(two_due_schedules[0].id)
        assert fired.last_triggered_at is not None
        recorded = fired.last_triggered_at
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=UTC)
        assert recorded > tick_start, (
            "trigger was anchored at the tick start, so a run longer than "
            "the cron interval is overdue the moment it finishes"
        )
