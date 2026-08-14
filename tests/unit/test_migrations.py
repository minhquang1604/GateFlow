"""Regression test for the alembic migration chain against SQLite.

The README's "Installation (local, SQLite)" path documents ``alembic
upgrade head`` against a ``sqlite:///`` URL as a supported way to run the
framework with no external services. Every other test in this suite
builds its schema via ``Base.metadata.create_all(engine)`` instead, which
compiles ``TimestampMixin``'s ``server_default=func.now()`` per-dialect
correctly on its own — so none of them exercised the migration files
themselves, and several ``created_at``/``updated_at`` columns across
001/003/004/005 carried a hardcoded ``server_default=sa.text("now()")``
(Postgres-only SQL, not a function SQLite has) for months. It only surfaced
by hand: ``alembic upgrade head`` against SQLite succeeded (DDL alone
doesn't evaluate the default), but the first INSERT into any of those
tables failed with ``sqlite3.OperationalError: unknown function: now()``.

This test runs the real migration chain — not ``create_all`` — against a
throwaway SQLite file and inserts one row, so a future migration that
reintroduces a hardcoded, dialect-specific default fails here instead of
only being discoverable by someone following the README by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alembic import command

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def sqlite_url(tmp_path, monkeypatch):
    db_path = tmp_path / "migrations_test.db"
    url = f"sqlite:///{db_path}"
    # alembic/env.py requires DATABASE_URL to be set and reads it via
    # os.getenv — this is the one migration entry point that does not go
    # through pydantic-settings, so it needs its own env var, not
    # get_settings().
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def _alembic_config(sqlite_url: str) -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sqlite_url)
    return cfg


class TestMigrationsAgainstSqlite:
    def test_upgrade_head_then_insert_a_timestamped_row(self, sqlite_url):
        """The exact scenario that used to fail: migrate, then insert into
        a table whose created_at/updated_at came from a migration file
        (not from Base.metadata.create_all)."""
        command.upgrade(_alembic_config(sqlite_url), "head")

        from mlops_framework.database.models.dataset import Dataset

        engine = create_engine(sqlite_url)
        session = sessionmaker(bind=engine)()
        try:
            ds = Dataset(name="migration-regression-check")
            session.add(ds)
            session.commit()
            assert ds.id is not None
            assert ds.created_at is not None
            assert ds.updated_at is not None
        finally:
            session.close()
            engine.dispose()

    def test_insert_into_a_week3_governance_table(self, sqlite_url):
        """004_week3_governance.py had the same hardcoded default on four
        tables — cover one directly rather than relying on the dataset
        table (001_initial.py) to stand in for every migration file."""
        command.upgrade(_alembic_config(sqlite_url), "head")

        from mlops_framework.database.models.dataset import Dataset
        from mlops_framework.database.models.dataset_version import DatasetVersion
        from mlops_framework.database.models.readiness_evaluation import (
            ReadinessEvaluation,
            ReadinessStatus,
        )

        engine = create_engine(sqlite_url)
        session = sessionmaker(bind=engine)()
        try:
            ds = Dataset(name="migration-regression-governance")
            session.add(ds)
            session.flush()
            dv = DatasetVersion(
                dataset_id=ds.id,
                version_number=1,
                storage_uri="s3://x/v1.csv",
                checksum="a" * 64,
                schema_hash="b" * 64,
                row_count=10,
            )
            session.add(dv)
            session.flush()
            evaluation = ReadinessEvaluation(
                dataset_version_id=dv.id,
                status=ReadinessStatus.READY,
                reasons_json="[]",
            )
            session.add(evaluation)
            session.commit()
            assert evaluation.id is not None
            assert evaluation.created_at is not None
        finally:
            session.close()
            engine.dispose()
