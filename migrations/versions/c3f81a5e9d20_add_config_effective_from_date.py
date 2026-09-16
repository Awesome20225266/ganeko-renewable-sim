"""add plant_config.effective_from_date (forward-dated config versions)

Purely additive and nullable. Every existing row keeps NULL, which means "applies
to every date" — exactly the behaviour before this column existed, so no backfill
is needed and no stored generation changes.

The column is what lets a config version take effect from a chosen sim_date without
restating any already-published day: see `app.simulate.load_config_for_date`.

Revision ID: c3f81a5e9d20
Revises: a1c4e7f20b3d
Create Date: 2026-09-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c3f81a5e9d20'
down_revision: str | None = 'a1c4e7f20b3d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'plant_config',
        sa.Column('effective_from_date', sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('plant_config', 'effective_from_date')
