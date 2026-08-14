"""Tests for ScheduleManager and the cron helpers it uses.

Real croniter, real SQLite-backed ORM (via the ``db_session`` fixture)
— no mocks. See ``scheduling/cron.py`` and ``scheduling/manager.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.exceptions import (
    DatasetNotFoundError,
    InvalidCronExpressionError,
    ModelNotFoundError,
    ScheduleNotFoundError,
)
from mlops_framework.model.manager import ModelManager
from mlops_framework.scheduling import cron
from mlops_framework.scheduling.manager import ScheduleManager


def _setup(db_session):
    dm = DatasetManager(db_session)
    mm = ModelManager(db_session)
    ds = dm.create_dataset(name="churn", description="d")
    model = mm.create_model(name="churn-xgboost", task="classification")
    db_session.commit()
    return ds, model


class TestCronValidation:
    def test_valid_expression_passes(self):
        cron.validate("0 2 * * *")  # daily at 2am — must not raise

    def test_every_minute_passes(self):
        cron.validate("* * * * *")

    @pytest.mark.parametrize(
        "bad",
        ["not a cron", "0 2 * *", "60 2 * * *", "0 25 * * *", ""],
    )
    def test_invalid_expression_raises(self, bad):
        with pytest.raises(InvalidCronExpressionError):
            cron.validate(bad)


class TestNextFireTime:
    def test_daily_2am_from_before(self):
        after = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        nxt = cron.next_fire_time("0 2 * * *", after)
        assert nxt == datetime(2026, 1, 1, 2, 0, tzinfo=UTC)

    def test_daily_2am_from_after_todays_slot(self):
        # Past today's 2am — next fire is tomorrow.
        after = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)
        nxt = cron.next_fire_time("0 2 * * *", after)
        assert nxt == datetime(2026, 1, 2, 2, 0, tzinfo=UTC)

    def test_naive_datetime_treated_as_utc(self):
        after = datetime(2026, 1, 1, 0, 0)  # no tzinfo
        nxt = cron.next_fire_time("0 2 * * *", after)
        assert nxt.tzinfo is not None


class TestIsDue:
    def test_never_fired_waits_for_next_occurrence_after_creation(self):
        # Created at 2:55am for a 2am-daily cron: must NOT be due
        # immediately (see cron.is_due's docstring on why).
        created_at = datetime(2026, 1, 1, 2, 55, tzinfo=UTC)
        now = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
        assert not cron.is_due(
            "0 2 * * *", last_triggered_at=None, created_at=created_at, now=now
        )

    def test_never_fired_is_due_once_its_first_slot_passes(self):
        created_at = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 1, tzinfo=UTC)
        assert cron.is_due(
            "0 2 * * *", last_triggered_at=None, created_at=created_at, now=now
        )

    def test_not_due_again_right_after_firing(self):
        created_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        last_triggered_at = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        now = last_triggered_at + timedelta(minutes=5)
        assert not cron.is_due(
            "0 2 * * *",
            last_triggered_at=last_triggered_at,
            created_at=created_at,
            now=now,
        )

    def test_due_again_after_the_next_full_cycle(self):
        created_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        last_triggered_at = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        now = datetime(2026, 1, 2, 2, 0, tzinfo=UTC)
        assert cron.is_due(
            "0 2 * * *",
            last_triggered_at=last_triggered_at,
            created_at=created_at,
            now=now,
        )


class TestScheduleManagerCRUD:
    def test_create_and_get(self, db_session):
        ds, model = _setup(db_session)
        mgr = ScheduleManager(db_session)
        schedule = mgr.create_schedule(
            model_id=model.id,
            dataset_id=ds.id,
            pipeline_id="case_studies.churn.pipelines:train",
            cron_expression="0 2 * * *",
            parameters={"max_depth": 6},
            min_f1=0.7,
        )
        assert schedule.id is not None
        assert schedule.enabled is True
        assert mgr.get_parameters(schedule.id) == {"max_depth": 6}

        fetched = mgr.get_schedule(schedule.id)
        assert fetched.cron_expression == "0 2 * * *"

    def test_create_rejects_bad_cron(self, db_session):
        ds, model = _setup(db_session)
        mgr = ScheduleManager(db_session)
        with pytest.raises(InvalidCronExpressionError):
            mgr.create_schedule(
                model_id=model.id,
                dataset_id=ds.id,
                pipeline_id="x:y",
                cron_expression="not a cron",
            )

    def test_create_rejects_unknown_model(self, db_session):
        ds, _ = _setup(db_session)
        mgr = ScheduleManager(db_session)
        with pytest.raises(ModelNotFoundError):
            mgr.create_schedule(
                model_id=9999, dataset_id=ds.id, pipeline_id="x:y", cron_expression="0 2 * * *"
            )

    def test_create_rejects_unknown_dataset(self, db_session):
        _, model = _setup(db_session)
        mgr = ScheduleManager(db_session)
        with pytest.raises(DatasetNotFoundError):
            mgr.create_schedule(
                model_id=model.id, dataset_id=9999, pipeline_id="x:y", cron_expression="0 2 * * *"
            )

    def test_get_missing_raises(self, db_session):
        mgr = ScheduleManager(db_session)
        with pytest.raises(ScheduleNotFoundError):
            mgr.get_schedule(9999)

    def test_list_filters_by_model_and_enabled(self, db_session):
        ds, model = _setup(db_session)
        mm = ModelManager(db_session)
        other_model = mm.create_model(name="other")
        db_session.commit()

        mgr = ScheduleManager(db_session)
        s1 = mgr.create_schedule(model.id, ds.id, "x:y", "0 2 * * *")
        mgr.create_schedule(other_model.id, ds.id, "x:y", "0 3 * * *")
        mgr.update_schedule(s1.id, enabled=False)

        assert {s.id for s in mgr.list_schedules(model_id=model.id)} == {s1.id}
        assert len(mgr.list_schedules()) == 2
        assert len(mgr.list_schedules(enabled_only=True)) == 1

    def test_update_toggles_and_edits(self, db_session):
        ds, model = _setup(db_session)
        mgr = ScheduleManager(db_session)
        schedule = mgr.create_schedule(model.id, ds.id, "x:y", "0 2 * * *")

        mgr.update_schedule(schedule.id, enabled=False)
        assert mgr.get_schedule(schedule.id).enabled is False

        mgr.update_schedule(schedule.id, cron_expression="0 3 * * *", min_f1=0.9)
        updated = mgr.get_schedule(schedule.id)
        assert updated.cron_expression == "0 3 * * *"
        assert updated.min_f1 == 0.9

    def test_update_rejects_bad_cron(self, db_session):
        ds, model = _setup(db_session)
        mgr = ScheduleManager(db_session)
        schedule = mgr.create_schedule(model.id, ds.id, "x:y", "0 2 * * *")
        with pytest.raises(InvalidCronExpressionError):
            mgr.update_schedule(schedule.id, cron_expression="garbage")
        # Unchanged after the rejected update.
        assert mgr.get_schedule(schedule.id).cron_expression == "0 2 * * *"

    def test_record_trigger(self, db_session):
        ds, model = _setup(db_session)
        mgr = ScheduleManager(db_session)
        schedule = mgr.create_schedule(model.id, ds.id, "x:y", "0 2 * * *")
        now = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)

        mgr.record_trigger(schedule.id, triggered_at=now, training_run_id=42)
        updated = mgr.get_schedule(schedule.id)
        assert updated.last_triggered_at == now
        assert updated.last_training_run_id == 42

    def test_delete(self, db_session):
        ds, model = _setup(db_session)
        mgr = ScheduleManager(db_session)
        schedule = mgr.create_schedule(model.id, ds.id, "x:y", "0 2 * * *")
        mgr.delete_schedule(schedule.id)
        with pytest.raises(ScheduleNotFoundError):
            mgr.get_schedule(schedule.id)
