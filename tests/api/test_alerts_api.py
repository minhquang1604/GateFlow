"""Tests for GET /api/alerts and the write paths that feed it.

Mirrors test_audit_api.py's shape: the endpoint itself (seeded rows,
filters), then the integration guarantee — that a readiness rejection
and a reported training failure each leave a GovernanceEvent behind.
The retraining.py-only paths (eligibility rejection, drift detection)
are exercised indirectly by RetrainingWorkflow's own tests; this file
covers the two api/routers/internal.py call sites directly, the same
split test_audit_api.py uses for AuditLog.
"""

from __future__ import annotations

import json

from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.governance_event import GovernanceEvent
from mlops_framework.database.models.training_run import RunStatus, TrainingRun


def _seed_dataset_version(session_factory, *, row_count: int = 10) -> dict[str, int]:
    s = session_factory()
    try:
        ds = Dataset(name="alerts-evidence")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
            checksum="a" * 64, schema_hash="b" * 64, row_count=row_count,
        )
        s.add(dv)
        s.commit()
        return {"dataset_id": ds.id, "dataset_version_id": dv.id}
    finally:
        s.close()


def _seed_training_run(session_factory) -> int:
    s = session_factory()
    try:
        ds = Dataset(name="alerts-training")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
            checksum="c" * 64, schema_hash="d" * 64, row_count=10,
        )
        s.add(dv)
        s.flush()
        run = TrainingRun(
            dataset_version_id=dv.id, status=RunStatus.RUNNING.value,
            pipeline_id="p",
        )
        s.add(run)
        s.commit()
        return run.id
    finally:
        s.close()


class TestListAlertsEndpoint:
    def _seed_rows(self, session_factory) -> None:
        s = session_factory()
        try:
            s.add(GovernanceEvent(
                event_type="TRAINING_FAILED", severity="CRITICAL",
                entity_type="TrainingRun", entity_id=1, message="run 1 failed",
                payload_json=json.dumps({"training_run_id": 1}),
            ))
            s.add(GovernanceEvent(
                event_type="RUN_BLOCKED", severity="WARNING",
                entity_type="Model", entity_id=2, message="model 2 blocked",
            ))
            s.commit()
        finally:
            s.close()

    def test_lists_newest_first_with_parsed_payload(self, client, session_factory):
        self._seed_rows(session_factory)
        body = client.get("/api/alerts").json()
        assert len(body) == 2
        assert body[0]["event_type"] == "RUN_BLOCKED"
        assert body[0]["payload"] is None
        assert body[1]["event_type"] == "TRAINING_FAILED"
        assert body[1]["payload"] == {"training_run_id": 1}
        assert body[1]["severity"] == "CRITICAL"
        assert body[1]["message"] == "run 1 failed"

    def test_filters_by_event_type(self, client, session_factory):
        self._seed_rows(session_factory)
        body = client.get("/api/alerts", params={"event_type": "TRAINING_FAILED"}).json()
        assert len(body) == 1

    def test_filters_by_severity(self, client, session_factory):
        self._seed_rows(session_factory)
        body = client.get("/api/alerts", params={"severity": "WARNING"}).json()
        assert len(body) == 1
        assert body[0]["event_type"] == "RUN_BLOCKED"

    def test_filters_by_entity(self, client, session_factory):
        self._seed_rows(session_factory)
        body = client.get(
            "/api/alerts", params={"entity_type": "TrainingRun", "entity_id": 1}
        ).json()
        assert len(body) == 1

    def test_empty_when_nothing_recorded(self, client):
        assert client.get("/api/alerts").json() == []


class TestReadinessBlockedIsAlerted:
    def test_blocked_readiness_creates_an_alert(self, client, session_factory):
        ids = _seed_dataset_version(session_factory, row_count=10)
        resp = client.post(
            f"/api/internal/readiness/{ids['dataset_version_id']}",
            json={"policy": {"required_size": 999999}},
        )
        assert resp.status_code == 200
        assert resp.json()["is_ready"] is False

        alerts = client.get("/api/alerts", params={"event_type": "RUN_BLOCKED"}).json()
        assert len(alerts) == 1
        assert alerts[0]["entity_type"] == "DatasetVersion"
        assert alerts[0]["entity_id"] == ids["dataset_version_id"]
        assert alerts[0]["payload"]["reason"] == "readiness_blocked"

    def test_ready_dataset_creates_no_alert(self, client, session_factory):
        ids = _seed_dataset_version(session_factory, row_count=10)
        resp = client.post(
            f"/api/internal/readiness/{ids['dataset_version_id']}", json={}
        )
        assert resp.json()["is_ready"] is True
        assert client.get("/api/alerts").json() == []


class TestTrainingFailureIsAlerted:
    def test_reported_failure_creates_a_critical_alert(self, client, session_factory):
        run_id = _seed_training_run(session_factory)
        resp = client.post(
            f"/api/internal/training-runs/{run_id}/finish",
            json={"status": "FAILED", "error_message": "subprocess exited 1"},
        )
        assert resp.status_code == 200

        alerts = client.get("/api/alerts", params={"event_type": "TRAINING_FAILED"}).json()
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "CRITICAL"
        assert alerts[0]["entity_type"] == "TrainingRun"
        assert alerts[0]["entity_id"] == run_id
        assert "subprocess exited 1" in alerts[0]["message"]
        assert alerts[0]["payload"]["error_message"] == "subprocess exited 1"

    def test_reported_success_creates_no_alert(self, client, session_factory):
        run_id = _seed_training_run(session_factory)
        client.post(
            f"/api/internal/training-runs/{run_id}/finish",
            json={"status": "SUCCESS"},
        )
        assert client.get("/api/alerts").json() == []
