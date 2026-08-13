"""Add Week 3 governance tables.

Revision ID: 004_week3_governance
Revises: 003_models
Create Date: 2026-07-25

Adds the tables and enums required for Week 3:

    * readiness_evaluations
        Persisted, auditable record of a ReadinessEngine evaluation.

    * drift_evaluations
        Persisted record of a DriftDetector evaluation.

    * model_promotion_events
        Framework-level "model promoted" events used to notify
        downstream consumers (serving bridge, etc.).

    * serving_instances
        Per-instance record of which ModelVersion a serving process
        is currently serving.

Each table is auditable (created_at / updated_at via TimestampMixin)
and the new enums (readiness_status_enum, drift_outcome_enum,
model_promotion_status_enum) are created in the same migration.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "004_week3_governance"
down_revision: Union[str, None] = "003_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # readiness_evaluations
    # ------------------------------------------------------------------ #
    readiness_status_enum = sa.Enum(
        "READY", "BLOCKED", name="readiness_status_enum"
    )
    op.create_table(
        "readiness_evaluations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "dataset_version_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("status", readiness_status_enum, nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=True),
        sa.Column("reasons_json", sa.Text(), nullable=True),
        sa.Column("policy_json", sa.Text(), nullable=True),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("observed_row_count", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["dataset_versions.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_readiness_evaluations_dataset_version_id",
        "readiness_evaluations",
        ["dataset_version_id"],
    )

    # ------------------------------------------------------------------ #
    # drift_evaluations
    # ------------------------------------------------------------------ #
    drift_outcome_enum = sa.Enum(
        "DRIFT_DETECTED", "NO_DRIFT", "INCONCLUSIVE", name="drift_outcome_enum"
    )
    op.create_table(
        "drift_evaluations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reference_dataset_version_id", sa.Integer(), nullable=False),
        sa.Column("current_dataset_version_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("outcome", drift_outcome_enum, nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["reference_dataset_version_id"],
            ["dataset_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["current_dataset_version_id"],
            ["dataset_versions.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_drift_evaluations_reference_dataset_version_id",
        "drift_evaluations",
        ["reference_dataset_version_id"],
    )
    op.create_index(
        "ix_drift_evaluations_current_dataset_version_id",
        "drift_evaluations",
        ["current_dataset_version_id"],
    )

    # ------------------------------------------------------------------ #
    # model_promotion_events
    # ------------------------------------------------------------------ #
    promotion_status_enum = sa.Enum(
        "PENDING", "PUBLISHED", "FAILED", name="model_promotion_status_enum"
    )
    op.create_table(
        "model_promotion_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "event_type",
            sa.String(length=64),
            nullable=False,
            server_default="MODEL_PROMOTED",
        ),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("model_version_number", sa.Integer(), nullable=False),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.Column(
            "status",
            promotion_status_enum,
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("published_at", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["model_id"], ["models.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"], ["model_versions.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_model_promotion_events_model_id",
        "model_promotion_events",
        ["model_id"],
    )
    op.create_index(
        "ix_model_promotion_events_model_version_id",
        "model_promotion_events",
        ["model_version_id"],
    )

    # ------------------------------------------------------------------ #
    # serving_instances
    # ------------------------------------------------------------------ #
    op.create_table(
        "serving_instances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("serving_instance_id", sa.String(length=128), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.Integer(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("reload_source", sa.String(length=64), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["model_id"], ["models.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"], ["model_versions.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_serving_instances_serving_instance_id",
        "serving_instances",
        ["serving_instance_id"],
    )
    op.create_index(
        "ix_serving_instances_model_id", "serving_instances", ["model_id"]
    )
    op.create_index(
        "ix_serving_instances_model_version_id",
        "serving_instances",
        ["model_version_id"],
    )


def downgrade() -> None:
    op.drop_table("serving_instances")
    op.drop_table("model_promotion_events")
    sa.Enum(name="model_promotion_status_enum").drop(op.get_bind(), checkfirst=True)
    op.drop_table("drift_evaluations")
    sa.Enum(name="drift_outcome_enum").drop(op.get_bind(), checkfirst=True)
    op.drop_table("readiness_evaluations")
    sa.Enum(name="readiness_status_enum").drop(op.get_bind(), checkfirst=True)
