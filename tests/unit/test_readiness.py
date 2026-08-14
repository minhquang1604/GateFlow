"""Unit tests for the readiness engine.

The engine is tested against in-memory SQLite (mirroring how the
integration suite is set up) so the tests can exercise real ORM
behaviour. No statistical libraries are required.
"""

from __future__ import annotations

import json
from datetime import UTC

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.readiness_evaluation import (
    ReadinessStatus,
)
from mlops_framework.readiness.engine import (
    ReadinessEngine,
    ReadinessResult,
    TrainingPolicy,
)


@pytest.fixture()
def session():
    """In-memory SQLite session, with all ORM models registered."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _make_version(
    session,
    *,
    name: str = "ds",
    row_count: int = 1000,
    metadata: dict | None = None,
):
    ds = Dataset(name=name)
    session.add(ds)
    session.flush()
    v = DatasetVersion(
        dataset_id=ds.id,
        version_number=1,
        storage_uri="s3://b/v1.csv",
        checksum="0" * 64,
        schema_hash="0" * 64,
        row_count=row_count,
        metadata_json=json.dumps(metadata) if metadata is not None else None,
        is_immutable=True,
    )
    session.add(v)
    session.flush()
    return v


class TestTrainingPolicy:
    def test_from_dict_defaults(self):
        p = TrainingPolicy.from_dict(None)
        assert p.required_size == 0
        assert p.freshness_hours is None
        assert p.required_columns == []

    def test_from_dict_parses_all_fields(self):
        p = TrainingPolicy.from_dict(
            {
                "required_size": 100,
                "freshness_hours": 24,
                "required_columns": ["a", "b"],
                "dtypes": {"a": "int64"},
                "max_missing_ratio": 0.05,
                "expected_column_count": 2,
                "validation_rules": {"amount_not_null": True},
            }
        )
        assert p.required_size == 100
        assert p.freshness_hours == 24
        assert p.required_columns == ["a", "b"]
        assert p.dtypes == {"a": "int64"}
        assert p.max_missing_ratio == 0.05
        assert p.expected_column_count == 2
        assert p.validation_rules == {"amount_not_null": True}

    def test_to_dict_round_trip(self):
        original = {
            "required_size": 50,
            "required_columns": ["x"],
            "dtypes": {"x": "int64"},
            "validation_rules": {"x": True},
        }
        p = TrainingPolicy.from_dict(original)
        round_tripped = TrainingPolicy.from_dict(p.to_dict())
        assert round_tripped.to_dict() == p.to_dict()


class TestNoPolicyReady:
    def test_existing_dataset_is_ready_when_no_policy(self, session):
        v = _make_version(session, row_count=10)
        engine = ReadinessEngine(session)
        result = engine.evaluate(v, policy=None)
        assert isinstance(result, ReadinessResult)
        assert result.is_ready is True
        assert result.passed is True
        assert result.reasons == []


class TestSizeCheck:
    def test_size_passes(self, session):
        v = _make_version(session, row_count=2000)
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(required_size=1000)
        )
        assert result.is_ready
        assert result.check_dict()["size"] == "PASSED"

    def test_size_fails(self, session):
        v = _make_version(session, row_count=500)
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(required_size=1000)
        )
        assert not result.is_ready
        assert result.check_dict()["size"] == "FAILED"
        assert any("500 rows" in r for r in result.reasons)

    def test_size_skipped_when_zero(self, session):
        v = _make_version(session, row_count=0)
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(required_size=0)
        )
        assert result.check_dict()["size"] == "SKIPPED"


class TestFreshnessCheck:
    def test_fresh_dataset_passes(self, session):
        v = _make_version(session)
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(freshness_hours=24)
        )
        assert result.check_dict()["freshness"] == "PASSED"

    def test_stale_dataset_fails(self, session):
        v = _make_version(session)
        # created_at is `now()` by default — simulate an old version
        from datetime import datetime, timedelta
        v.created_at = datetime.now(UTC) - timedelta(hours=72)
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(freshness_hours=24)
        )
        assert result.check_dict()["freshness"] == "FAILED"
        assert any("exceeds" in r for r in result.reasons)


class TestSchemaCheck:
    def test_schema_present_passes(self, session):
        v = _make_version(session)
        result = ReadinessEngine(session).evaluate(v, TrainingPolicy())
        assert result.check_dict()["schema"] == "PASSED"


class TestRequiredColumnsCheck:
    def test_required_columns_passes(self, session):
        v = _make_version(
            session,
            metadata={
                "columns": [
                    {"name": "amount", "dtype": "float64"},
                    {"name": "is_fraud", "dtype": "int64"},
                ]
            },
        )
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(required_columns=["amount", "is_fraud"])
        )
        assert result.check_dict()["required_columns"] == "PASSED"

    def test_required_columns_missing_fails(self, session):
        v = _make_version(
            session,
            metadata={
                "columns": [{"name": "amount", "dtype": "float64"}]
            },
        )
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(required_columns=["amount", "is_fraud"])
        )
        assert result.check_dict()["required_columns"] == "FAILED"
        assert any("is_fraud" in r for r in result.reasons)


class TestDtypeCheck:
    def test_dtype_match_passes(self, session):
        v = _make_version(
            session,
            metadata={"columns": [{"name": "amount", "dtype": "float64"}]},
        )
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(dtypes={"amount": "float64"})
        )
        assert result.check_dict()["dtypes"] == "PASSED"

    def test_dtype_mismatch_fails(self, session):
        v = _make_version(
            session,
            metadata={"columns": [{"name": "amount", "dtype": "float64"}]},
        )
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(dtypes={"amount": "int64"})
        )
        assert result.check_dict()["dtypes"] == "FAILED"


class TestColumnCountCheck:
    def test_column_count_match_passes(self, session):
        v = _make_version(
            session,
            metadata={
                "columns": [
                    {"name": "a", "dtype": "int64"},
                    {"name": "b", "dtype": "int64"},
                ]
            },
        )
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(expected_column_count=2)
        )
        assert result.check_dict()["column_count"] == "PASSED"

    def test_column_count_mismatch_fails(self, session):
        v = _make_version(
            session,
            metadata={"columns": [{"name": "a", "dtype": "int64"}]},
        )
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(expected_column_count=5)
        )
        assert result.check_dict()["column_count"] == "FAILED"


class TestMissingRatioCheck:
    def test_missing_ratio_under_threshold(self, session):
        v = _make_version(session, metadata={"missing_ratio": 0.01})
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(max_missing_ratio=0.05)
        )
        assert result.check_dict()["missing_ratio"] == "PASSED"

    def test_missing_ratio_over_threshold(self, session):
        v = _make_version(session, metadata={"missing_ratio": 0.5})
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(max_missing_ratio=0.05)
        )
        assert result.check_dict()["missing_ratio"] == "FAILED"

    def test_missing_ratio_unavailable(self, session):
        v = _make_version(session)
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(max_missing_ratio=0.05)
        )
        assert result.check_dict()["missing_ratio"] == "SKIPPED"


class TestValidationRulesCheck:
    def test_validation_rules_pass(self, session):
        v = _make_version(session, metadata={"amount_null_count": 0})
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(validation_rules={"amount_not_null": True})
        )
        assert result.check_dict()["validation_rules"] == "PASSED"

    def test_validation_rules_fail(self, session):
        v = _make_version(session, metadata={"amount_null_count": 17})
        result = ReadinessEngine(session).evaluate(
            v, TrainingPolicy(validation_rules={"amount_not_null": True})
        )
        assert result.check_dict()["validation_rules"] == "FAILED"


class TestPersistedEvaluation:
    def test_evaluation_is_persisted(self, session):
        v = _make_version(session)
        engine = ReadinessEngine(session)
        result = engine.evaluate(v, TrainingPolicy(required_size=10))
        session.commit()
        rows = engine.get_evaluations(v.id)
        assert len(rows) == 1
        row = rows[0]
        # The persisted row must agree with what the caller was told.
        assert row.status == result.status
        assert row.dataset_version_id == v.id
        assert row.status == ReadinessStatus.READY
        assert row.observed_row_count == v.row_count
        assert json.loads(row.checks_json)["size"] == "PASSED"

    def test_blocked_evaluation_persisted_with_reasons(self, session):
        v = _make_version(session, row_count=1)
        engine = ReadinessEngine(session)
        result = engine.evaluate(v, TrainingPolicy(required_size=10_000))
        session.commit()
        rows = engine.get_evaluations(v.id)
        assert len(rows) == 1
        assert rows[0].status == ReadinessStatus.BLOCKED
        assert rows[0].status == result.status
        reasons = json.loads(rows[0].reasons_json)
        assert reasons and "10000" in reasons[0]

    def test_history_is_preserved(self, session):
        v = _make_version(session, row_count=5)
        engine = ReadinessEngine(session)
        engine.evaluate(v, TrainingPolicy(required_size=10))
        v.row_count = 100
        engine.evaluate(v, TrainingPolicy(required_size=10))
        session.commit()
        rows = engine.get_evaluations(v.id)
        assert len(rows) == 2


class TestEndToEndPolicy:
    def test_blocked_when_size_and_columns_fail(self, session):
        v = _make_version(
            session,
            row_count=10,
            metadata={"columns": [{"name": "a", "dtype": "int64"}]},
        )
        policy = TrainingPolicy(
            required_size=1000,
            required_columns=["a", "b", "c"],
        )
        result = ReadinessEngine(session).evaluate(v, policy)
        assert not result.is_ready
        assert len(result.reasons) >= 2
        # the result is JSON-serialisable
        json.dumps(result.to_dict())
