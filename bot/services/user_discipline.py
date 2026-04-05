from __future__ import annotations

from html import escape

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import UserRentalDiscipline
from bot.services.user_bans import add_ban, is_user_banned, normalize_username

WARNINGS_BAN_THRESHOLD = 3
SUCCESSFUL_HANDOVERS_CLEAR_WARNINGS = 3
# Срок, о котором предупреждают пользователя в правилах (фактическая выдача — вручную арендодателем в боте).
RESPONSE_TO_LANDLORD_DEADLINE_MINUTES = 15


def discipline_username_norm(user_id: int, username: str | None) -> str:
    if username:
        return normalize_username(username)
    return f"__uid_{user_id}"


async def get_or_create_discipline(
    session: AsyncSession, user_id: int, username: str | None
) -> UserRentalDiscipline:
    r = await session.execute(
        select(UserRentalDiscipline).where(UserRentalDiscipline.user_id == user_id)
    )
    row = r.scalar_one_or_none()
    if row is None:
        row = UserRentalDiscipline(
            user_id=user_id,
            username_norm=discipline_username_norm(user_id, username),
            warnings=0,
            successful_handovers=0,
        )
        session.add(row)
        await session.flush()
        return row
    if username:
        row.username_norm = discipline_username_norm(user_id, username)
    return row


async def clear_warnings_for_user(
    session: AsyncSession, *, user_id: int, username: str | None
) -> tuple[str, int]:
    """Обнуляет предупреждения и счётчик успешных выдач.

    Возвращает (код, число):
    - ``("none", 0)`` — записи дисциплины не было;
    - ``("already", 0)`` — запись есть, предупреждений уже не было;
    - ``("cleared", n)`` — снято было ``n`` предупреждений (n > 0).
    """
    r = await session.execute(
        select(UserRentalDiscipline).where(UserRentalDiscipline.user_id == user_id)
    )
    row = r.scalar_one_or_none()
    if row is None:
        return "none", 0
    if username:
        row.username_norm = discipline_username_norm(user_id, username)
    prev = int(row.warnings)
    if prev == 0:
        return "already", 0
    row.warnings = 0
    row.successful_handovers = 0
    await session.flush()
    return "cleared", prev


async def warnings_count_for_user(session: AsyncSession, user_id: int) -> int:
    r = await session.execute(
        select(UserRentalDiscipline.warnings).where(UserRentalDiscipline.user_id == user_id)
    )
    v = r.scalar_one_or_none()
    return int(v or 0)


async def near_ban_notice_for_user(session: AsyncSession, user_id: int) -> str:
    """Если остался один шаг до бана — вернуть HTML для вставки с новой строки (или пустую строку)."""
    n = await warnings_count_for_user(session, user_id)
    if n != WARNINGS_BAN_THRESHOLD - 1:
        return ""
    return (
        "\n\n🔔 <b>Обратите внимание:</b> у вас уже "
        f"<b>{n} из {WARNINGS_BAN_THRESHOLD}</b> предупреждений.\n"
        "Если вы ещё раз не ответите арендодателю в течение "
        f"<b>{RESPONSE_TO_LANDLORD_DEADLINE_MINUTES} минут</b> после начала срока аренды и "
        "арендодатель выдаст предупреждение, доступ к боту будет <b>заблокирован</b>."
    )


async def record_successful_handover(
    session: AsyncSession, user_id: int, username: str | None
) -> None:
    d = await get_or_create_discipline(session, user_id, username)
    d.successful_handovers += 1
    if d.successful_handovers >= SUCCESSFUL_HANDOVERS_CLEAR_WARNINGS:
        d.warnings = 0
        d.successful_handovers = 0


async def add_warning(
    session: AsyncSession,
    *,
    user_id: int,
    username: str | None,
    reason_html: str,
    bot: Bot | None,
    ban_note: str,
    apply_auto_ban: bool = True,
) -> tuple[int, bool]:
    """Вернёт (число предупреждений после начисления, забанен ли сейчас в БД)."""
    if await is_user_banned(session, user_id=user_id, username=username):
        return 0, True
    d = await get_or_create_discipline(session, user_id, username)
    if d.warnings >= WARNINGS_BAN_THRESHOLD:
        banned_now = await is_user_banned(session, user_id=user_id, username=username)
        return d.warnings, banned_now
    d.warnings += 1
    await session.flush()
    count = d.warnings
    user_msg = (
        "⚠️ <b>Вам выдано предупреждение.</b>\n"
        f"Сейчас у вас: <b>{count} из {WARNINGS_BAN_THRESHOLD}</b> предупреждений.\n\n"
        "<b>Причина:</b>\n"
        f"{reason_html}\n\n"
        f"При <b>{WARNINGS_BAN_THRESHOLD}</b> предупреждениях доступ к боту блокируется. "
        f"<b>{SUCCESSFUL_HANDOVERS_CLEAR_WARNINGS}</b> успешные выдачи подряд обнуляют предупреждения."
    )
    if bot:
        try:
            await bot.send_message(user_id, user_msg, parse_mode=ParseMode.HTML)
        except TelegramForbiddenError:
            pass
        except TelegramBadRequest:
            pass

    banned = False
    if count >= WARNINGS_BAN_THRESHOLD:
        if apply_auto_ban:
            unorm = discipline_username_norm(user_id, username)
            await add_ban(
                session,
                username_norm=unorm,
                user_id=user_id,
                note=ban_note[:1950],
            )
            banned = True
            if bot:
                try:
                    await bot.send_message(
                        user_id,
                        "🚫 <b>Доступ к боту заблокирован</b> — набрано максимальное число предупреждений.",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramForbiddenError:
                    pass
                except TelegramBadRequest:
                    pass
        elif bot:
            try:
                await bot.send_message(
                    user_id,
                    "⏳ <b>Набрано максимальное число предупреждений.</b> "
                    "Решение о блокировке доступа принимает главный администратор.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramForbiddenError:
                pass
            except TelegramBadRequest:
                pass
    return count, banned


BOOKING_RULES_USER_HTML = (
    "⚠️ <b>Важно.</b> Если в течение "
    f"<b>{RESPONSE_TO_LANDLORD_DEADLINE_MINUTES} минут</b> после начала срока аренды "
    "ответа арендодателю не будет, <b>арендодатель</b> может выдать вам "
    "<b>предупреждение</b> (через бота).\n\n"
    f"<b>{WARNINGS_BAN_THRESHOLD} предупреждения</b> — запрет доступа к боту и аренде.\n\n"
    f"Предупреждения снимаются после <b>{SUCCESSFUL_HANDOVERS_CLEAR_WARNINGS}</b> успешных "
    "выдач в аренду (когда арендодатель подтвердил сдачу)."
)


def booking_rules_block() -> str:
    return "\n\n" + BOOKING_RULES_USER_HTML


async def list_users_with_warnings(session: AsyncSession) -> list[UserRentalDiscipline]:
    r = await session.execute(
        select(UserRentalDiscipline)
        .where(UserRentalDiscipline.warnings > 0)
        .order_by(UserRentalDiscipline.warnings.desc(), UserRentalDiscipline.user_id.asc())
    )
    return list(r.scalars().all())


def format_warn_reason_for_user(reason: str) -> str:
    return escape(reason.strip() or "нарушение правил.")

