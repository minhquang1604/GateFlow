"""GovernanceEvent ORM entity.

Where :class:`~mlops_framework.database.models.audit_log.AuditLog`
records actions someone (or something) *triggered*, this records
conditions the framework itself *detected* — a training run failing, a
dataset version drifting, a retrain getting blocked before it started.
Gateflow's Activity page surfaces both, on separate tabs (see
``ui/templates/activity.html``).

One table for every event kind, not one per kind — unlike
:class:`~mlops_framework.database.models.model_promotion_event.ModelPromotionEvent`,
which predates this and stays as its own table for backward
compatibility. ``event_type`` + ``payload_json`` is deliberately
polymorphic: a new kind of governance event a future call site wants to
raise needs a new ``events/publisher.py`` dataclass, not a new
migration.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy import (
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mlops_framework.database.base import Base, TimestampMixin


class GovernanceEventSeverity(str, enum.Enum):
    """How loudly the Activity page's Alerts tab should treat this row."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class GovernanceEvent(Base, TimestampMixin):
    """One detected governance condition. Append-only, like AuditLog."""

    __tablename__ = "governance_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    # e.g. "TRAINING_FAILED" / "DRIFT_DETECTED" / "RUN_BLOCKED" — matches
    # the corresponding Event dataclass's event_type in events/publisher.py.
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(
        SQLEnum(GovernanceEventSeverity, name="governance_event_severity_enum"),
        nullable=False,
        default=GovernanceEventSeverity.WARNING,
    )
    # What the event was about — "TrainingRun", "DatasetVersion", "Model"
    # — and its id. No FK, same reasoning as AuditLog: the entity_type
    # varies per row, and the entity may since have been deleted.
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # A short human-readable summary — unlike AuditLog, this table's
    # whole point is a scannable alert feed, so the call site's own
    # phrasing is worth keeping rather than reconstructing it from
    # payload_json at render time.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
