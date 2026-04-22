from __future__ import annotations

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import Settings
from bot.db.models import Base, BlackoutWindowItem, ItemBlackout

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
    if "notified_owner_before_1h" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE reservations ADD COLUMN notified_owner_before_1h BOOLEAN NOT NULL DEFAULT 0"
            )
        )
    if "notified_owner_before_15m" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE reservations ADD COLUMN notified_owner_before_15m BOOLEAN NOT NULL DEFAULT 0"
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


async def _migrate_sqlite_item_visibility(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(items)"))
    cols = {row[1] for row in r.fetchall()}
    if "is_visible" not in cols:
        await conn.execute(
            text("ALTER TABLE items ADD COLUMN is_visible BOOLEAN NOT NULL DEFAULT 1")
        )


async def _migrate_sqlite_rental_no_response_penalty(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(rentals)"))
    cols = {row[1] for row in r.fetchall()}
    if "no_response_penalty_applied" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE rentals ADD COLUMN no_response_penalty_applied BOOLEAN NOT NULL DEFAULT 0"
            )
        )


async def _migrate_sqlite_item_blackout_window(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(item_blackouts)"))
    cols = {row[1] for row in r.fetchall()}
    if "window_id" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE item_blackouts ADD COLUMN window_id INTEGER "
                "REFERENCES admin_blackout_windows(id) ON DELETE CASCADE"
            )
        )


async def _migrate_sqlite_item_blackout_subscription_cols(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(item_blackouts)"))
    cols = {row[1] for row in r.fetchall()}
    if "invoice_id" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE item_blackouts ADD COLUMN invoice_id INTEGER "
                "REFERENCES weekly_invoices(id) ON DELETE SET NULL"
            )
        )
    if "created_by_system" not in cols:
        await conn.execute(
            text("ALTER TABLE item_blackouts ADD COLUMN created_by_system BOOLEAN NOT NULL DEFAULT 0")
        )
    if "reason_code" not in cols:
        await conn.execute(text("ALTER TABLE item_blackouts ADD COLUMN reason_code VARCHAR(64)"))


async def migrate_blackout_window_links(session: AsyncSession) -> None:
    """Переносит связь общего окна из дублей item_blackouts в blackout_window_items (одно окно — без N строк в списке)."""
    r = await session.execute(select(ItemBlackout).where(ItemBlackout.window_id.is_not(None)))
    rows = list(r.scalars().unique())
    if not rows:
        return
    seen: set[tuple[int, int]] = set()
    for bo in rows:
        wid = bo.window_id
        if wid is None:
            continue
        key = (int(wid), int(bo.item_id))
        if key in seen:
            continue
        seen.add(key)
        session.add(BlackoutWindowItem(window_id=key[0], item_id=key[1]))
    await session.flush()
    await session.execute(delete(ItemBlackout).where(ItemBlackout.window_id.is_not(None)))


async def _migrate_sqlite_item_rent_hours(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(items)"))
    cols = {row[1] for row in r.fetchall()}
    if "rent_hours_min" not in cols:
        await conn.execute(text("ALTER TABLE items ADD COLUMN rent_hours_min INTEGER"))
    if "rent_hours_max" not in cols:
        await conn.execute(text("ALTER TABLE items ADD COLUMN rent_hours_max INTEGER"))


async def _migrate_sqlite_rental_handover_stat_actor(conn) -> None:
    if engine is None or "sqlite" not in str(engine.url).lower():
        return
    r = await conn.execute(text("PRAGMA table_info(rental_handover_stats)"))
    cols = {row[1] for row in r.fetchall()}
    if "handed_over_by_user_id" not in cols:
        await conn.execute(
            text("ALTER TABLE rental_handover_stats ADD COLUMN handed_over_by_user_id BIGINT")
        )


async def init_db() -> None:
    if engine is None:
        raise RuntimeError("Engine not configured")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_sqlite_reservation_columns(conn)
        await _migrate_sqlite_item_owner(conn)
        await _migrate_sqlite_item_category(conn)
        await _migrate_sqlite_item_display_order(conn)
        await _migrate_sqlite_item_visibility(conn)
        await _migrate_sqlite_item_rent_hours(conn)
        await _migrate_sqlite_rental_no_response_penalty(conn)
        await _migrate_sqlite_item_blackout_window(conn)
        await _migrate_sqlite_item_blackout_subscription_cols(conn)
        await _migrate_sqlite_rental_handover_stat_actor(conn)

    if async_session_maker is not None:
        async with async_session_maker() as session:
            async with session.begin():
                await migrate_blackout_window_links(session)
