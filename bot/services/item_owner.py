from __future__ import annotations

from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings, is_superadmin
from bot.db.models import Item


def admin_manages_item(user_id: int, item: Item | None) -> bool:
    """Вещь без владельца (старые записи) — доступны всем админам; иначе только владельцу."""
    if item is None:
        return False
    if item.owner_user_id is None:
        return True
    return int(item.owner_user_id) == int(user_id)


def item_notification_recipients(item: Item, settings: Settings) -> list[int]:
    """Кому слать уведомления по заявкам/брони этой вещи."""
    if item.owner_user_id is not None:
        return [int(item.owner_user_id)]
    return sorted(settings.admin_user_ids)


def booking_reminder_recipient_ids(item: Item, settings: Settings) -> list[int]:
    """Напоминания о скорой брони: владелец вещи (если есть) и все ADMIN_USER_IDS — без дублей.

    Так сообщение доходит и при рассинхроне owner_user_id с реальным админом, и при пустом owner
    (как fallback в notify_admins_*).
    """
    ids: set[int] = set(int(x) for x in settings.admin_user_ids)
    for uid in item_notification_recipients(item, settings):
        ids.add(int(uid))
    return sorted(ids)


async def landlord_contact_hint_html(bot: Bot, item: Item, settings: Settings) -> str:
    """Короткая HTML-строка: кому в Telegram написать за выдачей вещи (для арендатора)."""
    if item.owner_user_id is not None:
        stored = (item.owner_username or "").strip().lstrip("@")
        if stored:
            return f"<b>Кому написать за вещью:</b> @{escape(stored)}"
        try:
            chat = await bot.get_chat(int(item.owner_user_id))
            un = getattr(chat, "username", None)
            if un:
                u = str(un).strip().lstrip("@")
                if u:
                    return f"<b>Кому написать за вещью:</b> @{escape(u)}"
        except TelegramBadRequest:
            pass
        return (
            f"<b>Владелец вещи</b> в Telegram: id <code>{item.owner_user_id}</code> "
            "(username в профиле не виден — напишите в известный вам чат команды)."
        )
    admins = sorted(settings.admin_usernames)
    if admins:
        tags = ", ".join(f"@{escape(a)}" for a in admins)
        return f"<b>Общая вещь — кому можно написать:</b> {tags}"
    return (
        "<i>Вещь без отдельного владельца. Свяжитесь с администраторами через ваш общий канал.</i>"
    )


async def items_owned_by_admin(session: AsyncSession, admin_user_id: int) -> list[Item]:
    """Только вещи с явным владельцем (не «общие» с owner_user_id IS NULL)."""
    r = await session.execute(
        select(Item).where(Item.owner_user_id == int(admin_user_id)).order_by(Item.id)
    )
    return list(r.scalars().unique())


async def items_blackout_scope_for_admin(session: AsyncSession, admin_user_id: int) -> list[Item]:
    """Вещи для blackout: свои + legacy общие без владельца."""
    r = await session.execute(
        select(Item)
        .where(or_(Item.owner_user_id == int(admin_user_id), Item.owner_user_id.is_(None)))
        .order_by(Item.id)
    )
    return list(r.scalars().unique())


def admin_can_edit_item(user_id: int, item: Item | None, settings: Settings) -> bool:
    """Редактирование: владелец или общая вещь (любой админ); суперадмин — любые."""
    if item is None:
        return False
    if is_superadmin(user_id, settings):
        return True
    return admin_manages_item(user_id, item)


def admin_can_delete_item(user_id: int, item: Item | None, settings: Settings) -> bool:
    """Удаление: только владелец своей вещи; общие legacy — только суперадмин; суперадмин — любые."""
    if item is None:
        return False
    if is_superadmin(user_id, settings):
        return True
    if item.owner_user_id is None:
        return False
    return int(item.owner_user_id) == int(user_id)
