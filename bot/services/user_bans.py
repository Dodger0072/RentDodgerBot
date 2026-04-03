from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Rental, Reservation, UserBan, UserRentalDiscipline


def normalize_username(raw: str) -> str:
    return raw.strip().lstrip("@").lower()


async def resolve_user_id_by_username_norm(
    session: AsyncSession, username_norm: str
) -> int | None:
    """Найти Telegram user_id по нику из данных бота.

    ``getChat(@username)`` у Bot API часто не работает для личных профилей
    (в отличие от каналов). Тогда ищем последние заявки/брони/бан/дисциплину.
    """
    if not username_norm:
        return None

    r = await session.execute(
        select(UserBan.user_id).where(UserBan.username_norm == username_norm).limit(1)
    )
    uid = r.scalar_one_or_none()
    if uid is not None:
        return int(uid)

    r = await session.execute(
        select(UserRentalDiscipline.user_id)
        .where(UserRentalDiscipline.username_norm == username_norm)
        .limit(1)
    )
    uid = r.scalar_one_or_none()
    if uid is not None:
        return int(uid)

    lu = func.lower(func.replace(Rental.username, "@", ""))
    r = await session.execute(
        select(Rental.user_id)
        .where(Rental.username.isnot(None), lu == username_norm)
        .order_by(Rental.id.desc())
        .limit(1)
    )
    uid = r.scalar_one_or_none()
    if uid is not None:
        return int(uid)

    lu2 = func.lower(func.replace(Reservation.username, "@", ""))
    r = await session.execute(
        select(Reservation.user_id)
        .where(Reservation.username.isnot(None), lu2 == username_norm)
        .order_by(Reservation.id.desc())
        .limit(1)
    )
    uid = r.scalar_one_or_none()
    if uid is not None:
        return int(uid)

    return None


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
