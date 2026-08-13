"""Tests for GET /api/audit and the write endpoints that feed it.

Two layers: the endpoint itself (seeded rows, filters, shape) and the
integration guarantee that actually matters — that creating/updating/
deleting a schedule and promoting/rejecting a model each leave a row
behind, with the actor taken from ``X-Actor`` when sent and ``"system"``
otherwise. See ``audit/manager.py`` and ``api/deps.py::get_actor``.
"""

from __future__ import annotations

import json

import pytest

from mlops_framework.database.models.audit_log import AuditLog
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.training_run import RunStatus, TrainingRun

PIPELINE_ID = "tests._pipelines.e2e_training:main"


def _seed_schedule_deps(session_factory) -> dict[str, int]:
    s = session_factory()
    try:
        ds = Dataset(name="churn")
        s.add(ds)
        s.flush()
        s.add(DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
            checksum="a" * 64, schema_hash="b" * 64, row_count=1000,
        ))
        model = ModelRow(name="churn-xgboost-audit", task="classification")
        s.add(model)
        s.flush()
        s.commit()
        return {"dataset_id": ds.id, "model_id": model.id}
    finally:
        s.close()


def _seed_promote_deps(session_factory) -> dict[str, int]:
    s = session_factory()
    try:
        ds = Dataset(name="fraud")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.parquet",
            checksum="c" * 64, schema_hash="d" * 64, row_count=1000,
        )
        s.add(dv)
        s.flush()
        run = TrainingRun(
            dataset_version_id=dv.id, status=RunStatus.SUCCESS.value,
            pipeline_id="case_studies.fraud.pipelines:train",
        )
        s.add(run)
        s.flush()
        model = ModelRow(name="fraud-xgboost-audit", task="classification")
        s.add(model)
        s.flush()
        s.commit()
        return {"dataset_version_id": dv.id, "training_run_id": run.id, "model_id": model.id}
    finally:
        s.close()


def _promote_body(ids: dict[str, int], **overrides) -> dict:
    body = {
        "dataset_version_id": ids["dataset_version_id"],
        "training_run_id": ids["training_run_id"],
        "mlflow_run_id": "mlflow-run-audit",
        "metrics": {"f1": 0.91},
        "artifact_uri": "s3://bucket/model.pkl",
        "min_f1": 0.5,
    }
    body.update(overrides)
    return body


class TestListAuditEndpoint:
    def _seed_rows(self, session_factory) -> None:
        s = session_factory()
        try:
            s.add(AuditLog(actor="alice", action="SCHEDULE_CREATED", entity_type="Schedule", entity_id=1))
            s.add(AuditLog(actor="bob", action="MODEL_PROMOTED", entity_type="ModelVersion", entity_id=9,
                            metadata_json=json.dumps({"model_name": "x"})))
            s.commit()
        finally:
            s.close()

    def test_lists_newest_first_with_parsed_metadata(self, client, session_factory):
        self._seed_rows(session_factory)
        body = client.get("/api/audit").json()
        assert len(body) == 2
        assert body[0]["action"] == "MODEL_PROMOTED"
        assert body[0]["metadata"] == {"model_name": "x"}
        assert body[1]["action"] == "SCHEDULE_CREATED"
        assert body[1]["metadata"] is None

    def test_filters_by_entity_type(self, client, session_factory):
        self._seed_rows(session_factory)
        body = client.get("/api/audit", params={"entity_type": "Schedule"}).json()
        assert len(body) == 1
        assert body[0]["action"] == "SCHEDULE_CREATED"

    def test_filters_by_action(self, client, session_factory):
        self._seed_rows(session_factory)
        body = client.get("/api/audit", params={"action": "MODEL_PROMOTED"}).json()
        assert len(body) == 1

    def test_empty_when_nothing_recorded(self, client):
        assert client.get("/api/audit").json() == []


class TestScheduleActionsAreAudited:
    def test_create_defaults_actor_to_system(self, client, session_factory):
        ids = _seed_schedule_deps(session_factory)
        resp = client.post("/api/schedules", json={
            "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID, "cron_expression": "0 2 * * *",
        })
        schedule_id = resp.json()["id"]

        entries = client.get("/api/audit", params={"entity_type": "Schedule"}).json()
        assert len(entries) == 1
        assert entries[0]["action"] == "SCHEDULE_CREATED"
        assert entries[0]["actor"] == "system"
        assert entries[0]["entity_id"] == schedule_id
        assert entries[0]["metadata"]["cron_expression"] == "0 2 * * *"

    def test_create_honours_x_actor_header(self, client, session_factory):
        ids = _seed_schedule_deps(session_factory)
        client.post(
            "/api/schedules",
            json={
                "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
                "pipeline_id": PIPELINE_ID, "cron_expression": "0 2 * * *",
            },
            headers={"X-Actor": "alice@example.com"},
        )
        entries = client.get("/api/audit").json()
        assert entries[0]["actor"] == "alice@example.com"

    def test_update_and_delete_are_audited(self, client, session_factory):
        ids = _seed_schedule_deps(session_factory)
        created = client.post("/api/schedules", json={
            "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID, "cron_expression": "0 2 * * *",
        }).json()
        schedule_id = created["id"]

        client.patch(f"/api/schedules/{schedule_id}", json={"enabled": False},
                      headers={"X-Actor": "carol"})
        client.delete(f"/api/schedules/{schedule_id}", headers={"X-Actor": "carol"})

        entries = client.get("/api/audit", params={"entity_id": schedule_id}).json()
        actions = [e["action"] for e in entries]
        # Newest first: DELETE, then UPDATE, then CREATE.
        assert actions == ["SCHEDULE_DELETED", "SCHEDULE_UPDATED", "SCHEDULE_CREATED"]
        assert entries[0]["actor"] == "carol"
        assert entries[1]["actor"] == "carol"

    def test_run_now_is_audited(self, client, session_factory, monkeypatch, tmp_path):
        pytest.importorskip("mlflow", reason="mlflow SDK is not installed")
        from mlops_framework.config.settings import get_settings

        # A real file, not sqlite:///:memory: — each connection to an
        # in-memory sqlite DB is its own isolated database, so mlflow's
        # own store migrations from one connection are invisible to the
        # next; see test_schedules_api.py's identical mlflow_uri fixture.
        monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path}/mlflow.db")
        get_settings.cache_clear()
        try:
            ids = _seed_schedule_deps(session_factory)
            created = client.post("/api/schedules", json={
                "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
                "pipeline_id": PIPELINE_ID,
                "cron_expression": "0 3 1 1 *",  # never due on its own
                "min_f1": 0.5,
            }).json()

            resp = client.post(f"/api/schedules/{created['id']}/run-now")
            assert resp.status_code == 200

            entries = client.get(
                "/api/audit", params={"action": "SCHEDULE_RUN_NOW"}
            ).json()
            assert len(entries) == 1
            assert entries[0]["actor"] == "system"
            assert entries[0]["metadata"]["fired"] is True
        finally:
            get_settings.cache_clear()

    def test_run_now_rejection_is_audited_by_retraining_workflow(
        self, client, session_factory, monkeypatch, tmp_path
    ):
        """Covers workflow/retraining.py's own audit call — distinct from
        internal.py's (exercised above via /internal/models/.../promote)
        and from run_now's own SCHEDULE_RUN_NOW row. Actor is
        "schedule:{id}", set by scheduling/runner.py, not "system" — the
        one path where RetrainingWorkflow runs with no HTTP request to
        read an X-Actor header from."""
        pytest.importorskip("mlflow", reason="mlflow SDK is not installed")
        from mlops_framework.config.settings import get_settings

        monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path}/mlflow.db")
        get_settings.cache_clear()
        try:
            ids = _seed_schedule_deps(session_factory)
            created = client.post("/api/schedules", json={
                "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
                "pipeline_id": PIPELINE_ID,
                "cron_expression": "0 3 1 1 *",
                "min_f1": 0.99,  # e2e_training's f1 tops out ~0.849 — always rejected
            }).json()

            resp = client.post(f"/api/schedules/{created['id']}/run-now")
            assert resp.status_code == 200
            assert resp.json()["promoted"] is False

            entries = client.get(
                "/api/audit", params={"action": "MODEL_REJECTED"}
            ).json()
            assert len(entries) == 1
            assert entries[0]["actor"] == f"schedule:{created['id']}"
            assert entries[0]["metadata"]["reasons"]
        finally:
            get_settings.cache_clear()


class TestPromotionIsAudited:
    def test_approved_promotion_is_audited(self, client, session_factory):
        ids = _seed_promote_deps(session_factory)
        client.post(
            "/api/internal/models/fraud-xgboost-audit/promote",
            json=_promote_body(ids),
            headers={"X-Actor": "airflow-dag"},
        )
        entries = client.get(
            "/api/audit", params={"entity_type": "ModelVersion"}
        ).json()
        assert len(entries) == 1
        assert entries[0]["action"] == "MODEL_PROMOTED"
        assert entries[0]["actor"] == "airflow-dag"
        assert entries[0]["metadata"]["model_name"] == "fraud-xgboost-audit"

    def test_rejected_promotion_is_audited(self, client, session_factory):
        ids = _seed_promote_deps(session_factory)
        client.post(
            "/api/internal/models/fraud-xgboost-audit/promote",
            json=_promote_body(ids, metrics={"f1": 0.10}, min_f1=0.90),
        )
        entries = client.get(
            "/api/audit", params={"entity_type": "ModelVersion"}
        ).json()
        assert len(entries) == 1
        assert entries[0]["action"] == "MODEL_REJECTED"
        assert entries[0]["actor"] == "system"
        assert entries[0]["metadata"]["reasons"]
