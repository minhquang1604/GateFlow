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

Why the write goes inside a SAVEPOINT
-------------------------------------
Swallowing the exception is not on its own enough to keep that promise.
A failure raised by ``flush()`` — a constraint violation, a bad column
value — leaves the *session* in a rolled-back state, so the caller's
next statement (usually the ``commit()`` in ``api/deps.py``'s
``get_db``) dies with ``PendingRollbackError``. The promotion this was
recording is then lost anyway, which is precisely the outcome the
contract above exists to prevent.

``session.rollback()`` in the handler is not the fix either: it would
discard the caller's own uncommitted work and let the request return
2xx over a database that never saw the promotion — silent data loss in
place of a loud failure.

So the insert runs inside ``begin_nested()``. On failure SQLAlchemy
rolls back to the savepoint only: the audit row is discarded, the
caller's transaction is untouched and still usable, and the action goes
through with one missing audit row — the documented degradation. See
``tests/unit/test_audit.py``'s ``TestFailureIsolation``.
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
            # SAVEPOINT, so a failed insert rolls back only this row and
            # leaves the caller's transaction usable — see the module
            # docstring. json.dumps above is deliberately outside it:
            # that failure happens before any SQL and needs no unwinding.
            with self._session.begin_nested():
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
