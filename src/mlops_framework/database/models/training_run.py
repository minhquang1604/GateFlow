"""TrainingRun ORM model."""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mlops_framework.database.base import Base, TimestampMixin

if TYPE_CHECKING:
    from mlops_framework.database.models.dataset_version import DatasetVersion


class RunStatus(str, enum.Enum):
    """Status of a training run.

    Lifecycle:
        PENDING  -> RUNNING -> SUCCESS
                          -> FAILED
                          -> CANCELLED
        PENDING  -> CANCELLED

    SUCCESS / FAILED / CANCELLED are terminal states.
    """

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


# Strict status transition map. The framework owns the lifecycle — the
# orchestrator must go through TrainingManager methods, not bypass it.
VALID_STATUS_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PENDING: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.SUCCESS: set(),  # terminal
    RunStatus.FAILED: set(),  # terminal
    RunStatus.CANCELLED: set(),  # terminal
}


class TrainingRun(Base, TimestampMixin):
    """Represents a single training execution.

    A TrainingRun is linked to a specific DatasetVersion, ensuring
    reproducibility of experiments. It can also be linked to a Pipeline
    (nullable until the pipeline entity exists) and to an MLflow run for
    experiment tracking.
    """

    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # pipeline_id is a free-form string (the orchestrator's pipeline/DAG
    # identifier). Nullable because pipeline as a framework concept is not
    # implemented yet in Week 2.
    pipeline_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
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
    # MLflow run ID is set when the run is started through an
    # MLflowTracker. Nullable so that the framework can run without MLflow.
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationship to DatasetVersion
    dataset_version: Mapped["DatasetVersion"] = relationship(
        "DatasetVersion",
        back_populates="training_runs",
    )

    def __repr__(self) -> str:
        return (
            f"<TrainingRun(id={self.id}, status={self.status}, "
            f"dataset_version_id={self.dataset_version_id})>"
        )
