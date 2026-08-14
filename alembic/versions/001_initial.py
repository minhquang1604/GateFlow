"""Initial migration - create core tables.

Revision ID: 001_initial
Revises:
Create Date: 2026-07-23

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create datasets table
    op.create_table(
        "datasets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_datasets_name"), "datasets", ["name"], unique=True)

    # Create dataset_versions table
    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("storage_uri", sa.String(512), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("schema_hash", sa.String(64), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("is_immutable", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_dataset_versions_dataset_id"), "dataset_versions", ["dataset_id"])

    # Create training_runs table
    op.create_table(
        "training_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_version_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "RUNNING", "SUCCESS", "FAILED", "CANCELLED", name="run_status_enum"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "trigger_type",
            sa.Enum("MANUAL", "SCHEDULED", "DRIFT", "API", name="trigger_type_enum"),
            nullable=False,
            server_default="MANUAL",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["dataset_version_id"], ["dataset_versions.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_training_runs_dataset_version_id"), "training_runs", ["dataset_version_id"])


def downgrade() -> None:
    op.drop_table("training_runs")
    op.drop_table("dataset_versions")
    op.drop_table("datasets")
