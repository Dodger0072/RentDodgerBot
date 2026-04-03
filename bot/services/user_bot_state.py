from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import UserBotState


async def get_or_create_user_bot_state(session: AsyncSession, user_id: int) -> UserBotState:
    r = await session.execute(select(UserBotState).where(UserBotState.user_id == user_id))
    row = r.scalar_one_or_none()
    if row is None:
        row = UserBotState(user_id=user_id, main_menu_seen=False)
        session.add(row)
        await session.flush()
    return row


async def user_main_menu_seen(session: AsyncSession, user_id: int) -> bool:
    r = await session.execute(select(UserBotState.main_menu_seen).where(UserBotState.user_id == user_id))
    v = r.scalar_one_or_none()
    return bool(v)


async def mark_main_menu_seen(session: AsyncSession, user_id: int) -> None:
    row = await get_or_create_user_bot_state(session, user_id)
    row.main_menu_seen = True
    await session.flush()
