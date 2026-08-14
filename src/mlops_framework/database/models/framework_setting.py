"""FrameworkSetting ORM entity.

A generic key/value store for the small set of governance dataclasses
(``PromotionConfig``, ``EligibilityConfig``, ``TrainingPolicy``,
``DriftConfig``) that already round-trip through ``from_dict``/``to_dict``
but, until now, only ever existed as hardcoded literals at their call
sites (``scheduling/runner.py``, ``api/routers/internal.py``) — there was
no way to change a threshold without editing source and redeploying.

One polymorphic table rather than one per policy, same reasoning as
``AuditLog.metadata_json``/``GovernanceEvent.payload_json``: there will
only ever be a handful of rows, and a dataclass gaining or losing a field
needs zero schema change this way, since the shape is validated by the
dataclass's own ``from_dict``, not by SQL columns. See
``mlops_framework.framework_settings.manager.FrameworkSettingsManager``.
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mlops_framework.database.base import Base, TimestampMixin


class FrameworkSetting(Base, TimestampMixin):
    """One persisted override — ``key`` is one of
    ``framework_settings.manager.PROMOTION`` / ``ELIGIBILITY`` /
    ``TRAINING_POLICY`` / ``DRIFT``; ``value_json`` is that policy
    dataclass's ``to_dict()``, serialized. A key with no row here means
    "use the dataclass's own bare default" — rows are only created once
    something is actually customized, never pre-seeded."""

    __tablename__ = "framework_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<FrameworkSetting(id={self.id}, key='{self.key}')>"
