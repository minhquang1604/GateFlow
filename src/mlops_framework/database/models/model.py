"""Model ORM entity.

A Model is a logical container for one or more ModelVersions. Model
names are unique within the system.
"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mlops_framework.database.base import Base, TimestampMixin


class Model(Base, TimestampMixin):
    """A registered model in the MLOps Framework.

    A Model has a stable logical identity (its name) and one or more
    ModelVersion children. Promotion of a ModelVersion to PRODUCTION is
    a property of the version, not the model.
    """

    __tablename__ = "models"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form task label, e.g. "fraud_detection". Helps downstream
    # systems route the model without coupling to the model code.
    task: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    versions: Mapped[list["ModelVersion"]] = relationship(
        "ModelVersion",
        back_populates="model",
        cascade="all, delete-orphan",
        order_by="ModelVersion.version_number",
    )

    def __repr__(self) -> str:
        return f"<Model(id={self.id}, name='{self.name}', task='{self.task}')>"
