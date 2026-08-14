"""The scheduler's background loop itself — not just run_due_schedules()
in isolation. Proves ``settings.scheduler_enabled`` actually starts a
loop on app startup that fires a due Schedule for real, and that it
stays off by default (every other test in this suite builds an app
without ever wanting a background trainer running).
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

pytest.importorskip("mlflow", reason="mlflow SDK is not installed")

from datetime import UTC

from mlops_framework.api.app import create_app
from mlops_framework.api.deps import get_db_manager_dep
from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.schedule import Schedule
from mlops_framework.database.session import DatabaseManager

PIPELINE_ID = "tests._pipelines.e2e_training:main"


@pytest.fixture()
def db_setup(tmp_path, monkeypatch):
    """A real sqlite engine (not :memory: — the background loop opens
    its own connection from its own DatabaseManager(), which needs a
    file it can actually reach, not a per-connection in-memory db)."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/app.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/app.db")
    mlflow_uri = f"sqlite:///{tmp_path}/mlflow.db"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow_uri)

    yield engine, session_factory


def _seed_due_schedule(session_factory) -> int:
    from datetime import datetime, timedelta

    s = session_factory()
    try:
        ds = Dataset(name="churn")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
            checksum="a" * 64, schema_hash="b" * 64, row_count=1000,
        )
        s.add(dv)
        s.flush()
        model = ModelRow(name="churn-xgboost-loop", task="classification")
        s.add(model)
        s.flush()
        schedule = Schedule(
            model_id=model.id,
            dataset_id=ds.id,
            pipeline_id=PIPELINE_ID,
            cron_expression="* * * * *",
            enabled=True,
            min_f1=0.5,
            created_at=datetime.now(UTC) - timedelta(minutes=2),
        )
        s.add(schedule)
        s.flush()
        s.commit()
        return schedule.id
    finally:
        s.close()


class TestSchedulerDisabledByDefault:
    def test_no_background_task_without_the_flag(self, db_setup, monkeypatch):
        monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
        get_settings.cache_clear()
        engine, session_factory = db_setup

        mgr = DatabaseManager()
        mgr._engine = engine
        mgr._session_factory = session_factory
        app = create_app(mount_ui=False)
        app.dependency_overrides[get_db_manager_dep] = lambda: mgr

        with TestClient(app) as client:
            assert client.get("/api/models").status_code == 200
            assert getattr(app.state, "scheduler_task", None) is None
        get_settings.cache_clear()


class TestSchedulerEnabled:
    def test_background_loop_fires_a_due_schedule(self, db_setup, monkeypatch):
        engine, session_factory = db_setup
        schedule_id = _seed_due_schedule(session_factory)

        monkeypatch.setenv("SCHEDULER_ENABLED", "true")
        monkeypatch.setenv("SCHEDULER_POLL_SECONDS", "1")
        get_settings.cache_clear()

        mgr = DatabaseManager()
        mgr._engine = engine
        mgr._session_factory = session_factory
        app = create_app(mount_ui=False)
        app.dependency_overrides[get_db_manager_dep] = lambda: mgr

        try:
            with TestClient(app) as client:
                assert client.get("/api/models").status_code == 200
                assert app.state.scheduler_task is not None

                deadline = time.time() + 15
                fired = False
                while time.time() < deadline:
                    s = session_factory()
                    try:
                        row = s.get(Schedule, schedule_id)
                        if row.last_triggered_at is not None:
                            fired = True
                            break
                    finally:
                        s.close()
                    time.sleep(0.5)

                assert fired, "background loop did not fire the due schedule in time"
        finally:
            get_settings.cache_clear()
