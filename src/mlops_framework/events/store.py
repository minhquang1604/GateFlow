"""GovernanceEventStore: persist/list GovernanceEvent rows.

Deliberately independent of :class:`~mlops_framework.events.publisher.EventPublisher`:
persistence here always happens, whether or not a webhook publisher is
configured — that is the entire point of ``governance_events`` existing
(Gateflow's Alerts tab needs something to show even with no webhook set
up). A caller that also wants webhook fan-out calls
``EventPublisher.publish(event)`` itself alongside ``record()``; this
store does not do that for them.

Same "never raises" contract as ``audit/manager.py::AuditManager`` — a
write failing here must not take down the training/drift/readiness
decision it is recording — and, for the same reason, the same SAVEPOINT
around the insert. Swallowing the exception alone leaves the session in
a rolled-back state, so the caller's own ``commit()`` fails with
``PendingRollbackError`` and the decision this was recording is lost
anyway. See that module's docstring for the full reasoning.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.database.models.governance_event import (
    GovernanceEvent,
    GovernanceEventSeverity,
)
from mlops_framework.events.publisher import Event

_log = logging.getLogger("mlops_framework.events.store")


class GovernanceEventStore:
    """Manages GovernanceEvent entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        event: Event,
        *,
        message: str,
        severity: GovernanceEventSeverity = GovernanceEventSeverity.WARNING,
        entity_type: str | None = None,
        entity_id: int | None = None,
    ) -> GovernanceEvent | None:
        """Persist one governance event. Returns ``None`` (never raises)
        if the write itself fails — see the module docstring."""
        try:
            row = GovernanceEvent(
                event_type=event.event_type,
                severity=severity,
                entity_type=entity_type,
                entity_id=entity_id,
                message=message,
                payload_json=json.dumps(event.payload) if event.payload else None,
            )
            # SAVEPOINT — see the module docstring and AuditManager.record.
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
            return row
        except Exception:  # noqa: BLE001 - never fail the caller's real action
            _log.exception(
                "failed to record governance event event_type=%s", event.event_type
            )
            return None

    def list_entries(
        self,
        *,
        event_type: str | None = None,
        severity: str | None = None,
        entity_type: str | None = None,
        entity_id: int | None = None,
        limit: int = 100,
    ) -> list[GovernanceEvent]:
        stmt = select(GovernanceEvent).order_by(GovernanceEvent.id.desc()).limit(limit)
        if event_type is not None:
            stmt = stmt.where(GovernanceEvent.event_type == event_type)
        if severity is not None:
            stmt = stmt.where(GovernanceEvent.severity == severity)
        if entity_type is not None:
            stmt = stmt.where(GovernanceEvent.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(GovernanceEvent.entity_id == entity_id)
        return list(self._session.execute(stmt).scalars().all())
