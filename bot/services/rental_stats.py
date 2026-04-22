from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Item, RentalHandoverStat, WeeklyInvoice


@dataclass(frozen=True)
class RentalStatsSnapshot:
    earned_total: Decimal
    earned_today: Decimal
    earned_week: Decimal
    earned_month: Decimal
    handovers_total: int
    handovers_today: int
    handovers_week: int
    handovers_month: int


@dataclass(frozen=True)
class CommissionStatsSnapshot:
    earned_total: Decimal
    earned_week: Decimal
    earned_month: Decimal


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


def _stats_rows_for_admin(admin_user_id: int):
    """Строки статистики админа: явный подтвердивший или старая запись без поля — по владельцу вещи."""
    uid = int(admin_user_id)
    return or_(
        RentalHandoverStat.handed_over_by_user_id == uid,
        and_(
            RentalHandoverStat.handed_over_by_user_id.is_(None),
            Item.owner_user_id == uid,
        ),
    )


async def fetch_rental_stats(
    session: AsyncSession, settings: Settings, *, admin_user_id: int, item_id: int | None = None
) -> RentalStatsSnapshot:
    today_start, week_start, month_start = _utc_range_today_week_month(settings)
    now_utc = datetime.now(UTC)
    scope = _stats_rows_for_admin(admin_user_id)

    item_filter = []
    if item_id is not None:
        item_filter.append(RentalHandoverStat.item_id == int(item_id))

    total = await session.scalar(
        select(func.coalesce(func.sum(RentalHandoverStat.amount), 0))
        .select_from(RentalHandoverStat)
        .outerjoin(Item, RentalHandoverStat.item_id == Item.id)
        .where(scope, *item_filter)
    )
    cnt = await session.scalar(
        select(func.count())
        .select_from(RentalHandoverStat)
        .outerjoin(Item, RentalHandoverStat.item_id == Item.id)
        .where(scope, *item_filter)
    )

    earned_today = await session.scalar(
        select(func.coalesce(func.sum(RentalHandoverStat.amount), 0))
        .select_from(RentalHandoverStat)
        .outerjoin(Item, RentalHandoverStat.item_id == Item.id)
        .where(
            scope,
            *item_filter,
            RentalHandoverStat.handed_over_at >= today_start,
            RentalHandoverStat.handed_over_at <= now_utc,
        )
    )
    earned_week = await session.scalar(
        select(func.coalesce(func.sum(RentalHandoverStat.amount), 0))
        .select_from(RentalHandoverStat)
        .outerjoin(Item, RentalHandoverStat.item_id == Item.id)
        .where(
            scope,
            *item_filter,
            RentalHandoverStat.handed_over_at >= week_start,
            RentalHandoverStat.handed_over_at <= now_utc,
        )
    )
    earned_month = await session.scalar(
        select(func.coalesce(func.sum(RentalHandoverStat.amount), 0))
        .select_from(RentalHandoverStat)
        .outerjoin(Item, RentalHandoverStat.item_id == Item.id)
        .where(
            scope,
            *item_filter,
            RentalHandoverStat.handed_over_at >= month_start,
            RentalHandoverStat.handed_over_at <= now_utc,
        )
    )

    cnt_today = await session.scalar(
        select(func.count())
        .select_from(RentalHandoverStat)
        .outerjoin(Item, RentalHandoverStat.item_id == Item.id)
        .where(
            scope,
            *item_filter,
            RentalHandoverStat.handed_over_at >= today_start,
            RentalHandoverStat.handed_over_at <= now_utc,
        )
    )
    cnt_week = await session.scalar(
        select(func.count())
        .select_from(RentalHandoverStat)
        .outerjoin(Item, RentalHandoverStat.item_id == Item.id)
        .where(
            scope,
            *item_filter,
            RentalHandoverStat.handed_over_at >= week_start,
            RentalHandoverStat.handed_over_at <= now_utc,
        )
    )
    cnt_month = await session.scalar(
        select(func.count())
        .select_from(RentalHandoverStat)
        .outerjoin(Item, RentalHandoverStat.item_id == Item.id)
        .where(
            scope,
            *item_filter,
            RentalHandoverStat.handed_over_at >= month_start,
            RentalHandoverStat.handed_over_at <= now_utc,
        )
    )

    t = total if total is not None else Decimal("0")
    et = earned_today if earned_today is not None else Decimal("0")
    ew = earned_week if earned_week is not None else Decimal("0")
    em = earned_month if earned_month is not None else Decimal("0")
    c = int(cnt or 0)
    ct = int(cnt_today or 0)
    cw = int(cnt_week or 0)
    cm = int(cnt_month or 0)

    return RentalStatsSnapshot(
        earned_total=Decimal(t),
        earned_today=Decimal(et),
        earned_week=Decimal(ew),
        earned_month=Decimal(em),
        handovers_total=c,
        handovers_today=ct,
        handovers_week=cw,
        handovers_month=cm,
    )


async def fetch_commission_stats(
    session: AsyncSession, settings: Settings, *, viewer_user_id: int
) -> CommissionStatsSnapshot:
    _today_start, week_start, month_start = _utc_range_today_week_month(settings)
    total = await session.scalar(
        select(func.coalesce(func.sum(WeeklyInvoice.total_due), 0)).where(
            WeeklyInvoice.status == "paid",
            WeeklyInvoice.finalized_at.is_not(None),
            WeeklyInvoice.owner_user_id != int(viewer_user_id),
        )
    )
    week = await session.scalar(
        select(func.coalesce(func.sum(WeeklyInvoice.total_due), 0)).where(
            WeeklyInvoice.status == "paid",
            WeeklyInvoice.finalized_at.is_not(None),
            WeeklyInvoice.owner_user_id != int(viewer_user_id),
            WeeklyInvoice.finalized_at >= week_start,
        )
    )
    month = await session.scalar(
        select(func.coalesce(func.sum(WeeklyInvoice.total_due), 0)).where(
            WeeklyInvoice.status == "paid",
            WeeklyInvoice.finalized_at.is_not(None),
            WeeklyInvoice.owner_user_id != int(viewer_user_id),
            WeeklyInvoice.finalized_at >= month_start,
        )
    )
    return CommissionStatsSnapshot(
        earned_total=Decimal(total or 0),
        earned_week=Decimal(week or 0),
        earned_month=Decimal(month or 0),
    )


def record_handover_stat(
    session: AsyncSession,
    *,
    item_id: int,
    amount: Decimal,
    handed_over_at: datetime,
    handed_over_by_user_id: int,
) -> None:
    session.add(
        RentalHandoverStat(
            item_id=item_id,
            handed_over_by_user_id=int(handed_over_by_user_id),
            amount=amount,
            handed_over_at=handed_over_at,
        )
    )
