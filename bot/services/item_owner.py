from __future__ import annotations

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


def admin_can_delete_item(user_id: int, item: Item | None, settings: Settings) -> bool:
    """Удаление: только владелец своей вещи; общие legacy — только суперадмин; суперадмин — любые."""
    if item is None:
        return False
    if is_superadmin(user_id, settings):
        return True
    if item.owner_user_id is None:
        return False
    return int(item.owner_user_id) == int(user_id)
