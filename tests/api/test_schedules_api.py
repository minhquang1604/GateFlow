"""Live tests for the /schedules HTTP API — real DB, real cron
validation, and (for /run-now) the real training + MLflow-promotion
chain, same technique as test_internal_promote_mlflow_sync.py.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mlflow", reason="mlflow SDK is not installed")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# tests/api has no __init__.py, so pytest puts this directory on sys.path
# and the shared fixtures module is importable by its bare name.
from conftest import authenticated_client  # noqa: E402
from mlops_framework.api.app import create_app
from mlops_framework.api.deps import get_db_manager_dep
from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.session import DatabaseManager

PIPELINE_ID = "tests._pipelines.e2e_training:main"


@pytest.fixture()
def mlflow_uri(tmp_path, monkeypatch):
    uri = f"sqlite:///{tmp_path}/mlflow.db"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    get_settings.cache_clear()
    yield uri
    get_settings.cache_clear()


@pytest.fixture()
def api(mlflow_uri):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    mgr = DatabaseManager()
    mgr._engine = engine
    mgr._session_factory = session_factory

    app = create_app(mount_ui=False)
    app.dependency_overrides[get_db_manager_dep] = lambda: mgr
    yield authenticated_client(app), session_factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed(session_factory) -> dict[str, int]:
    s = session_factory()
    try:
        ds = Dataset(name="churn")
        s.add(ds)
        s.flush()
        from mlops_framework.database.models.dataset_version import DatasetVersion

        s.add(DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
            checksum="a" * 64, schema_hash="b" * 64, row_count=1000,
        ))
        model = ModelRow(name="churn-xgboost-api", task="classification")
        s.add(model)
        s.flush()
        s.commit()
        return {"dataset_id": ds.id, "model_id": model.id}
    finally:
        s.close()


class TestCreateSchedule:
    def test_creates_and_returns_next_fire_at(self, api):
        client, sf = api
        ids = _seed(sf)
        resp = client.post("/api/schedules", json={
            "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID, "cron_expression": "0 2 * * *",
            "min_f1": 0.6,
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["enabled"] is True
        assert body["model_name"] == "churn-xgboost-api"
        assert body["dataset_name"] == "churn"
        assert body["next_fire_at"] is not None

    def test_rejects_bad_cron(self, api):
        client, sf = api
        ids = _seed(sf)
        resp = client.post("/api/schedules", json={
            "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID, "cron_expression": "not a cron",
        })
        assert resp.status_code == 400

    def test_rejects_unknown_model(self, api):
        client, sf = api
        ids = _seed(sf)
        resp = client.post("/api/schedules", json={
            "model_id": 9999, "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID, "cron_expression": "0 2 * * *",
        })
        assert resp.status_code == 404


class TestListGetUpdateDelete:
    def test_full_lifecycle(self, api):
        client, sf = api
        ids = _seed(sf)
        created = client.post("/api/schedules", json={
            "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID, "cron_expression": "0 2 * * *",
        }).json()
        schedule_id = created["id"]

        assert len(client.get("/api/schedules").json()) == 1
        assert client.get(f"/api/schedules/{schedule_id}").json()["id"] == schedule_id

        patched = client.patch(f"/api/schedules/{schedule_id}", json={"enabled": False})
        assert patched.status_code == 200
        assert patched.json()["enabled"] is False

        assert client.delete(f"/api/schedules/{schedule_id}").status_code == 204
        assert client.get(f"/api/schedules/{schedule_id}").status_code == 404

    def test_get_missing_is_404(self, api):
        client, _ = api
        assert client.get("/api/schedules/9999").status_code == 404

    def test_update_missing_is_404(self, api):
        client, _ = api
        assert client.patch("/api/schedules/9999", json={"enabled": False}).status_code == 404

    def test_update_rejects_bad_cron(self, api):
        client, sf = api
        ids = _seed(sf)
        created = client.post("/api/schedules", json={
            "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID, "cron_expression": "0 2 * * *",
        }).json()
        resp = client.patch(f"/api/schedules/{created['id']}", json={"cron_expression": "junk"})
        assert resp.status_code == 400

    def test_delete_missing_is_404(self, api):
        client, _ = api
        assert client.delete("/api/schedules/9999").status_code == 404


class TestRunNow:
    def test_fires_immediately_and_promotes(self, api, mlflow_uri):
        client, sf = api
        ids = _seed(sf)
        created = client.post("/api/schedules", json={
            "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID,
            "cron_expression": "0 3 1 1 *",  # once a year — never "due"
            "min_f1": 0.5,
        }).json()

        resp = client.post(f"/api/schedules/{created['id']}/run-now")
        assert resp.status_code == 200
        body = resp.json()
        assert body["fired"] is True
        assert body["promoted"] is True
        assert body["training_run_id"] is not None

        # last_triggered_at/last_training_run_id now reflect the fire.
        refreshed = client.get(f"/api/schedules/{created['id']}").json()
        assert refreshed["last_training_run_id"] == body["training_run_id"]
        assert refreshed["last_triggered_at"] is not None

    def test_missing_schedule_is_404(self, api):
        client, _ = api
        assert client.post("/api/schedules/9999/run-now").status_code == 404
