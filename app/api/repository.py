"""Read-side query helpers for current (is_current) generation data."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DailySummary, GenerationBlock


def get_blocks(
    db: Session, plant_code: str, sim_date: date, data_mode: str | None = None
) -> list[GenerationBlock]:
    q = select(GenerationBlock).where(
        GenerationBlock.plant_code == plant_code,
        GenerationBlock.sim_date == sim_date,
        GenerationBlock.is_current.is_(True),
    )
    if data_mode:
        q = q.where(GenerationBlock.data_mode == data_mode)
    q = q.order_by(GenerationBlock.block_no)
    return list(db.scalars(q))


def get_blocks_range(
    db: Session, plant_code: str, start: date, end: date, data_mode: str | None = None
) -> list[GenerationBlock]:
    q = select(GenerationBlock).where(
        GenerationBlock.plant_code == plant_code,
        GenerationBlock.sim_date >= start,
        GenerationBlock.sim_date <= end,
        GenerationBlock.is_current.is_(True),
    )
    if data_mode:
        q = q.where(GenerationBlock.data_mode == data_mode)
    q = q.order_by(GenerationBlock.sim_date, GenerationBlock.block_no)
    return list(db.scalars(q))


def get_summary(
    db: Session, plant_code: str, sim_date: date, data_mode: str | None = None
) -> DailySummary | None:
    q = select(DailySummary).where(
        DailySummary.plant_code == plant_code,
        DailySummary.sim_date == sim_date,
        DailySummary.is_current.is_(True),
    )
    if data_mode:
        q = q.where(DailySummary.data_mode == data_mode)
    return db.scalars(q.order_by(DailySummary.processed_at.desc())).first()


def get_summaries_range(
    db: Session, plant_code: str, start: date, end: date
) -> list[DailySummary]:
    q = (
        select(DailySummary)
        .where(
            DailySummary.plant_code == plant_code,
            DailySummary.sim_date >= start,
            DailySummary.sim_date <= end,
            DailySummary.is_current.is_(True),
        )
        .order_by(DailySummary.sim_date)
    )
    return list(db.scalars(q))
