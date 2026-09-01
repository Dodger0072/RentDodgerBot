from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from bot.config import Settings
from bot.db import session as db_session
from bot.db.models import ManagedAdmin


async def load_managed_admin_ids(settings: Settings) -> None:
    """Добавляет сохранённые права в оперативную конфигурацию при старте."""
    async with db_session.async_session_maker() as session:
        result = await session.execute(select(ManagedAdmin.user_id))
        settings.admin_user_ids.update(int(user_id) for user_id in result.scalars())
        await session.commit()


async def list_managed_admins() -> list[ManagedAdmin]:
    async with db_session.async_session_maker() as session:
        result = await session.execute(select(ManagedAdmin).order_by(ManagedAdmin.created_at.asc()))
        rows = list(result.scalars())
        await session.commit()
    return rows


async def add_managed_admin(
    *, user_id: int, username: str | None, added_by_user_id: int, settings: Settings
) -> bool:
    """Сохраняет права; True означает, что это был новый администратор."""
    async with db_session.async_session_maker() as session:
        admin = await session.get(ManagedAdmin, user_id)
        if admin is not None:
            if username:
                admin.username = username
            await session.commit()
            settings.admin_user_ids.add(user_id)
            return False
        session.add(
            ManagedAdmin(
                user_id=user_id,
                username=username,
                added_by_user_id=added_by_user_id,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    settings.admin_user_ids.add(user_id)
    return True


async def remove_managed_admin(*, user_id: int, settings: Settings) -> bool:
    """Удаляет только роль, выданную через бота, не затрагивая .env."""
    async with db_session.async_session_maker() as session:
        admin = await session.get(ManagedAdmin, user_id)
        if admin is None:
            await session.commit()
            return False
        await session.delete(admin)
        await session.commit()
    if user_id not in settings.configured_admin_user_ids and user_id not in settings.superadmin_user_ids:
        settings.admin_user_ids.discard(user_id)
    return True
