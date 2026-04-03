from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import RentalHandoverStat


@dataclass(frozen=True)
class RentalStatsSnapshot:
    earned_total: Decimal
    earned_today: Decimal
    earned_week: Decimal
    earned_month: Decimal
    handovers_total: int


def _utc_range_today_week_month(settings: Settings, ref_utc: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    ref_utc = ref_utc or datetime.now(UTC)
    local = ref_utc.astimezone(settings.display_tz)
    today_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    w0 = local.weekday()
    week_start = (local - timedelta(days=w0)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        UTC
    )
    month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    return today_start, week_start, month_start


async def fetch_rental_stats(session: AsyncSession, settings: Settings) -> RentalStatsSnapshot:
    today_start, week_start, month_start = _utc_range_today_week_month(settings)
    now_utc = datetime.now(UTC)

    total = await session.scalar(select(func.coalesce(func.sum(RentalHandoverStat.amount), 0)))
    cnt = await session.scalar(select(func.count()).select_from(RentalHandoverStat))

    earned_today = await session.scalar(
        select(func.coalesce(func.sum(RentalHandoverStat.amount), 0)).where(
            RentalHandoverStat.handed_over_at >= today_start,
            RentalHandoverStat.handed_over_at <= now_utc,
        )
    )
    earned_week = await session.scalar(
        select(func.coalesce(func.sum(RentalHandoverStat.amount), 0)).where(
            RentalHandoverStat.handed_over_at >= week_start,
            RentalHandoverStat.handed_over_at <= now_utc,
        )
    )
    earned_month = await session.scalar(
        select(func.coalesce(func.sum(RentalHandoverStat.amount), 0)).where(
            RentalHandoverStat.handed_over_at >= month_start,
            RentalHandoverStat.handed_over_at <= now_utc,
        )
    )

    t = total if total is not None else Decimal("0")
    et = earned_today if earned_today is not None else Decimal("0")
    ew = earned_week if earned_week is not None else Decimal("0")
    em = earned_month if earned_month is not None else Decimal("0")
    c = int(cnt or 0)

    return RentalStatsSnapshot(
        earned_total=Decimal(t),
        earned_today=Decimal(et),
        earned_week=Decimal(ew),
        earned_month=Decimal(em),
        handovers_total=c,
    )


def record_handover_stat(
    session: AsyncSession,
    *,
    item_id: int,
    amount: Decimal,
    handed_over_at: datetime,
) -> None:
    session.add(
        RentalHandoverStat(
            item_id=item_id,
            amount=amount,
            handed_over_at=handed_over_at,
        )
    )
