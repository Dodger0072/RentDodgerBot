from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import UserBan


def normalize_username(raw: str) -> str:
    return raw.strip().lstrip("@").lower()


async def is_user_banned(
    session: AsyncSession, *, user_id: int, username: str | None
) -> bool:
    """Заблокирован по сохранённому user_id или по текущему @username."""
    conditions = [UserBan.user_id == user_id]
    if username:
        conditions.append(UserBan.username_norm == normalize_username(username))
    r = await session.execute(select(UserBan.id).where(or_(*conditions)).limit(1))
    return r.scalar_one_or_none() is not None


async def add_ban(
    session: AsyncSession,
    *,
    username_norm: str,
    user_id: int | None,
    note: str,
) -> UserBan:
    ban = UserBan(
        username_norm=username_norm,
        user_id=user_id,
        note=note,
        created_at=datetime.now(UTC),
    )
    session.add(ban)
    await session.flush()
    return ban


async def remove_ban_by_username(session: AsyncSession, username_norm: str) -> int:
    r = await session.execute(delete(UserBan).where(UserBan.username_norm == username_norm))
    await session.flush()
    return r.rowcount  # type: ignore[attr-defined]


async def list_bans(session: AsyncSession) -> list[UserBan]:
    q = await session.execute(select(UserBan).order_by(UserBan.created_at.desc()))
    return list(q.scalars().all())
