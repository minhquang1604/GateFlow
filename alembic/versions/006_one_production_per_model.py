"""Enforce at most one PRODUCTION ModelVersion per Model.

Revision ID: 006_one_production_per_model
Revises: 005_schedules
Create Date: 2026-08-11

Nothing in the ORM layer stopped a concurrent writer from promoting two
ModelVersions of the same Model to PRODUCTION at once — application code
(RetrainingWorkflow, the internal promote route, the demo scripts)
always archives the prior production version around the promotion, but
that is a convention, not a guarantee. A partial unique index makes the
database itself the guarantee: at most one row per model_id can have
state = 'PRODUCTION'. Rows in every other state are unaffected — a
Model can have any number of CANDIDATE/APPROVED/ARCHIVED/REJECTED
versions.

Portable across the two backends this project actually runs on:
Postgres (postgresql_where) and SQLite (sqlite_where) — both used
by op.create_index's dialect-specific WHERE clause support.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006_one_production_per_model"
down_revision: Union[str, None] = "005_schedules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "uq_model_versions_one_production_per_model"
_WHERE = "state = 'PRODUCTION'"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "model_versions",
        ["model_id"],
        unique=True,
        postgresql_where=sa.text(_WHERE),
        sqlite_where=sa.text(_WHERE),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="model_versions")
