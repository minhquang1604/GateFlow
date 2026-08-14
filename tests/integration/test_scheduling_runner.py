"""``run_due_schedules`` end to end — real cron math, real governance
chain (RetrainingWorkflow), real local subprocess training
(LocalDockerOrchestrator + tests._pipelines.e2e_training), real MLflow
(sqlite-backed local store, same technique as
tests/integration/test_mlflow_registry_sync.py). No mocks: this proves
a Schedule actually trains and promotes a model, not just that the
right functions were called.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("mlflow", reason="mlflow SDK is not installed")

from mlops_framework.config.settings import get_settings
from mlops_framework.database.models.training_run import RunStatus, TriggerType
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.scheduling.manager import ScheduleManager
from mlops_framework.scheduling.runner import run_due_schedules, run_schedule_now
from mlops_framework.training.manager import TrainingManager

PIPELINE_ID = "tests._pipelines.e2e_training:main"


@pytest.fixture()
def mlflow_uri(tmp_path, monkeypatch):
    uri = f"sqlite:///{tmp_path}/mlflow.db"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    get_settings.cache_clear()
    yield uri
    get_settings.cache_clear()


def _setup(db_session):
    dm = DatasetManager(db_session)
    mm = ModelManager(db_session)
    ds = dm.create_dataset(name="churn")
    dm.create_version(dataset_id=ds.id, storage_uri="s3://b/v1.csv", row_count=1000)
    model = mm.create_model(name="churn-xgboost-sched", task="classification")
    db_session.commit()
    return ds, model


def _backdate(db_session, schedule, minutes: int = 2):
    """Push ``created_at`` into the past so an "every minute" schedule
    (``* * * * *``) is immediately due in a test, without a real sleep.

    cron.is_due() anchors a never-fired schedule at its creation time
    (see that function's docstring on why) — a schedule created and
    checked in the same instant for a per-minute cron is correctly
    "not due yet" otherwise, since croniter's next slot after "right
    now" is up to 60 real seconds away.
    """
    schedule.created_at = datetime.now(UTC) - timedelta(minutes=minutes)
    db_session.flush()
    db_session.commit()


class TestRunDueSchedules:
    def test_due_schedule_trains_and_promotes(self, db_session, mlflow_uri):
        ds, model = _setup(db_session)
        sm = ScheduleManager(db_session)
        schedule = sm.create_schedule(
            model_id=model.id,
            dataset_id=ds.id,
            pipeline_id=PIPELINE_ID,
            cron_expression="* * * * *",  # every minute — always due
            min_f1=0.5,
        )
        db_session.commit()
        _backdate(db_session, schedule)

        results = run_due_schedules(db_session, mlflow_tracking_uri=mlflow_uri)
        assert len(results) == 1
        assert results[0].fired is True
        outcome = results[0].outcome
        assert outcome.promoted is True

        tm = TrainingManager(db_session, DatasetManager(db_session))
        run = tm.get_run(outcome.training_run_id)
        assert run.status == RunStatus.SUCCESS.value
        assert run.trigger_type == TriggerType.SCHEDULED.value
        assert run.mlflow_run_id is not None

        updated = sm.get_schedule(schedule.id)
        assert updated.last_training_run_id == outcome.training_run_id
        assert updated.last_triggered_at is not None

        # And it really landed on MLflow's own registry, not just the
        # framework's table — same proof as test_internal_promote_mlflow_sync.
        from mlflow.tracking import MlflowClient

        client = MlflowClient(tracking_uri=mlflow_uri)
        found = client.search_model_versions(f"run_id='{run.mlflow_run_id}'")
        assert len(found) == 1
        assert found[0].current_stage == "Production"

    def test_not_due_yet_is_skipped(self, db_session, mlflow_uri):
        ds, model = _setup(db_session)
        sm = ScheduleManager(db_session)
        sm.create_schedule(
            model_id=model.id,
            dataset_id=ds.id,
            pipeline_id=PIPELINE_ID,
            cron_expression="0 3 * * *",  # daily at 3am
        )
        db_session.commit()

        # "Now" is a moment that can't be due for a freshly created
        # 3am-daily schedule (see cron.is_due's anchoring rule).
        soon = datetime.now(UTC) + timedelta(seconds=5)
        results = run_due_schedules(db_session, mlflow_tracking_uri=mlflow_uri, now=soon)
        assert len(results) == 1
        assert results[0].fired is False
        assert results[0].skipped_reason == "not due"

    def test_disabled_schedule_is_not_reported(self, db_session, mlflow_uri):
        ds, model = _setup(db_session)
        sm = ScheduleManager(db_session)
        sm.create_schedule(
            model_id=model.id, dataset_id=ds.id, pipeline_id=PIPELINE_ID,
            cron_expression="* * * * *", enabled=False,
        )
        db_session.commit()
        assert run_due_schedules(db_session, mlflow_tracking_uri=mlflow_uri) == []

    def test_does_not_double_fire_immediately(self, db_session, mlflow_uri):
        ds, model = _setup(db_session)
        sm = ScheduleManager(db_session)
        schedule = sm.create_schedule(
            model_id=model.id, dataset_id=ds.id, pipeline_id=PIPELINE_ID,
            cron_expression="* * * * *", min_f1=0.5,
        )
        db_session.commit()
        _backdate(db_session, schedule)

        first = run_due_schedules(db_session, mlflow_tracking_uri=mlflow_uri)
        assert first[0].fired is True

        # Same minute, called again immediately — must not fire twice.
        second = run_due_schedules(
            db_session, mlflow_tracking_uri=mlflow_uri, now=datetime.now(UTC)
        )
        assert second[0].fired is False
        assert second[0].skipped_reason == "not due"

    def test_dataset_with_no_versions_is_skipped_not_raised(self, db_session, mlflow_uri):
        dm = DatasetManager(db_session)
        mm = ModelManager(db_session)
        ds = dm.create_dataset(name="empty-dataset")
        model = mm.create_model(name="empty-model")
        db_session.commit()

        sm = ScheduleManager(db_session)
        schedule = sm.create_schedule(
            model_id=model.id, dataset_id=ds.id, pipeline_id=PIPELINE_ID,
            cron_expression="* * * * *",
        )
        db_session.commit()
        _backdate(db_session, schedule)

        results = run_due_schedules(db_session, mlflow_tracking_uri=mlflow_uri)
        assert results[0].fired is False
        assert results[0].skipped_reason == "dataset has no versions"


class TestRunScheduleNow:
    def test_ignores_the_cron_and_fires_immediately(self, db_session, mlflow_uri):
        ds, model = _setup(db_session)
        sm = ScheduleManager(db_session)
        schedule = sm.create_schedule(
            model_id=model.id, dataset_id=ds.id, pipeline_id=PIPELINE_ID,
            cron_expression="0 3 1 1 *",  # once a year — never "due" in a test run
            min_f1=0.5,
        )
        db_session.commit()

        result = run_schedule_now(db_session, schedule.id, mlflow_tracking_uri=mlflow_uri)
        assert result.fired is True
        assert result.outcome.promoted is True
