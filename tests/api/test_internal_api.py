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


class TestWriteEndpoints:
    """The only write path into the deployed database.

    Every assertion reads back through a fresh session for the same reason
    the promote tests do: these handlers only ``flush()``, so a broken
    transaction boundary would return a healthy 200 and persist nothing.
    """

    def test_dataset_creation_is_get_or_create(self, client, session_factory):
        first = client.post(
            "/api/internal/datasets",
            json={"name": "credit-card-fraud", "description": "kaggle"},
        )
        assert first.status_code == 200
        assert first.json()["created"] is True

        second = client.post(
            "/api/internal/datasets", json={"name": "credit-card-fraud"}
        )
        assert second.json()["created"] is False
        assert second.json()["id"] == first.json()["id"]

        s = session_factory()
        try:
            rows = list(s.execute(select(Dataset)).scalars().all())
            assert len(rows) == 1, "get-or-create must not insert a duplicate"
            assert rows[0].description == "kaggle"
        finally:
            s.close()

    def test_version_is_deduplicated_by_content_hash(self, client, session_factory):
        """Re-running a client against unchanged data must not mint a version.

        The framework's own checksum hashes storage_uri + metadata rather
        than the bytes, so without this the same file would produce a new
        "immutable version" on every run.
        """
        ds = client.post(
            "/api/internal/datasets", json={"name": "credit-card-fraud"}
        ).json()["id"]
        body = {
            "storage_uri": "s3://bucket/creditcard.csv",
            "row_count": 284807,
            "metadata": {"content_sha256": "a" * 64, "n_fraud": 492},
        }

        first = client.post(f"/api/internal/datasets/{ds}/versions", json=body)
        second = client.post(f"/api/internal/datasets/{ds}/versions", json=body)

        assert first.json()["created"] is True
        assert second.json()["created"] is False
        assert second.json()["id"] == first.json()["id"]

        s = session_factory()
        try:
            versions = list(s.execute(select(DatasetVersion)).scalars().all())
            assert len(versions) == 1
            assert versions[0].row_count == 284807
        finally:
            s.close()

    def test_different_content_makes_a_new_version(self, client, session_factory):
        ds = client.post(
            "/api/internal/datasets", json={"name": "credit-card-fraud"}
        ).json()["id"]
        for digest in ("a" * 64, "b" * 64):
            client.post(
                f"/api/internal/datasets/{ds}/versions",
                json={
                    "storage_uri": "s3://bucket/creditcard.csv",
                    "row_count": 284807,
                    "metadata": {"content_sha256": digest},
                },
            )
        s = session_factory()
        try:
            versions = sorted(
                s.execute(select(DatasetVersion)).scalars().all(),
                key=lambda v: v.version_number,
            )
            assert [v.version_number for v in versions] == [1, 2]
        finally:
            s.close()

    def test_readiness_decision_is_persisted(self, client, session_factory):
        from mlops_framework.database.models.readiness_evaluation import (
            ReadinessEvaluation,
        )

        ds = client.post(
            "/api/internal/datasets", json={"name": "credit-card-fraud"}
        ).json()["id"]
        version_id = client.post(
            f"/api/internal/datasets/{ds}/versions",
            json={
                "storage_uri": "s3://bucket/creditcard.csv",
                "row_count": 284807,
                "metadata": {"columns": [{"name": "class", "dtype": "int64"}]},
            },
        ).json()["id"]

        response = client.post(
            f"/api/internal/readiness/{version_id}",
            json={"policy": {"required_size": 100000}},
        )
        assert response.status_code == 200
        assert response.json()["is_ready"] is True
        assert response.json()["checks"]["size"] == "PASSED"

        s = session_factory()
        try:
            rows = list(s.execute(select(ReadinessEvaluation)).scalars().all())
            assert len(rows) == 1, "readiness decision must be auditable"
            assert rows[0].dataset_version_id == version_id
        finally:
            s.close()

    def test_readiness_blocks_a_dataset_that_is_too_small(self, client):
        ds = client.post(
            "/api/internal/datasets", json={"name": "credit-card-fraud"}
        ).json()["id"]
        version_id = client.post(
            f"/api/internal/datasets/{ds}/versions",
            json={"storage_uri": "s3://b/x.csv", "row_count": 10, "metadata": {}},
        ).json()["id"]

        body = client.post(
            f"/api/internal/readiness/{version_id}",
            json={"policy": {"required_size": 100000}},
        ).json()
        assert body["is_ready"] is False
        assert body["status"] == "BLOCKED"
        assert body["reasons"], "a block must explain itself"

    def test_run_finish_records_success_and_metrics(self, client, session_factory):
        ids = _seed(session_factory)
        run_id = client.post(
            "/api/internal/training-runs",
            json={
                "dataset_version_id": ids["dataset_version_id"],
                "pipeline_id": "mlops_training_pipeline",
            },
        ).json()["id"]

        response = client.post(
            f"/api/internal/training-runs/{run_id}/finish",
            json={
                "status": "SUCCESS",
                "result": {
                    "metrics": {"f1": 0.86, "average_precision": 0.81},
                    "params": {"n_estimators": 200},
                    "artifact_path": "s3://bucket/model.json",
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "SUCCESS"

        # The UI reads runs through the public schema, not the internal one.
        shown = client.get(f"/api/training-runs/{run_id}").json()
        assert shown["status"] == "SUCCESS"
        assert shown["metrics"]["f1"] == 0.86
        assert shown["parameters"]["n_estimators"] == 200

        s = session_factory()
        try:
            run = s.get(TrainingRun, run_id)
            assert run.status == RunStatus.SUCCESS
            assert run.completed_at is not None
        finally:
            s.close()

    def test_run_finish_records_failure_from_a_pending_run(
        self, client, session_factory
    ):
        """A DAG that died in its first task must still close the run out.

        The lifecycle has no PENDING -> FAILED edge; the endpoint moves the
        run through RUNNING rather than 409-ing and leaving it PENDING,
        which is the stuck state this endpoint exists to prevent.
        """
        ids = _seed(session_factory)
        run_id = client.post(
            "/api/internal/training-runs",
            json={
                "dataset_version_id": ids["dataset_version_id"],
                "pipeline_id": "mlops_training_pipeline",
            },
        ).json()["id"]

        response = client.post(
            f"/api/internal/training-runs/{run_id}/finish",
            json={"status": "FAILED", "error_message": "resolve_context exploded"},
        )
        assert response.status_code == 200

        s = session_factory()
        try:
            run = s.get(TrainingRun, run_id)
            assert run.status == RunStatus.FAILED
            assert "resolve_context exploded" in run.error_message
        finally:
            s.close()

    def test_run_finish_with_skip_lifecycle_transition_records_metrics_only(
        self, client, session_factory
    ):
        """What mlops_training_pipeline.py's report_status sends for a run
        owned by RetrainingWorkflow: the pipeline's result must still be
        recorded (RetrainingWorkflow's own metrics resolution reads it
        back), but the run's own status must be untouched — that run
        closes itself out via TrainingService.wait_for_completion()
        instead. Calling complete_run() here too would 409 the moment
        wait_for_completion() also tried it against an already-terminal
        row."""
        ids = _seed(session_factory)
        run_id = client.post(
            "/api/internal/training-runs",
            json={
                "dataset_version_id": ids["dataset_version_id"],
                "pipeline_id": "mlops_training_pipeline",
            },
        ).json()["id"]

        response = client.post(
            f"/api/internal/training-runs/{run_id}/finish",
            json={
                "status": "SUCCESS",
                "result": {"metrics": {"f1": 0.83}, "params": {"n_estimators": 200}},
                "skip_lifecycle_transition": True,
            },
        )
        assert response.status_code == 200
        # Still PENDING — this call never touched the lifecycle.
        assert response.json()["status"] == "PENDING"

        s = session_factory()
        try:
            run = s.get(TrainingRun, run_id)
            assert run.status == RunStatus.PENDING
            assert run.completed_at is None
            meta = json.loads(run.metadata_json)
            assert meta["orchestrator_result"]["metrics"] == {"f1": 0.83}
        finally:
            s.close()

    def test_run_finish_rejects_an_unknown_status(self, client, session_factory):
        ids = _seed(session_factory)
        run_id = client.post(
            "/api/internal/training-runs",
            json={
                "dataset_version_id": ids["dataset_version_id"],
                "pipeline_id": "p",
            },
        ).json()["id"]
        r = client.post(
            f"/api/internal/training-runs/{run_id}/finish", json={"status": "MAYBE"}
        )
        assert r.status_code == 422

    def test_training_run_on_unknown_version_is_404(self, client):
        r = client.post(
            "/api/internal/training-runs",
            json={"dataset_version_id": 999, "pipeline_id": "p"},
        )
        assert r.status_code == 404

    def test_start_without_airflow_configured_is_503(
        self, client, session_factory, monkeypatch
    ):
        """Fail loudly rather than silently leaving the run PENDING."""
        monkeypatch.delenv("AIRFLOW_BASE_URL", raising=False)
        ids = _seed(session_factory)
        run_id = client.post(
            "/api/internal/training-runs",
            json={
                "dataset_version_id": ids["dataset_version_id"],
                "pipeline_id": "p",
            },
        ).json()["id"]
        r = client.post(f"/api/internal/training-runs/{run_id}/start", json={})
        assert r.status_code == 503
