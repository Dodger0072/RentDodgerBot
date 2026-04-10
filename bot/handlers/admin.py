from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import (
    Settings,
    can_autoban_from_warnings,
    can_ban_via_bot_commands,
    is_admin,
    is_superadmin,
    superadmin_roles_enabled,
)
from bot.db.models import (
    AdminBlackoutWindow,
    BlackoutWindowItem,
    Item,
    ItemBlackout,
    Rental,
    RentalState,
    Reservation,
    UserBan,
)
from bot.db import session as db_session
from bot.keyboards.inline import (
    admin_panel_keyboard,
    admin_hours_keyboard,
    admin_item_category_keyboard,
    admin_rental_decision_keyboard,
    category_keyboard_for_admin,
    edit_item_category_keyboard,
    edit_item_menu_keyboard,
)
from bot.item_categories import ITEM_CATEGORY_SLUGS, UNCATEGORIZED_SLUG, item_category_label
from bot.services.booking_schedule import parse_booking_start_text
from bot.services.item_blackout import (
    cancel_pending_rentals_hit_by_blackout,
    cancel_reservations_hit_by_blackout,
)
from bot.services.item_order import next_display_order_for_group, reorder_item_to_position
from bot.services.item_owner import (
    admin_can_delete_item,
    admin_can_edit_item,
    admin_manages_item,
    items_blackout_scope_for_admin,
)
from bot.services.rental import (
    MAX_RENT_HOURS,
    ensure_utc,
    expire_expired_rentals,
    format_money,
    price_for_hours,
    rent_hours_bounds,
)
from bot.services.admin_notify import notify_superadmins_discipline_warning
from bot.services.rental_stats import fetch_rental_stats, record_handover_stat
from bot.services.user_bans import (
    add_ban,
    is_user_banned,
    list_bans,
    normalize_username,
    remove_ban_by_username,
    resolve_user_id_by_username_norm,
)
from bot.services.user_discipline import (
    WARNINGS_BAN_THRESHOLD,
    add_warning,
    clear_warnings_for_user,
    format_warn_reason_for_user,
    list_users_with_warnings,
    record_successful_handover,
)
from bot.time_format import format_local_time
from bot.states import (
    AddItemStates,
    AdminBlackoutStates,
    AdminRentalStates,
    AdminReservationStates,
    EditItemStates,
)
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

router = Router(name="admin")


class _PanelMessageProxy:
    """Прокси message для вызова command-хендлеров из callback-кнопок.

    В callback у исходного Message автор — бот. Для проверок прав и логики
    нужен реальный пользователь из CallbackQuery.from_user.
    """

    def __init__(self, query: CallbackQuery, text: str = "") -> None:
        self.from_user = query.from_user
        self.text = text
        self._message = query.message

    async def answer(self, *args, **kwargs):
        return await self._message.answer(*args, **kwargs)


def _omit_fsm_keys(d: dict, *keys: str) -> dict:
    drop = set(keys)
    return {k: v for k, v in d.items() if k not in drop}


def _admin_only(settings: Settings, user_id: int, username: str | None) -> bool:
    # Суперадмин должен иметь доступ ко всем админским командам,
    # даже если не продублирован в ADMIN_USER_IDS/ADMIN_USERNAMES.
    return is_admin(user_id, username, settings) or is_superadmin(user_id, settings)


async def _safe_query_answer(query: CallbackQuery, *args, **kwargs) -> None:
    """Игнорируем протухшие callback'и, чтобы хендлер не падал."""
    try:
        await query.answer(*args, **kwargs)
    except TelegramBadRequest as exc:
        low = str(exc).lower()
        if "query is too old" in low or "query id is invalid" in low:
            return
        raise


def _admin_panel_pick_item_keyboard(
    items: list[Item], *, mode: str, uid: int, settings: Settings
) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for it in items:
        tag = "платн." if it.is_paid else "беспл."
        owner = "общая" if it.owner_user_id is None else "ваша"
        if it.owner_user_id is not None and it.owner_user_id != uid:
            owner = "чужая"
        if mode == "delete" and it.owner_user_id is None and not is_superadmin(uid, settings):
            owner = "общая (только суперадмин)"
        b.row(
            InlineKeyboardButton(
                text=f"#{it.id} {it.name} ({tag}, {owner})",
                callback_data=f"adm:panel:{mode}:{it.id}",
            )
        )
    b.row(InlineKeyboardButton(text="« Назад в панель", callback_data="adm:panel"))
    return b


async def _show_admin_item_picker(
    target: Message, *, mode: str, uid: int, settings: Settings
) -> None:
    async with db_session.async_session_maker() as session:
        r = await session.execute(select(Item).order_by(Item.display_order.asc(), Item.id.asc()))
        all_items = list(r.scalars().unique())
        await session.commit()
    if mode == "edit":
        items = [it for it in all_items if admin_can_edit_item(uid, it, settings)]
        if not items:
            await target.answer("Нет вещей, доступных для редактирования.")
            return
        kb = _admin_panel_pick_item_keyboard(items, mode="edit", uid=uid, settings=settings)
        await target.answer("Выберите вещь для редактирования:", reply_markup=kb.as_markup())
        return
    items = [it for it in all_items if admin_can_delete_item(uid, it, settings)]
    if not items:
        await target.answer("Нет вещей, доступных для удаления.")
        return
    kb = _admin_panel_pick_item_keyboard(items, mode="delete", uid=uid, settings=settings)
    await target.answer("Выберите вещь для удаления:", reply_markup=kb.as_markup())


def _admin_panel_blackout_keyboard(
    rows: list[tuple[int, datetime, datetime, str]], settings: Settings
) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for bid, start_at, end_at, title in rows:
        b.row(
            InlineKeyboardButton(
                text=f"#{bid} {title}: {format_local_time(start_at, settings)} - {format_local_time(end_at, settings)}",
                callback_data=f"adm:panel:delblackout:{bid}",
            )
        )
    b.row(InlineKeyboardButton(text="« Назад в панель", callback_data="adm:panel"))
    return b


async def _show_admin_blackout_picker(target: Message, *, uid: int, settings: Settings) -> None:
    now = datetime.now(UTC)
    rows: list[tuple[int, datetime, datetime, str]] = []
    async with db_session.async_session_maker() as session:
        r_w = await session.execute(
            select(AdminBlackoutWindow).where(
                AdminBlackoutWindow.owner_user_id == uid,
                AdminBlackoutWindow.end_at > now,
            )
        )
        windows = list(r_w.scalars().unique())
        for w in windows:
            rows.append((w.id, w.start_at, w.end_at, "общее окно"))

        r_b = await session.execute(
            select(ItemBlackout)
            .options(selectinload(ItemBlackout.item))
            .where(
                ItemBlackout.window_id.is_(None),
                ItemBlackout.end_at > now,
            )
        )
        legacy = list(r_b.scalars().unique())
        await session.commit()
    for bo in legacy:
        if not admin_manages_item(uid, bo.item):
            continue
        name = escape(bo.item.name if bo.item else "?")
        rows.append((bo.id, bo.start_at, bo.end_at, f"одна вещь ({name})"))
    rows.sort(key=lambda x: x[1])
    if not rows:
        await target.answer("Нет окон неактива для удаления.")
        return
    kb = _admin_panel_blackout_keyboard(rows, settings)
    await target.answer("Выберите окно неактива для удаления:", reply_markup=kb.as_markup())


@router.callback_query(F.data == "adm:panel")
async def admin_open_panel(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await query.message.answer("Панель администратора:", reply_markup=admin_panel_keyboard())
    await query.answer()


@router.callback_query(F.data.regexp(r"^adm:panel:([^:]+)$"))
async def admin_panel_action(
    query: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    action = (query.data or "").split(":")[2]
    await query.answer()
    msg = _PanelMessageProxy(query)
    if action == "add_item":
        msg.text = "/add_item"
        await cmd_add_item(msg, state, settings)
        return
    if action == "list_items":
        msg.text = "/list_items"
        await cmd_list_items(msg, settings)
        return
    if action == "pick_edit":
        await _show_admin_item_picker(
            query.message, mode="edit", uid=query.from_user.id, settings=settings
        )
        return
    if action == "pick_delete":
        await _show_admin_item_picker(
            query.message, mode="delete", uid=query.from_user.id, settings=settings
        )
        return
    if action == "bookings":
        msg.text = "/bookings"
        await cmd_bookings(msg, settings)
        return
    if action == "rent_stats":
        msg.text = "/rent_stats"
        await cmd_rent_stats(msg, settings)
        return
    if action == "add_blackout":
        msg.text = "/add_blackout"
        await cmd_add_blackout(msg, state, settings)
        return
    if action == "list_blackouts":
        msg.text = "/list_blackouts"
        await cmd_list_blackouts(msg, settings)
        return
    if action == "pick_delete_blackout":
        await _show_admin_blackout_picker(query.message, uid=query.from_user.id, settings=settings)
        return
    if action == "list_bans":
        msg.text = "/list_bans"
        await cmd_list_bans(msg, settings)
        return
    if action == "list_warnings":
        msg.text = "/list_warnings"
        await cmd_list_warnings(msg, settings)
        return
    await query.message.answer(
        "Неизвестный пункт панели. Откройте панель заново.",
        reply_markup=category_keyboard_for_admin(is_admin_user=True),
    )


@router.callback_query(F.data.regexp(r"^adm:panel:(edit|delete):(\d+)$"))
async def admin_panel_item_action(
    query: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = (query.data or "").split(":")
    mode = parts[2]
    item_id = int(parts[3])
    await query.answer()

    if mode == "edit":
        await state.clear()
        async with db_session.async_session_maker() as session:
            item, err = await _require_editable_item(
                session,
                item_id,
                query.from_user.id,
                query.from_user.username,
                settings,
            )
            if err:
                await query.message.answer(_edit_item_err_text(err))
                return
        await query.message.answer(
            _edit_item_header_html(item),
            reply_markup=edit_item_menu_keyboard(item.id, is_paid=bool(item.is_paid)),
            parse_mode=ParseMode.HTML,
        )
        return

    async with db_session.async_session_maker() as session:
        r = await session.execute(select(Item).where(Item.id == item_id))
        item = r.scalar_one_or_none()
        if item is None:
            await query.message.answer("Вещь не найдена.")
            return
        if not admin_can_delete_item(query.from_user.id, item, settings):
            await session.rollback()
            if item.owner_user_id is None:
                await query.message.answer(
                    "Общую вещь без владельца может удалить только суперадмин."
                )
            else:
                await query.message.answer("Удалить может только владелец вещи или суперадмин.")
            return
        name = item.name
        await session.delete(item)
        await session.commit()
    await state.clear()
    await query.message.answer(f"Вещь удалена: {name}")


@router.callback_query(F.data.regexp(r"^adm:panel:delblackout:(\d+)$"))
async def admin_panel_delete_blackout(query: CallbackQuery, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    bid = int((query.data or "").split(":")[3])
    uid = query.from_user.id
    await query.answer()
    async with db_session.async_session_maker() as session:
        w = await session.get(AdminBlackoutWindow, bid)
        if w is not None:
            if w.owner_user_id != uid:
                await session.rollback()
                await query.message.answer("Такого общего окна нет или оно создано другим администратором.")
                return
            await session.delete(w)
            await session.commit()
            await query.message.answer(f"Окно неактива #{bid} удалено со всех вещей.")
            return
        r = await session.execute(
            select(ItemBlackout)
            .options(selectinload(ItemBlackout.item))
            .where(ItemBlackout.id == bid)
        )
        bo = r.scalar_one_or_none()
        if bo is None:
            await session.rollback()
            await query.message.answer("Запись не найдена.")
            return
        if bo.window_id is not None:
            w2 = await session.get(AdminBlackoutWindow, bo.window_id)
            if w2 is not None and w2.owner_user_id == uid:
                wid = w2.id
                await session.delete(w2)
                await session.commit()
                await query.message.answer(f"Окно неактива #{wid} удалено со всех вещей.")
                return
            await session.rollback()
            await query.message.answer("Удаляйте по id общего окна из /list_blackouts.")
            return
        if not admin_manages_item(uid, bo.item):
            await session.rollback()
            await query.message.answer("Это окно на чужой вещи.")
            return
        await session.delete(bo)
        await session.commit()
    await query.message.answer(f"Окно неактива #{bid} удалено.")


async def _require_editable_item(
    session: AsyncSession,
    item_id: int,
    user_id: int,
    username: str | None,
    settings: Settings,
) -> tuple[Item | None, str | None]:
    if not _admin_only(settings, user_id, username):
        return None, "access"
    r = await session.execute(select(Item).where(Item.id == item_id))
    item = r.scalar_one_or_none()
    if item is None:
        return None, "missing"
    if not admin_can_edit_item(user_id, item, settings):
        return None, "rights"
    return item, None


def _edit_item_header_html(item: Item) -> str:
    cat = "платная" if item.is_paid else "бесплатная"
    ic = item_category_label(item.item_category)
    lo, hi = rent_hours_bounds(item)
    prices = ""
    if item.is_paid:
        ph = item.price_hour if item.price_hour is not None else Decimal("0")
        pd = item.price_day if item.price_day is not None else Decimal("0")
        pw = item.price_week if item.price_week is not None else Decimal("0")
        prices = (
            f"\nЦены: {format_money(ph)} / ч, {format_money(pd)} / сут., "
            f"{format_money(pw)} / нед."
        )
    return (
        f"<b>Вещь #{item.id}</b> — {escape(item.name)}\n"
        f"{cat} · {ic} · срок {lo}–{hi} ч{prices}\n\n"
        f"Что изменить?"
    )


async def _finalize_rental_handover(
    session: AsyncSession,
    rental_id: int,
    hours: int,
    settings: Settings,
    acting_user_id: int,
) -> tuple[bool, str]:
    now = datetime.now(UTC)
    r = await session.execute(select(Rental).where(Rental.id == rental_id))
    rental = r.scalar_one_or_none()
    if rental is None or rental.state != RentalState.pending_admin.value:
        return False, "Заявка не найдена или уже обработана."
    r_item = await session.execute(select(Item).where(Item.id == rental.item_id))
    item = r_item.scalar_one()
    if not admin_manages_item(acting_user_id, item):
        return False, "Эта заявка относится не к вашим вещам."
    lo, hi = rent_hours_bounds(item)
    if hours < lo or hours > hi:
        return False, f"Часы должны быть от {lo} до {hi} (по правилам этой вещи)."
    end = now + timedelta(hours=hours)
    req_hours = rental.requested_hours
    rental.state = RentalState.active.value
    rental.start_at = now
    rental.end_at = end
    try:
        total = price_for_hours(item, req_hours)
    except ValueError:
        total = Decimal("0")
    record_handover_stat(
        session,
        item_id=rental.item_id,
        amount=total,
        handed_over_at=now,
        handed_over_by_user_id=acting_user_id,
    )
    await record_successful_handover(session, rental.user_id, rental.username)
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
    await message.answer(
        "Введите название вещи (как на кнопке у пользователя):\n\n"
        "<i>Шаг назад в этом диалоге: /back (на первом шаге только подсказка). "
        "Со слова «назад» то же самое, кроме ввода названия — там «назад» идёт в название.</i>",
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(StateFilter(AddItemStates.category), F.data == "adm:addcat:back")
async def add_item_category_back_cb(
    query: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        await state.clear()
        return
    data = dict(await state.get_data())
    await state.set_data(_omit_fsm_keys(data, "item_category"))
    await state.set_state(AddItemStates.description)
    await query.message.edit_text("Введите описание:")
    await query.answer()


@router.message(StateFilter(AddItemStates), Command("back"))
async def add_item_back_command(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    st = await state.get_state()
    data = dict(await state.get_data())
    if st == AddItemStates.name.state:
        await message.answer(
            "Вы на первом шаге. Введите название или начните заново: /add_item"
        )
        return
    await _add_item_step_back(message, state, settings, st, data)


@router.message(
    StateFilter(
        AddItemStates.description,
        AddItemStates.category,
        AddItemStates.photos,
        AddItemStates.is_paid,
        AddItemStates.rent_hours_min,
        AddItemStates.rent_hours_max,
        AddItemStates.price_hour,
        AddItemStates.price_day,
        AddItemStates.price_week,
    ),
    F.text.lower() == "назад",
)
async def add_item_back_word(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    st = await state.get_state()
    data = dict(await state.get_data())
    await _add_item_step_back(message, state, settings, st, data)


async def _add_item_step_back(
    message: Message,
    state: FSMContext,
    settings: Settings,
    st: str,
    data: dict,
) -> None:
    if st == AddItemStates.description.state:
        await state.set_data(_omit_fsm_keys(data, "description"))
        await state.set_state(AddItemStates.name)
        await message.answer("Введите название вещи (как на кнопке у пользователя):")
        return
    if st == AddItemStates.category.state:
        await state.set_data(_omit_fsm_keys(data, "item_category"))
        await state.set_state(AddItemStates.description)
        await message.answer("Введите описание:")
        return
    if st == AddItemStates.photos.state:
        await state.set_data(_omit_fsm_keys(data, "item_category"))
        await state.set_state(AddItemStates.category)
        await message.answer(
            "Выберите <b>категорию</b> вещи:",
            reply_markup=admin_item_category_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    if st == AddItemStates.is_paid.state:
        await state.set_data(_omit_fsm_keys(data, "is_paid"))
        await state.set_state(AddItemStates.photos)
        await message.answer(
            "Пришлите фото (можно несколько сообщений). Когда закончите — напишите /done."
        )
        return
    if st == AddItemStates.rent_hours_min.state:
        await state.set_data(_omit_fsm_keys(data, "rent_hours_min"))
        await state.set_state(AddItemStates.is_paid)
        await message.answer(
            "Категория: платная или бесплатная аренда? Ответьте словом «платная» или «бесплатная»."
        )
        return
    if st == AddItemStates.rent_hours_max.state:
        await state.set_data(_omit_fsm_keys(data, "rent_hours_min"))
        await state.set_state(AddItemStates.rent_hours_min)
        await message.answer(
            "Минимальный срок аренды в часах (целое число от 1 до 168):"
        )
        return
    if st == AddItemStates.price_hour.state:
        new_data = _omit_fsm_keys(data, "price_hour", "rent_hours_max")
        await state.set_data(new_data)
        await state.set_state(AddItemStates.rent_hours_max)
        n = new_data.get("rent_hours_min")
        if n is None:
            await state.set_state(AddItemStates.rent_hours_min)
            await message.answer(
                "Минимальный срок аренды в часах (целое число от 1 до 168):"
            )
            return
        await message.answer(
            f"Максимальный срок аренды в часах "
            f"(не меньше {n}, не больше {MAX_RENT_HOURS}):"
        )
        return
    if st == AddItemStates.price_day.state:
        await state.set_data(_omit_fsm_keys(data, "price_day"))
        await state.set_state(AddItemStates.price_hour)
        await message.answer("Введите цену за час (число, например 100):")
        return
    if st == AddItemStates.price_week.state:
        await state.set_data(_omit_fsm_keys(data, "price_week"))
        await state.set_state(AddItemStates.price_day)
        await message.answer("Цена за сутки (24 часа):")


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
    await state.set_state(AddItemStates.category)
    await message.answer(
        "Выберите <b>категорию</b> вещи:",
        reply_markup=admin_item_category_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(StateFilter(AddItemStates.category), F.data.startswith("adm:addcat:"))
async def add_item_category_cb(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        await state.clear()
        return
    parts = (query.data or "").split(":")
    slug = parts[2] if len(parts) > 2 else ""
    if slug not in ITEM_CATEGORY_SLUGS:
        await query.answer("Неизвестная категория", show_alert=True)
        return
    await state.update_data(item_category=slug)
    await state.set_state(AddItemStates.photos)
    await query.message.edit_text(
        f"Категория: <b>{item_category_label(slug)}</b>.\n\n"
        "Пришлите фото (можно несколько сообщений). Когда закончите — напишите /done.",
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


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
        await state.set_state(AddItemStates.rent_hours_min)
        await message.answer(
            "Минимальный срок аренды в часах (целое число от 1 до 168):"
        )
        return
    if "плат" in t:
        await state.update_data(is_paid=True)
        await state.set_state(AddItemStates.rent_hours_min)
        await message.answer(
            "Минимальный срок аренды в часах (целое число от 1 до 168):"
        )
        return
    await message.answer("Напишите «платная» или «бесплатная».")


@router.message(AddItemStates.rent_hours_min, F.text)
async def add_item_rent_hours_min(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    try:
        n = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число часов (от 1 до 168).")
        return
    if n < 1 or n > MAX_RENT_HOURS:
        await message.answer(f"Укажите число от 1 до {MAX_RENT_HOURS}.")
        return
    await state.update_data(rent_hours_min=n)
    await state.set_state(AddItemStates.rent_hours_max)
    await message.answer(
        f"Максимальный срок аренды в часах "
        f"(не меньше {n}, не больше {MAX_RENT_HOURS}):"
    )


@router.message(AddItemStates.rent_hours_max, F.text)
async def add_item_rent_hours_max(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    data = await state.get_data()
    n = data.get("rent_hours_min")
    if n is None:
        await state.clear()
        return
    try:
        m = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число часов.")
        return
    if m < n or m > MAX_RENT_HOURS:
        await message.answer(f"Допустимо от {n} до {MAX_RENT_HOURS} ч.")
        return
    if data.get("is_paid"):
        await state.update_data(rent_hours_max=m)
        await state.set_state(AddItemStates.price_hour)
        await message.answer("Введите цену за час (число, например 100):")
        return
    async with db_session.async_session_maker() as session:
        ord_val = await next_display_order_for_group(
            session, is_paid=False, item_category=data.get("item_category")
        )
        item = Item(
            name=data["name"],
            description=data["description"],
            photos_json=json.dumps(data.get("photos") or [], ensure_ascii=False),
            is_paid=False,
            price_hour=None,
            price_day=None,
            price_week=None,
            item_category=data.get("item_category"),
            display_order=ord_val,
            owner_user_id=message.from_user.id,
            owner_username=message.from_user.username,
            rent_hours_min=int(n),
            rent_hours_max=m,
        )
        session.add(item)
        await session.commit()
    await state.clear()
    await message.answer("Вещь добавлена (бесплатная аренда).")


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
        ord_val = await next_display_order_for_group(
            session, is_paid=True, item_category=data.get("item_category")
        )
        item = Item(
            name=data["name"],
            description=data["description"],
            photos_json=json.dumps(data.get("photos") or [], ensure_ascii=False),
            is_paid=True,
            price_hour=Decimal(data["price_hour"]),
            price_day=Decimal(data["price_day"]),
            price_week=v,
            item_category=data.get("item_category"),
            display_order=ord_val,
            owner_user_id=message.from_user.id,
            owner_username=message.from_user.username,
            rent_hours_min=int(data["rent_hours_min"]),
            rent_hours_max=int(data["rent_hours_max"]),
        )
        session.add(item)
        await session.commit()
    await state.clear()
    await message.answer("Вещь добавлена (платная аренда).")


def _edit_item_cb_filter(query: CallbackQuery) -> bool:
    d = query.data or ""
    return d.startswith("adm:e:") and not d.startswith("adm:ec:")


def _edit_item_err_text(code: str | None) -> str:
    if code == "missing":
        return "Вещь не найдена."
    if code == "rights":
        return (
            "Редактировать может владелец вещи, любой админ для «общей» вещи без владельца "
            "или суперадмин."
        )
    return "Нет доступа."


@router.message(Command("edit_item"))
async def cmd_edit_item(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /edit_item 5 — где 5 это id вещи из /list_items")
        return
    try:
        iid = int(parts[1].strip())
    except ValueError:
        await message.answer("Нужен числовой id.")
        return
    async with db_session.async_session_maker() as session:
        item, err = await _require_editable_item(
            session,
            iid,
            message.from_user.id,
            message.from_user.username,
            settings,
        )
        if err:
            await message.answer(_edit_item_err_text(err))
            return
    await message.answer(
        _edit_item_header_html(item),
        reply_markup=edit_item_menu_keyboard(item.id, is_paid=bool(item.is_paid)),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(_edit_item_cb_filter)
async def edit_item_action_cb(
    query: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) < 4:
        await query.answer("Ошибка", show_alert=True)
        return
    try:
        iid = int(parts[2])
    except ValueError:
        await query.answer("Ошибка", show_alert=True)
        return
    action = parts[3]

    async with db_session.async_session_maker() as session:
        item, err = await _require_editable_item(
            session,
            iid,
            query.from_user.id,
            query.from_user.username,
            settings,
        )

        if action == "menu":
            if err or item is None:
                await query.answer(_edit_item_err_text(err), show_alert=True)
                return
            try:
                await query.message.edit_text(
                    _edit_item_header_html(item),
                    reply_markup=edit_item_menu_keyboard(
                        item.id, is_paid=bool(item.is_paid)
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                await query.message.answer(
                    _edit_item_header_html(item),
                    reply_markup=edit_item_menu_keyboard(
                        item.id, is_paid=bool(item.is_paid)
                    ),
                    parse_mode=ParseMode.HTML,
                )
            await query.answer()
            return

        if action == "x":
            if err or item is None:
                await query.answer(_edit_item_err_text(err), show_alert=True)
                return
            try:
                await query.message.edit_text(
                    "Меню редактирования закрыто. Снова: <code>/edit_item id</code>",
                    reply_markup=None,
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                await query.message.answer(
                    "Меню закрыто. Снова: /edit_item id",
                )
            await query.answer()
            return

        if err or item is None:
            await query.answer(_edit_item_err_text(err), show_alert=True)
            return

        if action == "tofree":
            if not item.is_paid:
                await query.answer("Уже бесплатная аренда.", show_alert=True)
                return
            item.is_paid = False
            item.price_hour = None
            item.price_day = None
            item.price_week = None
            item.display_order = await next_display_order_for_group(
                session, False, item.item_category
            )
            await session.commit()
            try:
                await query.message.edit_text(
                    _edit_item_header_html(item),
                    reply_markup=edit_item_menu_keyboard(
                        item.id, is_paid=False
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                await query.message.answer(
                    _edit_item_header_html(item),
                    reply_markup=edit_item_menu_keyboard(item.id, is_paid=False),
                    parse_mode=ParseMode.HTML,
                )
            await query.answer("Тип: бесплатная.")
            return

        if action == "topaid":
            if item.is_paid:
                await query.answer("Уже платная аренда.", show_alert=True)
                return

        if action == "prices":
            if not item.is_paid:
                await query.answer(
                    "Цены задаются только для платной аренды.",
                    show_alert=True,
                )
                return

    if action == "topaid":
        await query.answer()
        await state.set_state(EditItemStates.rent_hours_min)
        await state.update_data(edit_item_id=iid, edit_flow="topaid")
        await query.message.answer(
            "Минимальный срок аренды в часах (целое число от 1 до 168):"
        )
        return

    if action == "nm":
        await state.set_state(EditItemStates.name)
        await state.update_data(edit_item_id=iid)
        await query.message.answer("Введите новое название:")
        await query.answer()
        return
    if action == "dc":
        await state.set_state(EditItemStates.description)
        await state.update_data(edit_item_id=iid)
        await query.message.answer("Введите новое описание:")
        await query.answer()
        return
    if action == "ct":
        await query.message.answer(
            "Выберите категорию:",
            reply_markup=edit_item_category_keyboard(iid),
            parse_mode=ParseMode.HTML,
        )
        await query.answer()
        return
    if action == "ph":
        await state.set_state(EditItemStates.photos)
        await state.update_data(edit_item_id=iid, photos=[])
        await query.message.answer(
            "Пришлите новые фото (можно несколько). Они <b>заменят</b> все текущие. "
            "Когда закончите — напишите /done.",
            parse_mode=ParseMode.HTML,
        )
        await query.answer()
        return
    if action == "rh":
        await state.set_state(EditItemStates.rent_hours_min)
        await state.update_data(edit_item_id=iid, edit_flow="rent")
        await query.message.answer(
            "Минимальный срок аренды в часах (целое число от 1 до 168):"
        )
        await query.answer()
        return
    if action == "prices":
        await query.answer()
        await state.set_state(EditItemStates.price_hour)
        await state.update_data(edit_item_id=iid, edit_flow="prices")
        await query.message.answer("Введите цену за час (число, например 100):")
        return

    await query.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("adm:ec:"))
async def edit_item_category_cb(query: CallbackQuery, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) < 4:
        await query.answer("Ошибка", show_alert=True)
        return
    try:
        iid = int(parts[2])
    except ValueError:
        await query.answer("Ошибка", show_alert=True)
        return
    slug = parts[3]
    if slug == UNCATEGORIZED_SLUG:
        new_cat: str | None = None
    elif slug in ITEM_CATEGORY_SLUGS:
        new_cat = slug
    else:
        await query.answer("Неизвестная категория", show_alert=True)
        return

    async with db_session.async_session_maker() as session:
        item, err = await _require_editable_item(
            session,
            iid,
            query.from_user.id,
            query.from_user.username,
            settings,
        )
        if err or item is None:
            await query.answer(_edit_item_err_text(err), show_alert=True)
            return
        old = item.item_category
        item.item_category = new_cat
        if old != item.item_category:
            item.display_order = await next_display_order_for_group(
                session, bool(item.is_paid), item.item_category
            )
        await session.commit()
    try:
        await query.message.edit_text(
            _edit_item_header_html(item),
            reply_markup=edit_item_menu_keyboard(iid, is_paid=bool(item.is_paid)),
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        await query.message.answer(
            _edit_item_header_html(item),
            reply_markup=edit_item_menu_keyboard(iid, is_paid=bool(item.is_paid)),
            parse_mode=ParseMode.HTML,
        )
    await query.answer("Категория обновлена.")


@router.message(EditItemStates.name, F.text)
async def edit_item_name(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    data = await state.get_data()
    iid = data.get("edit_item_id")
    if iid is None:
        await state.clear()
        return
    name = (message.text or "").strip()
    async with db_session.async_session_maker() as session:
        item, err = await _require_editable_item(
            session,
            int(iid),
            message.from_user.id,
            message.from_user.username,
            settings,
        )
        if err or item is None:
            await state.clear()
            await message.answer(_edit_item_err_text(err))
            return
        item.name = name
        await session.commit()
        paid = bool(item.is_paid)
    await state.clear()
    await message.answer(
        "Название обновлено.",
        reply_markup=edit_item_menu_keyboard(int(iid), is_paid=paid),
    )


@router.message(EditItemStates.description, F.text)
async def edit_item_description(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    data = await state.get_data()
    iid = data.get("edit_item_id")
    if iid is None:
        await state.clear()
        return
    desc = (message.text or "").strip()
    async with db_session.async_session_maker() as session:
        item, err = await _require_editable_item(
            session,
            int(iid),
            message.from_user.id,
            message.from_user.username,
            settings,
        )
        if err or item is None:
            await state.clear()
            await message.answer(_edit_item_err_text(err))
            return
        item.description = desc
        await session.commit()
        paid = bool(item.is_paid)
    await state.clear()
    await message.answer(
        "Описание обновлено.",
        reply_markup=edit_item_menu_keyboard(int(iid), is_paid=paid),
    )


@router.message(EditItemStates.photos, Command("done"))
async def edit_item_photos_done(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    data = await state.get_data()
    iid = data.get("edit_item_id")
    if iid is None:
        await state.clear()
        return
    photos: list[str] = list(data.get("photos") or [])
    async with db_session.async_session_maker() as session:
        item, err = await _require_editable_item(
            session,
            int(iid),
            message.from_user.id,
            message.from_user.username,
            settings,
        )
        if err or item is None:
            await state.clear()
            await message.answer(_edit_item_err_text(err))
            return
        item.photos_json = json.dumps(photos, ensure_ascii=False)
        await session.commit()
        paid = bool(item.is_paid)
    await state.clear()
    await message.answer(
        "Фото обновлены.",
        reply_markup=edit_item_menu_keyboard(int(iid), is_paid=paid),
    )


@router.message(EditItemStates.photos, F.photo)
async def edit_item_photo_collect(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    data = await state.get_data()
    photos: list[str] = list(data.get("photos") or [])
    fid = message.photo[-1].file_id
    photos.append(fid)
    await state.update_data(photos=photos)
    await message.answer("Фото добавлено. Ещё фото или /done")


@router.message(EditItemStates.rent_hours_min, F.text)
async def edit_item_rent_hours_min(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    data = await state.get_data()
    iid = data.get("edit_item_id")
    if iid is None:
        await state.clear()
        return
    try:
        n = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число часов (от 1 до 168).")
        return
    if n < 1 or n > MAX_RENT_HOURS:
        await message.answer(f"Укажите число от 1 до {MAX_RENT_HOURS}.")
        return
    await state.update_data(rent_hours_min=n)
    await state.set_state(EditItemStates.rent_hours_max)
    await message.answer(
        f"Максимальный срок аренды в часах "
        f"(не меньше {n}, не больше {MAX_RENT_HOURS}):"
    )


@router.message(EditItemStates.rent_hours_max, F.text)
async def edit_item_rent_hours_max(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    data = await state.get_data()
    iid = data.get("edit_item_id")
    flow = data.get("edit_flow", "rent")
    n = data.get("rent_hours_min")
    if iid is None or n is None:
        await state.clear()
        return
    try:
        m = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число часов.")
        return
    if m < int(n) or m > MAX_RENT_HOURS:
        await message.answer(f"Допустимо от {n} до {MAX_RENT_HOURS} ч.")
        return

    if flow == "topaid":
        await state.update_data(rent_hours_max=m)
        await state.set_state(EditItemStates.price_hour)
        await message.answer("Введите цену за час (число, например 100):")
        return

    async with db_session.async_session_maker() as session:
        item, err = await _require_editable_item(
            session,
            int(iid),
            message.from_user.id,
            message.from_user.username,
            settings,
        )
        if err or item is None:
            await state.clear()
            await message.answer(_edit_item_err_text(err))
            return
        item.rent_hours_min = int(n)
        item.rent_hours_max = m
        await session.commit()
        paid = bool(item.is_paid)
    await state.clear()
    await message.answer(
        "Сроки аренды обновлены.",
        reply_markup=edit_item_menu_keyboard(int(iid), is_paid=paid),
    )


@router.message(EditItemStates.price_hour, F.text)
async def edit_item_price_hour(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    try:
        v = Decimal(message.text.strip().replace(",", "."))
    except InvalidOperation:
        await message.answer("Нужно число. Повторите цену за час:")
        return
    await state.update_data(price_hour=str(v))
    await state.set_state(EditItemStates.price_day)
    await message.answer("Цена за сутки (24 часа):")


@router.message(EditItemStates.price_day, F.text)
async def edit_item_price_day(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    try:
        v = Decimal(message.text.strip().replace(",", "."))
    except InvalidOperation:
        await message.answer("Нужно число. Повторите цену за сутки:")
        return
    await state.update_data(price_day=str(v))
    await state.set_state(EditItemStates.price_week)
    await message.answer("Цена за неделю (168 часов):")


@router.message(EditItemStates.price_week, F.text)
async def edit_item_price_week(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    try:
        v = Decimal(message.text.strip().replace(",", "."))
    except InvalidOperation:
        await message.answer("Нужно число. Повторите цену за неделю:")
        return
    data = await state.get_data()
    iid = data.get("edit_item_id")
    flow = data.get("edit_flow")
    if iid is None or flow not in ("prices", "topaid"):
        await state.clear()
        return
    async with db_session.async_session_maker() as session:
        item, err = await _require_editable_item(
            session,
            int(iid),
            message.from_user.id,
            message.from_user.username,
            settings,
        )
        if err or item is None:
            await state.clear()
            await message.answer(_edit_item_err_text(err))
            return
        if flow == "prices":
            if not item.is_paid:
                await state.clear()
                await message.answer("Вещь не в платной аренде — цены не сохранены.")
                return
            item.price_hour = Decimal(data["price_hour"])
            item.price_day = Decimal(data["price_day"])
            item.price_week = v
        else:
            item.rent_hours_min = int(data["rent_hours_min"])
            item.rent_hours_max = int(data["rent_hours_max"])
            item.price_hour = Decimal(data["price_hour"])
            item.price_day = Decimal(data["price_day"])
            item.price_week = v
            item.is_paid = True
            item.display_order = await next_display_order_for_group(
                session, True, item.item_category
            )
        await session.commit()
        paid = bool(item.is_paid)
    await state.clear()
    done = (
        "Цены обновлены."
        if flow == "prices"
        else "Вещь переведена в платную аренду."
    )
    await message.answer(
        done,
        reply_markup=edit_item_menu_keyboard(int(iid), is_paid=paid),
    )


@router.message(Command("list_items"))
async def cmd_list_items(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    uid = message.from_user.id
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Item)
            .where(or_(Item.owner_user_id.is_(None), Item.owner_user_id == uid))
            .order_by(Item.display_order.asc(), Item.id.asc())
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
        ic = item_category_label(it.item_category)
        lo, hi = rent_hours_bounds(it)
        lines.append(f"{it.id}. {it.name} ({cat} | {lo}–{hi}ч | {ic}{own})")
    await message.answer(
        "Ваши и общие вещи:\n"
        + "\n".join(lines)
        + "\n\n<i>Редактировать поля вещи: /edit_item id</i>",
        parse_mode=ParseMode.HTML,
    )


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
        try:
            await session.delete(item)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await message.answer(
                "Не удалось удалить вещь из-за связанных записей (аренды/брони/история). "
                "Сначала завершите или очистите связанные записи."
            )
            return
    await message.answer(f"Вещь {iid} удалена.")


@router.message(Command("item_order"))
async def cmd_item_order(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    tokens = (message.text or "").strip().split()
    if len(tokens) < 3:
        await message.answer(
            "Использование: <code>/item_order id позиция</code>\n\n"
            "<b>id</b> — из <code>/list_items</code>.\n"
            "<b>позиция</b> — место в списке у пользователя внутри той же группы "
            "(платная/бесплатная и одна категория): <code>1</code> = сверху.\n\n"
            "Пример: <code>/item_order 5 1</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        iid = int(tokens[1])
        pos = int(tokens[2])
    except ValueError:
        await message.answer("id и позиция должны быть целыми числами.")
        return
    if pos < 1:
        await message.answer("Позиция должна быть не меньше 1.")
        return
    async with db_session.async_session_maker() as session:
        ok, text = await reorder_item_to_position(
            session,
            item_id=iid,
            position_1based=pos,
            acting_user_id=message.from_user.id,
        )
        if ok:
            await session.commit()
        else:
            await session.rollback()
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("ban_user", "ban"))
async def cmd_ban_user(message: Message, bot: Bot, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    if not can_ban_via_bot_commands(message.from_user.id, settings):
        await message.answer(
            "Блокировать пользователей по команде может только <b>суперадмин</b> "
            "(переменная <code>SUPERADMIN_USER_IDS</code> в настройках бота). "
            "Обычный админ может выдать предупреждение: <code>/warn</code>.",
            parse_mode=ParseMode.HTML,
        )
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
        if resolved_id is None:
            resolved_id = await resolve_user_id_by_username_norm(session, uname)
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
            "\n\n<i>User_id не найден ни через Telegram, ни в базе бота — "
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
    if not can_ban_via_bot_commands(message.from_user.id, settings):
        await message.answer(
            "Снимать блокировку может только <b>суперадмин</b> "
            "(<code>SUPERADMIN_USER_IDS</code>).",
            parse_mode=ParseMode.HTML,
        )
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


@router.message(Command("warn_user", "warn"))
async def cmd_warn_user(message: Message, bot: Bot, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    tokens = (message.text or "").strip().split(maxsplit=2)
    if len(tokens) < 2:
        await message.answer(
            "Использование: <code>/warn_user</code> или <code>/warn</code> — "
            "затем <b>@username</b> или числовой <b>Telegram id</b>, при желании текст причины.\n"
            "Пример: <code>/warn vasya не вышел на связь</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    target = tokens[1].strip()
    reason_plain = tokens[2].strip() if len(tokens) > 2 else "Предупреждение от администратора."

    user_id: int | None = None
    username_for_row: str | None = None

    if target.isdigit():
        user_id = int(target)
        if user_id <= 0:
            await message.answer("Некорректный id.")
            return
    else:
        uname = normalize_username(target)
        if not uname:
            await message.answer("Укажите непустой username (с @ или без).")
            return
        try:
            chat = await bot.get_chat(f"@{uname}")
            if chat.id and chat.id > 0:
                user_id = int(chat.id)
            username_for_row = chat.username or uname
        except TelegramBadRequest:
            async with db_session.async_session_maker() as session:
                user_id = await resolve_user_id_by_username_norm(session, uname)
            if user_id is None:
                await message.answer(
                    "Telegram не отдаёт профиль по @username (для обычных пользователей так бывает), "
                    "и в базе бота не найдено заявок/броней с этим ником.\n\n"
                    "Укажите числовой <b>id</b> пользователя (Настройки Telegram) "
                    "или попросите его снова написать боту.",
                    parse_mode=ParseMode.HTML,
                )
                return
            username_for_row = uname

    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return

    admin_note = (
        f"Предупреждение админа (id {message.from_user.id}). "
        f"{reason_plain[:1500]}"
    )
    reason_html = (
        "<b>Предупреждение от администратора.</b>\n"
        f"{format_warn_reason_for_user(reason_plain)}"
    )
    apply_auto_ban = can_autoban_from_warnings(message.from_user.id, settings)
    issuer_id = message.from_user.id

    try:
        async with db_session.async_session_maker() as session:
            if await is_user_banned(session, user_id=user_id, username=username_for_row):
                await session.rollback()
                await message.answer("Этот пользователь уже в списке блокировки.")
                return
            cnt, banned = await add_warning(
                session,
                user_id=user_id,
                username=username_for_row,
                reason_html=reason_html,
                bot=bot,
                ban_note=admin_note,
                apply_auto_ban=apply_auto_ban,
            )
            await session.commit()
    except IntegrityError:
        await message.answer(
            "Не удалось сохранить (например, дубликат бана). Проверьте /list_bans.",
            parse_mode=ParseMode.HTML,
        )
        return

    if superadmin_roles_enabled(settings) and not is_superadmin(issuer_id, settings):
        await notify_superadmins_discipline_warning(
            bot,
            settings,
            issuer_user_id=issuer_id,
            issuer_username=message.from_user.username,
            target_user_id=user_id,
            target_username=username_for_row,
            warnings_count=cnt,
            reason_plain=reason_plain,
            at_threshold_without_ban=cnt >= WARNINGS_BAN_THRESHOLD and not banned,
        )

    extra = ""
    if banned:
        extra = f" Доступ заблокирован (≥{WARNINGS_BAN_THRESHOLD} предупреждений)."
    elif cnt >= WARNINGS_BAN_THRESHOLD:
        extra = (
            " Лимит предупреждений; автобан только от суперадмина. "
            "Суперадмины получили уведомление в Telegram."
            if superadmin_roles_enabled(settings)
            else ""
        )
    await message.answer(
        f"Готово. У пользователя <code>{user_id}</code> сейчас "
        f"<b>{cnt}</b> предупреждений из {WARNINGS_BAN_THRESHOLD}.{extra}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("unwarn_user", "unwarn", "clear_warnings"))
async def cmd_unwarn_user(message: Message, bot: Bot, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    tokens = (message.text or "").strip().split(maxsplit=1)
    if len(tokens) < 2:
        await message.answer(
            "Использование: <code>/unwarn</code> или <code>/unwarn_user</code> — "
            "затем <b>@username</b> или числовой <b>Telegram id</b>.\n"
            "Сбрасывает все предупреждения и счётчик успешных выдач у пользователя "
            "(блокировку /unban не снимает).\n"
            "Пример: <code>/unwarn vasya</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    target = tokens[1].strip()

    user_id: int | None = None
    username_for_row: str | None = None

    if target.isdigit():
        user_id = int(target)
        if user_id <= 0:
            await message.answer("Некорректный id.")
            return
    else:
        uname = normalize_username(target)
        if not uname:
            await message.answer("Укажите непустой username (с @ или без).")
            return
        try:
            chat = await bot.get_chat(f"@{uname}")
            if chat.id and chat.id > 0:
                user_id = int(chat.id)
            username_for_row = chat.username or uname
        except TelegramBadRequest:
            async with db_session.async_session_maker() as session:
                user_id = await resolve_user_id_by_username_norm(session, uname)
            if user_id is None:
                await message.answer(
                    "Telegram не отдаёт профиль по @username (для обычных пользователей так бывает), "
                    "и в базе бота не найдено заявок/броней с этим ником.\n\n"
                    "Укажите числовой <b>id</b> пользователя (Настройки Telegram) "
                    "или попросите его снова написать боту.",
                    parse_mode=ParseMode.HTML,
                )
                return
            username_for_row = uname

    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return

    async with db_session.async_session_maker() as session:
        banned = await is_user_banned(session, user_id=user_id, username=username_for_row)
        code, prev = await clear_warnings_for_user(
            session, user_id=user_id, username=username_for_row
        )
        await session.commit()

    if code == "none":
        await message.answer(
            f"У пользователя <code>{user_id}</code> не было записи о предупреждениях.",
            parse_mode=ParseMode.HTML,
        )
        return
    if code == "already":
        await message.answer(
            f"У пользователя <code>{user_id}</code> предупреждений уже не было.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await bot.send_message(
            user_id,
            "✅ <b>Администратор обнулил ваши предупреждения</b> "
            f"(было снято: <b>{prev}</b> из {WARNINGS_BAN_THRESHOLD}). "
            "Счётчик успешных выдач для автоматического сброса тоже обнулён.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramForbiddenError:
        pass
    except TelegramBadRequest:
        pass

    extra = ""
    if banned:
        extra = (
            " Пользователь всё ещё в <b>бане</b> — при необходимости снимите: "
            "<code>/unban</code>."
        )
    await message.answer(
        f"Готово. С пользователя <code>{user_id}</code> снято <b>{prev}</b> предупреждений.{extra}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("list_warnings"))
async def cmd_list_warnings(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    async with db_session.async_session_maker() as session:
        rows = await list_users_with_warnings(session)
        await session.commit()
    if not rows:
        await message.answer(
            "Нет записей с ненулевым числом предупреждений "
            f"(автобан при {WARNINGS_BAN_THRESHOLD})."
        )
        return
    lines = []
    for row in rows:
        un = escape(row.username_norm or "—")
        lines.append(
            f"id <code>{row.user_id}</code> | @{un} | "
            f"предупреждений: <b>{row.warnings}</b> | "
            f"успешных выдач (счётчик): {row.successful_handovers}"
        )
    text = "<b>Предупреждения арендаторов</b>\n\n" + "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "…"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("add_blackout"))
async def cmd_add_blackout(message: Message, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    await state.set_state(AdminBlackoutStates.waiting_start)
    await message.answer(
        "Окно неактива для <b>всех ваших управляемых вещей</b>.\n\n"
        "Будут сняты <b>брони</b> и <b>ожидающие выдачи заявки</b>, у которых "
        "<b>начало слота</b> попадает в это окно (если начало раньше окна, а дальше слот только пересекается — "
        "бронь остаётся).\n\n"
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
        now_bo = datetime.now(UTC)
        win = AdminBlackoutWindow(
            owner_user_id=admin_id,
            start_at=start_at,
            end_at=end_at,
            created_at=now_bo,
        )
        session.add(win)
        await session.flush()
        for item in items:
            n_res += await cancel_reservations_hit_by_blackout(
                session, bot, settings, item, start_at, end_at
            )
            n_rent += await cancel_pending_rentals_hit_by_blackout(
                session, bot, settings, item, start_at, end_at
            )
            session.add(BlackoutWindowItem(window_id=win.id, item_id=item.id))
            names.append(item.name)
        await session.commit()
    await state.clear()
    preview = ", ".join(escape(n) for n in names[:12])
    if len(names) > 12:
        preview += f"… (+{len(names) - 12})"
    await message.answer(
        f"Окно неактива добавлено для <b>{len(names)}</b> вещей: {preview}\n"
        f"Интервал: {format_local_time(start_at, settings)} — {format_local_time(end_at, settings)}.\n"
        f"В <code>/list_blackouts</code> — <b>один</b> id на всё это окно; <code>/delete_blackout</code> снимет его со всех вещей.\n"
        f"Снято броней: {n_res}, заявок на выдачу (ожидают админа): {n_rent}.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("list_blackouts"))
async def cmd_list_blackouts(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    uid = message.from_user.id
    now = datetime.now(UTC)
    async with db_session.async_session_maker() as session:
        r_w = await session.execute(
            select(AdminBlackoutWindow)
            .where(
                AdminBlackoutWindow.owner_user_id == uid,
                AdminBlackoutWindow.end_at > now,
            )
            .options(
                selectinload(AdminBlackoutWindow.window_items).selectinload(BlackoutWindowItem.item)
            )
            .order_by(AdminBlackoutWindow.start_at.asc())
        )
        windows = list(r_w.scalars().unique())
        r_b = await session.execute(
            select(ItemBlackout)
            .options(selectinload(ItemBlackout.item))
            .where(
                ItemBlackout.window_id.is_(None),
                ItemBlackout.end_at > now,
            )
            .order_by(ItemBlackout.start_at.asc())
        )
        legacy = list(r_b.scalars().unique())
        await session.commit()
    legacy = [bo for bo in legacy if admin_manages_item(uid, bo.item)]
    if not windows and not legacy:
        await message.answer("Предстоящих окон неактива по вашим вещам нет.")
        return
    chunks: list[tuple[datetime, str]] = []
    for w in windows:
        nm = sorted({escape(li.item.name) if li.item else "?" for li in w.window_items})
        nick = ", ".join(nm[:16])
        if len(nm) > 16:
            nick += f"… <i>(+{len(nm) - 16})</i>"
        line = (
            f"• id <code>{w.id}</code> — <b>общее окно</b> ({len(w.window_items)} вещей)\n"
            f"  {format_local_time(w.start_at, settings)} — {format_local_time(w.end_at, settings)}\n"
            f"  <i>{nick}</i>"
        )
        chunks.append((w.start_at, line))
    for bo in legacy:
        it = bo.item
        name = escape(it.name if it else "?")
        line = (
            f"• id <code>{bo.id}</code> — <b>одна вещь</b> | {name}\n"
            f"  {format_local_time(bo.start_at, settings)} — {format_local_time(bo.end_at, settings)}"
        )
        chunks.append((bo.start_at, line))
    chunks.sort(key=lambda x: x[0])
    lines = [c[1] for c in chunks]
    text = (
        "<b>Предстоящие окна неактива выдачи</b>\n"
        "<i>Окно из /add_blackout — одна строка и один id на все вещи; «одна вещь» — только старые записи. Прошедшие не показываются.</i>\n\n"
        + "\n\n".join(lines)
    )
    if len(text) > 4000:
        text = text[:3990] + "…"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("delete_blackout"))
async def cmd_delete_blackout(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: /delete_blackout 5 — id из /list_blackouts "
            "(для общего окна снимается со всех вещей сразу)."
        )
        return
    try:
        bid = int(parts[1].strip())
    except ValueError:
        await message.answer("Нужен числовой id.")
        return
    uid = message.from_user.id
    async with db_session.async_session_maker() as session:
        w = await session.get(AdminBlackoutWindow, bid)
        if w is not None:
            if w.owner_user_id != uid:
                await session.rollback()
                await message.answer("Такого общего окна нет или оно создано другим администратором.")
                return
            await session.delete(w)
            await session.commit()
            await message.answer(f"Общее окно #{bid} удалено со всех вещей.")
            return
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
        if bo.window_id is not None:
            w2 = await session.get(AdminBlackoutWindow, bo.window_id)
            if w2 is not None and w2.owner_user_id == uid:
                wid = w2.id
                await session.delete(w2)
                await session.commit()
                await message.answer(f"Общее окно #{wid} удалено со всех вещей.")
                return
            await session.rollback()
            await message.answer("Удаляйте по id общего окна из /list_blackouts.")
            return
        if not admin_manages_item(uid, bo.item):
            await session.rollback()
            await message.answer("Это окно на чужой вещи.")
            return
        await session.delete(bo)
        await session.commit()
    await message.answer(f"Окно неактива #{bid} удалено.")


def _booking_line_reservation(res: Reservation, settings: Settings) -> str:
    un = escape((res.username or "—").lstrip("@"))
    return (
        f"• <b>[бронь] #{res.id}</b> @{un} | "
        f"{format_local_time(res.start_at, settings)} → "
        f"{format_local_time(res.end_at, settings)} | {res.requested_hours} ч."
    )


def _booking_line_rental(rent: Rental, settings: Settings) -> str:
    un = escape((rent.username or "—").lstrip("@"))
    st = ensure_utc(rent.start_at)
    en = ensure_utc(rent.end_at)
    start_l = format_local_time(st, settings) if st else "?"
    end_l = format_local_time(en, settings) if en else "?"
    return (
        f"• <b>[аренда] #{rent.id}</b> @{un} | "
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


def _rent_stats_text(
    snap, settings: Settings, *, title: str, include_scope_note: bool = True
) -> str:
    tz_hint = escape(settings.time_zone_label.strip() or "локальному времени бота")
    text = (
        f"<b>{title}</b> <i>(сегодня / неделя / месяц — границы по {tz_hint})</i>\n\n"
        f"Заработано с аренды всего: {format_money(snap.earned_total)}\n"
        f"За сегодня: {format_money(snap.earned_today)}\n"
        f"За неделю: {format_money(snap.earned_week)}\n"
        f"За месяц: {format_money(snap.earned_month)}\n\n"
        f"Сдано аксессуаров всего: {snap.handovers_total}\n"
        f"За сегодня: {snap.handovers_today}\n"
        f"За неделю: {snap.handovers_week}\n"
        f"За месяц: {snap.handovers_month}"
    )
    if include_scope_note:
        text += (
            "\n\n<i>Только ваши выдачи: по подтвердившему админу; для старых записей без этого поля — "
            "по владельцу вещи на момент просмотра (общие вещи без владельца в персональную статистику "
            "не попадают). Учёт с момента появления функции; аренды после срока бот удаляет — "
            "в прошлое не восстанавливаются.</i>"
        )
    return text


def _rent_stats_item_keyboard(items: list[Item], selected_item_id: int | None = None):
    kb = InlineKeyboardBuilder()
    if selected_item_id is not None:
        kb.row(InlineKeyboardButton(text="<< Общая статистика", callback_data="adm:rst:all"))
    for item in items:
        title = escape(item.name)
        if item.id == selected_item_id:
            title = f"• {title}"
        kb.row(
            InlineKeyboardButton(
                text=title[:64],
                callback_data=f"adm:rst:item:{item.id}",
            )
        )
    return kb.as_markup()


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

    typed_rows: list[tuple[int, str, datetime, Reservation | Rental]] = []
    for res in upcoming_res:
        item_id = res.item.id if res.item is not None else 0
        typed_rows.append((item_id, "res", _booking_sort_key_reservation(res), res))
    for rent in active_rent:
        item_id = rent.item.id if rent.item is not None else 0
        typed_rows.append((item_id, "rt", _booking_sort_key_rental(rent), rent))
    typed_rows.sort(key=lambda x: (x[0], x[2], x[1]))

    if not typed_rows:
        await message.answer("Нет записей: ни будущих броней, ни действующих аренд.")
        return

    full_lines: list[str] = []
    full_kb_keys: list[tuple[str, int]] = []
    current_item_id: int | None = None
    for item_id, kind, _, obj in typed_rows:
        if item_id != current_item_id:
            current_item_id = item_id
            item_name = "?"
            category_name = item_category_label(None)
            if kind == "res":
                assert isinstance(obj, Reservation)
                item_name = escape(obj.item.name if obj.item else "?")
                category_name = escape(item_category_label(obj.item.item_category if obj.item else None))
            else:
                assert isinstance(obj, Rental)
                item_name = escape(obj.item.name if obj.item else "?")
                category_name = escape(item_category_label(obj.item.item_category if obj.item else None))
            if full_lines:
                full_lines.append("")
            full_lines.append(f"<b>Категория: {category_name}</b> — {item_name}")
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


@router.message(Command("drop_request", "drop_pending"))
async def cmd_drop_pending_request(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: /drop_request 5 — где 5 это id вещи из /list_items.\n"
            "Команда вручную снимает зависшую заявку на выдачу (ожидает админа)."
        )
        return
    try:
        item_id = int(parts[1].strip())
    except ValueError:
        await message.answer("Нужен числовой id вещи.")
        return

    async with db_session.async_session_maker() as session:
        r_item = await session.execute(select(Item).where(Item.id == item_id))
        item = r_item.scalar_one_or_none()
        if item is None:
            await session.rollback()
            await message.answer("Вещь не найдена.")
            return
        if not admin_manages_item(message.from_user.id, item):
            await session.rollback()
            await message.answer("Это не ваша вещь.")
            return
        r_pending = await session.execute(
            select(Rental).where(
                Rental.item_id == item_id,
                Rental.state == RentalState.pending_admin.value,
            )
        )
        pending_rows = list(r_pending.scalars().all())
        if not pending_rows:
            await session.rollback()
            await message.answer("По этой вещи нет зависших заявок в статусе «ожидает админа».")
            return
        for row in pending_rows:
            await session.delete(row)
        await session.commit()

    await message.answer(
        f"Снято заявок по вещи #{item_id}: {len(pending_rows)}. "
        "Вещь снова доступна для новых заявок/брони."
    )


@router.message(Command("rent_stats"))
async def cmd_rent_stats(message: Message, settings: Settings) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        return
    uid = message.from_user.id
    async with db_session.async_session_maker() as session:
        snap = await fetch_rental_stats(session, settings, admin_user_id=uid)
        r_items = await session.execute(select(Item).order_by(Item.id.asc()))
        all_items = list(r_items.scalars().unique())
        managed_items = [x for x in all_items if admin_manages_item(uid, x)]
        await session.commit()
    text = _rent_stats_text(snap, settings, title="Ваша статистика аренды")
    kb = _rent_stats_item_keyboard(managed_items) if managed_items else None
    if managed_items:
        text += "\n\nВыберите аксессуар, чтобы посмотреть статистику по нему:"
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@router.callback_query(F.data == "adm:rst:all")
async def admin_rent_stats_all(query: CallbackQuery, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    uid = query.from_user.id
    async with db_session.async_session_maker() as session:
        snap = await fetch_rental_stats(session, settings, admin_user_id=uid)
        r_items = await session.execute(select(Item).order_by(Item.id.asc()))
        all_items = list(r_items.scalars().unique())
        managed_items = [x for x in all_items if admin_manages_item(uid, x)]
        await session.commit()
    text = _rent_stats_text(snap, settings, title="Ваша статистика аренды")
    if managed_items:
        text += "\n\nВыберите аксессуар, чтобы посмотреть статистику по нему:"
    await query.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_rent_stats_item_keyboard(managed_items) if managed_items else None,
    )
    await query.answer()


@router.callback_query(F.data.regexp(r"^adm:rst:item:(\d+)$"))
async def admin_rent_stats_by_item(query: CallbackQuery, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    item_id = int(query.data.split(":")[3])
    uid = query.from_user.id
    async with db_session.async_session_maker() as session:
        r_item = await session.execute(select(Item).where(Item.id == item_id))
        item = r_item.scalar_one_or_none()
        if item is None:
            await session.rollback()
            await query.answer("Аксессуар не найден", show_alert=True)
            return
        if not admin_manages_item(uid, item):
            await session.rollback()
            await query.answer("Это не ваш аксессуар", show_alert=True)
            return
        snap = await fetch_rental_stats(session, settings, admin_user_id=uid, item_id=item_id)
        r_items = await session.execute(select(Item).order_by(Item.id.asc()))
        all_items = list(r_items.scalars().unique())
        managed_items = [x for x in all_items if admin_manages_item(uid, x)]
        await session.commit()

    title = f"Статистика по аксессуару: {escape(item.name)}"
    text = _rent_stats_text(snap, settings, title=title, include_scope_note=False)
    text += "\n\n<i>Выдачи считаются по выбранному аксессуару.</i>"
    await query.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_rent_stats_item_keyboard(managed_items, selected_item_id=item_id),
    )
    await query.answer()


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


async def _finalize_rental_not_handed(
    bot: Bot,
    settings: Settings,
    *,
    rental_id: int,
    acting_user_id: int,
    reason_plain: str,
    rental_card_chat_id: int,
    rental_card_message_id: int,
) -> tuple[bool, str]:
    """Снять ожидающую заявку, уведомить арендатора, обновить карточку админу. (ok, ошибка для alert)."""
    reason_plain = (reason_plain or "").strip()
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Rental).options(selectinload(Rental.item)).where(Rental.id == rental_id)
        )
        rental = r.scalar_one_or_none()
        if rental is None or rental.state != RentalState.pending_admin.value:
            return False, "Заявка не найдена или уже обработана."
        if not admin_manages_item(acting_user_id, rental.item):
            return False, "Это не ваша вещь."
        uid = rental.user_id
        req_h = rental.requested_hours
        item_name = rental.item.name if rental.item else "?"
        await session.delete(rental)
        await session.commit()

    if reason_plain:
        reason_block = f"\n<b>Причина:</b> {escape(reason_plain)}"
    else:
        reason_block = ""
    user_text = (
        "❌ <b>Вещь не сдана.</b> Заявка на аренду снята.\n\n"
        f"Вещь: <b>{escape(item_name)}</b>\n"
        f"Часов по заявке: {req_h}"
        f"{reason_block}"
    )
    try:
        await bot.send_message(uid, user_text, parse_mode=ParseMode.HTML)
    except TelegramForbiddenError:
        pass
    except TelegramBadRequest:
        pass

    admin_card = "Отмечено: вещь не сдана. Заявка снята."
    if reason_plain:
        admin_card += f"\n<i>Пользователю:</i> {escape(reason_plain)}"
    try:
        await bot.edit_message_text(
            admin_card,
            chat_id=rental_card_chat_id,
            message_id=rental_card_message_id,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass
    return True, ""


@router.callback_query(F.data.regexp(r"^adm:r:(\d+):no$"))
async def admin_rental_reject(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
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

    data = await state.get_data()
    if data.get("pending_rental_id") == rid:
        await state.clear()
    await state.set_state(AdminRentalStates.waiting_no_handover_reason)
    await state.update_data(
        pending_rental_id=None,
        handover_chat_id=None,
        handover_message_id=None,
        reject_rental_id=rid,
        reject_card_chat_id=query.message.chat.id,
        reject_card_message_id=query.message.message_id,
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Без причины", callback_data=f"adm:r:{rid}:noreason"))
    kb.row(InlineKeyboardButton(text="« Отмена", callback_data=f"adm:r:{rid}:noabort"))
    sent = await query.message.answer(
        "<b>Вещь не сдана</b> — заявка будет снята.\n\n"
        "Отправьте <b>причину</b> одним сообщением (её увидит пользователь) "
        "или нажмите «Без причины».",
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML,
    )
    await state.update_data(
        reject_prompt_chat_id=sent.chat.id,
        reject_prompt_message_id=sent.message_id,
    )
    await query.answer()


@router.callback_query(F.data.regexp(r"^adm:r:(\d+):noabort$"))
async def admin_rental_reject_abort(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    rid = int(query.data.split(":")[2])
    data = await state.get_data()
    if data.get("reject_rental_id") != rid:
        await query.answer("Устарело или другая заявка.", show_alert=True)
        return
    await state.clear()
    try:
        await query.message.edit_text("Снятие заявки отменено.")
    except TelegramBadRequest:
        pass
    await query.answer()


@router.callback_query(F.data.regexp(r"^adm:r:(\d+):noreason$"))
async def admin_rental_reject_no_reason(
    query: CallbackQuery, state: FSMContext, bot: Bot, settings: Settings
) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    rid = int(query.data.split(":")[2])
    data = await state.get_data()
    if data.get("reject_rental_id") != rid:
        await query.answer("Устарело или другая заявка.", show_alert=True)
        return
    card_cid = data.get("reject_card_chat_id")
    card_mid = data.get("reject_card_message_id")
    if card_cid is None or card_mid is None:
        await state.clear()
        await query.answer("Ошибка состояния", show_alert=True)
        return
    ok, err = await _finalize_rental_not_handed(
        bot,
        settings,
        rental_id=rid,
        acting_user_id=query.from_user.id,
        reason_plain="",
        rental_card_chat_id=int(card_cid),
        rental_card_message_id=int(card_mid),
    )
    await state.clear()
    if not ok:
        await query.answer(err, show_alert=True)
        return
    try:
        await query.message.edit_text("Заявка снята (без причины для пользователя).")
    except TelegramBadRequest:
        pass
    await query.answer("Готово")


@router.message(StateFilter(AdminRentalStates.waiting_no_handover_reason), F.text)
async def admin_rental_reject_reason_text(
    message: Message, state: FSMContext, bot: Bot, settings: Settings
) -> None:
    if not _admin_only(settings, message.from_user.id, message.from_user.username):
        await state.clear()
        return
    if (message.text or "").strip().startswith("/"):
        await state.clear()
        await message.answer("Ввод отменён. Заявка не снята.")
        return
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Причина не может быть пустой. Напишите текст или нажмите «Без причины».")
        return
    data = await state.get_data()
    rid = data.get("reject_rental_id")
    card_cid = data.get("reject_card_chat_id")
    card_mid = data.get("reject_card_message_id")
    if rid is None or card_cid is None or card_mid is None:
        await state.clear()
        return
    ok, err = await _finalize_rental_not_handed(
        bot,
        settings,
        rental_id=int(rid),
        acting_user_id=message.from_user.id,
        reason_plain=reason,
        rental_card_chat_id=int(card_cid),
        rental_card_message_id=int(card_mid),
    )
    pch = data.get("reject_prompt_chat_id")
    pmid = data.get("reject_prompt_message_id")
    await state.clear()
    if not ok:
        await message.answer(err)
        return
    if pch is not None and pmid is not None:
        try:
            await bot.edit_message_text(
                "Заявка снята (причина отправлена пользователю).",
                chat_id=int(pch),
                message_id=int(pmid),
            )
        except TelegramBadRequest:
            pass
    await message.answer("Заявка снята, пользователь уведомлён с указанной причиной.")


@router.callback_query(F.data.regexp(r"^adm:r:(\d+):warn$"))
async def admin_rental_warn(query: CallbackQuery, bot: Bot, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await query.answer("Нет доступа", show_alert=True)
        return
    rid = int(query.data.split(":")[2])
    try:
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
            if await is_user_banned(session, user_id=rental.user_id, username=rental.username):
                await session.rollback()
                await query.answer("Пользователь уже в списке блокировки.", show_alert=True)
                return
            reason_plain = (
                "Несвоевременный контакт или нарушение дисциплины по заявке на аренду "
                "(решение администратора)."
            )
            admin_note = (
                f"Кнопка «Выдать предупреждение» в заявке rental_id={rid}, "
                f"админ id {query.from_user.id}"
            )
            reason_html = (
                "<b>Предупреждение от администратора.</b>\n"
                f"{format_warn_reason_for_user(reason_plain)}"
            )
            apply_auto_ban = can_autoban_from_warnings(query.from_user.id, settings)
            cnt, banned = await add_warning(
                session,
                user_id=rental.user_id,
                username=rental.username,
                reason_html=reason_html,
                bot=bot,
                ban_note=admin_note,
                apply_auto_ban=apply_auto_ban,
            )
            await session.commit()
    except IntegrityError:
        await query.answer(
            "Не удалось сохранить (например, дубликат бана). Проверьте /list_bans.",
            show_alert=True,
        )
        return
    if superadmin_roles_enabled(settings) and not is_superadmin(query.from_user.id, settings):
        await notify_superadmins_discipline_warning(
            bot,
            settings,
            issuer_user_id=query.from_user.id,
            issuer_username=query.from_user.username,
            target_user_id=rental.user_id,
            target_username=rental.username,
            warnings_count=cnt,
            reason_plain=reason_plain,
            at_threshold_without_ban=cnt >= WARNINGS_BAN_THRESHOLD and not banned,
        )
    extra = ""
    if banned:
        extra = f" Доступ заблокирован (≥{WARNINGS_BAN_THRESHOLD})."
    elif cnt >= WARNINGS_BAN_THRESHOLD and superadmin_roles_enabled(settings):
        extra = " Лимит; суперадмины уведомлены."
    await query.answer(
        f"Предупреждение выдано. Сейчас у пользователя {cnt}/{WARNINGS_BAN_THRESHOLD}.{extra}",
        show_alert=True,
    )


@router.callback_query(F.data.regexp(r"^adm:r:(\d+):ok$"))
async def admin_rental_ok(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not _admin_only(settings, query.from_user.id, query.from_user.username):
        await _safe_query_answer(query, "Нет доступа", show_alert=True)
        return
    rid = int(query.data.split(":")[2])
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Rental).options(selectinload(Rental.item)).where(Rental.id == rid)
        )
        rental = r.scalar_one_or_none()
        if rental is None or rental.state != RentalState.pending_admin.value:
            await _safe_query_answer(query, "Заявка не найдена или уже обработана", show_alert=True)
            return
        if not admin_manages_item(query.from_user.id, rental.item):
            await _safe_query_answer(query, "Это не ваша вещь.", show_alert=True)
            return
        lo, hi = rent_hours_bounds(rental.item)
    base = query.message.html_text or query.message.text or ""
    hint = (
        f"\n\n<i>Выберите срок сдачи кнопкой или отправьте число часов "
        f"(от {lo} до {hi}) обычным сообщением в чат.</i>"
    )
    await query.message.edit_text(
        base + hint,
        reply_markup=admin_hours_keyboard(rid, lo, hi),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AdminRentalStates.waiting_handover_hours)
    await state.update_data(
        pending_rental_id=rid,
        handover_chat_id=query.message.chat.id,
        handover_message_id=query.message.message_id,
    )
    await _safe_query_answer(query)


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
    async with db_session.async_session_maker() as session:
        r0 = await session.execute(
            select(Rental).options(selectinload(Rental.item)).where(Rental.id == int(rid))
        )
        rent0 = r0.scalar_one_or_none()
        if rent0 is None or rent0.item is None:
            await state.clear()
            await message.answer("Заявка не найдена.")
            return
        lo, hi = rent_hours_bounds(rent0.item)
    try:
        hours = int((message.text or "").strip())
    except ValueError:
        await message.answer(f"Нужно целое число часов (для этой вещи: от {lo} до {hi}).")
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
