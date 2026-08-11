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
            "/", "/dashboard", "/datasets", "/runs", "/models", "/schedules",
            "/lineage", "/experiments", "/pipelines",
        ):
            assert client.get(path).status_code == 200
        # Static assets
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/favicon.svg").status_code == 200
        # OpenAPI exposes 47 distinct /api paths: 16 read endpoints over the
        # framework's own rows (readiness + drift included), 19 that proxy
        # the systems a run executed on (Airflow: health/import-errors/pools,
        # DAG list, DAG detail, per-run tasks, per-task log; MLflow: the
        # per-run view, experiments, the leaderboard, the registered-model
        # list, the registry reconciliation, and — each doubled, once scoped
        # to a framework run id and once to a raw MLflow run id, see
        # mlflow_views.py's ``_by_mlflow_id`` siblings — the single-run
        # summary, artifact listing, artifact download, the model
        # descriptor, and the sweep tree), 3 for cron scheduling
        # (api/routers/schedules.py — list/create, get/update/delete,
        # run-now), and 9 under /internal
        # (mlops_framework.api.routers.internal) — the DAG's callbacks plus
        # the write endpoints, which are the only route into the deployed
        # database from outside the VPC.
        spec = client.get("/openapi.json").json()
        api_paths = [p for p in spec["paths"] if p.startswith("/api/")]
        assert len(api_paths) == 47, f"Expected 47, got {len(api_paths)}: {api_paths}"
        internal = [p for p in api_paths if p.startswith("/api/internal/")]
        assert len(internal) == 9, internal
        external = [
            p
            for p in api_paths
            if "mlflow" in p
            or "registry" in p
            or "artifacts" in p
            or "airflow" in p
            or p.endswith(("/tasks", "/model-info", "/nested", "/log"))
        ]
        assert len(external) == 19, external

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
