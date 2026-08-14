"""``POST /api/model-versions/{id}/rollback``.

The manager's own behaviour is covered in
``tests/unit/test_model_rollback.py``. What is tested here is
everything the endpoint has to do *around* the swap for a rollback to
be real rather than a row edit: the audit row, the alert, the serving
reload, and the gate.
"""

from __future__ import annotations

import json

import pytest

from mlops_framework.database.models.audit_log import AuditLog
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.governance_event import (
    GovernanceEvent,
    GovernanceEventSeverity,
)
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelState, ModelVersion


@pytest.fixture()
def rolled_back_model(session_factory):
    """v1 ARCHIVED (known good), v2 PRODUCTION (the bad one)."""
    s = session_factory()
    try:
        ds = Dataset(name="churn")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
            row_count=100, checksum="c", schema_hash="h",
        )
        s.add(dv)
        s.flush()
        model = ModelRow(name="churn-clf")
        s.add(model)
        s.flush()
        v1 = ModelVersion(
            model_id=model.id, dataset_version_id=dv.id, version_number=1,
            state=ModelState.ARCHIVED.value, artifact_uri="s3://m/v1.json",
            metrics_json=json.dumps({"f1": 0.90}),
        )
        v2 = ModelVersion(
            model_id=model.id, dataset_version_id=dv.id, version_number=2,
            state=ModelState.PRODUCTION.value, artifact_uri="s3://m/v2.json",
            metrics_json=json.dumps({"f1": 0.95}),
        )
        s.add_all([v1, v2])
        s.commit()
        return {"model_id": model.id, "v1": v1.id, "v2": v2.id}
    finally:
        s.close()


class TestSwap:
    def test_restores_and_archives(self, client, session_factory, rolled_back_model):
        ids = rolled_back_model
        r = client.post(f"/api/model-versions/{ids['v1']}/rollback")
        assert r.status_code == 200

        body = r.json()
        assert body["model_name"] == "churn-clf"
        assert body["restored_version"] == 1
        assert body["previous_production_version"] == 2

        s = session_factory()
        try:
            assert s.get(ModelVersion, ids["v1"]).state == ModelState.PRODUCTION
            assert s.get(ModelVersion, ids["v2"]).state == ModelState.ARCHIVED
        finally:
            s.close()

    def test_already_production_is_409(self, client, rolled_back_model):
        r = client.post(f"/api/model-versions/{rolled_back_model['v2']}/rollback")
        assert r.status_code == 409
        assert "already the PRODUCTION" in r.json()["detail"]

    def test_unknown_version_is_404(self, client):
        assert client.post("/api/model-versions/9999/rollback").status_code == 404


class TestItIsRecorded:
    """A rollback is an operator decision. "Who put the old version back,
    and when" is the first question asked afterwards."""

    def test_writes_an_audit_row_naming_the_actor(
        self, client, session_factory, rolled_back_model
    ):
        ids = rolled_back_model
        r = client.post(
            f"/api/model-versions/{ids['v1']}/rollback",
            headers={"X-Actor": "alice"},
        )
        assert r.status_code == 200

        s = session_factory()
        try:
            row = s.query(AuditLog).filter_by(action="MODEL_ROLLED_BACK").one()
            assert row.actor == "alice"
            assert row.entity_type == "ModelVersion"
            assert row.entity_id == ids["v1"]
            meta = json.loads(row.metadata_json)
            assert meta["restored_version"] == 1
            assert meta["previous_production_version"] == 2
        finally:
            s.close()

    def test_raises_a_critical_alert(self, client, session_factory, rolled_back_model):
        """CRITICAL, not informational: a rollback means production was
        wrong. A normal promotion is not recorded as an alert at all."""
        ids = rolled_back_model
        client.post(f"/api/model-versions/{ids['v1']}/rollback")

        s = session_factory()
        try:
            ev = s.query(GovernanceEvent).filter_by(event_type="MODEL_ROLLED_BACK").one()
            assert ev.severity == GovernanceEventSeverity.CRITICAL
            assert "rolled back to v1" in ev.message
            assert "retiring v2" in ev.message
            assert json.loads(ev.payload_json)["model_name"] == "churn-clf"
        finally:
            s.close()

    def test_shows_up_on_the_alerts_feed(self, client, rolled_back_model):
        client.post(f"/api/model-versions/{rolled_back_model['v1']}/rollback")
        alerts = client.get("/api/alerts").json()
        assert any(a["event_type"] == "MODEL_ROLLED_BACK" for a in alerts)


class TestServingReload:
    def test_reports_false_when_no_bridge_is_configured(
        self, client, rolled_back_model, monkeypatch
    ):
        """The registry still rolls back — the caller is told the bridge
        did not confirm, rather than the rollback failing."""
        from mlops_framework.config.settings import get_settings

        monkeypatch.delenv("SERVING_BRIDGE_URL", raising=False)
        get_settings.cache_clear()

        r = client.post(f"/api/model-versions/{rolled_back_model['v1']}/rollback")
        assert r.status_code == 200
        assert r.json()["serving_reloaded"] is False
        get_settings.cache_clear()

    def test_publishes_the_restored_version_to_the_bridge(
        self, client, rolled_back_model, monkeypatch
    ):
        from mlops_framework.config.settings import get_settings

        published = {}

        class _FakePublisher:
            def __init__(self, url, **kw):
                published["url"] = url

            def publish(self, event):
                published["event"] = event
                return True

            def close(self):
                published["closed"] = True

        monkeypatch.setenv("SERVING_BRIDGE_URL", "http://serving:8001")
        get_settings.cache_clear()
        monkeypatch.setattr(
            "mlops_framework.api.routers.models.HttpEventPublisher", _FakePublisher
        )

        r = client.post(f"/api/model-versions/{rolled_back_model['v1']}/rollback")
        assert r.status_code == 200
        assert r.json()["serving_reloaded"] is True

        assert published["url"] == "http://serving:8001/internal/model/reload"
        assert published["closed"] is True
        payload = published["event"].payload
        assert payload["model_name"] == "churn-clf"
        assert payload["model_version"] == 1
        # The *restored* version's artifact, not the one being retired.
        assert payload["artifact_uri"] == "s3://m/v1.json"
        get_settings.cache_clear()

    def test_a_bridge_failure_does_not_undo_the_rollback(
        self, client, session_factory, rolled_back_model, monkeypatch
    ):
        from mlops_framework.config.settings import get_settings

        class _BrokenPublisher:
            def __init__(self, url, **kw):
                pass

            def publish(self, event):
                raise OSError("connection refused")

            def close(self):
                pass

        monkeypatch.setenv("SERVING_BRIDGE_URL", "http://serving:8001")
        get_settings.cache_clear()
        monkeypatch.setattr(
            "mlops_framework.api.routers.models.HttpEventPublisher", _BrokenPublisher
        )

        r = client.post(f"/api/model-versions/{rolled_back_model['v1']}/rollback")
        assert r.status_code == 200
        assert r.json()["serving_reloaded"] is False

        s = session_factory()
        try:
            assert s.get(ModelVersion, rolled_back_model["v1"]).state == ModelState.PRODUCTION
        finally:
            s.close()
        get_settings.cache_clear()


class TestGated:
    def test_anonymous_is_refused(self, anon_client, session_factory, rolled_back_model):
        ids = rolled_back_model
        assert anon_client.post(f"/api/model-versions/{ids['v1']}/rollback").status_code == 401

        s = session_factory()
        try:
            assert s.get(ModelVersion, ids["v2"]).state == ModelState.PRODUCTION
        finally:
            s.close()
