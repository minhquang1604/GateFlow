"""ModelPromotionEvent ORM entity.

When a :class:`ModelManager` transitions a ModelVersion to PRODUCTION,
the framework publishes a promotion event for downstream consumers
(serving bridge, notification, audit, etc.). Every event is also
persisted to the database so the framework can replay and audit.
"""

from sqlalchemy import (
    String,
    Text,
    Integer,
    ForeignKey,
    Enum as SQLEnum,
)
from sqlalchemy.orm import Mapped, mapped_column
import enum

from mlops_framework.database.base import Base, TimestampMixin


class ModelPromotionStatus(str, enum.Enum):
    """Status of a model-promotion event in the framework."""

    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class ModelPromotionEvent(Base, TimestampMixin):
    """A persisted model-promotion event.

    ``event_type`` is the framework-level event type (e.g.
    ``"MODEL_PROMOTED"``). The DB row records the event *and* whether
    downstream publishing succeeded.
    """

    __tablename__ = "model_promotion_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="MODEL_PROMOTED"
    )
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_version_id: Mapped[int] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        SQLEnum(ModelPromotionStatus, name="model_promotion_status_enum"),
        nullable=False,
        default=ModelPromotionStatus.PENDING,
    )
    published_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
