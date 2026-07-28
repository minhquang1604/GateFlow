"""Unit tests for the FastAPI serving bridge.

Uses FastAPI's TestClient to drive the endpoints in-process.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.drift_evaluation import DriftEvaluation
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_promotion_event import (
    ModelPromotionEvent,
)
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.database.models.readiness_evaluation import (
    ReadinessEvaluation,
)
from mlops_framework.database.models.serving_instance import ServingInstance
from mlops_framework.database.models.training_run import TrainingRun
from mlops_framework.serving.bridge import (
    ServingBridge,
    ServingModelRegistry,
)


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def _factory():
        s = Session()
        return s

    yield _factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _setup_model(session_factory):
    s = session_factory()
    try:
        ds = Dataset(name="fraud")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id,
            version_number=1,
            storage_uri="s3://b/v1.csv",
            checksum="0" * 64,
            schema_hash="0" * 64,
            row_count=1000,
            is_immutable=True,
        )
        s.add(dv)
        s.flush()
        m = ModelRow(name="fraud-model", task="fraud_detection")
        s.add(m)
        s.flush()
        mv = ModelVersion(
            model_id=m.id,
            dataset_version_id=dv.id,
            version_number=1,
            state=ModelState.PRODUCTION,
            metrics_json=json.dumps({"f1": 0.9}),
            artifact_uri="s3://models/fraud-v1.pkl",
        )
        s.add(mv)
        s.commit()
        return {"model_id": m.id, "model_version_id": mv.id}
    finally:
        s.close()


class TestServingModelRegistry:
    def test_set_and_get_active(self):
        reg = ServingModelRegistry()
        assert reg.get_active("missing") is None
        rec = reg.set_active(
            model_name="fraud-model",
            model_id=1,
            model_version_id=10,
            model_version_number=1,
            artifact_uri="s3://...",
            payload={"obj": "stub"},
        )
        assert rec["model_name"] == "fraud-model"
        assert rec["loaded_at"]
        out = reg.get_active("fraud-model")
        assert out is not None
        assert out["model_version_number"] == 1

    def test_atomic_swap(self):
        """The registry swap is atomic under concurrent access."""
        import threading

        reg = ServingModelRegistry()
        reg.set_active(
            model_name="m",
            model_id=1,
            model_version_id=1,
            model_version_number=1,
        )
        errors: list[Exception] = []

        def swap(i: int) -> None:
            try:
                for _ in range(50):
                    reg.set_active(
                        model_name="m",
                        model_id=1,
                        model_version_id=i,
                        model_version_number=i,
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=swap, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # The final read should always be a valid record.
        out = reg.get_active("m")
        assert out is not None and out["model_version_number"] > 0

    def test_list_active(self):
        reg = ServingModelRegistry()
        reg.set_active(
            model_name="a",
            model_id=1,
            model_version_id=1,
            model_version_number=1,
        )
        reg.set_active(
            model_name="b",
            model_id=2,
            model_version_id=2,
            model_version_number=1,
        )
        listed = reg.list_active()
        assert {row["model_name"] for row in listed} == {"a", "b"}


class TestServingBridge:
    def test_health(self):
        bridge = ServingBridge()
        client = TestClient(bridge.app)
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_reload_without_session_requires_ids(self):
        bridge = ServingBridge()
        client = TestClient(bridge.app)
        r = client.post(
            "/internal/model/reload",
            json={"model_name": "fraud-model", "model_version": 1},
        )
        assert r.status_code == 400

    def test_reload_with_session_persists(self, session_factory):
        ids = _setup_model(session_factory)
        bridge = ServingBridge(session_factory=session_factory)
        client = TestClient(bridge.app)
        r = client.post(
            "/internal/model/reload",
            json={
                "model_name": "fraud-model",
                "model_version": 1,
                "artifact_uri": "s3://models/fraud-v1.pkl",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["model_name"] == "fraud-model"
        assert body["model_version"] == 1

        # Active version is reported
        r2 = client.get("/internal/model/active/fraud-model")
        assert r2.status_code == 200
        assert r2.json()["model_version_number"] == 1

        # Reload is persisted in the database
        s = session_factory()
        try:
            si = (
                s.query(ServingInstance)
                .filter_by(model_version_id=ids["model_version_id"])
                .first()
            )
            assert si is not None
            assert si.is_active is True
            assert si.reload_source == "event"
        finally:
            s.close()

    def test_reload_marks_prior_inactive(self, session_factory):
        _setup_model(session_factory)
        bridge = ServingBridge(session_factory=session_factory)
        client = TestClient(bridge.app)
        r1 = client.post(
            "/internal/model/reload",
            json={"model_name": "fraud-model", "model_version": 1},
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/internal/model/reload",
            json={"model_name": "fraud-model", "model_version": 1},
        )
        assert r2.status_code == 200

        s = session_factory()
        try:
            active = (
                s.query(ServingInstance)
                .filter_by(serving_instance_id=bridge._serving_instance_id, is_active=True)
                .all()
            )
            assert len(active) == 1
        finally:
            s.close()

    def test_unknown_model_404(self, session_factory):
        bridge = ServingBridge(session_factory=session_factory)
        client = TestClient(bridge.app)
        r = client.post(
            "/internal/model/reload",
            json={"model_name": "missing", "model_version": 1},
        )
        assert r.status_code == 404

    def test_404_when_no_active(self):
        bridge = ServingBridge()
        client = TestClient(bridge.app)
        r = client.get("/internal/model/active/unknown")
        assert r.status_code == 404

    def test_loader_invoked(self, session_factory):
        _setup_model(session_factory)
        loader_calls: list[str] = []
        bridge = ServingBridge(
            session_factory=session_factory,
            loader=lambda uri: (loader_calls.append(uri), {"loaded": True})[1],
        )
        client = TestClient(bridge.app)
        r = client.post(
            "/internal/model/reload",
            json={
                "model_name": "fraud-model",
                "model_version": 1,
                "artifact_uri": "s3://models/fraud-v1.pkl",
            },
        )
        assert r.status_code == 200
        assert loader_calls == ["s3://models/fraud-v1.pkl"]
        # The active record carries the loaded payload
        rec = bridge._registry.get_active("fraud-model")
        assert rec["payload"] == {"loaded": True}
