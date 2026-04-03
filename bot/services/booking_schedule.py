from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Item, ItemBlackout, Rental, RentalState, Reservation
from bot.services.rental import MAX_RENT_HOURS, ensure_utc, rent_hours_bounds
from bot.time_format import format_local_time

# Интервалы занятости: [start, end) — правая граница не входит (можно стыковать 15:00 ↔15:00).

MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START = 1


def user_may_cancel_reservation(*, now_utc: datetime, reservation_start_utc: datetime) -> bool:
    """Своя отмена брони: не позднее чем за час до начала слота (включительно ровно за час)."""
    start = ensure_utc(reservation_start_utc)
    now_u = ensure_utc(now_utc)
    if start is None or now_u is None:
        return False
    deadline = start - timedelta(hours=MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START)
    return now_u <= deadline


def normalize_interval_bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    s = ensure_utc(start)
    e = ensure_utc(end)
    if s is None or e is None or e <= s:
        raise ValueError("invalid interval")
    return s, e


def intervals_overlap(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    """Пересечение [a_start, a_end) и [b_start, b_end)."""
    return a_start < b_end and b_start < a_end


def point_inside_busy(t: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    t = ensure_utc(t)
    if t is None:
        return True
    return any(s <= t < e for s, e in busy)


async def load_rr_busy_intervals_utc(session: AsyncSession, item_id: int) -> list[tuple[datetime, datetime]]:
    """Брони и аренды (active / pending_admin): вещь занята по расписанию. Окна «не у компа» (blackout) не включаются."""
    busy: list[tuple[datetime, datetime]] = []

    r_res = await session.execute(
        select(Reservation).where(Reservation.item_id == item_id).order_by(Reservation.start_at)
    )
    for res in r_res.scalars():
        s, e = ensure_utc(res.start_at), ensure_utc(res.end_at)
        if s is not None and e is not None and e > s:
            busy.append((s, e))

    r_rent = await session.execute(
        select(Rental).where(
            Rental.item_id == item_id,
            func.coalesce(func.trim(Rental.state), "").in_(
                (RentalState.active.value, RentalState.pending_admin.value)
            ),
        )
    )
    for r in r_rent.scalars():
        s, e = ensure_utc(r.start_at), ensure_utc(r.end_at)
        if s is not None and e is not None and e > s:
            busy.append((s, e))
    return busy


async def load_blackout_intervals_utc(session: AsyncSession, item_id: int) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    r_bo = await session.execute(
        select(ItemBlackout).where(ItemBlackout.item_id == item_id).order_by(ItemBlackout.start_at)
    )
    for bo in r_bo.scalars():
        s, e = ensure_utc(bo.start_at), ensure_utc(bo.end_at)
        if s is not None and e is not None and e > s:
            out.append((s, e))
    return out


async def load_busy_intervals_utc(session: AsyncSession, item_id: int) -> list[tuple[datetime, datetime]]:
    """Объединение RR и blackout (календарь). Пересечение периода аренды проверяйте по RR отдельно."""
    rr = await load_rr_busy_intervals_utc(session, item_id)
    bo = await load_blackout_intervals_utc(session, item_id)
    return rr + bo


def blackout_max_end_covering_point(
    t: datetime, bo_intervals: list[tuple[datetime, datetime]]
) -> datetime | None:
    """Если t попадает в [s, e) какого‑либо blackout, вернуть max(e) по всем таким окнам."""
    tu = ensure_utc(t)
    if tu is None:
        return None
    ends: list[datetime] = []
    for bs, be in bo_intervals:
        if bs <= tu < be:
            ends.append(be)
    return max(ends) if ends else None


def blackout_max_end_overlapping_slot(
    slot_start: datetime, slot_end: datetime, bo_intervals: list[tuple[datetime, datetime]]
) -> datetime | None:
    """Макс. конец blackout среди окон, пересекающихся со слотом [slot_start, slot_end))."""
    s, e = normalize_interval_bounds(slot_start, slot_end)
    ends: list[datetime] = []
    for bs, be in bo_intervals:
        if intervals_overlap(s, e, bs, be):
            ends.append(be)
    return max(ends) if ends else None


def user_msg_blocked_by_blackout_until(settings: Settings, until: datetime) -> str:
    return (
        f"Владелец не сможет сдать вещь в аренду до {format_local_time(until, settings)}."
    )


async def explain_booking_start_conflict(
    session: AsyncSession,
    item_id: int,
    start: datetime,
    settings: Settings,
) -> str:
    """Сообщение, когда начало брони попадает в занятый слот (blackout или бронь/аренда)."""
    su = ensure_utc(start)
    if su is None:
        return (
            "Это время уже занято другой бронью или текущей арендой. Укажите другое начало."
        )
    bo = await load_blackout_intervals_utc(session, item_id)
    until = blackout_max_end_covering_point(su, bo)
    if until is not None:
        return user_msg_blocked_by_blackout_until(settings, until) + " Укажите другое начало."
    rr = await load_rr_busy_intervals_utc(session, item_id)
    if point_inside_busy(su, rr):
        return (
            "Это время уже занято другой бронью или текущей арендой. Укажите другое начало."
        )
    return (
        "Это время уже занято другой бронью или текущей арендой. Укажите другое начало."
    )


def next_busy_start_after(t: datetime, busy: list[tuple[datetime, datetime]]) -> datetime | None:
    """Минимальный start среди интервалов, у которых start > t."""
    t = ensure_utc(t)
    if t is None:
        return None
    cand = [s for s, _ in busy if s > t]
    return min(cand) if cand else None


def max_reservation_end_utc(start: datetime, busy: list[tuple[datetime, datetime]]) -> datetime:
    """Правая граница: до следующей занятости или до лимита по часам."""
    s = ensure_utc(start)
    if s is None:
        raise ValueError("start")
    cap = s + timedelta(hours=MAX_RENT_HOURS)
    nxt = next_busy_start_after(s, busy)
    if nxt is None:
        return cap
    return min(cap, nxt)


def max_hours_from_start(
    start: datetime, busy: list[tuple[datetime, datetime]], lo: int, hi: int
) -> int:
    """Сколько полных часов можно взять: не больше hi и места до следующей «занятости вещи» (RR, без blackout)."""
    max_end = max_reservation_end_utc(start, busy)
    start_u = ensure_utc(start)
    if start_u is None:
        return 0
    span_sec = (max_end - start_u).total_seconds()
    raw = int(span_sec // 3600)
    if raw < lo:
        return raw
    return min(hi, raw)


def parse_booking_start_text(text: str, settings: Settings) -> datetime | None:
    """ДД.ММ.ГГГГ ЧЧ:ММ в display_tz → UTC."""
    t = (text or "").strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})$", t)
    if not m:
        return None
    d, mo, y, h, mi = (int(x) for x in m.groups())
    try:
        local = datetime(y, mo, d, h, mi, tzinfo=settings.display_tz)
    except ValueError:
        return None
    return local.astimezone(UTC)


def reservation_fits(
    busy: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> bool:
    """Новый слот [start, end) не пересекается ни с одним интервалом в списке (обычно только RR)."""
    s, e = normalize_interval_bounds(start, end)
    return not any(intervals_overlap(s, e, bs, be) for bs, be in busy)


def rent_lo_hi(item: Item) -> tuple[int, int]:
    return rent_hours_bounds(item)


_AVAIL_UI_MAX_LINES = 14
_AVAIL_TAIL_HORIZON_DAYS = 800


def merge_intervals_utc(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    cleaned: list[tuple[datetime, datetime]] = []
    for s, e in intervals:
        su, eu = ensure_utc(s), ensure_utc(e)
        if su is None or eu is None or eu <= su:
            continue
        cleaned.append((su, eu))
    if not cleaned:
        return []
    cleaned.sort(key=lambda x: x[0])
    out: list[tuple[datetime, datetime]] = [cleaned[0]]
    for s, e in cleaned[1:]:
        ps, pe = out[-1]
        if s <= pe:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


def _subtract_blackout_from_segments(
    segs: list[tuple[datetime, datetime]],
    bs: datetime,
    be: datetime,
) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for sa, se in segs:
        if not (sa < be and bs < se):
            out.append((sa, se))
            continue
        if sa < bs:
            left_end = min(se, bs)
            if left_end > sa:
                out.append((sa, left_end))
        if be < se:
            rb = max(sa, be)
            if se > rb:
                out.append((rb, se))
    return out


def free_segments_excluding_blackout(
    a: datetime,
    b: datetime,
    bo_merged: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Подотрезки [a,b), где можно выбрать момент начала (не внутри blackout)."""
    au, bu = ensure_utc(a), ensure_utc(b)
    if au is None or bu is None or bu <= au:
        return []
    segs = [(au, bu)]
    for bs, be in bo_merged:
        if not segs:
            break
        segs = _subtract_blackout_from_segments(segs, bs, be)
    return [(s, e) for s, e in segs if e > s]


async def format_user_booking_availability_block(
    session: AsyncSession,
    item_id: int,
    item: Item,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> str:
    """HTML: окна времени начала брони с учётом минимума часов до ближайшей RR-занятости."""
    now_u = ensure_utc(now) or datetime.now(UTC)
    lo, hi = rent_lo_hi(item)
    rr = merge_intervals_utc(await load_rr_busy_intervals_utc(session, item_id))
    bo = merge_intervals_utc(await load_blackout_intervals_utc(session, item_id))
    cursor = now_u
    lines: list[str] = []
    horizon = cursor + timedelta(days=_AVAIL_TAIL_HORIZON_DAYS)

    def add_finite(sa: datetime, se: datetime) -> None:
        nonlocal lines
        if len(lines) >= _AVAIL_UI_MAX_LINES:
            return
        latest = se - timedelta(hours=lo)
        if latest < sa:
            return
        busy_lbl = format_local_time(se, settings)
        lines.append(
            f"• с <b>{format_local_time(sa, settings)}</b> до <b>{format_local_time(latest, settings)}</b>\n"
            f"  <i>время начала; при мин. {lo} ч следующая бронь/аренда с {busy_lbl}</i>"
        )

    def add_open(sa: datetime) -> None:
        nonlocal lines
        if len(lines) >= _AVAIL_UI_MAX_LINES:
            return
        lines.append(
            f"• с <b>{format_local_time(sa, settings)}</b>\n"
            f"  <i>в очереди нет ближайшей брони/аренды; длительность от {lo} до {hi} ч "
            f"(не дольше {MAX_RENT_HOURS} ч подряд от начала)</i>"
        )

    for s, e in rr:
        su, eu = ensure_utc(s), ensure_utc(e)
        if su is None or eu is None:
            continue
        if cursor < su:
            for sa, se in free_segments_excluding_blackout(cursor, su, bo):
                if len(lines) >= _AVAIL_UI_MAX_LINES:
                    break
                add_finite(sa, se)
        if cursor < eu:
            cursor = eu
        if len(lines) >= _AVAIL_UI_MAX_LINES:
            break

    if len(lines) < _AVAIL_UI_MAX_LINES:
        for sa, se in free_segments_excluding_blackout(cursor, horizon, bo):
            if len(lines) >= _AVAIL_UI_MAX_LINES:
                break
            near_horizon = se >= horizon - timedelta(minutes=1)
            latest = se - timedelta(hours=lo)
            if latest < sa:
                continue
            if near_horizon:
                add_open(sa)
            else:
                add_finite(sa, se)

    if not lines:
        return (
            "<b>Когда можно начать бронь</b>\n"
            f"Подходящих окон под минимум <b>{lo} ч</b> сейчас нет — выберите другую дату "
            "или зайдите позже."
        )

    tail = ""
    if len(lines) >= _AVAIL_UI_MAX_LINES:
        tail = "\n<i>…показаны первые окна.</i>"

    head = (
        "<b>Когда можно начать бронь</b>\n"
        f"<i>Вторая дата — последнее подходящее <b>начало</b> слота при минимуме {lo} ч "
        f"до следующей брони или аренды.</i>\n\n"
    )
    return head + "\n".join(lines) + tail


async def validate_new_reservation(
    session: AsyncSession,
    item_id: int,
    start: datetime,
    end: datetime,
    settings: Settings,
    *,
    now: datetime,
) -> str | None:
    """None = ок, иначе текст ошибки для пользователя."""
    now_u = ensure_utc(now) or datetime.now(UTC)
    s, e = ensure_utc(start), ensure_utc(end)
    if s is None or e is None or e <= s:
        return "Некорректный интервал брони."
    if s < now_u:
        return "Начало брони не может быть в прошлом."
    r_pend = await session.execute(
        select(Rental.id).where(
            Rental.item_id == item_id,
            func.coalesce(func.trim(Rental.state), "") == RentalState.pending_admin.value,
        )
    )
    if r_pend.scalar_one_or_none() is not None:
        return "Есть ожидающая заявка у администратора — бронь временно недоступна."

    busy_rr = await load_rr_busy_intervals_utc(session, item_id)
    bo = await load_blackout_intervals_utc(session, item_id)
    # Blackout: нельзя начать выдачу в этот момент; пересечение [s,e) с blackout допустимо.
    if point_inside_busy(s, bo):
        until = blackout_max_end_covering_point(s, bo)
        if until is not None:
            return user_msg_blocked_by_blackout_until(settings, until) + " Укажите другое время начала."
        return (
            "В это время владелец не сможет выдать вещь (окно недоступности). "
            "Укажите другое время начала."
        )
    if point_inside_busy(s, busy_rr):
        return (
            "Это время уже занято другой бронью или текущей арендой. Укажите другое время начала."
        )
    if not reservation_fits(busy_rr, s, e):
        return (
            "Интервал пересекается с уже существующей бронью или арендой. "
            "Выберите другие время или длительность."
        )

    span_h = (e - s).total_seconds() / 3600
    if abs(span_h - round(span_h)) > 1e-6:
        return "Длительность должна быть целым числом часов."
    h = int(round(span_h))
    r_item = await session.execute(select(Item).where(Item.id == item_id))
    item = r_item.scalar_one_or_none()
    if item is None:
        return "Вещь не найдена."
    lo, hi = rent_lo_hi(item)
    if h < lo or h > hi:
        return f"Длительность от {lo} до {hi} ч."
    max_h = max_hours_from_start(s, busy_rr, lo, hi)
    if h > max_h:
        return f"До следующей брони можно максимум {max_h} ч."
    return None
