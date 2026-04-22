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
from bot.services.rental_logs import log_rental_event
from bot.services.item_owner import booking_reminder_recipient_ids, landlord_contact_hint_html
from bot.services.rental import ensure_utc, expire_expired_rentals, price_for_hours
from bot.time_format import format_local_time

logger = logging.getLogger(__name__)

# Остаток времени до начала брони (секунды): широкие окна под опрос раз в ~45 с
_REM_1H_LO = 50 * 60
_REM_1H_HI = 70 * 60
_REM_15M_LO = 10 * 60
_REM_15M_HI = 20 * 60


def _renter_line_html(res: Reservation) -> str:
    un = (res.username or "").strip().lstrip("@")
    if un:
        return f"@{escape(un)} (<code>{res.user_id}</code>)"
    return f"id <code>{res.user_id}</code>"


async def process_reservation_reminders(bot: Bot, settings: Settings) -> None:
    now = datetime.now(UTC)
    async with db_session.async_session_maker() as session:
        q = await session.execute(
            select(Reservation)
            .options(selectinload(Reservation.item))
            .where(
                (Reservation.notified_before_1h.is_(False))
                | (Reservation.notified_before_15m.is_(False))
                | (Reservation.notified_owner_before_1h.is_(False))
                | (Reservation.notified_owner_before_15m.is_(False)),
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
                contact = await landlord_contact_hint_html(bot, item, settings)
                text = (
                    "⏰ <b>Через час</b> начинается ваша бронь.\n"
                    f"Вещь: <b>{escape(item.name)}</b>\n"
                    f"С {format_local_time(start, settings)} по {format_local_time(end, settings)}\n\n"
                    f"{contact}"
                )
                await _send_reminder(bot, res.user_id, text)
                res.notified_before_1h = True
                changed = True

            if not res.notified_before_15m and _REM_15M_LO <= rem_sec <= _REM_15M_HI:
                contact = await landlord_contact_hint_html(bot, item, settings)
                text = (
                    "⏰ <b>Через 15 минут</b> начинается ваша бронь.\n"
                    f"Вещь: <b>{escape(item.name)}</b>\n"
                    f"С {format_local_time(start, settings)} по {format_local_time(end, settings)}\n\n"
                    f"{contact}"
                )
                await _send_reminder(bot, res.user_id, text)
                res.notified_before_15m = True
                changed = True

            if not res.notified_owner_before_1h and _REM_1H_LO <= rem_sec <= _REM_1H_HI:
                renter = _renter_line_html(res)
                owner_text = (
                    "⏰ <b>Через час</b> начинается бронь по вашей вещи.\n"
                    f"Вещь: <b>{escape(item.name)}</b>\n"
                    f"Арендатор: {renter}\n"
                    f"С {format_local_time(start, settings)} по {format_local_time(end, settings)}"
                )
                for admin_id in booking_reminder_recipient_ids(item, settings):
                    if admin_id == res.user_id:
                        continue
                    await _send_reminder(bot, admin_id, owner_text)
                res.notified_owner_before_1h = True
                changed = True

            if not res.notified_owner_before_15m and _REM_15M_LO <= rem_sec <= _REM_15M_HI:
                renter = _renter_line_html(res)
                owner_text = (
                    "⏰ <b>Через 15 минут</b> нужно сдать вещь арендатору.\n"
                    f"Вещь: <b>{escape(item.name)}</b>\n"
                    f"Арендатор: {renter}\n"
                    f"Начало слота: {format_local_time(start, settings)} — {format_local_time(end, settings)}"
                )
                for admin_id in booking_reminder_recipient_ids(item, settings):
                    if admin_id == res.user_id:
                        continue
                    await _send_reminder(bot, admin_id, owner_text)
                res.notified_owner_before_15m = True
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
            owner_uid = int(item.owner_user_id) if item.owner_user_id is not None else int(
                (booking_reminder_recipient_ids(item, settings) or [0])[0]
            )
            await log_rental_event(
                session,
                item_id=item.id,
                owner_user_id=owner_uid,
                rental_id=rental.id,
                renter_user_id=rental.user_id,
                renter_username=rental.username,
                event_type="request_created",
                requested_hours=rental.requested_hours,
                chosen_hours=None,
                note="Автозаявка из начала брони",
            )
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
            try:
                contact = await landlord_contact_hint_html(bot, item, settings)
                user_started = (
                    "✅ <b>Началось время вашей брони.</b>\n"
                    f"Вещь: <b>{escape(item.name)}</b>\n"
                    f"Слот: {format_local_time(start, settings)} — {format_local_time(end, settings)}.\n\n"
                    f"{contact}\n\n"
                    "Заявка на выдачу ушла арендодателю в боте — можно дождаться ответа здесь "
                    "или написать ему в Telegram."
                )
                await _send_reminder(bot, rental.user_id, user_started)
            except Exception:
                logger.exception("notify user for booking start failed")
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
