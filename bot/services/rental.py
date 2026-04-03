from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Item, ItemBlackout, Rental, RentalState, Reservation

MAX_RENT_HOURS = 168
MIN_RENT_HOURS_FREE = 1
MIN_RENT_HOURS_PAID = 3


def ensure_utc(dt: datetime | None) -> datetime | None:
    """SQLite часто отдаёт naive datetime — приводим к UTC для сравнений."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def rent_hours_bounds(item: Item) -> tuple[int, int]:
    lo = MIN_RENT_HOURS_PAID if item.is_paid else MIN_RENT_HOURS_FREE
    return lo, MAX_RENT_HOURS


def _int_grouped_dots(n: int) -> str:
    negative = n < 0
    s = str(abs(n))
    parts: list[str] = []
    while len(s) > 3:
        parts.insert(0, s[-3:])
        s = s[:-3]
    parts.insert(0, s)
    body = ".".join(parts)
    return f"-{body}" if negative else body


def format_money(value: Decimal) -> str:
    n = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{_int_grouped_dots(n)}$"


def price_for_hours(item: Item, hours: int, *, strict_paid_min: bool = True) -> Decimal:
    min_h = MIN_RENT_HOURS_PAID if (item.is_paid and strict_paid_min) else MIN_RENT_HOURS_FREE
    if hours < min_h or hours > MAX_RENT_HOURS:
        if item.is_paid and strict_paid_min:
            raise ValueError(
                f"Платная аренда: от {MIN_RENT_HOURS_PAID} до {MAX_RENT_HOURS} часов."
            )
        raise ValueError(f"Допустимо от {MIN_RENT_HOURS_FREE} до {MAX_RENT_HOURS} часов.")
    if not item.is_paid:
        return Decimal("0")
    ph, pd, pw = item.price_hour, item.price_day, item.price_week
    if ph is None or pd is None or pw is None:
        raise ValueError("paid item missing prices")

    if hours == 168:
        return Decimal(pw)
    if 1 <= hours <= 23:
        return Decimal(ph) * hours
    if 24 <= hours <= 167:
        return (Decimal(hours) / Decimal(24)) * Decimal(pd)
    raise ValueError("invalid hours")


@dataclass
class ItemStatus:
    pending_admin: bool
    active_rental: Rental | None
    next_booking_start: datetime
    in_blackout: bool
    blackout_until: datetime | None = None


def blackout_max_end_covering_now(
    blackouts: list[ItemBlackout], ref_now: datetime
) -> datetime | None:
    now_u = ensure_utc(ref_now) or datetime.now(UTC)
    ends: list[datetime] = []
    for bo in blackouts:
        s, e = ensure_utc(bo.start_at), ensure_utc(bo.end_at)
        if s is not None and e is not None and s <= now_u < e:
            ends.append(e)
    return max(ends) if ends else None


def item_now_in_item_blackout(blackouts: list[ItemBlackout], ref_now: datetime) -> bool:
    return blackout_max_end_covering_now(blackouts, ref_now) is not None


def item_list_button_text(name: str, st: ItemStatus, *, ref_now: datetime) -> str:
    if st.pending_admin:
        icon = "⏳"
    elif st.active_rental is not None:
        icon = "🔒"
    elif st.in_blackout:
        icon = "⛔"
    elif st.next_booking_start > ref_now:
        icon = "📅"
    else:
        icon = "✅"
    text = f"{icon} {name.strip()}"
    if len(text) > 64:
        text = text[:61] + "…"
    return text


def can_take_immediate_rent(st: ItemStatus, ref_now: datetime) -> bool:
    return (
        not st.pending_admin
        and not st.in_blackout
        and st.active_rental is None
        and st.next_booking_start <= ref_now
    )


async def items_availability_batch(
    session: AsyncSession, item_ids: list[int]
) -> tuple[datetime, dict[int, ItemStatus]]:
    await expire_expired_rentals(session)
    ref_now = datetime.now(UTC)
    out: dict[int, ItemStatus] = {}
    if not item_ids:
        return ref_now, out

    r_rent = await session.execute(select(Rental).where(Rental.item_id.in_(item_ids)))
    rentals_by: dict[int, list[Rental]] = defaultdict(list)
    for row in r_rent.scalars():
        rentals_by[row.item_id].append(row)

    r_res = await session.execute(
        select(Reservation)
        .where(Reservation.item_id.in_(item_ids))
        .order_by(Reservation.start_at)
    )
    res_by: dict[int, list[Reservation]] = defaultdict(list)
    for row in r_res.scalars():
        res_by[row.item_id].append(row)

    r_bo = await session.execute(select(ItemBlackout).where(ItemBlackout.item_id.in_(item_ids)))
    bo_by: dict[int, list[ItemBlackout]] = defaultdict(list)
    for row in r_bo.scalars():
        bo_by[row.item_id].append(row)

    for iid in item_ids:
        rentals = rentals_by.get(iid, [])
        reservations = res_by.get(iid, [])
        pending = any(r.state == RentalState.pending_admin.value for r in rentals)
        active = None
        for r in rentals:
            if r.state != RentalState.active.value:
                continue
            end_at = ensure_utc(r.end_at)
            if end_at is None or end_at <= ref_now:
                continue
            active = r
            break
        nxt = next_booking_start_utc(ref_now, active, reservations)
        bo_list = bo_by.get(iid, [])
        bu = blackout_max_end_covering_now(bo_list, ref_now)
        in_bo = bu is not None
        out[iid] = ItemStatus(
            pending_admin=pending,
            active_rental=active,
            next_booking_start=nxt,
            in_blackout=in_bo,
            blackout_until=bu,
        )
    return ref_now, out


async def expire_expired_rentals(session: AsyncSession, now: datetime | None = None) -> None:
    now = ensure_utc(now) or datetime.now(UTC)
    q = await session.execute(
        select(Rental).where(
            Rental.state == RentalState.active.value,
            Rental.end_at.is_not(None),
        )
    )
    expired = []
    for r in q.scalars():
        end_at = ensure_utc(r.end_at)
        if end_at is not None and end_at < now:
            expired.append(r)
    for r in expired:
        await session.delete(r)


async def _load_item_rentals_reservations(session: AsyncSession, item_id: int) -> tuple[Item | None, list[Rental], list[Reservation]]:
    r_item = await session.execute(select(Item).where(Item.id == item_id))
    item = r_item.scalar_one_or_none()
    if item is None:
        return None, [], []

    r_rent = await session.execute(select(Rental).where(Rental.item_id == item_id))
    rentals = list(r_rent.scalars().all())

    r_res = await session.execute(
        select(Reservation).where(Reservation.item_id == item_id).order_by(Reservation.start_at)
    )
    reservations = list(r_res.scalars().all())
    return item, rentals, reservations


def next_booking_start_utc(
    now: datetime,
    active: Rental | None,
    reservations: list[Reservation],
) -> datetime:
    now_utc = ensure_utc(now) or datetime.now(UTC)
    if active is not None and active.end_at is not None:
        end_a = ensure_utc(active.end_at)
        if end_a is not None and end_a > now_utc:
            base = end_a
        else:
            base = now_utc
    else:
        base = now_utc
    if not reservations:
        return base
    ends = [e for r in reservations if (e := ensure_utc(r.end_at)) is not None]
    if not ends:
        return base
    last_end = max(ends)
    return max(base, last_end)


async def user_facing_status(session: AsyncSession, item_id: int) -> ItemStatus | None:
    await expire_expired_rentals(session)
    item, rentals, reservations = await _load_item_rentals_reservations(session, item_id)
    if item is None:
        return None

    now = datetime.now(UTC)
    pending = any(r.state == RentalState.pending_admin.value for r in rentals)
    active = None
    for r in rentals:
        if r.state != RentalState.active.value:
            continue
        end_at = ensure_utc(r.end_at)
        if end_at is None or end_at <= now:
            continue
        active = r
        break

    r_bo = await session.execute(select(ItemBlackout).where(ItemBlackout.item_id == item_id))
    blackouts = list(r_bo.scalars().all())
    bu = blackout_max_end_covering_now(blackouts, now)
    in_bo = bu is not None

    nxt = next_booking_start_utc(now, active, reservations)
    return ItemStatus(
        pending_admin=pending,
        active_rental=active,
        next_booking_start=nxt,
        in_blackout=in_bo,
        blackout_until=bu,
    )


def item_photos_list(item: Item) -> list[str]:
    try:
        data = json.loads(item.photos_json or "[]")
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, str)]
    except json.JSONDecodeError:
        pass
    return []


def set_item_photos(item: Item, file_ids: list[str]) -> None:
    item.photos_json = json.dumps(file_ids, ensure_ascii=False)
