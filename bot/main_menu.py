from __future__ import annotations

from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings, is_admin
from bot.db import session as db_session
from bot.services.booking_schedule import MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START
from bot.services.user_bot_state import mark_main_menu_seen, user_main_menu_seen
from bot.keyboards.inline import category_keyboard
from bot.keyboards.reply import remove_reply_keyboard, start_reply_keyboard

_RENTAL_TYPES_INFO = (
    "<b>Платная аренда</b> — аренда за деньги. Доступна всем на срок от 3 до 168 часов.\n\n"
    "<b>Бесплатная аренда</b> — аксессуары сдаются бесплатно. Доступна новичкам до 30-го уровня "
    "из семьи Dodger. Если вы не подходите под одно из условий — рассмотрите платную аренду. "
    "Максимальный срок бесплатной аренды — 12 часов.\n\n"
    "Привет! <b>Выберите каталог аренды:</b>"
)


async def send_main_menu(message: Message, state: FSMContext, settings: Settings) -> None:
    """Сброс FSM и главный экран. Reply-кнопка «Начать» только до первого показа меню."""
    await state.clear()
    uid = message.from_user.id
    async with db_session.async_session_maker() as session:
        first_menu = not await user_main_menu_seen(session, uid)
        await session.commit()

    extra = ""
    if is_admin(message.from_user.id, message.from_user.username, settings):
        extra = (
            "\n\nАдмин: /add_item — добавить вещь, /list_items, /item_order id позиция, /delete_item (id); "
            "/bookings, /add_blackout; /list_blackouts, /delete_blackout id; /delete_item — свои вещи (общие — суперадмин); "
            "/ban @name [причина], /unban @name, /list_bans."
        )
    await message.answer(
        _RENTAL_TYPES_INFO + extra,
        reply_markup=category_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    footer = (
        f"/my_bookings — список ваших броней и отмена (не позднее чем за {MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START} ч до начала слота).\n\n"
    )
    if first_menu:
        footer += (
            "Кнопка «Начать» внизу экрана — то же, что /start (сброс шагов и этот каталог)."
        )
        reply_bottom = start_reply_keyboard()
    else:
        footer += "Вернуться в этот каталог позже: команда /start."
        reply_bottom = remove_reply_keyboard()
    await message.answer(footer, reply_markup=reply_bottom)

    if first_menu:
        async with db_session.async_session_maker() as session:
            await mark_main_menu_seen(session, uid)
            await session.commit()
