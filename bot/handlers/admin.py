from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import Settings, is_admin, is_superadmin
from bot.db.models import Item, ItemBlackout, Rental, RentalState, Reservation, UserBan
from bot.db import session as db_session
from bot.keyboards.inline import admin_hours_keyboard, admin_rental_decision_keyboard
from bot.services.booking_schedule import parse_booking_start_text
from bot.services.item_blackout import (
    add_item_blackout_record,
    cancel_pending_rentals_hit_by_blackout,
    cancel_reservations_hit_by_blackout,
)
from bot.services.item_owner import (
    admin_can_delete_item,
    admin_manages_item,
    items_blackout_scope_for_admin,
)
from bot.services.rental import ensure_utc, expire_expired_rentals, format_money, price_for_hours
from bot.services.user_bans import add_ban, list_bans, normalize_username, remove_ban_by_username
from bot.time_format import format_local_time
from bot.states import AddItemStates, AdminBlackoutStates, AdminRentalStates, AdminReservationStates
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

router = Router(name="admin")


def _admin_only(settings: Settings, user_id: int, username: str | None) -> bool:
    return is_admin(user_id, username, settings)


async def _finalize_rental_handover(
    session: AsyncSession,
    rental_id: int,
    hours: int,
    settings: Settings,
    acting_user_id: int,
) -> tuple[bool, str]:
    if hours < 1 or hours > 168:
        return False, "Часы должны быть от 1 до 168."
    now = datetime.now(UTC)
    end = now + timedelta(hours=hours)
    r = await session.execute(select(Rental).where(Rental.id == rental_id))
    rental = r.scalar_one_or_none()
    if rental is None or rental.state != RentalState.pending_admin.value:
        return False, "Заявка не найдена или уже обработана."
    r_item = await session.execute(select(Item).where(Item.id == rental.item_id))
    item = r_item.scalar_one()
    if not admin_manages_item(acting_user_id, item):
        return False, "Эта заявка относится не к вашим вещам."
    req_hours = rental.requested_hours
    rental.state = RentalState.active.value
    rental.start_at = now
    rental.end_at = end
    try:
        total = price_for_hours(item, req_hours, strict_paid_min=False)
    except ValueError:
        total = Decimal("0")
    await session.commit()
    end_label = format_local_time(end, settings)
    text = (
        f"Вещь сдана на {hours} ч. Аренда активна до {end_label}.\n"
        f"Сумма по заявке пользователя ({req_hours} ч): {format_money(total)}."
    )
    return True, text


@router.message(Command("add_item"))
async def cmd_add_item(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    await state.set_state(AddItemStates.name)
    await message.answer("Введите название вещи (как на кнопке у пользователя):")


@router.message(AddItemStates.name, F.text)
async def add_item_name(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(AddItemStates.description)
    await message.answer("Введите описание:")


@router.message(AddItemStates.description, F.text)
async def add_item_description(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    await state.update_data(description=message.text.strip(), photos=[])
    await state.set_state(AddItemStates.photos)
    await message.answer(
        "Пришлите фото (можно несколько сообщений). Когда закончите — напишите /done или отправьте команду без фото.",
    )


@router.message(AddItemStates.photos, Command("done"))
async def add_item_photos_done(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    await state.set_state(AddItemStates.is_paid)
    await message.answer("Категория: платная или бесплатная аренда? Ответьте словом «платная» или «бесплатная».")


@router.message(AddItemStates.photos, F.photo)
async def add_item_photo_collect(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    data = await state.get_data()
    photos: list[str] = list(data.get("photos") or [])
    fid = message.photo[-1].file_id
    photos.append(fid)
    await state.update_data(photos=photos)
    await message.answer("Фото добавлено. Ещё фото или /done")


@router.message(AddItemStates.is_paid, F.text)
async def add_item_is_paid(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    t = message.text.strip().lower()
    if "бесплат" in t:
        await state.update_data(is_paid=False)
        async with db_session.async_session_maker() as session:
            data = await state.get_data()
            item = Item(
                name=data["name"],
                description=data["description"],
                photos_json=json.dumps(data.get("photos") or [], ensure_ascii=False),
                is_paid=False,
                price_hour=None,
                price_day=None,
                price_week=None,
                owner_user_id=message.from_user.id,
                owner_username=message.from_user.username,
            )
            session.add(item)
            await session.commit()
        await state.clear()
        await message.answer("Вещь добавлена (бесплатная аренда).")
        return
    if "плат" in t:
        await state.update_data(is_paid=True)
        await state.set_state(AddItemStates.price_hour)
        await message.answer("Введите цену за час (число, например 100):")
        return
    await message.answer("Напишите «платная» или «бесплатная».")


@router.message(AddItemStates.price_hour, F.text)
async def add_item_price_hour(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    try:
        v = Decimal(message.text.strip().replace(",", "."))
    except InvalidOperation:
        await message.answer("Нужно число. Повторите цену за час:")
        return
    await state.update_data(price_hour=str(v))
    await state.set_state(AddItemStates.price_day)
    await message.answer("Цена за сутки (24 часа):")


@router.message(AddItemStates.price_day, F.text)
async def add_item_price_day(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    try:
        v = Decimal(message.text.strip().replace(",", "."))
    except InvalidOperation:
        await message.answer("Нужно число. Повторите цену за сутки:")
        return
    await state.update_data(price_day=str(v))
    await state.set_state(AddItemStates.price_week)
    await message.answer("Цена за неделю (168 часов):")


@router.message(AddItemStates.price_week, F.text)
async def add_item_price_week(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    try:
        v = Decimal(message.text.strip().replace(",", "."))
    except InvalidOperation:
        await message.answer("Нужно число. Повторите цену за неделю:")
        return
    data = await state.get_data()
    async with db_session.async_session_maker() as session:
        item = Item(
            name=data["name"],
            description=data["description"],
            photos_json=json.dumps(data.get("photos") or [], ensure_ascii=False),
            is_paid=True,
            price_hour=Decimal(data["price_hour"]),
            price_day=Decimal(data["price_day"]),
            price_week=v,
            owner_user_id=message.from_user.id,
            owner_username=message.from_user.username,
        )
        session.add(item)
        await session.commit()
    await state.clear()
    await message.answer("Вещь добавлена (платная аренда).")


@router.message(Command("list_items"))
async def cmd_list_items(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    uid = message.from_user.id
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Item)
            .where(or_(Item.owner_user_id.is_(None), Item.owner_user_id == uid))
            .order_by(Item.id)
        )
        items = list(r.scalars())
    if not items:
        await message.answer(
            "Нет ваших вещей (создайте через /add_item). "
            "Общие вещи без владельца из старой базы видны всем админам."
        )
        return
    lines = []
    for it in items:
        cat = "платная" if it.is_paid else "бесплатная"
        own = ""
        if it.owner_user_id is None:
            own = " | общая"
            if not is_superadmin(uid, settings):
                own += " (удалить: только суперадмин)"
        elif it.owner_user_id == uid:
            own = " | ваша"
        lines.append(f"{it.id}. {it.name} ({cat}{own})")
    await message.answer("Ваши и общие вещи:\n" + "\n".join(lines))


@router.message(Command("delete_item"))
async def cmd_delete_item(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /delete_item 5 — где 5 это id вещи из /list_items")
        return
    try:
        iid = int(parts[1].strip())
    except ValueError:
        await message.answer("Нужен числовой id.")
        return
    async with db_session.async_session_maker() as session:
        r = await session.execute(select(Item).where(Item.id == iid))
        item = r.scalar_one_or_none()
        if item is None:
            await message.answer("Вещь не найдена.")
            return
        if not admin_can_delete_item(message.from_user.id, item, settings):
            await session.rollback()
            if item.owner_user_id is None:
                await message.answer(
                    "Общая вещь без владельца может удалить только суперадмин "
                    "(переменная окружения SUPERADMIN_USER_IDS)."
                )
            else:
                await message.answer(
                    "Удалить может только владелец вещи или суперадмин."
                )
            return
        await session.delete(item)
        await session.commit()
    await message.answer(f"Вещь {iid} удалена.")


@router.message(Command("ban_user", "ban"))
async def cmd_ban_user(message: Message, bot: Bot, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    tokens = (message.text or "").strip().split()
    if len(tokens) < 2:
        await message.answer(
            "Использование: <code>/ban_user</code> или <code>/ban</code> — затем username и при желании комментарий.\n"
            "Пример: <code>/ban vasya_spam</code> или "
            "<code>/ban @vasya_spam нарушение правил</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    uname = normalize_username(tokens[1])
    if not uname:
        await message.answer("Укажите непустой username (без @ можно).")
        return
    note = " ".join(tokens[2:]).strip()[:2000]
    resolved_id: int | None = None
    try:
        chat = await bot.get_chat(f"@{uname}")
        if chat.id and chat.id > 0:
            resolved_id = int(chat.id)
    except TelegramBadRequest:
        pass

    async with db_session.async_session_maker() as session:
        r = await session.execute(select(UserBan).where(UserBan.username_norm == uname))
        if r.scalar_one_or_none() is not None:
            await session.rollback()
            await message.answer(f"Пользователь @{uname} уже в списке блокировки.")
            return
        if resolved_id is not None:
            r2 = await session.execute(select(UserBan).where(UserBan.user_id == resolved_id))
            if r2.scalar_one_or_none() is not None:
                await session.rollback()
                await message.answer(
                    "Этот Telegram-аккаунт уже заблокирован (другой username в базе). "
                    "Сначала /list_bans и при необходимости /unban_user."
                )
                return
        try:
            await add_ban(session, username_norm=uname, user_id=resolved_id, note=note)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await message.answer("Не удалось сохранить (конфликт в базе). Проверьте /list_bans.")
            return

    hint = ""
    if resolved_id is None:
        hint = (
            "\n\n<i>User_id не удалось определить (нет чата с пользователем или неверный username) — "
            "блокировка только по @username при следующем обращении с этим ником.</i>"
        )
    await message.answer(
        f"Заблокирован @{escape(uname)}"
        + (f" (id <code>{resolved_id}</code>)" if resolved_id else "")
        + "."
        + (f" Комментарий: {escape(note)}" if note else "")
        + hint
        + "\nПользователь не сможет пользоваться ботом.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("unban_user", "unban"))
async def cmd_unban_user(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/unban_user</code> или <code>/unban</code> username (с @ или без)",
            parse_mode=ParseMode.HTML,
        )
        return
    uname = normalize_username(parts[1])
    async with db_session.async_session_maker() as session:
        n = await remove_ban_by_username(session, uname)
        await session.commit()
    if n:
        await message.answer(f"Блокировка снята для @{escape(uname)}.", parse_mode=ParseMode.HTML)
    else:
        await message.answer("Такого username в списке блокировок нет.")


@router.message(Command("list_bans"))
async def cmd_list_bans(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    async with db_session.async_session_maker() as session:
        rows = await list_bans(session)
        await session.commit()
    if not rows:
        await message.answer("Список блокировок пуст.")
        return
    lines = []
    for b in rows:
        uid = f"{b.user_id}" if b.user_id is not None else "—"
        lines.append(
            f"@{escape(b.username_norm)} | id {uid} | {format_local_time(b.created_at, settings)}"
            + (f"\n   {escape(b.note)}" if b.note else "")
        )
    text = "<b>Блокировки</b>\n\n" + "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "…"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("add_blackout"))
async def cmd_add_blackout(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    await state.set_state(AdminBlackoutStates.waiting_start)
    await message.answer(
        "Окно недоступности для <b>всех ваших управляемых вещей</b>.\n\n"
        "Будут сняты пересекающиеся <b>брони</b> и <b>ожидающие выдачи заявки</b> по каждой из них.\n\n"
        "Начало окна: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(AdminBlackoutStates.waiting_start, F.text)
async def blackout_start(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    if (message.text or "").strip().startswith("/"):
        await state.clear()
        return
    parsed = parse_booking_start_text(message.text or "", settings)
    if parsed is None:
        await message.answer("Не разобрал дату. Пример: <code>04.05.2026 10:00</code>", parse_mode=ParseMode.HTML)
        return
    await state.update_data(blackout_start_iso=parsed.isoformat())
    await state.set_state(AdminBlackoutStates.waiting_end)
    await message.answer("Конец окна — тот же формат <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>:", parse_mode=ParseMode.HTML)


@router.message(AdminBlackoutStates.waiting_end, F.text)
async def blackout_end(message: Message, state: FSMContext, bot: Bot, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    if (message.text or "").strip().startswith("/"):
        await state.clear()
        return
    data = await state.get_data()
    start_iso = data.get("blackout_start_iso")
    if start_iso is None:
        await state.clear()
        return
    end_parsed = parse_booking_start_text(message.text or "", settings)
    if end_parsed is None:
        await message.answer("Не разобрал дату конца.", parse_mode=ParseMode.HTML)
        return
    start_at = ensure_utc(datetime.fromisoformat(str(start_iso)))
    end_at = ensure_utc(end_parsed)
    if start_at is None or end_at is None or end_at <= start_at:
        await message.answer("Конец должен быть позже начала.")
        return

    admin_id = message.from_user.id
    async with db_session.async_session_maker() as session:
        items = await items_blackout_scope_for_admin(session, admin_id)
        if not items:
            await session.rollback()
            await state.clear()
            await message.answer(
                "У вас пока нет вещей, по которым вы управляете выдачей. "
                "Добавьте вещь через /add_item."
            )
            return
        n_res = 0
        n_rent = 0
        names: list[str] = []
        for item in items:
            n_res += await cancel_reservations_hit_by_blackout(
                session, bot, settings, item, start_at, end_at
            )
            n_rent += await cancel_pending_rentals_hit_by_blackout(
                session, bot, settings, item, start_at, end_at
            )
            await add_item_blackout_record(session, item.id, start_at, end_at)
            names.append(item.name)
        await session.commit()
    await state.clear()
    preview = ", ".join(escape(n) for n in names[:12])
    if len(names) > 12:
        preview += f"… (+{len(names) - 12})"
    await message.answer(
        f"Окно недоступности добавлено для <b>{len(names)}</b> вещей: {preview}\n"
        f"Интервал: {format_local_time(start_at, settings)} — {format_local_time(end_at, settings)}.\n"
        f"Снято броней: {n_res}, заявок на выдачу (ожидают админа): {n_rent}.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("list_blackouts"))
async def cmd_list_blackouts(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(ItemBlackout)
            .options(selectinload(ItemBlackout.item))
            .order_by(ItemBlackout.start_at.desc()),
        )
        rows = list(r.scalars().unique())
        await session.commit()
    rows = [bo for bo in rows if admin_manages_item(message.from_user.id, bo.item)]
    if not rows:
        await message.answer("Окон недоступности по вашим вещам нет.")
        return
    lines = []
    for bo in rows:
        it = bo.item
        name = escape(it.name if it else "?")
        lines.append(
            f"• id <code>{bo.id}</code> | {name}\n"
            f"  {format_local_time(bo.start_at, settings)} — {format_local_time(bo.end_at, settings)}"
        )
    text = "<b>Окна недоступности выдачи</b>\n\n" + "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "…"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("delete_blackout"))
async def cmd_delete_blackout(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /delete_blackout 3 — где 3 это id из /list_blackouts")
        return
    try:
        bid = int(parts[1].strip())
    except ValueError:
        await message.answer("Нужен числовой id.")
        return
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(ItemBlackout)
            .options(selectinload(ItemBlackout.item))
            .where(ItemBlackout.id == bid)
        )
        bo = r.scalar_one_or_none()
        if bo is None:
            await session.rollback()
            await message.answer("Запись не найдена.")
            return
        if not admin_manages_item(message.from_user.id, bo.item):
            await session.rollback()
            await message.answer("Это окно на чужой вещи.")
            return
        await session.delete(bo)
        await session.commit()
    await message.answer(f"Окно недоступности #{bid} удалено.")


def _booking_line_reservation(res: Reservation, settings: Settings) -> str:
    item = res.item
    name = escape(item.name if item else "?")
    un = escape((res.username or "—").lstrip("@"))
    return (
        f"• <b>[бронь] #{res.id}</b> {name} | @{un} | "
        f"{format_local_time(res.start_at, settings)} → "
        f"{format_local_time(res.end_at, settings)} | {res.requested_hours} ч."
    )


def _booking_line_rental(rent: Rental, settings: Settings) -> str:
    item = rent.item
    name = escape(item.name if item else "?")
    un = escape((rent.username or "—").lstrip("@"))
    st = ensure_utc(rent.start_at)
    en = ensure_utc(rent.end_at)
    start_l = format_local_time(st, settings) if st else "?"
    end_l = format_local_time(en, settings) if en else "?"
    return (
        f"• <b>[аренда] #{rent.id}</b> {name} | @{un} | "
        f"{start_l} → {end_l} | {rent.requested_hours} ч."
    )


def _bookings_cancel_keyboard(rows: list[tuple[str, int]]) -> InlineKeyboardBuilder:
    """rows: («res»|«rt», id)."""
    b = InlineKeyboardBuilder()
    for kind, eid in rows:
        if kind == "res":
            b.row(
                InlineKeyboardButton(
                    text=f"Отменить бронь #{eid}",
                    callback_data=f"adm:cnl:res:{eid}",
                )
            )
        else:
            b.row(
                InlineKeyboardButton(
                    text=f"Отменить аренду #{eid}",
                    callback_data=f"adm:cnl:rt:{eid}",
                )
            )
    return b


def _booking_sort_key_reservation(res: Reservation) -> datetime:
    st = ensure_utc(res.start_at)
    return st or datetime.min.replace(tzinfo=UTC)


def _booking_sort_key_rental(rent: Rental) -> datetime:
    st = ensure_utc(rent.start_at)
    return st or datetime.min.replace(tzinfo=UTC)


@router.message(Command("bookings"))
async def cmd_bookings(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    now = datetime.now(UTC)
    uid = message.from_user.id
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        r = await session.execute(
            select(Reservation)
            .options(selectinload(Reservation.item))
            .order_by(Reservation.start_at.asc())
        )
        all_res = list(r.scalars().unique())
        r2 = await session.execute(
            select(Rental)
            .options(selectinload(Rental.item))
            .where(
                Rental.state == RentalState.active.value,
                Rental.end_at.is_not(None),
            )
        )
        all_rent = list(r2.scalars().unique())
        await session.commit()

    upcoming_res = [
        x
        for x in all_res
        if ensure_utc(x.end_at) is not None
        and ensure_utc(x.end_at) > now
        and admin_manages_item(uid, x.item)
    ]
    active_rent = [
        x
        for x in all_rent
        if ensure_utc(x.end_at) is not None
        and ensure_utc(x.end_at) > now
        and admin_manages_item(uid, x.item)
    ]

    typed_rows: list[tuple[datetime, str, Reservation | Rental]] = []
    for res in upcoming_res:
        typed_rows.append((_booking_sort_key_reservation(res), "res", res))
    for rent in active_rent:
        typed_rows.append((_booking_sort_key_rental(rent), "rt", rent))
    typed_rows.sort(key=lambda x: x[0])

    if not typed_rows:
        await message.answer("Нет записей: ни будущих броней, ни действующих аренд.")
        return

    full_lines: list[str] = []
    full_kb_keys: list[tuple[str, int]] = []
    for _, kind, obj in typed_rows:
        if kind == "res":
            assert isinstance(obj, Reservation)
            full_lines.append(_booking_line_reservation(obj, settings))
            full_kb_keys.append(("res", obj.id))
        else:
            assert isinstance(obj, Rental)
            full_lines.append(_booking_line_rental(obj, settings))
            full_kb_keys.append(("rt", obj.id))

    header = (
        f"<b>Брони и аренды</b> ({len(typed_rows)}). «Отменить …» — затем причина "
        f"(пользователь её увидит).\n\n"
    )
    chunk_size = 10
    for i in range(0, len(full_lines), chunk_size):
        chunk_lines = full_lines[i : i + chunk_size]
        chunk_keys = full_kb_keys[i : i + chunk_size]
        text = header if i == 0 else "<b>Продолжение списка</b>\n\n"
        text += "\n".join(chunk_lines)
        kb = _bookings_cancel_keyboard(chunk_keys).as_markup()
        if len(text) > 4000:
            text = text[:3990] + "…"
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "adm:res:abort")
async def admin_res_cancel_abort(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await query.message.edit_text("Снятие брони отменено.")
    await query.answer()


@router.callback_query(F.data.regexp(r"^adm:cnl:(res|rt):(\d+)$"))
async def admin_booking_cancel_ask_reason(
    query: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = query.data.split(":")
    kind = parts[2]
    eid = int(parts[3])
    now = datetime.now(UTC)
    if kind == "res":
        async with db_session.async_session_maker() as session:
            r = await session.execute(
                select(Reservation)
                .options(selectinload(Reservation.item))
                .where(Reservation.id == eid)
            )
            res = r.scalar_one_or_none()
            if res is None:
                await query.answer("Бронь не найдена", show_alert=True)
                return
            if ensure_utc(res.end_at) is not None and ensure_utc(res.end_at) <= now:
                await query.answer("Бронь уже в прошлом", show_alert=True)
                return
            if not admin_manages_item(query.from_user.id, res.item):
                await query.answer("Это не ваша вещь.", show_alert=True)
                return
        label = "Бронь"
    else:
        async with db_session.async_session_maker() as session:
            r = await session.execute(
                select(Rental)
                .options(selectinload(Rental.item))
                .where(Rental.id == eid)
            )
            rent = r.scalar_one_or_none()
            if rent is None:
                await query.answer("Аренда не найдена", show_alert=True)
                return
            if rent.state != RentalState.active.value:
                await query.answer("Это не активная аренда", show_alert=True)
                return
            if ensure_utc(rent.end_at) is not None and ensure_utc(rent.end_at) <= now:
                await query.answer("Аренда уже завершена", show_alert=True)
                return
            if not admin_manages_item(query.from_user.id, rent.item):
                await query.answer("Это не ваша вещь.", show_alert=True)
                return
        label = "Аренда"

    await state.set_state(AdminReservationStates.waiting_cancel_reason)
    await state.update_data(cancel_kind=kind, cancel_id=eid)
    abort_kb = InlineKeyboardBuilder()
    abort_kb.row(InlineKeyboardButton(text="« Не отменять", callback_data="adm:res:abort"))
    await query.message.answer(
        f"{label} <b>#{eid}</b>. Отправьте <b>причину</b> одним сообщением "
        "(текст увидит пользователь).",
        reply_markup=abort_kb.as_markup(),
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


@router.message(AdminReservationStates.waiting_cancel_reason, F.text)
async def admin_res_cancel_apply(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    if (message.text or "").strip().startswith("/"):
        await state.clear()
        await message.answer("Ввод отмены прерван.")
        return
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Причина не может быть пустой. Отправьте текст или нажмите «Не отменять» в прошлом сообщении.")
        return
    data = await state.get_data()
    kind = data.get("cancel_kind")
    eid = data.get("cancel_id")
    if kind not in ("res", "rt") or eid is None:
        await state.clear()
        return
    eid = int(eid)
    now = datetime.now(UTC)
    user_text: str
    done_label: str

    if kind == "res":
        async with db_session.async_session_maker() as session:
            r = await session.execute(
                select(Reservation)
                .options(selectinload(Reservation.item))
                .where(Reservation.id == eid)
            )
            res = r.scalar_one_or_none()
            if res is None:
                await session.rollback()
                await state.clear()
                await message.answer("Бронь уже удалена или не найдена.")
                return
            if ensure_utc(res.end_at) is not None and ensure_utc(res.end_at) <= now:
                await session.rollback()
                await state.clear()
                await message.answer("Бронь уже в прошлом, отмена не нужна.")
                return
            if not admin_manages_item(message.from_user.id, res.item):
                await session.rollback()
                await state.clear()
                await message.answer("Это не ваша вещь.")
                return
            user_id = res.user_id
            item_name = res.item.name if res.item else "?"
            start_at = res.start_at
            end_at = res.end_at
            req_h = res.requested_hours
            await session.delete(res)
            await session.commit()

        user_text = (
            "❌ <b>Бронь отменена администратором.</b>\n\n"
            f"Вещь: <b>{escape(item_name)}</b>\n"
            f"Было: {format_local_time(start_at, settings)} — {format_local_time(end_at, settings)} "
            f"({req_h} ч.)\n"
            f"<b>Причина:</b> {escape(reason)}"
        )
        done_label = "Бронь"
    else:
        async with db_session.async_session_maker() as session:
            r = await session.execute(
                select(Rental)
                .options(selectinload(Rental.item))
                .where(Rental.id == eid)
            )
            rent = r.scalar_one_or_none()
            if rent is None:
                await session.rollback()
                await state.clear()
                await message.answer("Аренда не найдена.")
                return
            if rent.state != RentalState.active.value:
                await session.rollback()
                await state.clear()
                await message.answer("Это уже не активная аренда.")
                return
            if ensure_utc(rent.end_at) is not None and ensure_utc(rent.end_at) <= now:
                await session.rollback()
                await state.clear()
                await message.answer("Аренда уже завершена.")
                return
            if not admin_manages_item(message.from_user.id, rent.item):
                await session.rollback()
                await state.clear()
                await message.answer("Это не ваша вещь.")
                return
            user_id = rent.user_id
            item_name = rent.item.name if rent.item else "?"
            start_at = rent.start_at
            end_at = rent.end_at
            req_h = rent.requested_hours
            await session.delete(rent)
            await session.commit()

        user_text = (
            "❌ <b>Аренда прервана администратором.</b>\n\n"
            f"Вещь: <b>{escape(item_name)}</b>\n"
            f"Было: {format_local_time(start_at, settings)} — {format_local_time(end_at, settings)} "
            f"({req_h} ч. по заявке)\n"
            f"<b>Причина:</b> {escape(reason)}"
        )
        done_label = "Аренда"

    notified = True
    try:
        await message.bot.send_message(user_id, user_text, parse_mode=ParseMode.HTML)
    except TelegramForbiddenError:
        notified = False
    except TelegramBadRequest:
        notified = False
    await state.clear()
    if notified:
        await message.answer(f"{done_label} #{eid} снята, пользователь уведомлён.")
    else:
        await message.answer(
            f"{done_label} #{eid} снята в базе. Уведомление пользователю не доставлено "
            "(нет чата с ботом или другая ошибка)."
        )


@router.callback_query(F.data.regexp(r"^adm:r:(\d+):no$"))
async def admin_rental_reject(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    rid = int(query.data.split(":")[2])
    data = await state.get_data()
    if data.get("pending_rental_id") == rid:
        await state.clear()
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Rental).options(selectinload(Rental.item)).where(Rental.id == rid)
        )
        rental = r.scalar_one_or_none()
        if rental is None or rental.state != RentalState.pending_admin.value:
            await query.answer("Заявка не найдена или уже обработана", show_alert=True)
            return
        if not admin_manages_item(query.from_user.id, rental.item):
            await query.answer("Это не ваша вещь.", show_alert=True)
            return
        await session.delete(rental)
        await session.commit()
    await query.message.edit_text("Отмечено: вещь не сдана. Заявка снята.")
    await query.answer()


@router.callback_query(F.data.regexp(r"^adm:r:(\d+):ok$"))
async def admin_rental_ok(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    rid = int(query.data.split(":")[2])
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Rental).options(selectinload(Rental.item)).where(Rental.id == rid)
        )
        rental = r.scalar_one_or_none()
        if rental is None or rental.state != RentalState.pending_admin.value:
            await query.answer("Заявка не найдена или уже обработана", show_alert=True)
            return
        if not admin_manages_item(query.from_user.id, rental.item):
            await query.answer("Это не ваша вещь.", show_alert=True)
            return
    base = query.message.html_text or query.message.text or ""
    hint = (
        "\n\n<i>Выберите срок сдачи кнопкой или отправьте число часов "
        "(от 1 до 168) обычным сообщением в чат.</i>"
    )
    await query.message.edit_text(
        base + hint,
        reply_markup=admin_hours_keyboard(rid),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AdminRentalStates.waiting_handover_hours)
    await state.update_data(
        pending_rental_id=rid,
        handover_chat_id=query.message.chat.id,
        handover_message_id=query.message.message_id,
    )
    await query.answer()


@router.callback_query(F.data.regexp(r"^adm:r:(\d+):cancel$"))
async def admin_rental_cancel(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    rid = int(query.data.split(":")[2])
    data = await state.get_data()
    if data.get("pending_rental_id") == rid:
        await state.clear()
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Rental).options(selectinload(Rental.item)).where(Rental.id == rid)
        )
        rental = r.scalar_one_or_none()
        if rental is None:
            await query.answer()
            return
        if rental.state == RentalState.pending_admin.value:
            if not admin_manages_item(query.from_user.id, rental.item):
                await query.answer("Это не ваша вещь.", show_alert=True)
                return
            await session.delete(rental)
            await session.commit()
            await query.message.edit_text("Выбор срока отменён, заявка снята.")
        else:
            await query.message.edit_text("Уже обработано.")
    await query.answer()


@router.callback_query(F.data.regexp(r"^adm:r:(\d+):h:(\d+)$"))
async def admin_rental_hours(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = query.data.split(":")
    rid = int(parts[2])
    hours = int(parts[4])
    async with db_session.async_session_maker() as session:
        ok, text = await _finalize_rental_handover(
            session, rid, hours, settings, query.from_user.id
        )
    if not ok:
        await query.answer(text, show_alert=True)
        return
    data = await state.get_data()
    if data.get("pending_rental_id") == rid:
        await state.clear()
    await query.message.edit_text(text)
    await query.answer()


@router.message(AdminRentalStates.waiting_handover_hours, F.text)
async def admin_handover_hours_text(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    data = await state.get_data()
    rid = data.get("pending_rental_id")
    chat_id = data.get("handover_chat_id")
    msg_id = data.get("handover_message_id")
    if rid is None or chat_id is None or msg_id is None:
        await state.clear()
        return
    if (message.text or "").strip().startswith("/"):
        await state.clear()
        return
    try:
        hours = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число часов от 1 до 168.")
        return
    async with db_session.async_session_maker() as session:
        ok, text = await _finalize_rental_handover(
            session, int(rid), hours, settings, message.from_user.id
        )
    if not ok:
        await message.answer(text)
        return
    await message.bot.edit_message_text(text, chat_id=int(chat_id), message_id=int(msg_id))
    await state.clear()
    await message.answer("Срок зафиксирован.")
