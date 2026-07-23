"""TrainingRun ORM model."""

from sqlalchemy import String, Text, Integer, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
import enum

from mlops_framework.database.base import Base, TimestampMixin


class RunStatus(str, enum.Enum):
    """Status of a training run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TriggerType(str, enum.Enum):
    """Trigger type for a training run."""

    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"
    DRIFT = "DRIFT"
    API = "API"


class TrainingRun(Base, TimestampMixin):
    """Represents a single training execution.

    A TrainingRun is linked to a specific DatasetVersion, ensuring
    reproducibility of experiments.
    """

    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        SQLEnum(RunStatus, name="run_status_enum"),
        nullable=False,
        default=RunStatus.PENDING,
    )
    trigger_type: Mapped[str] = mapped_column(
        SQLEnum(TriggerType, name="trigger_type_enum"),
        nullable=False,
        default=TriggerType.MANUAL,
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationship to DatasetVersion
    dataset_version: Mapped["DatasetVersion"] = relationship(
        "DatasetVersion",
        back_populates="training_runs",
    )

    def __repr__(self) -> str:
        return f"<TrainingRun(id={self.id}, status={self.status}, dataset_version_id={self.dataset_version_id})>"
