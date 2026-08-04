"""Tests for /api/internal — the endpoints the Airflow DAG calls.

These are the framework's only *write* endpoints, so they are also the
regression guard for transaction handling: every test here asserts
against a **fresh session**, not just the HTTP response body. A handler
that flushes without committing returns a perfectly healthy 200 while
losing every row it wrote, which is exactly the bug this file exists to
catch.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.database.models.training_run import RunStatus, TrainingRun


def _seed(session_factory, *, with_production: bool = False) -> dict[str, int]:
    """Create a dataset version, a finished training run, and a model.

    Returns the ids the promote endpoint needs.
    """
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
        run = TrainingRun(
            dataset_version_id=dv.id,
            status=RunStatus.SUCCESS.value,
            pipeline_id="case_studies.churn.pipelines:train",
            metadata_json=json.dumps({"model_name": "churn-xgboost"}),
        )
        s.add(run)
        s.flush()
        model = ModelRow(name="churn-xgboost", task="classification")
        s.add(model)
        s.flush()

        ids = {
            "dataset_version_id": dv.id,
            "training_run_id": run.id,
            "model_id": model.id,
        }

        if with_production:
            incumbent = ModelVersion(
                model_id=model.id,
                version_number=1,
                state=ModelState.PRODUCTION.value,
                dataset_version_id=dv.id,
                training_run_id=run.id,
                metrics_json=json.dumps({"f1": 0.80}),
            )
            s.add(incumbent)
            s.flush()
            ids["incumbent_id"] = incumbent.id

        s.commit()
        return ids
    finally:
        s.close()


def _promote_body(ids: dict[str, int], **overrides) -> dict:
    body = {
        "dataset_version_id": ids["dataset_version_id"],
        "training_run_id": ids["training_run_id"],
        "mlflow_run_id": "mlflow-run-abc",
        "metrics": {"f1": 0.91},
        "artifact_uri": "s3://bucket/model.pkl",
        "min_f1": 0.5,
    }
    body.update(overrides)
    return body


class TestPromoteModel:
    def test_approved_promotion_is_persisted(self, client, session_factory):
        """The happy path must survive the request that created it.

        Regression guard: the endpoint only ``flush()``es through
        ModelManager, so if the request-scoped session never commits,
        the response still reports ``promoted: true`` while the database
        keeps nothing.
        """
        ids = _seed(session_factory)

        response = client.post(
            "/api/internal/models/churn-xgboost/promote",
            json=_promote_body(ids),
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["promoted"] is True
        assert payload["model_version"] == 1

        # The assertion that matters: a *new* session, i.e. the database
        # rather than the identity map of the request that wrote it.
        s = session_factory()
        try:
            versions = list(s.execute(select(ModelVersion)).scalars().all())
            assert len(versions) == 1, "promoted ModelVersion was not committed"
            promoted = versions[0]
            assert promoted.id == payload["model_version_id"]
            assert promoted.state == ModelState.PRODUCTION
            assert promoted.model_id == ids["model_id"]
            assert promoted.training_run_id == ids["training_run_id"]
            assert promoted.mlflow_run_id == "mlflow-run-abc"
            assert promoted.artifact_uri == "s3://bucket/model.pkl"
            assert json.loads(promoted.metrics_json) == {"f1": 0.91}
        finally:
            s.close()

    def test_rejected_promotion_is_persisted(self, client, session_factory):
        """A rejection is a decision too — it must be auditable later."""
        ids = _seed(session_factory)

        response = client.post(
            "/api/internal/models/churn-xgboost/promote",
            json=_promote_body(ids, metrics={"f1": 0.10}, min_f1=0.90),
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["promoted"] is False
        assert payload["reasons"], "a rejection must explain itself"

        s = session_factory()
        try:
            versions = list(s.execute(select(ModelVersion)).scalars().all())
            assert len(versions) == 1, "rejected ModelVersion was not committed"
            assert versions[0].state == ModelState.REJECTED
        finally:
            s.close()

    def test_previous_production_is_archived_and_persisted(
        self, client, session_factory
    ):
        """Promotion must leave exactly one PRODUCTION version behind."""
        ids = _seed(session_factory, with_production=True)

        response = client.post(
            "/api/internal/models/churn-xgboost/promote",
            json=_promote_body(ids),
        )
        assert response.status_code == 200
        assert response.json()["promoted"] is True

        s = session_factory()
        try:
            by_id = {
                v.id: v for v in s.execute(select(ModelVersion)).scalars().all()
            }
            assert len(by_id) == 2
            assert by_id[ids["incumbent_id"]].state == ModelState.ARCHIVED
            production = [
                v for v in by_id.values() if v.state == ModelState.PRODUCTION
            ]
            assert len(production) == 1
            assert production[0].version_number == 2
        finally:
            s.close()

    def test_unknown_model_is_404_and_writes_nothing(
        self, client, session_factory
    ):
        ids = _seed(session_factory)

        response = client.post(
            "/api/internal/models/does-not-exist/promote",
            json=_promote_body(ids),
        )

        assert response.status_code == 404
        s = session_factory()
        try:
            assert s.execute(select(ModelVersion)).scalars().first() is None
        finally:
            s.close()

    def test_failure_mid_request_rolls_back_the_candidate(
        self, client, session_factory, monkeypatch
    ):
        """A crash after the CANDIDATE insert must not leave it behind.

        The candidate row is written before the policy runs, so without a
        rollback an exploding policy would strand an orphan CANDIDATE in
        the registry.
        """
        from mlops_framework.api.routers import internal

        class _ExplodingPolicy:
            def evaluate(self, *args, **kwargs):
                raise RuntimeError("policy blew up")

        monkeypatch.setattr(internal, "ModelPromotionPolicy", _ExplodingPolicy)

        ids = _seed(session_factory)

        with pytest.raises(RuntimeError, match="policy blew up"):
            client.post(
                "/api/internal/models/churn-xgboost/promote",
                json=_promote_body(ids),
            )

        s = session_factory()
        try:
            assert s.execute(select(ModelVersion)).scalars().first() is None
        finally:
            s.close()


class TestTrainingRunContext:
    def test_returns_run_and_dataset_version(self, client, session_factory):
        ids = _seed(session_factory)

        response = client.get(
            f"/api/internal/training-runs/{ids['training_run_id']}/context"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["training_run_id"] == ids["training_run_id"]
        assert body["dataset_version_id"] == ids["dataset_version_id"]
        assert body["storage_uri"] == "s3://bucket/v1.parquet"
        assert body["row_count"] == 1000
        assert body["metadata"] == {"model_name": "churn-xgboost"}

    def test_unknown_run_is_404(self, client):
        assert client.get("/api/internal/training-runs/999/context").status_code == 404
