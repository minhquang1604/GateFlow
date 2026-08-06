"""End-to-end smoke test: boot the full app (API + UI) against an
in-memory SQLite DB and hit every router.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.api.app import create_app
from mlops_framework.api.deps import get_db_manager_dep
from mlops_framework.database.base import Base
from mlops_framework.database.session import DatabaseManager


@pytest.fixture()
def in_memory_app():
    """Build a fully-wired app with an in-memory SQLite DB."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False)
    mgr = DatabaseManager()
    mgr._engine = eng  # type: ignore[attr-defined]
    mgr._session_factory = factory  # type: ignore[attr-defined]

    app = create_app(mount_ui=True)

    def _mgr_dep():
        return mgr

    # Only the manager is overridden — `get_db` itself must run for real
    # so its commit/rollback handling is under test. See tests/api/conftest.py.
    app.dependency_overrides[get_db_manager_dep] = _mgr_dep
    return app


class TestFullAppBoot:
    def test_app_with_ui_mounted(self, in_memory_app):
        client = TestClient(in_memory_app)
        # UI
        for path in (
            "/", "/dashboard", "/datasets", "/runs", "/models", "/lineage",
            "/experiments",
        ):
            assert client.get(path).status_code == 200
        # Static assets
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/favicon.svg").status_code == 200
        # OpenAPI exposes 31 distinct /api paths: 15 read endpoints over the
        # framework's own rows, 7 that proxy the systems a run executed on
        # (1 for Airflow task state, 6 for MLflow — the per-run view,
        # experiments, the leaderboard, artifact listing and download, and
        # the model descriptor), and 9 under /internal
        # (mlops_framework.api.routers.internal) — the DAG's callbacks plus
        # the write endpoints, which are the only route into the deployed
        # database from outside the VPC.
        spec = client.get("/openapi.json").json()
        api_paths = [p for p in spec["paths"] if p.startswith("/api/")]
        assert len(api_paths) == 31, f"Expected 31, got {len(api_paths)}: {api_paths}"
        internal = [p for p in api_paths if p.startswith("/api/internal/")]
        assert len(internal) == 9, internal
        external = [p for p in api_paths if "mlflow" in p or p.endswith("/tasks")
                    or "artifacts" in p or p.endswith("/model-info")]
        assert len(external) == 7, external

    def test_app_without_ui(self, in_memory_app):
        # Build a second app with UI disabled
        eng = in_memory_app.dependency_overrides[get_db_manager_dep]()._engine
        factory = sessionmaker(bind=eng, expire_on_commit=False)
        mgr = DatabaseManager()
        mgr._engine = eng  # type: ignore[attr-defined]
        mgr._session_factory = factory  # type: ignore[attr-defined]
        app = create_app(mount_ui=False)

        def _mgr_dep():
            return mgr

        app.dependency_overrides[get_db_manager_dep] = _mgr_dep

        client = TestClient(app)
        # UI is not mounted — but '/' returns a 404
        assert client.get("/").status_code == 404
        # API is still served
        assert client.get("/api/dashboard").status_code == 200
