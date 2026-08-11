"""DriftEvaluation ORM entity.

Persists the result of a :class:`DriftDetector` evaluation so that
operators can see *when* drift was observed, *which* dataset versions
were compared, and *what* the framework decided.
"""

import enum

from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy import (
    Float,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mlops_framework.database.base import Base, TimestampMixin


class DriftOutcome(str, enum.Enum):
    """Result of a drift evaluation."""

    DRIFT_DETECTED = "DRIFT_DETECTED"
    NO_DRIFT = "NO_DRIFT"
    INCONCLUSIVE = "INCONCLUSIVE"


class DriftEvaluation(Base, TimestampMixin):
    """An auditable drift evaluation.

    Captures the reference/current dataset version pair, the method
    that was used, the headline score, and the framework's normalized
    verdict.
    """

    __tablename__ = "drift_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference_dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    current_dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(
        SQLEnum(DriftOutcome, name="drift_outcome_enum"),
        nullable=False,
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-feature results JSON (e.g. {feature: {method, score, drift_detected}}).
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
