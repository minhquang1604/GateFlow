"""Tests for the liveness and readiness probes.

The point of having two is that they answer different questions, so
most of what is worth asserting here is that they *disagree* when the
database is down: liveness stays green (the process is fine; restarting
it would not help) while readiness goes red (it cannot serve a domain
request). A single probe conflating the two is what lets an orchestrator
restart a healthy app because someone else's database blinked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mlops_framework.api.deps import get_db_manager_dep


class TestLiveness:
    def test_health_is_200(self, anon_client):
        r = anon_client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_health_needs_no_token(self, anon_client):
        """A probe cannot carry a credential — the write gate must not
        reach these routes."""
        assert anon_client.get("/health").status_code == 200
        assert anon_client.get("/ready").status_code == 200


class TestReadiness:
    def test_ready_reports_the_database(self, anon_client):
        r = anon_client.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["database"] == "ok"

    def test_ready_is_503_when_the_database_is_unreachable(self, app):
        class _BrokenManager:
            def get_session(self):
                raise OSError("could not connect to server: Connection refused")

        app.dependency_overrides[get_db_manager_dep] = lambda: _BrokenManager()
        client = TestClient(app)

        r = client.get("/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not ready"
        assert body["database"] == "unreachable"
        assert "Connection refused" in body["detail"]

    def test_liveness_stays_green_while_readiness_is_red(self, app):
        """The distinction the two endpoints exist for."""

        class _BrokenManager:
            def get_session(self):
                raise OSError("down")

        app.dependency_overrides[get_db_manager_dep] = lambda: _BrokenManager()
        client = TestClient(app)

        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503


class TestNotUnderApiPrefix:
    """Container runtimes and load balancers get a short fixed path; the
    compose healthcheck and the ECS task definition both hardcode it."""

    @pytest.mark.parametrize("path", ["/health", "/ready"])
    def test_root_path_serves_it(self, anon_client, path):
        assert anon_client.get(path).status_code in (200, 503)

    @pytest.mark.parametrize("path", ["/api/health", "/api/ready"])
    def test_api_prefixed_path_does_not(self, anon_client, path):
        assert anon_client.get(path).status_code == 404
