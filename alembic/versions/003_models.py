"""Add models and model_versions tables.

Revision ID: 003_models
Revises: 002_training_run_lifecycle
Create Date: 2026-07-23

Creates the Model / ModelVersion entities and the model_state_enum.
ModelVersion links to a DatasetVersion (lineage) and a TrainingRun
(lineage) and stores a free-form metrics JSON blob.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_models"
down_revision: str | None = "002_training_run_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    model_state_enum = sa.Enum(
        "TRAINING",
        "CANDIDATE",
        "APPROVED",
        "PRODUCTION",
        "ARCHIVED",
        "REJECTED",
        name="model_state_enum",
    )

    op.create_table(
        "models",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("task", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_models_name"),
    )
    op.create_index("ix_models_task", "models", ["task"])

    op.create_table(
        "model_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("dataset_version_id", sa.Integer(), nullable=False),
        sa.Column("training_run_id", sa.Integer(), nullable=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", model_state_enum, nullable=False),
        sa.Column("mlflow_run_id", sa.String(length=64), nullable=True),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["dataset_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["training_run_id"],
            ["training_runs.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "model_id", "version_number", name="uq_model_versions_model_version"
        ),
    )
    op.create_index("ix_model_versions_model_id", "model_versions", ["model_id"])
    op.create_index("ix_model_versions_dataset_version_id", "model_versions", ["dataset_version_id"])
    op.create_index("ix_model_versions_training_run_id", "model_versions", ["training_run_id"])
    op.create_index("ix_model_versions_mlflow_run_id", "model_versions", ["mlflow_run_id"])


def downgrade() -> None:
    op.drop_index("ix_model_versions_mlflow_run_id", table_name="model_versions")
    op.drop_index("ix_model_versions_training_run_id", table_name="model_versions")
    op.drop_index("ix_model_versions_dataset_version_id", table_name="model_versions")
    op.drop_index("ix_model_versions_model_id", table_name="model_versions")
    op.drop_table("model_versions")
    op.drop_index("ix_models_task", table_name="models")
    op.drop_table("models")
    sa.Enum(name="model_state_enum").drop(op.get_bind(), checkfirst=True)
