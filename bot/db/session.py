from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import Settings
from bot.db.models import Base

engine = None
async_session_maker: async_sessionmaker[AsyncSession] | None = None


def setup_engine(settings: Settings) -> None:
    global engine, async_session_maker
    engine = create_async_engine(settings.database_url, echo=False)
    async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def _migrate_sqlite_reservation_columns(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(reservations)"))
    cols = {row[1] for row in r.fetchall()}
    if "notified_before_1h" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE reservations ADD COLUMN notified_before_1h BOOLEAN NOT NULL DEFAULT 0"
            )
        )
    if "notified_before_15m" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE reservations ADD COLUMN notified_before_15m BOOLEAN NOT NULL DEFAULT 0"
            )
        )


async def _migrate_sqlite_item_owner(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(items)"))
    cols = {row[1] for row in r.fetchall()}
    if "owner_user_id" not in cols:
        await conn.execute(text("ALTER TABLE items ADD COLUMN owner_user_id BIGINT"))
    if "owner_username" not in cols:
        await conn.execute(text("ALTER TABLE items ADD COLUMN owner_username VARCHAR(255)"))


async def _migrate_sqlite_item_category(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(items)"))
    cols = {row[1] for row in r.fetchall()}
    if "item_category" not in cols:
        await conn.execute(text("ALTER TABLE items ADD COLUMN item_category VARCHAR(64)"))


async def _migrate_sqlite_item_display_order(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(items)"))
    cols = {row[1] for row in r.fetchall()}
    if "display_order" not in cols:
        await conn.execute(
            text("ALTER TABLE items ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0")
        )


async def _migrate_sqlite_item_rent_hours(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(items)"))
    cols = {row[1] for row in r.fetchall()}
    if "rent_hours_min" not in cols:
        await conn.execute(text("ALTER TABLE items ADD COLUMN rent_hours_min INTEGER"))
    if "rent_hours_max" not in cols:
        await conn.execute(text("ALTER TABLE items ADD COLUMN rent_hours_max INTEGER"))


async def init_db() -> None:
    if engine is None:
        raise RuntimeError("Engine not configured")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_sqlite_reservation_columns(conn)
        await _migrate_sqlite_item_owner(conn)
        await _migrate_sqlite_item_category(conn)
        await _migrate_sqlite_item_display_order(conn)
        await _migrate_sqlite_item_rent_hours(conn)
