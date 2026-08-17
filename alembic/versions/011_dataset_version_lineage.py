"""Add dataset_versions.parent_version_id — dataset-to-dataset lineage.

Revision ID: 011_dataset_version_lineage
Revises: 010_api_keys
Create Date: 2026-08-16

Until now lineage started at a DatasetVersion and walked *forward*
(version -> training run -> model version -> serving). A version's own
ancestry was not modelled at all, so a version built by extending an
earlier one — the retraining case, where V2 is V1 plus the production
data that drifted — was indistinguishable from a version that arrived
from nowhere. "Why does this model exist?" could be answered only as
far back as the data it trained on, never to the data that *caused*
that data.

Self-referencing and nullable: a version with no parent is the normal
case (the first version of any dataset, and every version registered by
existing callers), so this is additive and non-breaking. ``ondelete`` is
SET NULL rather than CASCADE — losing an ancestor should orphan the
lineage edge, never delete the descendant version and, through the
existing CASCADE on dataset_versions -> training_runs, the runs that
trained on it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "011_dataset_version_lineage"
down_revision: str | None = "010_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table so this also applies on SQLite, which cannot ALTER
    # a table to add a constraint in place — the local/dev path and the
    # test suite both run on SQLite.
    with op.batch_alter_table("dataset_versions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("parent_version_id", sa.Integer(), nullable=True)
        )
        batch_op.create_index(
            "ix_dataset_versions_parent_version_id",
            ["parent_version_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_dataset_versions_parent_version_id",
            "dataset_versions",
            ["parent_version_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("dataset_versions", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_dataset_versions_parent_version_id", type_="foreignkey"
        )
        batch_op.drop_index("ix_dataset_versions_parent_version_id")
        batch_op.drop_column("parent_version_id")
