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
from mlops_framework.api.deps import get_db, get_db_manager_dep
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

    def _db_dep():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db_manager_dep] = _mgr_dep
    app.dependency_overrides[get_db] = _db_dep
    return app


class TestFullAppBoot:
    def test_app_with_ui_mounted(self, in_memory_app):
        client = TestClient(in_memory_app)
        # UI
        for path in ("/", "/dashboard", "/datasets", "/runs", "/models", "/lineage"):
            assert client.get(path).status_code == 200
        # Static assets
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        # OpenAPI exposes 17 distinct /api paths (15 public + 2
        # internal — the Airflow DAG's HTTP calls, see
        # mlops_framework.api.routers.internal).
        spec = client.get("/openapi.json").json()
        api_paths = [p for p in spec["paths"] if p.startswith("/api/")]
        assert len(api_paths) == 17, f"Expected 17, got {len(api_paths)}: {api_paths}"

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

        def _db_dep():
            s = factory()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db_manager_dep] = _mgr_dep
        app.dependency_overrides[get_db] = _db_dep

        client = TestClient(app)
        # UI is not mounted — but '/' returns a 404
        assert client.get("/").status_code == 404
        # API is still served
        assert client.get("/api/dashboard").status_code == 200
