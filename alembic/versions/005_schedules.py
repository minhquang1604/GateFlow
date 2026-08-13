"""Add schedules table.

Revision ID: 005_schedules
Revises: 004_week3_governance
Create Date: 2026-08-10

Adds ``schedules`` — the framework's own record of "train this model
again on a cron cadence" jobs, run by ``scheduling/runner.py``. See
``database/models/schedule.py`` for why the dataset version isn't
pinned here (resolved fresh at fire time) and why this reuses
``TrainingRun.trigger_type == SCHEDULED`` rather than adding a new
concept for the run itself.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "005_schedules"
down_revision: Union[str, None] = "004_week3_governance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("pipeline_id", sa.String(length=255), nullable=False),
        sa.Column("cron_expression", sa.String(length=128), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("parameters_json", sa.Text(), nullable=True),
        sa.Column(
            "min_f1", sa.Float(), nullable=False, server_default=sa.text("0.0")
        ),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_training_run_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_training_run_id"], ["training_runs.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_schedules_model_id", "schedules", ["model_id"])
    op.create_index("ix_schedules_dataset_id", "schedules", ["dataset_id"])


def downgrade() -> None:
    op.drop_table("schedules")
