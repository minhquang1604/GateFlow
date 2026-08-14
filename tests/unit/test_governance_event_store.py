"""Unit tests for GovernanceEventStore."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models.governance_event import GovernanceEventSeverity
from mlops_framework.events.publisher import (
    DriftDetectedEvent,
    RunBlockedEvent,
    TrainingFailedEvent,
)
from mlops_framework.events.store import GovernanceEventStore


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


class TestRecord:
    def test_persists_event_type_severity_and_payload(self, session):
        store = GovernanceEventStore(session)
        row = store.record(
            TrainingFailedEvent(training_run_id=42, error_message="boom"),
            message="Training run #42 failed: boom",
            severity=GovernanceEventSeverity.CRITICAL,
            entity_type="TrainingRun",
            entity_id=42,
        )
        assert row is not None
        assert row.id is not None
        assert row.event_type == "TRAINING_FAILED"
        assert row.severity == GovernanceEventSeverity.CRITICAL
        assert row.entity_type == "TrainingRun"
        assert row.entity_id == 42
        assert row.message == "Training run #42 failed: boom"
        assert '"training_run_id": 42' in row.payload_json
        assert row.created_at is not None

    def test_default_severity_is_warning(self, session):
        store = GovernanceEventStore(session)
        row = store.record(RunBlockedEvent(reason="not_eligible"), message="blocked")
        assert row.severity == GovernanceEventSeverity.WARNING

    def test_no_payload_is_null(self, session):
        store = GovernanceEventStore(session)
        row = store.record(RunBlockedEvent(reason="x"), message="m")
        # RunBlockedEvent always carries at least {"reason": "x"} — use a
        # fresh Event with an empty payload to check the null-not-{} path.
        from mlops_framework.events.publisher import Event

        empty_row = store.record(Event(event_type="CUSTOM"), message="m")
        assert row.payload_json is not None
        assert empty_row.payload_json is None



class TestFailureIsolation:
    """Same contract, same SAVEPOINT, as AuditManager — see
    ``tests/unit/test_audit.py``'s ``TestFailureIsolation`` for why
    catching the exception is not on its own enough."""

    def test_flush_time_failure_leaves_the_session_usable(self, session):
        from mlops_framework.database.models.dataset import Dataset
        from mlops_framework.database.models.governance_event import GovernanceEvent
        from mlops_framework.dataset.manager import DatasetManager

        dm = DatasetManager(session)
        dm.create_dataset("the-callers-real-work")

        # message is NOT NULL — fails inside flush(), like a constraint
        # violation would in production.
        assert (
            GovernanceEventStore(session).record(
                TrainingFailedEvent(training_run_id=1), message=None
            )
            is None
        )

        dm.create_dataset("work-after-the-failed-event-write")
        session.commit()

        names = {d.name for d in session.query(Dataset).all()}
        assert names == {"the-callers-real-work", "work-after-the-failed-event-write"}
        assert session.query(GovernanceEvent).count() == 0

    def test_a_later_event_write_still_succeeds(self, session):
        store = GovernanceEventStore(session)
        assert store.record(TrainingFailedEvent(training_run_id=1), message=None) is None
        row = store.record(TrainingFailedEvent(training_run_id=2), message="ok")
        assert row is not None and row.id is not None
        session.commit()


class TestListEntries:
    def _seed(self, store: GovernanceEventStore) -> None:
        store.record(
            TrainingFailedEvent(training_run_id=1), message="a",
            severity=GovernanceEventSeverity.CRITICAL,
            entity_type="TrainingRun", entity_id=1,
        )
        store.record(
            DriftDetectedEvent(dataset_version_id=2), message="b",
            severity=GovernanceEventSeverity.CRITICAL,
            entity_type="DatasetVersion", entity_id=2,
        )
        store.record(
            RunBlockedEvent(reason="not_eligible"), message="c",
            severity=GovernanceEventSeverity.WARNING,
            entity_type="Model", entity_id=3,
        )

    def test_returns_newest_first(self, session):
        store = GovernanceEventStore(session)
        self._seed(store)
        entries = store.list_entries()
        assert [e.event_type for e in entries] == [
            "RUN_BLOCKED", "DRIFT_DETECTED", "TRAINING_FAILED",
        ]

    def test_filters_by_event_type(self, session):
        store = GovernanceEventStore(session)
        self._seed(store)
        entries = store.list_entries(event_type="DRIFT_DETECTED")
        assert len(entries) == 1
        assert entries[0].entity_id == 2

    def test_filters_by_severity(self, session):
        store = GovernanceEventStore(session)
        self._seed(store)
        entries = store.list_entries(severity=GovernanceEventSeverity.WARNING)
        assert len(entries) == 1
        assert entries[0].event_type == "RUN_BLOCKED"

    def test_filters_by_entity(self, session):
        store = GovernanceEventStore(session)
        self._seed(store)
        entries = store.list_entries(entity_type="TrainingRun", entity_id=1)
        assert len(entries) == 1

    def test_respects_limit(self, session):
        store = GovernanceEventStore(session)
        self._seed(store)
        assert len(store.list_entries(limit=1)) == 1

    def test_empty_when_nothing_recorded(self, session):
        assert GovernanceEventStore(session).list_entries() == []
