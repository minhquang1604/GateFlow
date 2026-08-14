"""Add framework_settings table.

Revision ID: 009_framework_settings
Revises: 008_governance_events
Create Date: 2026-08-14

Adds ``framework_settings`` — a generic key/value store for the
governance dataclasses (``PromotionConfig``, ``EligibilityConfig``,
``TrainingPolicy``, ``DriftConfig``) that until now only existed as
hardcoded literals at their call sites. See
``database/models/framework_setting.py`` for why this is one
polymorphic table rather than one per policy.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "009_framework_settings"
down_revision: Union[str, None] = "008_governance_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "framework_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
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
    op.create_index(
        op.f("ix_framework_settings_key"), "framework_settings", ["key"], unique=True
    )


def downgrade() -> None:
    op.drop_table("framework_settings")
