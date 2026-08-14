"""Add api_keys table.

Revision ID: 010_api_keys
Revises: 009_framework_settings
Create Date: 2026-08-14

Gives the framework its first per-principal identity. Until now there
was one shared secret (``CONSOLE_WRITE_TOKEN``) plus a self-declared
``X-Actor`` header, so an audit row recorded whatever the caller
claimed. See ``database/models/api_key.py`` for why the row stores a
hash and why revocation is a timestamp rather than a delete.

Additive and non-breaking: the shared secret keeps working (see
``api/security.py``), so an existing deployment is unaffected until
someone mints a key.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "010_api_keys"
down_revision: str | None = "009_framework_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("name", name="uq_api_keys_name"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    # Every authenticated request resolves a key by exactly this column.
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])
    op.create_index("ix_api_keys_name", "api_keys", ["name"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_name", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
