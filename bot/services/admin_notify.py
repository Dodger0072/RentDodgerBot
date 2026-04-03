from __future__ import annotations

from decimal import Decimal
from html import escape

from aiogram import Bot
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import Item, Rental, Reservation
from bot.keyboards.inline import admin_rental_decision_keyboard
from bot.services.item_owner import item_notification_recipients
from bot.services.rental import format_money
from bot.time_format import format_local_time


async def notify_admins_pending_rental(
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    rental: Rental,
    item: Item,
    total: Decimal,
    planned_end,
    *,
    heading: str = "Новая заявка на аренду",
) -> None:
    uname = rental.username or "—"
    uid = rental.user_id
    text = (
        f"<b>{escape(heading)}</b>\n"
        f"Вещь: {escape(item.name)} (id {item.id})\n"
        f"Пользователь: @{uname} ({uid})\n"
        f"Часов по заявке: {rental.requested_hours}\n"
        f"Планируемый конец (по заявке): {format_local_time(planned_end, settings)}\n"
        f"Сумма: {format_money(total) if item.is_paid else '0$ (бесплатно)'}\n"
    )
    markup = admin_rental_decision_keyboard(rental.id)
    recipients = item_notification_recipients(item, settings)
    if not recipients:
        recipients = sorted(settings.admin_user_ids)
    first = True
    for admin_id in recipients:
        try:
            m = await bot.send_message(admin_id, text, reply_markup=markup)
            if first:
                rental.admin_message_chat_id = m.chat.id
                rental.admin_message_id = m.message_id
                first = False
        except Exception:
            continue
    await session.flush()


async def notify_admins_new_reservation(
    bot: Bot,
    settings: Settings,
    item: Item,
    reservation: Reservation,
    total: Decimal,
) -> None:
    """Информирование админов о создании брони (без действий в сообщении — управление: /bookings)."""
    uname = reservation.username or "—"
    uid = reservation.user_id
    text = (
        "<b>Новая бронь</b>\n"
        f"Бронь id: <code>{reservation.id}</code>\n"
        f"Вещь: {escape(item.name)} (id {item.id})\n"
        f"Пользователь: @{escape(uname.lstrip('@'))} ({uid})\n"
        f"Часов: {reservation.requested_hours}\n"
        f"С: {format_local_time(reservation.start_at, settings)}\n"
        f"По: {format_local_time(reservation.end_at, settings)}\n"
        f"Сумма: {format_money(total) if item.is_paid else '0$ (бесплатно)'}\n"
        "<i>Отмена: /bookings</i>"
    )
    recipients = item_notification_recipients(item, settings)
    if not recipients:
        recipients = sorted(settings.admin_user_ids)
    for admin_id in recipients:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            continue


async def notify_admins_user_cancelled_reservation(
    bot: Bot,
    settings: Settings,
    item: Item | None,
    *,
    reservation_id: int,
    user_id: int,
    username: str | None,
    hours: int,
    start_at,
    end_at,
) -> None:
    """Пользователь сам снял бронь в допустимый срок."""
    name = escape(item.name) if item else "?"
    iid = item.id if item else "?"
    uname = escape((username or "—").lstrip("@"))
    text = (
        "ℹ️ <b>Пользователь отменил бронь</b>\n"
        f"Бронь id: <code>{reservation_id}</code>\n"
        f"Вещь: {name} (id {iid})\n"
        f"Пользователь: @{uname} ({user_id})\n"
        f"Часов: {hours}\n"
        f"Было: {format_local_time(start_at, settings)} — {format_local_time(end_at, settings)}"
    )
    recipients = item_notification_recipients(item, settings) if item is not None else []
    if not recipients:
        recipients = sorted(settings.admin_user_ids)
    for admin_id in recipients:
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            continue
