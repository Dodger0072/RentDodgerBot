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
from bot.services.user_discipline import WARNINGS_BAN_THRESHOLD
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


def _admin_tag_html(user_id: int, username: str | None) -> str:
    u = (username or "").strip().lstrip("@")
    if u:
        return f"@{escape(u)}"
    return f"id <code>{user_id}</code>"


def _ban_arg_plain(user_id: int, username: str | None) -> str:
    u = (username or "").strip().lstrip("@")
    return u if u else str(user_id)


async def notify_superadmins_discipline_warning(
    bot: Bot,
    settings: Settings,
    *,
    issuer_user_id: int,
    issuer_username: str | None,
    target_user_id: int,
    target_username: str | None,
    warnings_count: int,
    reason_plain: str,
    at_threshold_without_ban: bool,
) -> None:
    """Обычный админ выдал предупреждение — уведомить всех суперадминов (если роли заданы в .env)."""
    if not settings.superadmin_user_ids:
        return
    issuer = _admin_tag_html(issuer_user_id, issuer_username)
    target = _admin_tag_html(target_user_id, target_username)
    reason = escape((reason_plain or "").strip()[:500] or "—")
    extra = ""
    if at_threshold_without_ban:
        ban_arg = escape(_ban_arg_plain(target_user_id, target_username))
        extra = (
            f"\n\n⚠️ У пользователя уже <b>{WARNINGS_BAN_THRESHOLD}</b> предупреждений; "
            "автобан не применён (не суперадмин). Забанить вручную: "
            f"<code>/ban {ban_arg}</code>"
        )
    text = (
        "📋 <b>Предупреждение арендатору</b>\n"
        f"Выдал: {issuer} (id <code>{issuer_user_id}</code>)\n"
        f"Кому: {target} (id <code>{target_user_id}</code>)\n"
        f"Сейчас предупреждений: <b>{warnings_count}</b> из "
        f"<b>{WARNINGS_BAN_THRESHOLD}</b>\n"
        f"<b>Комментарий:</b> {reason}"
        f"{extra}"
    )
    for su_id in sorted(settings.superadmin_user_ids):
        try:
            await bot.send_message(su_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            continue
