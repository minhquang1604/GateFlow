"""Add governance_events table.

Revision ID: 008_governance_events
Revises: 007_audit_log
Create Date: 2026-08-13

Adds ``governance_events`` — a persisted feed of conditions the
framework itself detects (training failed, drift detected, a retrain
blocked before it started), surfaced on Gateflow's Activity page
alongside ``audit_logs``. See ``database/models/governance_event.py``
for why this is one polymorphic table rather than one per event kind.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "008_governance_events"
down_revision: Union[str, None] = "007_audit_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "governance_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "severity",
            sa.Enum("INFO", "WARNING", "CRITICAL", name="governance_event_severity_enum"),
            nullable=False,
            server_default="WARNING",
        ),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
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
    )
    op.create_index("ix_governance_events_event_type", "governance_events", ["event_type"])
    op.create_index("ix_governance_events_entity_id", "governance_events", ["entity_id"])


def downgrade() -> None:
    op.drop_table("governance_events")
    sa.Enum(name="governance_event_severity_enum").drop(op.get_bind(), checkfirst=True)
