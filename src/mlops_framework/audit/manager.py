"""AuditManager: append/list AuditLog rows.

Lives under its own top-level package (not ``api/``) for the same reason
``mlops_framework.tracking.mlflow_registry`` does — ``RetrainingWorkflow``
(``workflow/retraining.py``) needs it directly and must not import
``mlops_framework.api``, which would pull in the whole FastAPI app just
to reach one manager.

Every call is best-effort: ``record()`` never raises. An audit write
failing must not take down the promotion/schedule action it is
recording — the framework's own state change is what happened; a
missed audit row is a degraded record of it, not a reason to fail the
action itself. Same design contract as ``mlflow_registry``'s "nothing
here ever raises".
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.database.models.audit_log import AuditLog

_log = logging.getLogger("mlops_framework.audit")


class AuditManager:
    """Manages AuditLog entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        actor: str | None,
        action: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditLog | None:
        """Persist one audit row. Returns ``None`` (never raises) if the
        write itself fails — see the module docstring."""
        try:
            entry = AuditLog(
                actor=actor or "system",
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata_json=json.dumps(metadata) if metadata else None,
            )
            self._session.add(entry)
            self._session.flush()
            return entry
        except Exception:  # noqa: BLE001 - never fail the caller's real action
            _log.exception("failed to record audit log entry action=%s", action)
            return None

    def list_entries(
        self,
        *,
        entity_type: str | None = None,
        entity_id: int | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if entity_type is not None:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        return list(self._session.execute(stmt).scalars().all())
