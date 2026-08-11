"""Live proof that ``POST /internal/models/{name}/promote`` actually
pushes into MLflow's own Model Registry, not just the framework's own
table — the wiring added to ``api/routers/internal.py::promote_model``
(see ``mlops_framework.tracking.mlflow_registry``).

Real local (sqlite-backed) MLflow instance, same technique as
``tests/integration/test_mlflow_registry_sync.py`` — no Docker, no
stand-in for what MLflow itself decides about stages/aliases.
``tests/api/test_internal_api.py`` already covers the framework-table
side of promotion (with a fake ``mlflow_run_id`` and no MLflow
configured, so sync silently no-ops there); this file is the other
half.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("mlflow", reason="mlflow SDK is not installed")

from mlops_framework.api.app import create_app
from mlops_framework.api.deps import get_db_manager_dep
from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.training_run import RunStatus, TrainingRun
from mlops_framework.database.session import DatabaseManager

MODEL_NAME = "churn-xgboost-live-sync"


@pytest.fixture()
def mlflow_client(tmp_path, monkeypatch):
    """Point Settings at a fresh local MLflow store and hand back a raw
    client on the same store for assertions."""
    uri = f"sqlite:///{tmp_path}/mlflow.db"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    get_settings.cache_clear()

    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=uri)
    yield client
    get_settings.cache_clear()


@pytest.fixture()
def api(mlflow_client):
    """TestClient wired to a fresh in-memory DB, with Settings already
    pointed at the live MLflow instance (mlflow_client fixture runs
    first) so promote_model's registry-sync calls have somewhere real
    to land."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    mgr = DatabaseManager()
    mgr._engine = engine  # type: ignore[attr-defined]
    mgr._session_factory = session_factory  # type: ignore[attr-defined]

    app = create_app(mount_ui=False)
    app.dependency_overrides[get_db_manager_dep] = lambda: mgr
    try:
        yield TestClient(app), session_factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_framework_rows(session_factory) -> dict[str, int]:
    s = session_factory()
    try:
        ds = Dataset(name="churn")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id,
            version_number=1,
            storage_uri="s3://bucket/v1.parquet",
            checksum="a" * 64,
            schema_hash="b" * 64,
            row_count=1000,
        )
        s.add(dv)
        s.flush()
        tr = TrainingRun(
            dataset_version_id=dv.id,
            status=RunStatus.SUCCESS.value,
            pipeline_id="case_studies.churn.pipelines:train",
            metadata_json="{}",
        )
        s.add(tr)
        s.flush()
        model = ModelRow(name=MODEL_NAME, task="classification")
        s.add(model)
        s.flush()
        s.commit()
        return {
            "dataset_version_id": dv.id,
            "training_run_id": tr.id,
            "model_id": model.id,
        }
    finally:
        s.close()


def _log_real_mlflow_run(mlflow_client, filename: str = "model.json") -> str:
    exp = mlflow_client.get_experiment_by_name("live-sync-test")
    exp_id = exp.experiment_id if exp else mlflow_client.create_experiment("live-sync-test")
    run = mlflow_client.create_run(exp_id)
    local = Path(tempfile.mkdtemp()) / filename
    local.write_text("{}")
    mlflow_client.log_artifact(run.info.run_id, str(local))
    return run.info.run_id


class TestPromoteSyncsToMlflow:
    def test_approved_promotion_registers_and_promotes_on_mlflow(self, api, mlflow_client):
        test_client, session_factory = api
        ids = _seed_framework_rows(session_factory)
        mlflow_run_id = _log_real_mlflow_run(mlflow_client)

        resp = test_client.post(
            f"/api/internal/models/{MODEL_NAME}/promote",
            json={
                "dataset_version_id": ids["dataset_version_id"],
                "training_run_id": ids["training_run_id"],
                "mlflow_run_id": mlflow_run_id,
                "metrics": {"f1": 0.93},
                "artifact_uri": "/tmp/whatever-local-tmp-dir/model.json",
                "min_f1": 0.5,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["promoted"] is True

        found = mlflow_client.search_model_versions(f"run_id='{mlflow_run_id}'")
        assert len(found) == 1, "promote_model did not register a version on MLflow"
        mv = found[0]
        assert mv.current_stage == "Production"
        registered = mlflow_client.get_registered_model(MODEL_NAME)
        assert str(registered.aliases.get("champion")) == str(mv.version)

    def test_rejected_candidate_is_registered_but_not_promoted(self, api, mlflow_client):
        """CANDIDATE registration happens up front, independent of the
        promotion decision — see internal.py's comment on why."""
        test_client, session_factory = api
        ids = _seed_framework_rows(session_factory)
        mlflow_run_id = _log_real_mlflow_run(mlflow_client)

        resp = test_client.post(
            f"/api/internal/models/{MODEL_NAME}/promote",
            json={
                "dataset_version_id": ids["dataset_version_id"],
                "training_run_id": ids["training_run_id"],
                "mlflow_run_id": mlflow_run_id,
                "metrics": {"f1": 0.10},
                "artifact_uri": "model.json",
                "min_f1": 0.90,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["promoted"] is False

        found = mlflow_client.search_model_versions(f"run_id='{mlflow_run_id}'")
        assert len(found) == 1, "a rejected CANDIDATE should still be registered on MLflow"
        assert found[0].current_stage in ("None", None, "")

    def test_second_promotion_archives_the_first_on_mlflow(self, api, mlflow_client):
        test_client, session_factory = api

        ids1 = _seed_framework_rows(session_factory)
        run1 = _log_real_mlflow_run(mlflow_client)
        resp1 = test_client.post(
            f"/api/internal/models/{MODEL_NAME}/promote",
            json={
                "dataset_version_id": ids1["dataset_version_id"],
                "training_run_id": ids1["training_run_id"],
                "mlflow_run_id": run1,
                "metrics": {"f1": 0.80},
                "artifact_uri": "model.json",
                "min_f1": 0.5,
            },
        )
        assert resp1.json()["promoted"] is True

        # A second dataset version + training run against the *same*
        # model, mirroring what a real retrain produces.
        s = session_factory()
        try:
            dv2 = DatasetVersion(
                dataset_id=1,
                version_number=2,
                storage_uri="s3://bucket/v2.parquet",
                checksum="c" * 64,
                schema_hash="d" * 64,
                row_count=1200,
            )
            s.add(dv2)
            s.flush()
            tr2 = TrainingRun(
                dataset_version_id=dv2.id,
                status=RunStatus.SUCCESS.value,
                pipeline_id="case_studies.churn.pipelines:train",
                metadata_json="{}",
            )
            s.add(tr2)
            s.flush()
            s.commit()
            ids2 = {"dataset_version_id": dv2.id, "training_run_id": tr2.id}
        finally:
            s.close()

        run2 = _log_real_mlflow_run(mlflow_client)
        resp2 = test_client.post(
            f"/api/internal/models/{MODEL_NAME}/promote",
            json={
                "dataset_version_id": ids2["dataset_version_id"],
                "training_run_id": ids2["training_run_id"],
                "mlflow_run_id": run2,
                "metrics": {"f1": 0.90},
                "artifact_uri": "model.json",
                "min_f1": 0.5,
            },
        )
        assert resp2.json()["promoted"] is True

        mv1 = mlflow_client.search_model_versions(f"run_id='{run1}'")[0]
        mv2 = mlflow_client.search_model_versions(f"run_id='{run2}'")[0]
        assert mv1.current_stage == "Archived"
        assert mv2.current_stage == "Production"
        registered = mlflow_client.get_registered_model(MODEL_NAME)
        assert str(registered.aliases.get("champion")) == str(mv2.version)
