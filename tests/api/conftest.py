"""Shared test fixtures for the API tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.api.app import create_app
from mlops_framework.api.deps import (
    get_db,
    get_db_manager_dep,
)
from mlops_framework.database.base import Base
from mlops_framework.database.session import DatabaseManager


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

    def _override_get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    test_app.dependency_overrides[get_db_manager_dep] = _override_get_db_manager_dep
    test_app.dependency_overrides[get_db] = _override_get_db
    return test_app


@pytest.fixture()
def client(app):
    return TestClient(app)
