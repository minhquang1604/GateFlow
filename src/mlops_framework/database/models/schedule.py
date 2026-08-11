"""Schedule ORM model.

A Schedule is the framework's own record of "train this model again on
a cron cadence, regardless of drift" — the counterpart to drift-gated
retraining (``workflow.retraining.RetrainingWorkflow`` triggered by
``DriftService``) rather than a replacement for it. Both funnel through
the same workflow; only what triggers them differs, which is exactly
what ``TrainingRun.trigger_type`` already distinguishes (``SCHEDULED``
vs ``DRIFT`` vs ``MANUAL``) — see ``scheduling/runner.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mlops_framework.database.base import Base, TimestampMixin


class Schedule(Base, TimestampMixin):
    """A recurring "train this model on the latest dataset version" job.

    ``cron_expression`` is a standard 5-field cron string (minute hour
    day month day-of-week), interpreted by ``croniter`` — see
    ``scheduling/cron.py``. The dataset version to train on is resolved
    at fire time (``DatasetManager.get_latest_version``), not pinned
    here, so a schedule always trains on whatever is newest rather than
    going stale the moment a new version is registered.
    """

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # module:callable — same convention as TrainingRun.metadata_json's
    # "training_entrypoint" / the demo scripts' PIPELINE_ID.
    pipeline_id: Mapped[str] = mapped_column(String(255), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Training hyperparameters, forwarded to the pipeline the same way
    # TrainingRun.metadata_json["parameters"] does. JSON-encoded rather
    # than a fixed column set: the pipeline is arbitrary, so its
    # parameter shape can't be schematized here.
    parameters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_f1: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_triggered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_training_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("training_runs.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    model = relationship("Model")
    dataset = relationship("Dataset")
    last_training_run = relationship("TrainingRun")

    def __repr__(self) -> str:
        return (
            f"<Schedule(id={self.id}, model_id={self.model_id}, "
            f"cron='{self.cron_expression}', enabled={self.enabled})>"
        )
