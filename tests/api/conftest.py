"""Shared test fixtures for the API tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.api.app import create_app
from mlops_framework.api.deps import get_db_manager_dep
from mlops_framework.api.security import HEADER_NAME
from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base
from mlops_framework.database.session import DatabaseManager

WRITE_TOKEN = "test-console-token"


@pytest.fixture(autouse=True)
def configured_write_token(monkeypatch):
    """Give every API test a deployment with a write token configured.

    The write gate (``api/security.py``) fails closed, so without this
    every test touching ``/api/internal/*`` or a schedule mutation would
    get a 503 about configuration rather than exercising the handler it
    is actually about. Tests that care about the *gate* itself override
    this locally — see ``test_write_auth.py`` and
    ``test_airflow_views.py``'s ``test_clear_requires_token_when_none_configured``.

    The cache is cleared on the way out as well as in: ``get_settings``
    is ``lru_cache``d process-wide, so leaving a populated cache behind
    would leak this token into whatever test module runs next.
    """
    monkeypatch.setenv("CONSOLE_WRITE_TOKEN", WRITE_TOKEN)
    get_settings.cache_clear()
    yield WRITE_TOKEN
    get_settings.cache_clear()


def authenticated_client(app) -> TestClient:
    """A TestClient that sends the write token on every request.

    Used by the ``client`` fixture and by the two modules that build
    their own client (``test_schedules_api.py``,
    ``test_internal_promote_mlflow_sync.py``) so all three agree on how
    an authorized caller looks.
    """
    return TestClient(app, headers={HEADER_NAME: WRITE_TOKEN})


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def app(session_factory):
    """Build a FastAPI app wired to a fresh in-memory SQLite DB."""
    # A fresh DatabaseManager with the test session factory.
    mgr = DatabaseManager()
    mgr._engine = session_factory().get_bind()  # type: ignore[attr-defined]
    mgr._session_factory = session_factory  # type: ignore[attr-defined]

    test_app = create_app(mount_ui=False)

    def _override_get_db_manager_dep():
        return mgr

    # Only the manager is overridden. `get_db` itself is deliberately left
    # alone so the tests exercise the real transaction handling — an
    # override that re-implements it would hide bugs in the code that
    # actually runs in production.
    test_app.dependency_overrides[get_db_manager_dep] = _override_get_db_manager_dep
    return test_app


@pytest.fixture()
def client(app):
    return authenticated_client(app)


@pytest.fixture()
def anon_client(app):
    """A client with no write token — what an unauthenticated caller
    reaching the port looks like. Used by ``test_write_auth.py``."""
    return TestClient(app)
