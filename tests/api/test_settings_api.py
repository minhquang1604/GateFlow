"""Tests for GET /api/settings — masking and configured/reachable shape.

No live MLflow/Airflow server is required: the "not configured" cases hit
neither client constructor at all (see ``mlflow_gateway``/``airflow_gateway``
``client_or_reason`` — both bail out before importing anything when no URL
is set), and the "configured but unreachable" case points at a closed port
so the real network path is exercised without needing docker compose up.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mlops_framework.api.app import create_app
from mlops_framework.config.settings import get_settings

_UNREACHABLE = "http://127.0.0.1:1"


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("AIRFLOW_BASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://appuser:s3cr3t@db-host:5432/mlops")
    monkeypatch.setenv("AIRFLOW_PASSWORD", "hunter2")
    get_settings.cache_clear()
    yield TestClient(create_app(mount_ui=False))


class TestNotConfigured:
    def test_mlflow_and_airflow_report_not_configured(self, client):
        body = client.get("/api/settings").json()
        assert body["mlflow"]["configured"] is False
        assert body["mlflow"]["reachable"] is False
        assert body["airflow"]["configured"] is False
        assert body["airflow"]["reachable"] is False

    def test_app_and_scheduler_metadata_present(self, client):
        body = client.get("/api/settings").json()
        assert body["app_name"] == "mlops-framework"
        assert "enabled" in body["scheduler"]
        assert "poll_seconds" in body["scheduler"]


class TestSecretsMasked:
    def test_database_password_is_masked_not_dropped(self, client):
        body = client.get("/api/settings").json()
        url = body["database"]["fields"]["url"]
        assert "s3cr3t" not in url
        assert "appuser" in url and "db-host" in url and "mlops" in url

    def test_airflow_password_is_masked(self, client):
        body = client.get("/api/settings").json()
        password = body["airflow"]["fields"]["password"]
        assert password != "hunter2"
        assert password  # still non-empty — "set" is still visible, value is not

    def test_mlflow_panel_has_no_secret_fields(self, client):
        # MLflow config carries no in-band credential today (S3 creds are
        # env-only, read directly by the mlflow/boto client) — nothing to
        # mask, just confirming the expected fields are the only ones out.
        body = client.get("/api/settings").json()
        assert set(body["mlflow"]["fields"]) == {
            "tracking_uri", "experiment_name", "s3_endpoint_url",
        }


class TestConfiguredButUnreachable:
    def test_airflow_configured_but_unreachable(self, monkeypatch, client):
        monkeypatch.setenv("AIRFLOW_BASE_URL", _UNREACHABLE)
        get_settings.cache_clear()
        body = client.get("/api/settings").json()
        assert body["airflow"]["configured"] is True
        assert body["airflow"]["reachable"] is False
        assert body["airflow"]["reason"]

    def test_mlflow_configured_but_unreachable(self, monkeypatch, client):
        pytest.importorskip("mlflow", reason="mlflow SDK is not installed")
        monkeypatch.setenv("MLFLOW_TRACKING_URI", _UNREACHABLE)
        get_settings.cache_clear()
        body = client.get("/api/settings").json()
        assert body["mlflow"]["configured"] is True
        assert body["mlflow"]["reachable"] is False
        assert body["mlflow"]["reason"]
