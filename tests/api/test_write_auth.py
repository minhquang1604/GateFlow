"""Regression tests for the write gate on every state-changing endpoint.

These pin the fix for the hole described in ``api/security.py``'s module
docstring: ``/api/internal/*`` and the write half of ``/api/schedules``
used to accept anonymous requests, which meant anyone who could reach
the port could promote a model to PRODUCTION, trigger an Airflow DAG, or
hand ``LocalDockerOrchestrator`` a ``pipeline_id`` to import and call.

Three properties, one per class below:

* an unauthenticated caller is refused (401), and the refusal happens
  *before* the handler runs — nothing is written;
* a wrong token is refused (403);
* an unconfigured deployment refuses everyone (503) rather than falling
  back to open access.

Reads stay open throughout — the console renders for a reader with no
token, which is the whole reason the gate is per-route rather than
global.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import WRITE_TOKEN  # noqa: E402 - see the note in test_schedules_api.py
from mlops_framework.config.settings import get_settings
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelVersion
from mlops_framework.database.models.schedule import Schedule

# (method, path, json body) for every route that changes state.
WRITE_ROUTES = [
    ("post", "/api/internal/datasets", {"name": "x"}),
    ("post", "/api/internal/datasets/1/versions", {"storage_uri": "s3://x", "row_count": 1}),
    ("post", "/api/internal/models", {"name": "x"}),
    ("post", "/api/internal/readiness/1", {}),
    ("post", "/api/internal/training-runs", {"dataset_version_id": 1, "pipeline_id": "m:f"}),
    ("post", "/api/internal/training-runs/1/start", {}),
    ("post", "/api/internal/training-runs/1/finish", {"status": "SUCCESS"}),
    (
        "post",
        "/api/internal/models/x/promote",
        {"dataset_version_id": 1, "training_run_id": 1},
    ),
    # The GET on this router is gated too: it hands out dataset storage URIs.
    ("get", "/api/internal/training-runs/1/context", None),
    ("post", "/api/schedules", {
        "model_id": 1, "dataset_id": 1, "pipeline_id": "m:f", "cron_expression": "* * * * *",
    }),
    ("patch", "/api/schedules/1", {"enabled": False}),
    ("delete", "/api/schedules/1", None),
    ("post", "/api/schedules/1/run-now", None),
]

READ_ROUTES = [
    "/api/dashboard",
    "/api/datasets",
    "/api/models",
    "/api/training-runs",
    "/api/schedules",
    "/api/audit",
    "/api/alerts",
]


def _call(client: TestClient, method: str, path: str, body):
    fn = getattr(client, method)
    return fn(path, json=body) if body is not None else fn(path)


class TestUnauthenticatedIsRefused:
    @pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
    def test_no_token_is_401(self, anon_client, method, path, body):
        r = _call(anon_client, method, path, body)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"

    @pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
    def test_wrong_token_is_401(self, app, method, path, body):
        """401, not 403. A credential that resolves to nobody is an
        *authentication* failure; 403 is reserved for a caller we can
        name who lacks the scope — see TestScopes below. This was 403
        before API keys existed, when "wrong secret" was the only shape
        a refusal could take."""
        client = TestClient(app, headers={"X-Console-Token": "not-the-token"})
        r = _call(client, method, path, body)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"

    @pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
    def test_an_unknown_bearer_key_is_401(self, app, method, path, body):
        client = TestClient(app, headers={"Authorization": "Bearer mlops_ak_nope"})
        r = _call(client, method, path, body)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"

    @pytest.mark.parametrize("path", READ_ROUTES)
    def test_reads_stay_open(self, anon_client, path):
        assert anon_client.get(path).status_code == 200


class TestRefusalHappensBeforeTheHandler:
    """A 401 must mean "nothing happened", not "it happened and then we
    complained" — the dependency has to run ahead of the handler body."""

    def test_rejected_dataset_registration_writes_nothing(self, anon_client, client, session_factory):
        r = anon_client.post("/api/internal/datasets", json={"name": "attacker-dataset"})
        assert r.status_code == 401

        s = session_factory()
        try:
            assert s.query(Dataset).filter_by(name="attacker-dataset").first() is None
        finally:
            s.close()
        # …and the same call with the token does create it, so the
        # assertion above is about the gate and not a broken handler.
        assert client.post(
            "/api/internal/datasets", json={"name": "attacker-dataset"}
        ).status_code == 200

    def test_rejected_promotion_creates_no_model_version(self, anon_client, session_factory):
        s = session_factory()
        try:
            s.add(ModelRow(name="fraud-xgboost"))
            s.commit()
        finally:
            s.close()

        r = anon_client.post(
            "/api/internal/models/fraud-xgboost/promote",
            json={"dataset_version_id": 1, "training_run_id": 1, "metrics": {"f1": 0.99}},
        )
        assert r.status_code == 401

        s = session_factory()
        try:
            model = s.query(ModelRow).filter_by(name="fraud-xgboost").one()
            assert s.query(ModelVersion).filter_by(model_id=model.id).count() == 0
        finally:
            s.close()

    def test_rejected_schedule_creation_writes_nothing(self, anon_client, session_factory):
        r = anon_client.post(
            "/api/schedules",
            json={
                "model_id": 1,
                "dataset_id": 1,
                "pipeline_id": "os:system",
                "cron_expression": "* * * * *",
            },
        )
        assert r.status_code == 401

        s = session_factory()
        try:
            assert s.query(Schedule).count() == 0
        finally:
            s.close()


class TestUnconfiguredDeploymentFailsClosed:
    """With no CONSOLE_WRITE_TOKEN the gate must refuse everyone — the
    dangerous regression would be falling back to open access."""

    @pytest.fixture()
    def no_token(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_WRITE_TOKEN", raising=False)
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
    def test_every_write_route_is_503(self, app, no_token, method, path, body):
        client = TestClient(app, headers={"X-Console-Token": "anything"})
        r = _call(client, method, path, body)
        assert r.status_code == 503, f"{method.upper()} {path} -> {r.status_code}"

    @pytest.mark.parametrize("path", READ_ROUTES)
    def test_reads_still_work(self, app, no_token, path):
        assert TestClient(app).get(path).status_code == 200


class TestScopes:
    """What 403 means now: we know who you are, and you may not do this."""

    def _mint(self, session_factory, name, scopes):
        from mlops_framework.auth.manager import ApiKeyManager

        s = session_factory()
        try:
            key = ApiKeyManager(s).create(name=name, scopes=scopes)
            s.commit()
            return key.plaintext
        finally:
            s.close()

    def test_a_read_only_key_cannot_write(self, app, session_factory):
        key = self._mint(session_factory, "grafana", ["read"])
        client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

        r = client.post("/api/internal/datasets", json={"name": "x"})
        assert r.status_code == 403
        assert "'read'" in r.json()["detail"] or "read" in r.json()["detail"]

    def test_a_write_key_can_write(self, app, session_factory):
        key = self._mint(session_factory, "alice", ["write"])
        client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

        r = client.post("/api/internal/datasets", json={"name": "alices-dataset"})
        assert r.status_code == 200

    def test_admin_implies_write(self, app, session_factory):
        key = self._mint(session_factory, "root", ["admin"])
        client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

        assert client.post(
            "/api/internal/datasets", json={"name": "admins-dataset"}
        ).status_code == 200

    def test_a_revoked_key_is_401(self, app, session_factory):
        from mlops_framework.auth.manager import ApiKeyManager

        key = self._mint(session_factory, "leaked", ["write"])
        s = session_factory()
        try:
            ApiKeyManager(s).revoke("leaked")
            s.commit()
        finally:
            s.close()

        client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
        assert client.post(
            "/api/internal/datasets", json={"name": "x"}
        ).status_code == 401

    def test_a_key_is_not_silently_downgraded_to_the_shared_secret(
        self, app, session_factory
    ):
        """Presenting a bad key while the shared secret is also
        configured must fail, not quietly succeed as `system` — that
        would put the wrong name in the audit trail."""
        client = TestClient(
            app,
            headers={
                "Authorization": "Bearer mlops_ak_wrong",
                "X-Console-Token": WRITE_TOKEN,
            },
        )
        assert client.post(
            "/api/internal/datasets", json={"name": "x"}
        ).status_code == 401


class TestVerifiedActor:
    def test_the_audit_trail_records_the_key_name_not_the_header(
        self, app, session_factory
    ):
        """The point of the whole feature: with a key, `actor` comes from
        the key row, so X-Actor can no longer be used to impersonate."""
        from mlops_framework.auth.manager import ApiKeyManager
        from mlops_framework.database.models.audit_log import AuditLog

        s = session_factory()
        try:
            key = ApiKeyManager(s).create(name="alice", scopes=["write"]).plaintext
            s.commit()
        finally:
            s.close()

        client = TestClient(
            app,
            headers={"Authorization": f"Bearer {key}", "X-Actor": "definitely-bob"},
        )
        model = client.post("/api/internal/models", json={"name": "m"}).json()
        client.post(
            f"/api/internal/models/{model['name']}/promote",
            json={"dataset_version_id": 1, "training_run_id": 1},
        )

        s = session_factory()
        try:
            actors = {row.actor for row in s.query(AuditLog).all()}
            assert "definitely-bob" not in actors
            assert actors <= {"alice"}
        finally:
            s.close()

    def test_the_shared_secret_still_believes_x_actor(self, client, session_factory):
        """Unchanged, and visibly so: that path never verified anything,
        and pretending otherwise would be worse than keeping it honest."""
        from mlops_framework.database.models.audit_log import AuditLog

        client.post(
            "/api/internal/models",
            json={"name": "m"},
            headers={"X-Actor": "whoever"},
        )
        client.post(
            "/api/internal/models/m/promote",
            json={"dataset_version_id": 1, "training_run_id": 1},
            headers={"X-Actor": "whoever"},
        )
        s = session_factory()
        try:
            rows = s.query(AuditLog).all()
            assert rows and all(r.actor == "whoever" for r in rows)
        finally:
            s.close()
