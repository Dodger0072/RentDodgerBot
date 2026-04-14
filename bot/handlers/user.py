from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from bot.config import Settings, is_admin
from bot.time_format import format_local_time
from bot.db.models import Item, Rental, RentalState, Reservation
from bot.db import session as db_session
from bot.item_categories import UNCATEGORIZED_SLUG, item_category_label
from bot.keyboards.inline import (
    category_keyboard_for_admin,
    category_keyboard,
    confirm_keyboard,
    home_keyboard,
    inventory_subcategory_keyboard,
    item_list_keyboard,
    nav_back_keyboard,
)
from bot.services.admin_notify import (
    notify_admins_new_reservation,
    notify_admins_pending_rental,
    notify_admins_user_cancelled_reservation,
)
from bot.services.booking_schedule import (
    explain_booking_start_conflict,
    format_user_booking_availability_block,
    load_blackout_intervals_utc,
    load_rr_busy_intervals_utc,
    max_hours_from_start,
    max_reservation_end_utc,
    parse_booking_start_text,
    point_inside_busy,
    rent_lo_hi,
    reservation_fits,
    reservation_start_in_past_error,
    validate_new_reservation,
    MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START,
    user_may_cancel_reservation,
)
from bot.services.item_order import non_empty_rental_category_menu_rows
from bot.services.item_owner import landlord_contact_hint_html
from bot.services.rental import (
    can_take_immediate_rent,
    ensure_utc,
    expire_expired_rentals,
    format_money,
    item_list_button_text,
    item_photos_list,
    items_availability_batch,
    price_for_hours,
    rent_hours_bounds,
    user_facing_status,
)
from bot.services.user_discipline import booking_rules_block, near_ban_notice_for_user
from bot.states import UserBookStates, UserRentStates

router = Router(name="user")


def _my_reservations_keyboard(reservations: list[Reservation], *, now: datetime) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for res in reservations:
        if user_may_cancel_reservation(now_utc=now, reservation_start_utc=res.start_at):
            b.row(
                InlineKeyboardButton(
                    text=f"Отменить бронь #{res.id}",
                    callback_data=f"u:cnlres:{res.id}",
                )
            )
    b.row(InlineKeyboardButton(text="« К каталогу", callback_data="u:home"))
    return b.as_markup()


def _fmt_utc_local(dt: datetime, settings: Settings) -> str:
    return format_local_time(dt, settings)


def _item_caption(item: Item, settings: Settings, extra: str = "") -> str:
    lines = [
        f"<b>{escape(item.name)}</b>\n",
        f"<i>{escape(item_category_label(item.item_category))}</i>\n",
        escape(item.description),
    ]
    if item.is_paid and item.price_hour is not None:
        lines.append(
            f"\nЦена: {format_money(item.price_hour)} / час, "
            f"{format_money(item.price_day or 0)} / сутки, "
            f"{format_money(item.price_week or 0)} / неделя"
        )
    else:
        lines.append("\nБесплатная аренда")
    if extra:
        lines.append("\n" + extra)
    return "\n".join(lines)


async def _send_item_visual(target: Message, item: Item, caption: str, reply_markup) -> None:
    photos = item_photos_list(item)
    if len(photos) == 1:
        await target.answer_photo(photos[0], caption=caption, reply_markup=reply_markup)
    elif len(photos) > 1:
        media = [InputMediaPhoto(media=photos[0], caption=caption, parse_mode="HTML")]
        for fid in photos[1:]:
            media.append(InputMediaPhoto(media=fid))
        await target.answer_media_group(media)
        await target.answer("Действия:", reply_markup=reply_markup)
    else:
        await target.answer(caption, reply_markup=reply_markup)


def _catalog_kind_and_slug(item: Item) -> tuple[str, str]:
    kind = "paid" if item.is_paid else "free"
    raw = (item.item_category or "").strip()
    slug = UNCATEGORIZED_SLUG if not raw else raw
    return kind, slug


async def _send_subcategory_choice_menu(message: Message, *, is_paid: bool) -> bool:
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Item.id).where(Item.is_paid.is_(is_paid), Item.is_visible.is_(True)).limit(1)
        )
        if r.scalar_one_or_none() is None:
            await session.commit()
            return False
        cat_rows = await non_empty_rental_category_menu_rows(session, is_paid=is_paid)
        await session.commit()
    if not cat_rows:
        return False
    kind_ru = "Платная" if is_paid else "Бесплатная"
    await message.answer(
        f"{kind_ru} аренда — выберите <b>категорию вещей</b>:",
        reply_markup=inventory_subcategory_keyboard(is_paid=is_paid, rows=cat_rows),
        parse_mode=ParseMode.HTML,
    )
    return True


async def _send_group_inventory_list(
    message: Message, *, is_paid: bool, slug: str, settings: Settings
) -> bool:
    if not slug or len(slug) > 48 or ":" in slug:
        return False
    async with db_session.async_session_maker() as session:
        q = select(Item).where(Item.is_paid.is_(is_paid), Item.is_visible.is_(True))
        if slug == UNCATEGORIZED_SLUG:
            q = q.where(or_(Item.item_category.is_(None), Item.item_category == ""))
        else:
            q = q.where(Item.item_category == slug)
        q = q.order_by(Item.display_order.asc(), Item.id.asc())
        r = await session.execute(q)
        rows = list(r.scalars().all())
        ids = [it.id for it in rows]
        ref_now, status_map = await items_availability_batch(session, ids)
        items = [
            (it.id, item_list_button_text(it.name, status_map[it.id], ref_now=ref_now))
            for it in rows
        ]
        await session.commit()
    if not items:
        return False
    kind_ru = "Платная" if is_paid else "Бесплатная"
    if slug == UNCATEGORIZED_SLUG:
        title = f"{kind_ru} аренда — без категории"
    else:
        title = f"{kind_ru} аренда — {item_category_label(slug)}"
    catalog_kind = "paid" if is_paid else "free"
    await message.answer(
        f"{title}. Выберите вещь:",
        reply_markup=item_list_keyboard(items, "u", catalog_kind=catalog_kind),
    )
    return True


async def _send_user_item_card(target: Message, item_id: int, settings: Settings) -> None:
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        r = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        item = r.scalar_one_or_none()
        if item is None:
            await session.rollback()
            await target.answer("Вещь не найдена.", reply_markup=home_keyboard())
            return
        st = await user_facing_status(session, item_id)
        await session.commit()

    extra = ""
    b = InlineKeyboardBuilder()
    if st.pending_admin:
        extra = (
            "\n\n⏳ <b>Статус:</b> заявка на рассмотрении у администратора — "
            "аренда и бронь временно недоступны."
        )
    elif st.active_rental is not None:
        until_s = (
            _fmt_utc_local(st.active_rental.end_at, settings)
            if st.active_rental.end_at is not None
            else "—"
        )
        extra = (
            f"\n\n🔒 <b>Статус:</b> занята до "
            f"<b>{until_s}</b> — бронь можно "
            "оформить на время после освобождения (укажите дату и начало в форме брони)."
        )
        b.row(
            InlineKeyboardButton(
                text="Забронировать",
                callback_data=f"book:{item_id}",
            ),
        )
    elif st.in_blackout:
        until_l = (
            _fmt_utc_local(st.blackout_until, settings) if st.blackout_until is not None else "—"
        )
        extra = (
            "\n\n⛔ <b>Статус:</b> владелец не сможет сдать эту вещь в аренду "
            f"до <b>{until_l}</b>. "
            "Прямая аренда сейчас недоступна; бронь можно оформить на время "
            "<b>после</b> этой даты — нажмите «Забронировать» и укажите начало слота."
        )
        b.row(
            InlineKeyboardButton(
                text="Забронировать",
                callback_data=f"book:{item_id}",
            ),
        )
    elif st.in_reserved_slot:
        until_l = (
            _fmt_utc_local(st.reserved_until, settings) if st.reserved_until is not None else "—"
        )
        extra = (
            f"\n\n🔒 <b>Статус:</b> занята по брони до "
            f"<b>{until_l}</b> — можно забронировать время после окончания этого слота."
        )
        b.row(InlineKeyboardButton(text="Забронировать", callback_data=f"book:{item_id}"))
    elif st.immediate_rent_max_hours >= st.min_rent_hours:
        lo_i, hi_i = rent_hours_bounds(item)
        extra = "\n\n✅ <b>Статус:</b> можно взять в аренду сейчас."
        if st.immediate_rent_max_hours < hi_i:
            extra += (
                f"\nДо ближайшей брони или занятой аренды можно взять не более "
                f"<b>{st.immediate_rent_max_hours}</b> ч."
            )
        b.row(InlineKeyboardButton(text="Взять в аренду", callback_data=f"take:{item_id}"))
        b.row(InlineKeyboardButton(text="Забронировать", callback_data=f"book:{item_id}"))
    else:
        hint_next = ""
        if st.next_busy_after is not None:
            hint_next = (
                f"\n\nБлижайшее начало брони или аренды по данным бота: "
                f"<b>{_fmt_utc_local(st.next_busy_after, settings)}</b>."
            )
        extra = (
            f"\n\n📅 <b>Статус:</b> сейчас нельзя взять сразу: минимальный срок для этой вещи "
            f"<b>{st.min_rent_hours}</b> ч., а период не может пересечься с чужой бронью или арендой "
            f"дольше, чем <b>{st.immediate_rent_max_hours}</b> ч.{hint_next}"
            f"\n\nНажмите «Забронировать», чтобы выбрать время в свободном слоте."
        )
        b.row(InlineKeyboardButton(text="Забронировать", callback_data=f"book:{item_id}"))

    ck, cslug = _catalog_kind_and_slug(item)
    b.row(InlineKeyboardButton(text="« Назад", callback_data=f"u:items:{ck}:{cslug}"))
    caption = _item_caption(item, settings, extra)
    await target.answer("Карточка вещи:")
    await _send_item_visual(target, item, caption, b.as_markup())


async def _send_rent_hours_prompt(
    message: Message, item_id: int, settings: Settings, state: FSMContext
) -> bool:
    notice = ""
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        await session.commit()
        st = await user_facing_status(session, item_id)
        ref_now = datetime.now(UTC)
        r_item = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        item = r_item.scalar_one_or_none()
        notice = await near_ban_notice_for_user(session, message.from_user.id)
        await session.commit()
    if st is None or item is None or not can_take_immediate_rent(st, ref_now):
        return False
    lo, hi = rent_hours_bounds(item)
    hi_cap = min(st.immediate_rent_max_hours, hi)
    await state.update_data(immediate_max_hours=hi_cap)
    hours_line = (
        f"Укажите срок аренды в часах (целое число от {lo} до {hi_cap}; "
        f"не дольше, чем до ближайшей брони или занятой аренды):"
    )
    await message.answer(
        hours_line + notice,
        reply_markup=nav_back_keyboard(),
        parse_mode=ParseMode.HTML if notice else None,
    )
    return True


async def _send_booking_start_prompt(
    message: Message, item_id: int, settings: Settings, state: FSMContext
) -> None:
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        await session.commit()
        st = await user_facing_status(session, item_id)
    if st is None:
        await state.clear()
        await message.answer("Ошибка загрузки.", reply_markup=home_keyboard())
        return
    if st.pending_admin:
        await state.clear()
        await message.answer(
            "Сначала дождитесь решения администратора по текущей заявке",
            reply_markup=home_keyboard(),
        )
        return
    async with db_session.async_session_maker() as session:
        r_item = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        book_item = r_item.scalar_one_or_none()
    if book_item is None:
        await state.clear()
        await message.answer("Вещь не найдена.", reply_markup=home_keyboard())
        return
    notice = ""
    avail_html = ""
    async with db_session.async_session_maker() as session:
        notice = await near_ban_notice_for_user(session, message.from_user.id)
        avail_html = await format_user_booking_availability_block(
            session, item_id, book_item, settings, now=datetime.now(UTC)
        )
        await session.commit()
    lines = [
        "Введите дату и время <b>начала</b> брони.\n"
        "Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
        "Пример: <code>04.05.2026 10:00</code>\n\n"
        "Ниже — когда можно выбрать начало с учётом минимальной аренды; "
        "слот не должен пересекаться с чужими бронями.\n\n",
    ]
    if st.in_blackout and st.blackout_until is not None:
        lines.append(
            f"\nСейчас до <b>{_fmt_utc_local(st.blackout_until, settings)}</b> владелец не сдаёт вещь — "
            f"укажите начало <b>после</b> этого времени (слот не должен с этим пересекаться)."
        )
    lines.append(booking_rules_block())
    if notice:
        lines.append(notice)
    lines.append("\n\n")
    lines.append(avail_html)
    await message.answer(
        "".join(lines),
        reply_markup=nav_back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def _resend_book_hours_prompt(
    message: Message, item_id: int, settings: Settings, state: FSMContext
) -> bool:
    data = await state.get_data()
    start_raw = data.get("book_start_iso")
    if not start_raw:
        return False
    start_at = ensure_utc(datetime.fromisoformat(str(start_raw)))
    if start_at is None:
        return False
    now = datetime.now(UTC)
    if reservation_start_in_past_error(start_at, now) is not None:
        return False
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        r = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        item = r.scalar_one_or_none()
        if item is None:
            await session.rollback()
            return False
        rr = await load_rr_busy_intervals_utc(session, item_id)
        await session.commit()
    lo, hi = rent_lo_hi(item)
    max_h = max_hours_from_start(start_at, rr, lo, hi)
    if max_h < lo:
        return False
    hi_eff = min(hi, max_h)
    cap_end = max_reservation_end_utc(start_at, rr)
    notice = ""
    async with db_session.async_session_maker() as session:
        notice = await near_ban_notice_for_user(session, message.from_user.id)
        await session.commit()
    await message.answer(
        f"Начало: <b>{_fmt_utc_local(start_at, settings)}</b>.\n"
        f"Можно забронировать не длиннее <b>{max_h}</b> ч. "
        f"(не позже {_fmt_utc_local(cap_end, settings)} — дальше уже занято или лимит {hi} ч.).\n"
        f"Введите число часов от <b>{lo}</b> до <b>{hi_eff}</b>:"
        + notice,
        reply_markup=nav_back_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return True


@router.callback_query(F.data == "cat:paid")
async def cat_paid(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    ok = await _send_subcategory_choice_menu(query.message, is_paid=True)
    if not ok:
        await query.answer("Пока нет вещей в платной аренде", show_alert=True)
        return
    await query.answer()


@router.callback_query(F.data == "cat:free")
async def cat_free(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    ok = await _send_subcategory_choice_menu(query.message, is_paid=False)
    if not ok:
        await query.answer("Пока нет вещей в бесплатной аренде", show_alert=True)
        return
    await query.answer()


@router.callback_query(F.data.regexp(r"^u:subcat:(paid|free)$"))
async def user_subcat_back(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    kind = (query.data or "").split(":")[2]
    is_paid = kind == "paid"
    ok = await _send_subcategory_choice_menu(query.message, is_paid=is_paid)
    if not ok:
        await query.answer("Каталог временно недоступен", show_alert=True)
        return
    await query.answer()


@router.callback_query(F.data.startswith("u:grp:"))
async def cat_then_inventory_group(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[0] != "u" or parts[1] != "grp":
        await query.answer()
        return
    kind, slug = parts[2], parts[3]
    if kind not in ("paid", "free"):
        await query.answer()
        return
    is_paid = kind == "paid"
    ok = await _send_group_inventory_list(
        query.message, is_paid=is_paid, slug=slug, settings=settings
    )
    if not ok:
        await query.answer("В этой категории пока нет вещей", show_alert=True)
        return
    await query.answer()


@router.callback_query(F.data.regexp(r"^u:items:(paid|free):(.+)$"))
async def user_back_to_inventory_list(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    parts = (query.data or "").split(":")
    kind, slug = parts[2], parts[3]
    if kind not in ("paid", "free"):
        await query.answer()
        return
    is_paid = kind == "paid"
    ok = await _send_group_inventory_list(
        query.message, is_paid=is_paid, slug=slug, settings=settings
    )
    if not ok:
        await query.answer("Список недоступен", show_alert=True)
        return
    await query.answer()


@router.callback_query(F.data == "u:back")
async def user_back(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await query.message.answer(
        "Выберите каталог:",
        reply_markup=category_keyboard_for_admin(
            is_admin_user=is_admin(query.from_user.id, query.from_user.username, settings)
        ),
    )
    await query.answer()


@router.callback_query(F.data.regexp(r"^u:item:(\d+)$"))
async def user_open_item(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    item_id = int(query.data.split(":")[2])
    await _send_user_item_card(query.message, item_id, settings)
    await query.answer()


@router.callback_query(F.data == "u:nav:back")
async def user_nav_back(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    st = await state.get_state()
    data = await state.get_data()

    if st == UserRentStates.waiting_confirm.state:
        item_id = int(data.get("item_id", 0))
        await state.set_state(UserRentStates.waiting_hours)
        await state.update_data(hours=None, total=None)
        ok = await _send_rent_hours_prompt(query.message, item_id, settings, state)
        if not ok:
            await state.clear()
            await query.message.answer(
                "Вещь недоступна для этой операции.",
                reply_markup=home_keyboard(),
            )
        await query.answer()
        return

    if st == UserRentStates.waiting_hours.state and data.get("flow") == "rent":
        item_id = int(data.get("item_id", 0))
        await state.clear()
        await _send_user_item_card(query.message, item_id, settings)
        await query.answer()
        return

    if st == UserBookStates.waiting_confirm.state:
        item_id = int(data.get("item_id", 0))
        await state.set_state(UserBookStates.waiting_hours)
        await state.update_data(hours=None, total=None)
        if not await _resend_book_hours_prompt(query.message, item_id, settings, state):
            await state.clear()
            await query.message.answer(
                "Не удалось вернуться к шагу с часами — начните бронь заново.",
                reply_markup=home_keyboard(),
            )
        await query.answer()
        return

    if st == UserBookStates.waiting_hours.state:
        item_id = int(data.get("item_id", 0))
        await state.set_state(UserBookStates.waiting_start_datetime)
        await state.update_data(book_start_iso=None, hours=None, total=None)
        await _send_booking_start_prompt(query.message, item_id, settings, state)
        await query.answer()
        return

    if st == UserBookStates.waiting_start_datetime.state:
        item_id = int(data.get("item_id", 0))
        await state.clear()
        await _send_user_item_card(query.message, item_id, settings)
        await query.answer()
        return

    await state.clear()
    await query.message.answer("Выберите каталог:", reply_markup=category_keyboard())
    await query.answer()


@router.callback_query(F.data == "u:home")
async def user_home(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await query.message.answer(
        "Выберите каталог:",
        reply_markup=category_keyboard_for_admin(
            is_admin_user=is_admin(query.from_user.id, query.from_user.username, settings)
        ),
    )
    await query.answer()


@router.callback_query(F.data.regexp(r"^take:(\d+)$"))
async def user_take_start(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    item_id = int(query.data.split(":")[1])
    notice = ""
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        await session.commit()
        st = await user_facing_status(session, item_id)
        ref_now = datetime.now(UTC)
        r_item = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        item = r_item.scalar_one_or_none()
        notice = await near_ban_notice_for_user(session, query.from_user.id)
        await session.commit()
    if st is None or item is None:
        await query.answer("Ошибка", show_alert=True)
        return
    if not can_take_immediate_rent(st, ref_now):
        if st.in_blackout and st.blackout_until is not None:
            await query.answer(
                f"Владелец не сможет сдать вещь до {_fmt_utc_local(st.blackout_until, settings)}.",
                show_alert=True,
            )
        else:
            await query.answer("Вещь сейчас недоступна для прямой аренды", show_alert=True)
        return
    lo, hi = rent_hours_bounds(item)
    hi_cap = st.immediate_rent_max_hours
    await state.clear()
    await state.set_state(UserRentStates.waiting_hours)
    await state.update_data(item_id=item_id, flow="rent", immediate_max_hours=hi_cap)
    hours_line = (
        f"Укажите срок аренды в часах (целое число от {lo} до {hi_cap}; "
        f"не дольше, чем до ближайшей брони или занятой аренды):"
    )
    await query.message.answer(
        hours_line + notice,
        reply_markup=nav_back_keyboard(),
        parse_mode=ParseMode.HTML if notice else None,
    )
    await query.answer()


@router.callback_query(F.data.regexp(r"^book:(\d+)$"))
async def user_book_start(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    item_id = int(query.data.split(":")[1])
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        await session.commit()
        st = await user_facing_status(session, item_id)
    if st is None:
        await query.answer("Ошибка", show_alert=True)
        return
    if st.pending_admin:
        await query.answer("Сначала дождитесь решения администратора по текущей заявке", show_alert=True)
        return
    async with db_session.async_session_maker() as session:
        r_item = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        book_item = r_item.scalar_one_or_none()
    if book_item is None:
        await query.answer("Вещь не найдена", show_alert=True)
        return
    await state.clear()
    await state.set_state(UserBookStates.waiting_start_datetime)
    await state.update_data(item_id=item_id, flow="book")
    await _send_booking_start_prompt(query.message, item_id, settings, state)
    await query.answer()


@router.message(UserBookStates.waiting_start_datetime, F.text)
async def user_book_start_datetime(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    item_id = int(data["item_id"])
    parsed = parse_booking_start_text(message.text or "", settings)
    if parsed is None:
        await message.answer(
            "Не получилось разобрать дату. Используйте формат "
            "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>, например <code>04.05.2026 10:00</code>.",
            reply_markup=nav_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    now = datetime.now(UTC)
    past_err = reservation_start_in_past_error(parsed, now)
    if past_err is not None:
        await message.answer(past_err, reply_markup=nav_back_keyboard())
        return
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        r_pend = await session.execute(
            select(Rental.id).where(
                Rental.item_id == item_id,
                Rental.state == RentalState.pending_admin.value,
            )
        )
        if r_pend.scalar_one_or_none() is not None:
            await session.rollback()
            await state.clear()
            await message.answer(
                "Есть ожидающая заявка у администратора — бронь недоступна.",
                reply_markup=home_keyboard(),
            )
            return
        r_item = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        item = r_item.scalar_one_or_none()
        if item is None:
            await session.rollback()
            await state.clear()
            await message.answer("Вещь не найдена.", reply_markup=home_keyboard())
            return
        rr = await load_rr_busy_intervals_utc(session, item_id)
        bo = await load_blackout_intervals_utc(session, item_id)
        if point_inside_busy(parsed, bo) or point_inside_busy(parsed, rr):
            msg = await explain_booking_start_conflict(session, item_id, parsed, settings)
            avail = await format_user_booking_availability_block(
                session, item_id, item, settings, now=now
            )
            await session.rollback()
            await message.answer(
                msg + "\n\n" + avail,
                reply_markup=nav_back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return
        lo, hi = rent_lo_hi(item)
        max_h = max_hours_from_start(parsed, rr, lo, hi)
        if max_h < lo:
            avail = await format_user_booking_availability_block(
                session, item_id, item, settings, now=now
            )
            await session.rollback()
            await message.answer(
                f"После выбранного начала до ближайшей брони или аренды меньше {lo} ч. "
                "Выберите другое время — ниже подсказка по свободным окнам.\n\n"
                + avail,
                reply_markup=nav_back_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return
        await session.commit()

    cap_end = max_reservation_end_utc(parsed, rr)
    hi_eff = min(hi, max_h)
    await state.update_data(book_start_iso=parsed.isoformat())
    await state.set_state(UserBookStates.waiting_hours)
    notice = ""
    async with db_session.async_session_maker() as session:
        notice = await near_ban_notice_for_user(session, message.from_user.id)
        await session.commit()
    await message.answer(
        f"Начало: <b>{_fmt_utc_local(parsed, settings)}</b>.\n"
        f"Можно забронировать не длиннее <b>{max_h}</b> ч. "
        f"(не позже {_fmt_utc_local(cap_end, settings)} — дальше уже занято или лимит {hi} ч.).\n"
        f"Введите число часов от <b>{lo}</b> до <b>{hi_eff}</b>:"
        + notice,
        reply_markup=nav_back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.message(UserRentStates.waiting_hours, F.text)
async def user_rent_hours(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    item_id = int(data["item_id"])
    try:
        h = int((message.text or "").strip())
    except ValueError:
        await message.answer(
            "Нужно целое число часов.",
            reply_markup=nav_back_keyboard(),
        )
        return
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        item = r.scalar_one_or_none()
    if item is None:
        await state.clear()
        await message.answer("Вещь не найдена.", reply_markup=home_keyboard())
        return
    lo, hi = rent_hours_bounds(item)
    cap = min(int(data.get("immediate_max_hours", hi)), hi)
    if h < lo or h > cap:
        await message.answer(
            f"Допустимо от {lo} до {cap} ч. (до ближайшей брони или аренды не больше {cap} ч.).",
            reply_markup=nav_back_keyboard(),
        )
        return
    try:
        total = price_for_hours(item, h)
    except ValueError as e:
        await message.answer(
            f"Ошибка расчёта: {e}",
            reply_markup=nav_back_keyboard(),
        )
        return
    notice = ""
    async with db_session.async_session_maker() as session:
        notice = await near_ban_notice_for_user(session, message.from_user.id)
        await session.commit()
    await state.update_data(hours=h, total=str(total))
    await state.set_state(UserRentStates.waiting_confirm)
    if item.is_paid:
        await message.answer(
            f"Итого за {h} ч: <b>{format_money(total)}</b>\nПодтвердить заявку?"
            + notice,
            reply_markup=confirm_keyboard("rent", item_id),
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            f"Аренда бесплатная ({h} ч).\nПодтвердить заявку?" + notice,
            reply_markup=confirm_keyboard("rent", item_id),
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data.regexp(r"^rent:(yes|no):(\d+)$"))
async def user_rent_confirm(query: CallbackQuery, state: FSMContext, bot: Bot, settings: Settings) -> None:
    parts = query.data.split(":")
    yes = parts[1] == "yes"
    item_id = int(parts[2])
    if not yes:
        await state.clear()
        await query.message.edit_text("Заявка отменена.", reply_markup=None)
        await _send_user_item_card(query.message, item_id, settings)
        await query.answer()
        return
    data = await state.get_data()
    if int(data.get("item_id", -1)) != item_id:
        await query.answer("Данные устарели", show_alert=True)
        return
    hours = int(data.get("hours", 0))
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        st = await user_facing_status(session, item_id)
        now = datetime.now(UTC)
        if st is None or not can_take_immediate_rent(st, now):
            await session.rollback()
            await query.answer("Вещь уже недоступна для этой операции", show_alert=True)
            await state.clear()
            return
        r = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        item = r.scalar_one_or_none()
        if item is None:
            await session.rollback()
            await state.clear()
            await query.answer("Вещь не найдена", show_alert=True)
            return
        lo, hi = rent_hours_bounds(item)
        cap = min(int(data.get("immediate_max_hours", hi)), hi)
        if hours < lo or hours > cap:
            await session.rollback()
            await query.answer(
                f"Недопустимый срок: от {lo} до {cap} ч. (лимит до ближайшей брони или аренды).",
                show_alert=True,
            )
            await state.clear()
            return
        rr = await load_rr_busy_intervals_utc(session, item_id)
        planned_end = now + timedelta(hours=hours)
        if not reservation_fits(rr, now, planned_end):
            await session.rollback()
            await query.answer(
                "Период пересекается с чужой бронью или арендой — выберите меньше часов или позже.",
                show_alert=True,
            )
            await state.clear()
            return
        total = price_for_hours(item, hours)
        rental = Rental(
            item_id=item_id,
            user_id=query.from_user.id,
            username=query.from_user.username,
            state=RentalState.pending_admin.value,
            start_at=now,
            end_at=planned_end,
            requested_hours=hours,
        )
        session.add(rental)
        await session.flush()
        await notify_admins_pending_rental(bot, settings, session, rental, item, total, planned_end)
        contact_html = await landlord_contact_hint_html(bot, item, settings)
        await session.commit()
    await state.clear()
    await query.message.edit_text(
        "Заявка отправлена администратору. Ожидайте подтверждения.\n\n" + contact_html,
        reply_markup=home_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


@router.message(UserBookStates.waiting_hours, F.text)
async def user_book_hours(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    item_id = int(data["item_id"])
    start_raw = data.get("book_start_iso")
    if not start_raw:
        await state.clear()
        await message.answer(
            "Сессия брони сброшена. Начните с кнопки «Забронировать».",
            reply_markup=home_keyboard(),
        )
        return
    try:
        h = int((message.text or "").strip())
    except ValueError:
        await message.answer(
            "Нужно целое число часов.",
            reply_markup=nav_back_keyboard(),
        )
        return
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        r = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        item = r.scalar_one_or_none()
        if item is None:
            await session.rollback()
            await state.clear()
            await message.answer("Вещь не найдена.", reply_markup=home_keyboard())
            return
        rr = await load_rr_busy_intervals_utc(session, item_id)
        await session.commit()
    start_at = ensure_utc(datetime.fromisoformat(str(start_raw)))
    if start_at is None:
        await state.clear()
        await message.answer(
            "Ошибка данных. Начните бронь заново.",
            reply_markup=home_keyboard(),
        )
        return
    now = datetime.now(UTC)
    past_err = reservation_start_in_past_error(start_at, now)
    if past_err is not None:
        await state.clear()
        await message.answer(past_err, reply_markup=home_keyboard())
        return
    lo, hi = rent_lo_hi(item)
    max_h = max_hours_from_start(start_at, rr, lo, hi)
    if max_h < lo:
        await state.clear()
        await message.answer(
            "Слот больше недоступен. Начните с выбора даты начала.",
            reply_markup=home_keyboard(),
        )
        return
    hi_eff = min(hi, max_h)
    if h < lo or h > hi_eff:
        await message.answer(
            f"Укажите целое число часов от {lo} до {hi_eff} "
            f"(до {_fmt_utc_local(max_reservation_end_utc(start_at, rr), settings)}).",
            reply_markup=nav_back_keyboard(),
        )
        return
    try:
        total = price_for_hours(item, h)
    except ValueError as e:
        await message.answer(
            f"Ошибка расчёта: {e}",
            reply_markup=nav_back_keyboard(),
        )
        return
    notice = ""
    async with db_session.async_session_maker() as session:
        notice = await near_ban_notice_for_user(session, message.from_user.id)
        await session.commit()
    await state.update_data(hours=h, total=str(total))
    await state.set_state(UserBookStates.waiting_confirm)
    if item.is_paid:
        await message.answer(
            f"Итого за {h} ч: <b>{format_money(total)}</b>\nПодтвердить бронь?"
            + booking_rules_block()
            + notice,
            reply_markup=confirm_keyboard("book", item_id),
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            f"Бронь бесплатная ({h} ч).\nПодтвердить?"
            + booking_rules_block()
            + notice,
            reply_markup=confirm_keyboard("book", item_id),
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data.regexp(r"^book:(yes|no):(\d+)$"))
async def user_book_confirm(
    query: CallbackQuery, state: FSMContext, bot: Bot, settings: Settings
) -> None:
    parts = query.data.split(":")
    yes = parts[1] == "yes"
    item_id = int(parts[2])
    if not yes:
        await state.clear()
        await query.message.edit_text("Бронь отменена.", reply_markup=None)
        await _send_user_item_card(query.message, item_id, settings)
        await query.answer()
        return
    data = await state.get_data()
    if int(data.get("item_id", -1)) != item_id:
        await query.answer("Данные устарели", show_alert=True)
        return
    hours = int(data.get("hours", 0))
    start_iso = data.get("book_start_iso")
    if not start_iso:
        await state.clear()
        await query.answer("Сессия устарела", show_alert=True)
        return
    async with db_session.async_session_maker() as session:
        await expire_expired_rentals(session)
        now = datetime.now(UTC)
        r_item = await session.execute(
            select(Item).where(Item.id == item_id, Item.is_visible.is_(True))
        )
        item = r_item.scalar_one_or_none()
        if item is None:
            await session.rollback()
            await state.clear()
            await query.answer("Вещь не найдена", show_alert=True)
            return
        lo, hi = rent_lo_hi(item)
        if hours < lo or hours > hi:
            await session.rollback()
            await query.answer(f"Недопустимый срок: от {lo} до {hi} ч.", show_alert=True)
            await state.clear()
            return
        start_at = ensure_utc(datetime.fromisoformat(str(start_iso)))
        if start_at is None:
            await session.rollback()
            await state.clear()
            await query.answer("Ошибка данных", show_alert=True)
            return
        end_at = start_at + timedelta(hours=hours)
        err = await validate_new_reservation(
            session, item_id, start_at, end_at, settings, now=now
        )
        if err is not None:
            await session.rollback()
            await query.answer(err, show_alert=True)
            await state.clear()
            return
        res = Reservation(
            item_id=item_id,
            user_id=query.from_user.id,
            username=query.from_user.username,
            start_at=start_at,
            end_at=end_at,
            requested_hours=hours,
            created_at=now,
        )
        session.add(res)
        await session.flush()
        try:
            total = price_for_hours(item, hours)
        except ValueError:
            total = Decimal("0")
        await notify_admins_new_reservation(bot, settings, item, res, total)
        await session.commit()
    await state.clear()
    await query.message.edit_text(
        f"Бронь создана с {_fmt_utc_local(start_at, settings)} по {_fmt_utc_local(end_at, settings)}.\n\n"
        f"<i>Свяжитесь с арендодателем вовремя — см. правила предупреждений выше.</i>\n\n"
        f"/my_bookings — посмотреть или отменить бронь (не позднее чем за "
        f"{MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START} ч до начала).",
        reply_markup=home_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


@router.message(Command("my_bookings"))
async def cmd_my_bookings(message: Message, settings: Settings) -> None:
    now = datetime.now(UTC)
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Reservation)
            .options(selectinload(Reservation.item))
            .where(
                Reservation.user_id == message.from_user.id,
                Reservation.end_at > now,
            )
            .order_by(Reservation.start_at.asc())
        )
        rows = list(r.scalars().unique())
        await session.commit()
    if not rows:
        await message.answer(
            "У вас нет активных броней на будущее.\n\n"
            f"Свою бронь можно отменить самостоятельно не позднее чем за "
            f"<b>{MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START}</b> ч до начала — команда /my_bookings.",
            reply_markup=home_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    lines = [
        "<b>Ваши брони</b>\n",
        f"<i>Снять бронь самому — не позднее чем за {MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START} ч до начала.</i>\n",
    ]
    for res in rows:
        it = res.item
        name = escape(it.name if it else "?")
        lines.append(
            f"• #{res.id} <b>{name}</b>\n"
            f"  {_fmt_utc_local(res.start_at, settings)} — {_fmt_utc_local(res.end_at, settings)} "
            f"({res.requested_hours} ч)"
        )
    await message.answer(
        "\n".join(lines),
        reply_markup=_my_reservations_keyboard(rows, now=now),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.regexp(r"^u:cnlres:(\d+)$"))
async def user_cancel_reservation_cb(query: CallbackQuery, bot: Bot, settings: Settings) -> None:
    rid = int(query.data.split(":")[2])
    uid = query.from_user.id
    now = datetime.now(UTC)
    async with db_session.async_session_maker() as session:
        r = await session.execute(
            select(Reservation)
            .options(selectinload(Reservation.item))
            .where(Reservation.id == rid)
        )
        res = r.scalar_one_or_none()
        if res is None or res.user_id != uid:
            await session.rollback()
            await query.answer("Бронь не найдена", show_alert=True)
            return
        end_u = ensure_utc(res.end_at)
        if end_u is not None and end_u <= now:
            await session.rollback()
            await query.answer("Бронь уже недоступна для отмены", show_alert=True)
            return
        if not user_may_cancel_reservation(now_utc=now, reservation_start_utc=res.start_at):
            await session.rollback()
            await query.answer(
                f"Отмена возможна не позднее чем за {MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START} ч до начала.",
                show_alert=True,
            )
            return
        item = res.item
        res_id = res.id
        hours = res.requested_hours
        st_copy = res.start_at
        en_copy = res.end_at
        uname = res.username
        await session.delete(res)
        await session.commit()

    await notify_admins_user_cancelled_reservation(
        bot,
        settings,
        item,
        reservation_id=res_id,
        user_id=uid,
        username=uname or query.from_user.username,
        hours=hours,
        start_at=st_copy,
        end_at=en_copy,
    )
    await query.message.edit_text(
        f"Бронь #{res_id} отменена.",
        reply_markup=home_keyboard(),
    )
    await query.answer()
