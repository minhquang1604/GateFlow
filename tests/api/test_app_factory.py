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
            "/lineage", "/experiments", "/pipelines", "/settings", "/activity",
        ):
            assert client.get(path).status_code == 200
        # Static assets
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/favicon.svg").status_code == 200
        # OpenAPI exposes 57 distinct /api paths: 18 read endpoints over the
        # framework's own rows (readiness + drift included, plus
        # api/routers/models.py's reproducibility-report download — see
        # sdk/report.py — and runs.py's SSE status stream, GET
        # /training-runs/{id}/events — see run_detail.html's
        # subscribeToRunEvents()), 19 that proxy
        # the systems a run executed on (Airflow: health/import-errors/pools,
        # DAG list, DAG detail, per-run tasks, per-task log; MLflow: the
        # per-run view, experiments, the leaderboard, the registered-model
        # list, the registry reconciliation, and — each doubled, once scoped
        # to a framework run id and once to a raw MLflow run id, see
        # mlflow_views.py's ``_by_mlflow_id`` siblings — the single-run
        # summary, artifact listing, artifact download, the model
        # descriptor, and the sweep tree), 3 for cron scheduling
        # (api/routers/schedules.py — list/create, get/update/delete,
        # run-now), 9 under /internal
        # (mlops_framework.api.routers.internal) — the DAG's callbacks plus
        # the write endpoints, which are the only route into the deployed
        # database from outside the VPC — 1 for
        # api/routers/settings.py's single read-only config/reachability
        # pane (GET /api/settings), 3 for api/routers/policy_settings.py's
        # persisted, editable governance-policy defaults (GET
        # /api/settings/policies, GET+PUT /api/settings/policies/{key},
        # POST /api/settings/policies/{key}/reset — see
        # framework_settings/manager.py), 2 for airflow_views.py's gated
        # task-control writes (POST .../tasks/{task_id}/clear and
        # .../retry — see api/security.py's require_write_token), 1 for
        # api/routers/audit.py's read-only audit-trail list (GET
        # /api/audit — see audit/manager.py), and 1 for
        # api/routers/alerts.py's read-only governance-event list (GET
        # /api/alerts — see events/store.py::GovernanceEventStore).
        spec = client.get("/openapi.json").json()
        api_paths = [p for p in spec["paths"] if p.startswith("/api/")]
        # 65, not 57: + 3 for /api/api-keys (admin scope — see
        # routers/api_keys.py), + POST /api/model-versions/{id}/rollback,
        # + POST /api/drift/{id}/check, + POST /api/internal/drift,
        # + GET /api/internal/dataset-versions/{id} (the last two are
        # what mlops_drift_check.py calls), + GET /api/lineage/dataset/{id}
        # (the whole-dataset, every-version-in-parallel lineage view —
        # see LineageManager.graph_for_dataset). The two probes /health
        # and /ready are deliberately absent from this count — they are
        # not /api-prefixed, on purpose (see api/routers/health.py).
        assert len(api_paths) == 65, f"Expected 65, got {len(api_paths)}: {api_paths}"
        internal = [p for p in api_paths if p.startswith("/api/internal/")]
        assert len(internal) == 11, internal
        # The read-only proxy count stays 19: clear/retry are a distinct
        # write path (see the module docstring above), not part of the
        # "read a view of another system" family this filter enumerates.
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
