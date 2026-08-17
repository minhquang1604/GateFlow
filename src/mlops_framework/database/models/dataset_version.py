"""DatasetVersion ORM model."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mlops_framework.database.base import Base, TimestampMixin

if TYPE_CHECKING:
    from mlops_framework.database.models.dataset import Dataset
    from mlops_framework.database.models.training_run import TrainingRun


class DatasetVersion(Base, TimestampMixin):
    """Represents an immutable snapshot of a dataset.

    DatasetVersion is an immutable snapshot of data at a specific point in time.
    Once created, its content cannot be modified - this ensures reproducibility
    and traceability of training runs.

    Key attributes:
        - version_number: Sequential integer, unique within a Dataset
        - checksum: SHA-256 hash of the underlying data content
        - schema_hash: Deterministic hash of the data schema (columns, types, order)
        - row_count: Number of rows in this version
        - is_immutable: Always True, enforced at application level
        - parent_version_id: The version this one was *derived from*, if any

    ``parent_version_id`` is what distinguishes "a new version of this
    dataset" from "a new version built by extending an earlier one". The
    retraining loop produces the latter: V2 is V1 plus the production
    data that drifted, and recording that lets
    :class:`~mlops_framework.lineage.manager.LineageManager` answer why a
    model trained on V2 exists at all. ``None`` — the common case — means
    the version stands on its own.
    """

    __tablename__ = "dataset_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 = 64 hex chars
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 = 64 hex chars
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_immutable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # SET NULL, not CASCADE — see migration 011's docstring: losing an
    # ancestor must orphan the edge, never delete the descendant.
    parent_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Relationship to Dataset
    dataset: Mapped["Dataset"] = relationship(
        "Dataset",
        back_populates="versions",
    )

    # Self-referencing lineage. remote_side pins which end of the join is
    # the "one" side — without it SQLAlchemy cannot tell a self-join's
    # direction and treats both ends as collections.
    parent: Mapped["DatasetVersion | None"] = relationship(
        "DatasetVersion",
        remote_side=[id],
        back_populates="derived_versions",
    )
    derived_versions: Mapped[list["DatasetVersion"]] = relationship(
        "DatasetVersion",
        back_populates="parent",
    )

    # Relationship to TrainingRuns
    training_runs: Mapped[list["TrainingRun"]] = relationship(
        "TrainingRun",
        back_populates="dataset_version",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<DatasetVersion(id={self.id}, dataset_id={self.dataset_id}, version={self.version_number})>"
