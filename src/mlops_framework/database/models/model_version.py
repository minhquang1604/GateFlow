"""ModelVersion ORM entity.

A ModelVersion is a concrete, registered version of a Model. It is
linked to:

* a Model (parent)
* a DatasetVersion (input data lineage)
* a TrainingRun (training lineage)
* an MLflow run id (experiment tracking)
* an artifact URI (where the model is stored)
* per-version metrics

Lifecycle:

    TRAINING  -> CANDIDATE -> APPROVED -> PRODUCTION -> ARCHIVED
                CANDIDATE -> REJECTED
"""

import enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mlops_framework.database.base import Base, TimestampMixin

if TYPE_CHECKING:
    from mlops_framework.database.models.dataset_version import DatasetVersion
    from mlops_framework.database.models.model import Model
    from mlops_framework.database.models.training_run import TrainingRun


class ModelState(str, enum.Enum):
    """Lifecycle states for a ModelVersion.

    Strict transition map (see ``VALID_MODEL_STATE_TRANSITIONS``).
    """

    TRAINING = "TRAINING"
    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    PRODUCTION = "PRODUCTION"
    ARCHIVED = "ARCHIVED"
    REJECTED = "REJECTED"


VALID_MODEL_STATE_TRANSITIONS: dict[ModelState, set[ModelState]] = {
    ModelState.TRAINING: {ModelState.CANDIDATE, ModelState.REJECTED},
    ModelState.CANDIDATE: {ModelState.APPROVED, ModelState.REJECTED, ModelState.PRODUCTION},
    ModelState.APPROVED: {ModelState.PRODUCTION, ModelState.ARCHIVED, ModelState.REJECTED},
    ModelState.PRODUCTION: {ModelState.ARCHIVED},
    ModelState.ARCHIVED: set(),  # terminal
    ModelState.REJECTED: set(),  # terminal
}


class ModelVersion(Base, TimestampMixin):
    """A concrete, versioned model artifact.

    Holds the lineage chain DatasetVersion -> TrainingRun -> ModelVersion
    and the state machine for promotion.
    """

    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    training_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("training_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        SQLEnum(ModelState, name="model_state_enum"),
        nullable=False,
        default=ModelState.TRAINING,
    )
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    artifact_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Metrics are stored as a JSON blob to remain schema-flexible
    # (f1, roc_auc, accuracy, etc.). The framework does not enforce a
    # metric vocabulary.
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    model: Mapped["Model"] = relationship("Model", back_populates="versions")
    dataset_version: Mapped["DatasetVersion"] = relationship("DatasetVersion")
    training_run: Mapped["TrainingRun | None"] = relationship("TrainingRun")

    def __repr__(self) -> str:
        return (
            f"<ModelVersion(id={self.id}, model_id={self.model_id}, "
            f"version={self.version_number}, state={self.state})>"
        )
