"""Dataset ORM model."""

from typing import TYPE_CHECKING

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mlops_framework.database.base import Base, TimestampMixin

if TYPE_CHECKING:
    from mlops_framework.database.models.dataset_version import DatasetVersion


class Dataset(Base, TimestampMixin):
    """Represents a logical dataset in the MLOps Framework.

    A Dataset is a logical container for one or more DatasetVersions.
    Dataset names must be unique within the system.
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationship to DatasetVersions
    versions: Mapped[list["DatasetVersion"]] = relationship(
        "DatasetVersion",
        back_populates="dataset",
        cascade="all, delete-orphan",
        order_by="DatasetVersion.version_number",
    )

    def __repr__(self) -> str:
        return f"<Dataset(id={self.id}, name='{self.name}')>"
