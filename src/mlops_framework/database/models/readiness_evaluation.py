"""ReadinessEvaluation ORM entity.

A ReadinessEvaluation is an auditable record of a single dataset-readiness
decision. It is created by the :class:`ReadinessEngine` and persisted so
that operators can see *why* a dataset version was marked READY or
BLOCKED.
"""

from sqlalchemy import (
    String,
    Text,
    Integer,
    ForeignKey,
    Enum as SQLEnum,
    JSON,
)
from sqlalchemy.orm import Mapped, mapped_column
import enum

from mlops_framework.database.base import Base, TimestampMixin


class ReadinessStatus(str, enum.Enum):
    """Result of a readiness evaluation."""

    READY = "READY"
    BLOCKED = "BLOCKED"


class ReadinessCheckOutcome(str, enum.Enum):
    """Outcome of a single readiness check."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ReadinessEvaluation(Base, TimestampMixin):
    """An auditable dataset-readiness evaluation.

    A row is created every time the :class:`ReadinessEngine` evaluates a
    :class:`DatasetVersion`. The evaluation is immutable after creation
    — subsequent evaluations create new rows, so history is preserved.
    """

    __tablename__ = "readiness_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        SQLEnum(ReadinessStatus, name="readiness_status_enum"),
        nullable=False,
    )
    # JSON blob: per-check outcome, e.g.
    #   {"size": "PASSED", "freshness": "FAILED", ...}
    checks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON blob: machine-readable reasons (strings).
    reasons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON blob: the policy that was applied.
    policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional structured snapshot of the dataset fields the engine read.
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Total row count snapshot (denormalized for reporting).
    observed_row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
