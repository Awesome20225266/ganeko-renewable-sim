"""add schedule_block (day-ahead P90 schedule)

Purely additive: creates one new table. No existing table, column, index or
constraint is touched, so every current query and API response is unaffected.

Revision ID: a1c4e7f20b3d
Revises: 187b7cae8814
Create Date: 2026-08-17
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a1c4e7f20b3d'
down_revision: str | None = '187b7cae8814'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'schedule_block',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plant_code', sa.String(length=64), nullable=False),
        sa.Column('sim_date', sa.Date(), nullable=False),
        sa.Column('block_no', sa.Integer(), nullable=False),
        sa.Column('block_start', sa.DateTime(), nullable=False),
        sa.Column('block_end', sa.DateTime(), nullable=False),
        sa.Column('solar_p90_mw', sa.Float(), nullable=False),
        sa.Column('wind_p90_mw', sa.Float(), nullable=False),
        sa.Column('total_p90_mw', sa.Float(), nullable=False),
        sa.Column('solar_p90_mwh', sa.Float(), nullable=False),
        sa.Column('wind_p90_mwh', sa.Float(), nullable=False),
        sa.Column('total_p90_mwh', sa.Float(), nullable=False),
        sa.Column('solar_band_low_mw', sa.Float(), nullable=False),
        sa.Column('solar_band_high_mw', sa.Float(), nullable=False),
        sa.Column('wind_band_low_mw', sa.Float(), nullable=False),
        sa.Column('wind_band_high_mw', sa.Float(), nullable=False),
        sa.Column('total_band_low_mw', sa.Float(), nullable=False),
        sa.Column('total_band_high_mw', sa.Float(), nullable=False),
        sa.Column('anchor_mode', sa.String(length=32), nullable=False),
        sa.Column('schedule_version', sa.String(length=32), nullable=False),
        sa.Column('plant_config_version', sa.Integer(), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('is_current', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('plant_code', 'sim_date', 'block_no', 'schedule_version',
                            name='uq_schedule_block'),
    )
    with op.batch_alter_table('schedule_block', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_schedule_block_plant_code'),
                              ['plant_code'], unique=False)
        batch_op.create_index('ix_schedule_plant_date_cur',
                              ['plant_code', 'sim_date', 'is_current'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('schedule_block', schema=None) as batch_op:
        batch_op.drop_index('ix_schedule_plant_date_cur')
        batch_op.drop_index(batch_op.f('ix_schedule_block_plant_code'))
    op.drop_table('schedule_block')
