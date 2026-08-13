"""AuditLog ORM entity.

An append-only record of "who did what" for the small set of actions
Gateflow can trigger against the framework's own data — schedule
create/update/delete/run-now, a model promotion decision (approved or
rejected), whichever path it came through (a human via the console, the
Airflow DAG's callback, or ``RetrainingWorkflow`` running unattended).

There is still no auth layer anywhere in this app (see
``api/security.py``'s module docstring) — ``actor`` is free text, read
from an optional ``X-Actor`` request header when one is present
(``api/deps.py::get_actor``) and ``"system"`` otherwise. This is
attribution on the honour system, not identity verification; it answers
"what does the caller say happened", which is still strictly more than
today's nothing.
"""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mlops_framework.database.base import Base, TimestampMixin


class AuditLog(Base, TimestampMixin):
    """One recorded action. Rows are never updated or deleted by the
    framework itself — ``updated_at`` (from ``TimestampMixin``) is
    carried for schema consistency with every other table, not because
    an audit row is expected to change after it's written."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="system")
    # Screaming-snake-case, matching ModelPromotionEvent.event_type's
    # convention (e.g. "SCHEDULE_CREATED", "MODEL_PROMOTED") — not an
    # enum: this table's whole point is to keep recording new action
    # kinds as they're added at call sites, without a migration each time.
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # What the action was about — "Schedule", "ModelVersion", etc. — and
    # its id, when the action has one. No FK: entity_type varies per row,
    # and the entity may since have been deleted; an audit row must
    # survive that.
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Free-form JSON blob: whatever detail the call site thought was
    # worth keeping (a cron expression, promotion-policy reasons, ...).
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
