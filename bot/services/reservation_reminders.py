from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from html import escape

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.config import Settings
from bot.db import session as db_session
from bot.db.models import Rental, RentalState, Reservation
from bot.services.admin_notify import notify_admins_pending_rental
from bot.services.rental import ensure_utc, expire_expired_rentals, price_for_hours
from bot.time_format import format_local_time

logger = logging.getLogger(__name__)

# Остаток времени до начала брони (секунды): широкие окна под опрос раз в ~45 с
_REM_1H_LO = 50 * 60
_REM_1H_HI = 70 * 60
_REM_15M_LO = 10 * 60
_REM_15M_HI = 20 * 60


async def process_reservation_reminders(bot: Bot, settings: Settings) -> None:
    now = datetime.now(UTC)
    async with db_session.async_session_maker() as session:
        q = await session.execute(
            select(Reservation)
            .options(selectinload(Reservation.item))
            .where(
                (Reservation.notified_before_1h.is_(False))
                | (Reservation.notified_before_15m.is_(False)),
            )
        )
        rows = list(q.scalars().unique())
        changed = False
        for res in rows:
            item = res.item
            if item is None:
                continue
            start = ensure_utc(res.start_at)
            end = ensure_utc(res.end_at)
            if start is None or end is None:
                continue
            if end < now or start <= now:
                continue
            rem_sec = (start - now).total_seconds()

            if not res.notified_before_1h and _REM_1H_LO <= rem_sec <= _REM_1H_HI:
                text = (
                    "⏰ <b>Через час</b> начинается ваша бронь.\n"
                    f"Вещь: <b>{escape(item.name)}</b>\n"
                    f"С {format_local_time(start, settings)} по {format_local_time(end, settings)}"
                )
                await _send_reminder(bot, res.user_id, text)
                res.notified_before_1h = True
                changed = True

            if not res.notified_before_15m and _REM_15M_LO <= rem_sec <= _REM_15M_HI:
                text = (
                    "⏰ <b>Через 15 минут</b> начинается ваша бронь.\n"
                    f"Вещь: <b>{escape(item.name)}</b>\n"
                    f"С {format_local_time(start, settings)} по {format_local_time(end, settings)}"
                )
                await _send_reminder(bot, res.user_id, text)
                res.notified_before_15m = True
                changed = True

        if changed:
            await session.commit()


async def process_reservation_booking_starts(bot: Bot, settings: Settings) -> None:
    """Когда наступает start_at брони — заявка админу как при мгновенной аренде (Rental pending_admin)."""
    now = datetime.now(UTC)
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)

        q = await session.execute(select(Reservation).options(selectinload(Reservation.item)))
        all_res = list(q.scalars().unique())
        for res in all_res:
            end = ensure_utc(res.end_at)
            if end is not None and end <= now:
                await session.delete(res)
        await session.commit()

        q2 = await session.execute(select(Reservation).options(selectinload(Reservation.item)))
        for res in list(q2.scalars().unique()):
            start = ensure_utc(res.start_at)
            end = ensure_utc(res.end_at)
            if start is None or end is None:
                continue
            if not (start <= now < end):
                continue
            item = res.item
            if item is None:
                continue

            r_pend = await session.execute(
                select(Rental.id).where(
                    Rental.item_id == item.id,
                    Rental.state == RentalState.pending_admin.value,
                )
            )
            if r_pend.scalar_one_or_none() is not None:
                continue

            r_act = await session.execute(
                select(Rental).where(
                    Rental.item_id == item.id,
                    Rental.state == RentalState.active.value,
                )
            )
            blocked = False
            for ar in r_act.scalars():
                ae = ensure_utc(ar.end_at)
                if ae is not None and ae > now:
                    blocked = True
                    break
            if blocked:
                continue

            try:
                total = price_for_hours(item, res.requested_hours)
            except ValueError:
                total = Decimal("0")

            rental = Rental(
                item_id=item.id,
                user_id=res.user_id,
                username=res.username,
                state=RentalState.pending_admin.value,
                start_at=start,
                end_at=end,
                requested_hours=res.requested_hours,
            )
            session.add(rental)
            await session.delete(res)
            await session.flush()
            try:
                await notify_admins_pending_rental(
                    bot,
                    settings,
                    session,
                    rental,
                    item,
                    total,
                    end,
                    heading="Начало брони — выдача вещи",
                )
            except Exception:
                logger.exception("notify admins for booking start failed (rental id will still save)")
            await session.commit()


async def _send_reminder(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, parse_mode=ParseMode.HTML)
    except TelegramForbiddenError:
        logger.info("Reminder skipped: user %s blocked the bot", user_id)
    except TelegramBadRequest as e:
        logger.warning("Reminder failed for %s: %s", user_id, e)
    except Exception:
        logger.exception("Reminder error for %s", user_id)


async def reservation_reminder_loop(bot: Bot, settings: Settings, interval_sec: float = 45.0) -> None:
    while True:
        try:
            await asyncio.sleep(interval_sec)
            await process_reservation_booking_starts(bot, settings)
            await process_reservation_reminders(bot, settings)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("reservation_reminder_loop tick failed")
