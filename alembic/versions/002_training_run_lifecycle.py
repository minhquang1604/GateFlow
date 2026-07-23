"""Add pipeline_id, mlflow_run_id, error_message to training_runs.

Revision ID: 002_training_run_lifecycle
Revises: 001_initial
Create Date: 2026-07-23

This migration extends the TrainingRun entity for the Week 2 training
lifecycle. The three new columns are:

    pipeline_id    VARCHAR(255) NULL  - orchestrator pipeline/DAG ID
    mlflow_run_id  VARCHAR(64)  NULL  - linked MLflow run identifier
    error_message  TEXT         NULL  - failure message captured on FAILED

All columns are nullable so that existing rows remain valid. The strict
status transition enforcement lives in application code (training.manager)
and is not duplicated at the database level.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "002_training_run_lifecycle"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "training_runs",
        sa.Column("pipeline_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "training_runs",
        sa.Column("mlflow_run_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "training_runs",
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_training_runs_pipeline_id",
        "training_runs",
        ["pipeline_id"],
    )
    op.create_index(
        "ix_training_runs_mlflow_run_id",
        "training_runs",
        ["mlflow_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_training_runs_mlflow_run_id", table_name="training_runs")
    op.drop_index("ix_training_runs_pipeline_id", table_name="training_runs")
    op.drop_column("training_runs", "error_message")
    op.drop_column("training_runs", "mlflow_run_id")
    op.drop_column("training_runs", "pipeline_id")
