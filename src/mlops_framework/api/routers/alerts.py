"""Alerts router — read-only view over GovernanceEvent rows.

Mirrors ``audit.py``'s shape exactly, one level over
:class:`~mlops_framework.events.store.GovernanceEventStore` instead of
:class:`~mlops_framework.audit.manager.AuditManager`. Writes happen at
the point a condition is detected — ``workflow/retraining.py`` and
``api/routers/internal.py`` — never here.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.events.store import GovernanceEventStore

router = APIRouter()


class AlertOut(BaseModel):
    id: int
    event_type: str
    severity: str
    entity_type: str | None = None
    entity_id: int | None = None
    message: str
    payload: dict[str, Any] | None = None
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> AlertOut:
        payload = None
        if row.payload_json:
            try:
                payload = json.loads(row.payload_json)
            except (TypeError, ValueError):
                payload = None
        return cls(
            id=row.id,
            event_type=row.event_type,
            severity=row.severity.value if hasattr(row.severity, "value") else row.severity,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            message=row.message,
            payload=payload,
            created_at=row.created_at.isoformat() if row.created_at else "",
        )


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    event_type: str | None = None,
    severity: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[AlertOut]:
    entries = GovernanceEventStore(db).list_entries(
        event_type=event_type,
        severity=severity,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
    )
    return [AlertOut.from_row(e) for e in entries]
