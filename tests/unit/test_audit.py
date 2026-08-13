"""Unit tests for AuditManager."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.audit.manager import AuditManager
from mlops_framework.database.base import Base


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
    def test_records_actor_action_entity(self, session):
        am = AuditManager(session)
        entry = am.record(
            actor="alice",
            action="SCHEDULE_CREATED",
            entity_type="Schedule",
            entity_id=7,
            metadata={"cron_expression": "0 2 * * *"},
        )
        assert entry is not None
        assert entry.id is not None
        assert entry.actor == "alice"
        assert entry.action == "SCHEDULE_CREATED"
        assert entry.entity_type == "Schedule"
        assert entry.entity_id == 7
        assert entry.metadata_json == '{"cron_expression": "0 2 * * *"}'
        assert entry.created_at is not None

    def test_missing_actor_defaults_to_system(self, session):
        am = AuditManager(session)
        entry = am.record(actor=None, action="MODEL_PROMOTED")
        assert entry.actor == "system"

    def test_empty_actor_string_defaults_to_system(self, session):
        am = AuditManager(session)
        entry = am.record(actor="", action="MODEL_PROMOTED")
        assert entry.actor == "system"

    def test_no_metadata_is_null_not_empty_object(self, session):
        am = AuditManager(session)
        entry = am.record(actor="system", action="MODEL_PROMOTED")
        assert entry.metadata_json is None

    def test_never_raises_on_unserializable_metadata(self, session):
        """A set() isn't JSON-serializable — record() must swallow that,
        not take down whatever real action it was recording."""
        am = AuditManager(session)
        result = am.record(actor="system", action="MODEL_PROMOTED", metadata={"x": {1, 2}})
        assert result is None
        assert am.list_entries() == []


class TestListEntries:
    def _seed(self, am: AuditManager) -> None:
        am.record(actor="a", action="SCHEDULE_CREATED", entity_type="Schedule", entity_id=1)
        am.record(actor="b", action="SCHEDULE_DELETED", entity_type="Schedule", entity_id=1)
        am.record(actor="a", action="MODEL_PROMOTED", entity_type="ModelVersion", entity_id=9)

    def test_returns_newest_first(self, session):
        am = AuditManager(session)
        self._seed(am)
        entries = am.list_entries()
        assert [e.action for e in entries] == [
            "MODEL_PROMOTED", "SCHEDULE_DELETED", "SCHEDULE_CREATED",
        ]

    def test_filters_by_entity_type(self, session):
        am = AuditManager(session)
        self._seed(am)
        entries = am.list_entries(entity_type="ModelVersion")
        assert len(entries) == 1
        assert entries[0].action == "MODEL_PROMOTED"

    def test_filters_by_entity_id(self, session):
        am = AuditManager(session)
        self._seed(am)
        entries = am.list_entries(entity_type="Schedule", entity_id=1)
        assert len(entries) == 2

    def test_filters_by_action(self, session):
        am = AuditManager(session)
        self._seed(am)
        entries = am.list_entries(action="SCHEDULE_CREATED")
        assert len(entries) == 1

    def test_respects_limit(self, session):
        am = AuditManager(session)
        self._seed(am)
        entries = am.list_entries(limit=1)
        assert len(entries) == 1
        assert entries[0].action == "MODEL_PROMOTED"

    def test_empty_when_nothing_recorded(self, session):
        am = AuditManager(session)
        assert am.list_entries() == []
