"""Add retraining_decisions — the governed retraining attempt as a record.

Revision ID: 012_retraining_decisions
Revises: 011_dataset_version_lineage
Create Date: 2026-08-21

Readiness and drift each wrote an auditable row; eligibility and human
approval did not. Eligibility reached the database only when it *failed*
(as a RunBlockedEvent) and approval only as an AuditLog row keyed by
entity id, so the framework could show why a retrain was refused but not
why one was permitted. The workflow's own five-gate step trace —
RetrainingOutcome.steps — was never stored at all.

This table records one row per RetrainingWorkflow execution, referencing
the readiness and drift rows that already exist rather than copying
them, and carrying the trace plus the denormalized gate verdicts needed
to query it.

Purely additive: no existing table or column is touched, and nothing
reads this table unless it is present, so an older deployment upgrades
without a backfill. Rows written before this migration have no decision
record and never will — the trace they would have carried was already
discarded at the time, and inventing one now would be fabricating
provenance, which is the opposite of the point.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "012_retraining_decisions"
down_revision: str | None = "011_dataset_version_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retraining_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_version_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=True),
        sa.Column("readiness_evaluation_id", sa.Integer(), nullable=True),
        sa.Column("drift_evaluation_id", sa.Integer(), nullable=True),
        sa.Column("training_run_id", sa.Integer(), nullable=True),
        sa.Column("model_version_id", sa.Integer(), nullable=True),
        sa.Column("promotion_event_id", sa.Integer(), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "PROMOTED",
                "BLOCKED",
                "COMPLETED",
                name="retraining_outcome_enum",
            ),
            nullable=False,
        ),
        sa.Column("blocked_at_step", sa.String(length=32), nullable=True),
        sa.Column("blocked_reason", sa.String(length=64), nullable=True),
        sa.Column("eligible", sa.Boolean(), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=True),
        sa.Column("approval_responder", sa.String(length=255), nullable=True),
        sa.Column("approval_reason", sa.Text(), nullable=True),
        sa.Column("steps_json", sa.Text(), nullable=True),
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
        # CASCADE on the two things the decision is *about*: a decision
        # about a deleted dataset version or a deleted model is not a
        # record of anything. SET NULL on everything the decision
        # *authorised* — losing the run must orphan the edge, never
        # delete the evidence that the decision was taken.
        sa.ForeignKeyConstraint(
            ["dataset_version_id"], ["dataset_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["readiness_evaluation_id"],
            ["readiness_evaluations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["drift_evaluation_id"],
            ["drift_evaluations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["training_run_id"], ["training_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"], ["model_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["promotion_event_id"],
            ["model_promotion_events.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_retraining_decisions_dataset_version_id",
        "retraining_decisions",
        ["dataset_version_id"],
    )
    op.create_index(
        "ix_retraining_decisions_model_id",
        "retraining_decisions",
        ["model_id"],
    )
    op.create_index(
        "ix_retraining_decisions_training_run_id",
        "retraining_decisions",
        ["training_run_id"],
    )
    op.create_index(
        "ix_retraining_decisions_model_version_id",
        "retraining_decisions",
        ["model_version_id"],
    )
    op.create_index(
        "ix_retraining_decisions_outcome",
        "retraining_decisions",
        ["outcome"],
    )
    op.create_index(
        "ix_retraining_decisions_blocked_at_step",
        "retraining_decisions",
        ["blocked_at_step"],
    )
    op.create_index(
        "ix_retraining_decisions_blocked_reason",
        "retraining_decisions",
        ["blocked_reason"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_retraining_decisions_blocked_reason", table_name="retraining_decisions"
    )
    op.drop_index(
        "ix_retraining_decisions_blocked_at_step", table_name="retraining_decisions"
    )
    op.drop_index(
        "ix_retraining_decisions_outcome", table_name="retraining_decisions"
    )
    op.drop_index(
        "ix_retraining_decisions_model_version_id",
        table_name="retraining_decisions",
    )
    op.drop_index(
        "ix_retraining_decisions_training_run_id", table_name="retraining_decisions"
    )
    op.drop_index(
        "ix_retraining_decisions_model_id", table_name="retraining_decisions"
    )
    op.drop_index(
        "ix_retraining_decisions_dataset_version_id",
        table_name="retraining_decisions",
    )
    op.drop_table("retraining_decisions")
    # Postgres creates a named type for the enum and does not drop it with
    # the table; SQLite has no such type and needs no cleanup.
    sa.Enum(name="retraining_outcome_enum").drop(op.get_bind(), checkfirst=True)
