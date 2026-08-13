"""Audit router — read-only view over AuditLog rows.

Writes never happen here directly; every audited action records its own
row at the point it happens (``schedules.py``, ``internal.py``,
``workflow/retraining.py``) via ``AuditManager.record()``. This router
only answers "what happened" for the Activity page.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mlops_framework.api.deps import get_audit_manager
from mlops_framework.audit.manager import AuditManager

router = APIRouter()


class AuditLogOut(BaseModel):
    id: int
    actor: str
    action: str
    entity_type: str | None = None
    entity_id: int | None = None
    metadata: dict[str, Any] | None = None
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> AuditLogOut:
        metadata = None
        if row.metadata_json:
            try:
                metadata = json.loads(row.metadata_json)
            except (TypeError, ValueError):
                metadata = None
        return cls(
            id=row.id,
            actor=row.actor,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            metadata=metadata,
            created_at=row.created_at.isoformat() if row.created_at else "",
        )


@router.get("/audit", response_model=list[AuditLogOut])
def list_audit_log(
    entity_type: str | None = None,
    entity_id: int | None = None,
    action: str | None = None,
    limit: int = 100,
    am: AuditManager = Depends(get_audit_manager),
) -> list[AuditLogOut]:
    entries = am.list_entries(
        entity_type=entity_type, entity_id=entity_id, action=action, limit=limit
    )
    return [AuditLogOut.from_row(e) for e in entries]
