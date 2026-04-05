from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Item, ItemBlackout, Rental, RentalState, Reservation

MAX_RENT_HOURS = 168
MAX_RENT_HOURS_FREE = 12
MIN_RENT_HOURS_FREE = 1
MIN_RENT_HOURS_PAID = 3


def _rental_state_norm(state: str | None) -> str:
    return (state or "").strip()


def _busy_intervals_still_relevant(
    ref_now: datetime, busy: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Интервалы с концом строго после ref_now (полностью прошедшие не мешают расчёту «сейчас»)."""
    now_u = ensure_utc(ref_now) or datetime.now(UTC)
    out: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        ee = ensure_utc(e)
        if ee is not None and ee > now_u:
            out.append((s, e))
    return out


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Naive datetime из SQLite считаем UTC; при NAIVE_DATETIME_TZ — сначала как локальное время в этом поясе."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        tz_name = os.environ.get("NAIVE_DATETIME_TZ", "").strip()
        if tz_name:
            try:
                return dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(UTC)
            except Exception:
                pass
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def rent_hours_bounds(item: Item) -> tuple[int, int]:
    if item.rent_hours_min is not None and item.rent_hours_max is not None:
        return item.rent_hours_min, item.rent_hours_max
    if item.is_paid:
        return MIN_RENT_HOURS_PAID, MAX_RENT_HOURS
    return MIN_RENT_HOURS_FREE, MAX_RENT_HOURS_FREE


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


def price_for_hours(item: Item, hours: int) -> Decimal:
    lo, hi = rent_hours_bounds(item)
    if hours < lo or hours > hi:
        kind = "Платная" if item.is_paid else "Бесплатная"
        raise ValueError(f"{kind} аренда (эта вещь): от {lo} до {hi} ч.")
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
    min_rent_hours: int
    immediate_rent_max_hours: int
    in_reserved_slot: bool
    reserved_until: datetime | None
    # Мин. время начала занятости со start > now (для подсказки).
    next_busy_after: datetime | None
    in_blackout: bool
    blackout_until: datetime | None = None


def blackout_max_end_covering_now_intervals(
    intervals: list[tuple[datetime, datetime]], ref_now: datetime
) -> datetime | None:
    now_u = ensure_utc(ref_now) or datetime.now(UTC)
    ends: list[datetime] = []
    for bs, be in intervals:
        s, e = ensure_utc(bs), ensure_utc(be)
        if s is not None and e is not None and s <= now_u < e:
            ends.append(e)
    return max(ends) if ends else None


def blackout_max_end_covering_now(
    blackouts: list[ItemBlackout], ref_now: datetime
) -> datetime | None:
    intervals: list[tuple[datetime, datetime]] = []
    for bo in blackouts:
        s, e = ensure_utc(bo.start_at), ensure_utc(bo.end_at)
        if s is not None and e is not None and e > s:
            intervals.append((s, e))
    return blackout_max_end_covering_now_intervals(intervals, ref_now)


def item_now_in_item_blackout(blackouts: list[ItemBlackout], ref_now: datetime) -> bool:
    return blackout_max_end_covering_now(blackouts, ref_now) is not None


def reservation_covering_now(
    reservations: list[Reservation], ref_now: datetime
) -> Reservation | None:
    now_u = ensure_utc(ref_now) or datetime.now(UTC)
    for r in reservations:
        s, e = ensure_utc(r.start_at), ensure_utc(r.end_at)
        if s is not None and e is not None and s <= now_u < e:
            return r
    return None


def _compute_immediate_rent_cap_hours(
    now: datetime,
    busy: list[tuple[datetime, datetime]],
    lo: int,
    hi: int,
) -> int:
    """Макс. целое h в [lo..hi], чтобы [now, now+h) не пересекался с бронями и арендами (RR, не blackout)."""
    from bot.services.booking_schedule import point_inside_busy, reservation_fits

    now_u = ensure_utc(now) or datetime.now(UTC)
    if point_inside_busy(now_u, busy):
        return 0

    lo_b, hi_b = 0, hi
    best = 0
    while lo_b <= hi_b:
        mid = (lo_b + hi_b + 1) // 2
        if mid == 0:
            ok = True
        else:
            ok = reservation_fits(busy, now_u, now_u + timedelta(hours=mid))
        if ok:
            best = mid
            lo_b = mid + 1
        else:
            hi_b = mid - 1
    return best if best >= lo else 0


def item_list_button_text(name: str, st: ItemStatus, *, ref_now: datetime) -> str:
    if st.pending_admin:
        icon = "⏳"
    elif st.active_rental is not None:
        icon = "🔒"
    elif st.in_reserved_slot:
        icon = "🔒"
    elif st.in_blackout:
        icon = "⛔"
    elif st.immediate_rent_max_hours >= st.min_rent_hours:
        icon = "✅"
    else:
        icon = "📅"
    text = f"{icon} {name.strip()}"
    if len(text) > 64:
        text = text[:61] + "…"
    return text


def can_take_immediate_rent(st: ItemStatus, ref_now: datetime) -> bool:
    _ = ref_now
    return (
        not st.pending_admin
        and not st.in_blackout
        and st.active_rental is None
        and not st.in_reserved_slot
        and st.immediate_rent_max_hours >= st.min_rent_hours
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

    from bot.services.booking_schedule import (
        load_blackout_intervals_for_item_ids,
        load_rr_busy_intervals_utc,
        next_busy_start_after,
    )

    bo_by = await load_blackout_intervals_for_item_ids(session, item_ids)

    r_items = await session.execute(select(Item).where(Item.id.in_(item_ids)))
    items_map: dict[int, Item] = {it.id: it for it in r_items.scalars().all()}

    for iid in item_ids:
        item = items_map[iid]
        rentals = rentals_by.get(iid, [])
        reservations = res_by.get(iid, [])
        pending = any(
            _rental_state_norm(r.state) == RentalState.pending_admin.value for r in rentals
        )
        active = None
        for r in rentals:
            if _rental_state_norm(r.state) != RentalState.active.value:
                continue
            end_at = ensure_utc(r.end_at)
            if end_at is None or end_at <= ref_now:
                continue
            active = r
            break
        res_cov = reservation_covering_now(reservations, ref_now)
        in_rs = res_cov is not None
        res_end = ensure_utc(res_cov.end_at) if res_cov is not None else None

        bo_list = bo_by.get(iid, [])
        bu = blackout_max_end_covering_now_intervals(bo_list, ref_now)
        in_bo = bu is not None

        busy = await load_rr_busy_intervals_utc(session, iid)
        busy_eff = _busy_intervals_still_relevant(ref_now, busy)
        nxt_after = next_busy_start_after(ref_now, busy_eff) if busy_eff else None
        lo, hi = rent_hours_bounds(item)
        if pending or in_bo or active is not None or in_rs:
            imm = 0
        else:
            imm = _compute_immediate_rent_cap_hours(ref_now, busy_eff, lo, hi)

        nxt = next_booking_start_utc(ref_now, active, reservations)
        out[iid] = ItemStatus(
            pending_admin=pending,
            active_rental=active,
            next_booking_start=nxt,
            min_rent_hours=lo,
            immediate_rent_max_hours=imm,
            in_reserved_slot=in_rs,
            reserved_until=res_end,
            next_busy_after=nxt_after,
            in_blackout=in_bo,
            blackout_until=bu,
        )
    return ref_now, out


async def expire_expired_rentals(session: AsyncSession, now: datetime | None = None) -> None:
    now = ensure_utc(now) or datetime.now(UTC)
    q = await session.execute(
        select(Rental).where(
            func.coalesce(func.trim(Rental.state), "") == RentalState.active.value,
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
    from bot.services.booking_schedule import load_rr_busy_intervals_utc, next_busy_start_after

    await expire_expired_rentals(session)
    item, rentals, reservations = await _load_item_rentals_reservations(session, item_id)
    if item is None:
        return None

    now = datetime.now(UTC)
    pending = any(
        _rental_state_norm(r.state) == RentalState.pending_admin.value for r in rentals
    )
    active = None
    for r in rentals:
        if _rental_state_norm(r.state) != RentalState.active.value:
            continue
        end_at = ensure_utc(r.end_at)
        if end_at is None or end_at <= now:
            continue
        active = r
        break

    from bot.services.booking_schedule import load_blackout_intervals_for_item_ids

    bo_map = await load_blackout_intervals_for_item_ids(session, [item_id])
    blackouts = bo_map.get(item_id, [])
    bu = blackout_max_end_covering_now_intervals(blackouts, now)
    in_bo = bu is not None

    res_cov = reservation_covering_now(reservations, now)
    in_rs = res_cov is not None
    res_end = ensure_utc(res_cov.end_at) if res_cov is not None else None

    busy = await load_rr_busy_intervals_utc(session, item_id)
    busy_eff = _busy_intervals_still_relevant(now, busy)
    nxt_after = next_busy_start_after(now, busy_eff) if busy_eff else None
    lo, hi = rent_hours_bounds(item)
    if pending or in_bo or active is not None or in_rs:
        imm = 0
    else:
        imm = _compute_immediate_rent_cap_hours(now, busy_eff, lo, hi)

    nxt = next_booking_start_utc(now, active, reservations)
    return ItemStatus(
        pending_admin=pending,
        active_rental=active,
        next_booking_start=nxt,
        min_rent_hours=lo,
        immediate_rent_max_hours=imm,
        in_reserved_slot=in_rs,
        reserved_until=res_end,
        next_busy_after=nxt_after,
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
