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
    def test_wrong_token_is_403(self, app, method, path, body):
        client = TestClient(app, headers={"X-Console-Token": "not-the-token"})
        r = _call(client, method, path, body)
        assert r.status_code == 403, f"{method.upper()} {path} -> {r.status_code}"

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
