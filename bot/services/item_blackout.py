from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from bot.config import Settings
from bot.db.models import Item, ItemBlackout, Rental, RentalState, Reservation
from bot.services.rental import ensure_utc
from bot.time_format import format_local_time


def blackout_user_cancel_text(
    item: Item, res: Reservation, settings: Settings
) -> str:
    return (
        "❌ <b>Ваша бронь отменена.</b>\n\n"
        f"Арендодатель не сможет сдать «{escape(item.name)}» в это время.\n"
        f"Слот: {format_local_time(res.start_at, settings)} — "
        f"{format_local_time(res.end_at, settings)}."
    )


def blackout_user_cancel_rental_text(
    item: Item, rental: Rental, settings: Settings
) -> str:
    return (
        "❌ <b>Ваша заявка на аренду отменена.</b>\n\n"
        f"Арендодатель не сможет сдать «{escape(item.name)}» в это время.\n"
        f"Слот: {format_local_time(rental.start_at, settings)} — "
        f"{format_local_time(rental.end_at, settings)}."
    )


def _handover_start_inside_blackout(
    slot_start: datetime, bo_start: datetime, bo_end: datetime
) -> bool:
    """Начало выдачи попадает в [bo_start, bo_end)? Пересечение слота с blackout без этого не отменяем."""
    rs, bs, be = ensure_utc(slot_start), ensure_utc(bo_start), ensure_utc(bo_end)
    if rs is None or bs is None or be is None or be <= bs:
        return False
    return bs <= rs < be


async def cancel_reservations_hit_by_blackout(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    item: Item,
    bo_start: datetime,
    bo_end: datetime,
) -> int:
    """Удаляет брони, у которых начало слота попадает в окно blackout; уведомляет. Пересечение «внутри аренды» допустимо."""
    bs, be = ensure_utc(bo_start), ensure_utc(bo_end)
    if bs is None or be is None or be <= bs:
        return 0
    r = await session.execute(select(Reservation).where(Reservation.item_id == item.id))
    removed = 0
    for res in list(r.scalars().unique()):
        rs, re_ = ensure_utc(res.start_at), ensure_utc(res.end_at)
        if rs is None or re_ is None:
            continue
        if not _handover_start_inside_blackout(rs, bs, be):
            continue
        text = blackout_user_cancel_text(item, res, settings)
        uid = res.user_id
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except TelegramForbiddenError:
            pass
        except TelegramBadRequest:
            pass
        except Exception:
            pass
        await session.delete(res)
        removed += 1
    return removed


async def cancel_pending_rentals_hit_by_blackout(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    item: Item,
    bo_start: datetime,
    bo_end: datetime,
) -> int:
    """Снимает pending_admin заявки, если начало слота попадает в blackout; уведомляет и правит сообщение админу."""
    bs, be = ensure_utc(bo_start), ensure_utc(bo_end)
    if bs is None or be is None or be <= bs:
        return 0
    r = await session.execute(
        select(Rental).where(
            Rental.item_id == item.id,
            Rental.state == RentalState.pending_admin.value,
        )
    )
    removed = 0
    for rental in list(r.scalars().unique()):
        rs, re_ = ensure_utc(rental.start_at), ensure_utc(rental.end_at)
        if rs is None or re_ is None:
            continue
        if not _handover_start_inside_blackout(rs, bs, be):
            continue
        text = blackout_user_cancel_rental_text(item, rental, settings)
        uid = rental.user_id
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except TelegramForbiddenError:
            pass
        except TelegramBadRequest:
            pass
        except Exception:
            pass
        cid, mid = rental.admin_message_chat_id, rental.admin_message_id
        if cid is not None and mid is not None:
            try:
                await bot.edit_message_text(
                    "<i>Заявка отменена: арендодатель недоступен в это время.</i>",
                    chat_id=int(cid),
                    message_id=int(mid),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                pass
            except TelegramForbiddenError:
                pass
            except Exception:
                pass
        await session.delete(rental)
        removed += 1
    return removed


async def add_item_blackout_record(
    session: AsyncSession,
    item_id: int,
    start_at: datetime,
    end_at: datetime,
    *,
    window_id: int | None = None,
) -> ItemBlackout:
    now = datetime.now(UTC)
    bo = ItemBlackout(
        item_id=item_id,
        start_at=start_at,
        end_at=end_at,
        created_at=now,
        window_id=window_id,
    )
    session.add(bo)
    await session.flush()
    return bo
