from __future__ import annotations

import re
from datetime import date
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import AdminBlackoutWindow, BlackoutWindowItem, Item, ItemBlackout, Rental, RentalState, Reservation
from bot.services.rental import MAX_RENT_HOURS, ensure_utc, rent_hours_bounds
from bot.time_format import format_local_time

# Интервалы занятости: [start, end) — правая граница не входит (можно стыковать 15:00 ↔15:00).

MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START = 1
_RECURRING_DEFAULT_PAST_DAYS = 1
_RECURRING_DEFAULT_FUTURE_DAYS = 45


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


def _append_blackout_interval(
    out: dict[int, list[tuple[datetime, datetime]]], item_id: int, start_at, end_at
) -> None:
    s, e = ensure_utc(start_at), ensure_utc(end_at)
    if s is not None and e is not None and e > s:
        out[item_id].append((s, e))


def _display_tz() -> ZoneInfo:
    from os import getenv

    tz_name = (getenv("DISPLAY_TZ", "Europe/Moscow") or "Europe/Moscow").strip()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def _valid_recurring_bounds(start_minute: int | None, end_minute: int | None) -> bool:
    if start_minute is None or end_minute is None:
        return False
    if start_minute == end_minute:
        return False
    return 0 <= start_minute < 24 * 60 and 0 <= end_minute < 24 * 60


def _recurring_interval_covering_point_utc(
    t: datetime, start_minute: int, end_minute: int
) -> tuple[datetime, datetime] | None:
    tu = ensure_utc(t)
    if tu is None or not _valid_recurring_bounds(start_minute, end_minute):
        return None
    tz = _display_tz()
    local_t = tu.astimezone(tz)
    minute = local_t.hour * 60 + local_t.minute
    day = local_t.date()
    if start_minute < end_minute:
        if not (start_minute <= minute < end_minute):
            return None
        start_day = day
        end_day = day
    else:
        if minute >= start_minute:
            start_day = day
            end_day = day + timedelta(days=1)
        elif minute < end_minute:
            start_day = day - timedelta(days=1)
            end_day = day
        else:
            return None
    s_local = datetime(
        start_day.year,
        start_day.month,
        start_day.day,
        start_minute // 60,
        start_minute % 60,
        tzinfo=tz,
    )
    e_local = datetime(
        end_day.year,
        end_day.month,
        end_day.day,
        end_minute // 60,
        end_minute % 60,
        tzinfo=tz,
    )
    return s_local.astimezone(UTC), e_local.astimezone(UTC)


def _expand_recurring_daily_intervals_utc(
    range_start: datetime,
    range_end: datetime,
    start_minute: int,
    end_minute: int,
) -> list[tuple[datetime, datetime]]:
    rs, re = ensure_utc(range_start), ensure_utc(range_end)
    if rs is None or re is None or re <= rs:
        return []
    if not _valid_recurring_bounds(start_minute, end_minute):
        return []
    tz = _display_tz()
    local_start = rs.astimezone(tz)
    local_end = re.astimezone(tz)
    day: date = local_start.date() - timedelta(days=1)
    last_day: date = local_end.date() + timedelta(days=1)
    out: list[tuple[datetime, datetime]] = []
    while day <= last_day:
        s_local = datetime(
            day.year, day.month, day.day, start_minute // 60, start_minute % 60, tzinfo=tz
        )
        if start_minute < end_minute:
            e_day = day
        else:
            e_day = day + timedelta(days=1)
        e_local = datetime(
            e_day.year,
            e_day.month,
            e_day.day,
            end_minute // 60,
            end_minute % 60,
            tzinfo=tz,
        )
        su = s_local.astimezone(UTC)
        eu = e_local.astimezone(UTC)
        if su < re and eu > rs:
            out.append((max(su, rs), min(eu, re)))
        day += timedelta(days=1)
    return out


async def load_blackout_intervals_for_item_ids(
    session: AsyncSession,
    item_ids: list[int],
    *,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
) -> dict[int, list[tuple[datetime, datetime]]]:
    """Интервалы blackout: общие окна (/add_blackout) через blackout_window_items + legacy по одной вещи."""
    out: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    if not item_ids:
        return {}
    ids = list({int(x) for x in item_ids})
    rs = ensure_utc(range_start) or (datetime.now(UTC) - timedelta(days=_RECURRING_DEFAULT_PAST_DAYS))
    re = ensure_utc(range_end) or (datetime.now(UTC) + timedelta(days=_RECURRING_DEFAULT_FUTURE_DAYS))
    r_w = await session.execute(
        select(
            BlackoutWindowItem.item_id,
            AdminBlackoutWindow.start_at,
            AdminBlackoutWindow.end_at,
            AdminBlackoutWindow.is_recurring_daily,
            AdminBlackoutWindow.recurring_start_minute,
            AdminBlackoutWindow.recurring_end_minute,
        )
        .select_from(BlackoutWindowItem)
        .join(AdminBlackoutWindow, AdminBlackoutWindow.id == BlackoutWindowItem.window_id)
        .where(BlackoutWindowItem.item_id.in_(ids))
    )
    for iid, sa, ea, is_rec, rec_s, rec_e in r_w.all():
        key = int(iid)
        if bool(is_rec):
            for s, e in _expand_recurring_daily_intervals_utc(rs, re, rec_s, rec_e):
                out[key].append((s, e))
        else:
            _append_blackout_interval(out, key, sa, ea)
    r_leg = await session.execute(
        select(ItemBlackout.item_id, ItemBlackout.start_at, ItemBlackout.end_at).where(
            ItemBlackout.item_id.in_(ids),
            ItemBlackout.window_id.is_(None),
        )
    )
    for iid, sa, ea in r_leg.all():
        _append_blackout_interval(out, int(iid), sa, ea)
    return dict(out)


async def load_blackout_intervals_utc(
    session: AsyncSession,
    item_id: int,
    *,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    m = await load_blackout_intervals_for_item_ids(
        session, [item_id], range_start=range_start, range_end=range_end
    )
    iv = m.get(item_id, [])
    return sorted(iv, key=lambda t: t[0])


async def blackout_max_end_covering_point_db(
    session: AsyncSession, item_id: int, t: datetime
) -> datetime | None:
    tu = ensure_utc(t)
    if tu is None:
        return None
    iv = await load_blackout_intervals_for_item_ids(
        session,
        [item_id],
        range_start=tu - timedelta(days=1),
        range_end=tu + timedelta(days=2),
    )
    best = blackout_max_end_covering_point(tu, iv.get(item_id, []))
    if best is not None:
        return best
    r_daily = await session.execute(
        select(
            AdminBlackoutWindow.recurring_start_minute,
            AdminBlackoutWindow.recurring_end_minute,
        )
        .select_from(BlackoutWindowItem)
        .join(AdminBlackoutWindow, AdminBlackoutWindow.id == BlackoutWindowItem.window_id)
        .where(
            BlackoutWindowItem.item_id == item_id,
            AdminBlackoutWindow.is_recurring_daily.is_(True),
        )
    )
    ends: list[datetime] = []
    for smin, emin in r_daily.all():
        iv2 = _recurring_interval_covering_point_utc(tu, smin, emin)
        if iv2 is None:
            continue
        ends.append(iv2[1])
    return max(ends) if ends else None


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
    until = await blackout_max_end_covering_point_db(session, item_id, su)
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
    """Лояльный парсинг даты/времени в display_tz -> UTC.

    Поддерживает варианты вроде:
    - 10.04.2026 10:00
    - 10.04.2026 10.00
    - 10:04:2026 10:00
    - 10-04-2026 10-00
    - 10/04/2026 10:00:30
    """
    t = (text or "").strip()
    m = re.match(
        r"^(\d{1,2})\D+(\d{1,2})\D+(\d{4})\s+(\d{1,2})\D+(\d{1,2})(?:\D+(\d{1,2}))?$",
        t,
    )
    if not m:
        return None
    d, mo, y, h, mi, sec_raw = m.groups()
    sec = int(sec_raw) if sec_raw is not None else 0
    try:
        local = datetime(
            int(y),
            int(mo),
            int(d),
            int(h),
            int(mi),
            sec,
            tzinfo=settings.display_tz,
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def reservation_start_in_past_error(start: datetime, now: datetime) -> str | None:
    """None — начало не в прошлом; иначе короткий текст ошибки для пользователя."""
    s = ensure_utc(start)
    now_u = ensure_utc(now) or datetime.now(UTC)
    if s is None:
        return "Некорректное время начала."
    if s < now_u:
        return "Начало брони не может быть в прошлом — укажите будущее время."
    return None


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


def _same_instant(a: datetime | None, b: datetime | None) -> bool:
    au, bu = ensure_utc(a), ensure_utc(b)
    if au is None or bu is None:
        return False
    return abs((au - bu).total_seconds()) < 1.0


async def format_user_booking_availability_block(
    session: AsyncSession,
    item_id: int,
    item: Item,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> str:
    """HTML: краткий список окон, где можно указать начало брони."""
    now_u = ensure_utc(now) or datetime.now(UTC)
    lo, hi = rent_lo_hi(item)
    horizon = now_u + timedelta(days=_AVAIL_TAIL_HORIZON_DAYS)
    rr = merge_intervals_utc(await load_rr_busy_intervals_utc(session, item_id))
    bo = merge_intervals_utc(
        await load_blackout_intervals_utc(
            session,
            item_id,
            range_start=now_u - timedelta(days=1),
            range_end=horizon,
        )
    )
    cursor = now_u
    lines: list[str] = []

    def _right_edge_is_rr_start(se: datetime) -> bool:
        se_u = ensure_utc(se)
        if se_u is None:
            return False
        for bs, _ in rr:
            bu = ensure_utc(bs)
            if bu is not None and _same_instant(se_u, bu):
                return True
        return False

    def add_finite(sa: datetime, se: datetime) -> None:
        nonlocal lines
        if len(lines) >= _AVAIL_UI_MAX_LINES:
            return
        sa_u = ensure_utc(sa)
        if sa_u is None:
            return
        if sa_u < now_u:
            sa_u = now_u
        # Перед чужой бронью/арендой нужно уложить минимум lo ч; окно «не дома»
        # ограничивает только момент начала — бронь может пересекаться с ним дальше.
        if _right_edge_is_rr_start(se):
            latest = se - timedelta(hours=lo)
        else:
            latest = se - timedelta(minutes=1)
        if latest < sa_u:
            return
        lines.append(
            f"• с <b>{format_local_time(sa_u, settings)}</b> по <b>{format_local_time(latest, settings)}</b>"
        )

    def add_open(sa: datetime) -> None:
        nonlocal lines
        if len(lines) >= _AVAIL_UI_MAX_LINES:
            return
        sa_u = ensure_utc(sa)
        if sa_u is None:
            return
        if sa_u < now_u:
            sa_u = now_u
        lines.append(f"• с <b>{format_local_time(sa_u, settings)}</b>")

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
            if near_horizon:
                add_open(sa)
            else:
                add_finite(sa, se)

    if not lines:
        return (
            "<b>Окна, свободные для брони</b>\n"
            f"Сейчас нет подходящих промежутков (нужно минимум <b>{lo} ч</b> до занятости). "
            "Выберите другую дату или зайдите позже."
        )

    tail = ""
    if len(lines) >= _AVAIL_UI_MAX_LINES:
        tail = "\n\n<i>…показаны первые окна.</i>"

    head = "<b>Окна, свободные для брони:</b>\n\n"
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
    past_err = reservation_start_in_past_error(s, now_u)
    if past_err is not None:
        return past_err
    r_pend = await session.execute(
        select(Rental.id).where(
            Rental.item_id == item_id,
            func.coalesce(func.trim(Rental.state), "") == RentalState.pending_admin.value,
        )
    )
    if r_pend.scalar_one_or_none() is not None:
        return "Есть ожидающая заявка у администратора — бронь временно недоступна."

    busy_rr = await load_rr_busy_intervals_utc(session, item_id)
    until = await blackout_max_end_covering_point_db(session, item_id, s)
    # Blackout: нельзя начать выдачу в этот момент; пересечение [s,e) с blackout допустимо.
    if until is not None:
        return user_msg_blocked_by_blackout_until(settings, until) + " Укажите другое время начала."
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
