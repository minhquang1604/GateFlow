"""ServingInstance ORM entity.

Records the (logical) serving instances that have loaded a particular
ModelVersion. Updated by the serving bridge when a reload succeeds.
"""

from sqlalchemy import (
    Boolean,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mlops_framework.database.base import Base, TimestampMixin


class ServingInstance(Base, TimestampMixin):
    """A logical serving instance for a model.

    One row per (serving_instance_id, model_version_id) reload. The
    ``is_active`` flag distinguishes the *currently loaded* version
    from older ones that have been replaced.
    """

    __tablename__ = "serving_instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    serving_instance_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reload_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
