from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import UserProfile


def normalize_server_nickname(raw: str) -> str | None:
    nickname = raw.strip()
    if not nickname:
        return None
    return nickname[:64]


async def get_server_nickname(session: AsyncSession, user_id: int) -> str | None:
    result = await session.execute(
        select(UserProfile.server_nickname).where(UserProfile.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def save_server_nickname(session: AsyncSession, user_id: int, nickname: str) -> None:
    profile = await session.get(UserProfile, user_id)
    if profile is None:
        session.add(UserProfile(user_id=user_id, server_nickname=nickname))
    else:
        profile.server_nickname = nickname
    await session.flush()
