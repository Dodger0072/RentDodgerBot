from __future__ import annotations

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings, is_admin
from bot.keyboards.inline import category_keyboard, home_keyboard

router = Router(name="common")

_ADMIN_HELP = """<b>Команды администратора</b>

<b>Вещи</b>
• /add_item — добавить вещь (она закрепляется за вами как за владельцем)
• /list_items — ваши вещи и «общие» без владельца (старая база)
• /delete_item &lt;id&gt; — удалить свою вещь; общие legacy — только суперадмин (SUPERADMIN_USER_IDS)

<b>Заявки и брони</b>
• /bookings — список броней и активных аренд, отмена с причиной

<b>Недоступность выдачи</b>
• /add_blackout — окно «не дома» для <b>всех ваших</b> вещей (брони и ожидающие выдачи заявки)
• /list_blackouts — список окон
• /delete_blackout &lt;id&gt; — удалить окно (id из списка)

<b>Пользователи</b>
• /ban или /ban_user &lt;username&gt; [комментарий] — запретить бота (с @ или без; нужен пробел)
• /unban или /unban_user &lt;username&gt;
• /list_bans — кто в бане

<b>Прочее</b>
• /start — сбросить шаги и открыть меню
• /help — это сообщение
"""


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    if is_admin(message.from_user.id, message.from_user.username, settings):
        await message.answer(_ADMIN_HELP, reply_markup=home_keyboard(), parse_mode=ParseMode.HTML)
        return
    await message.answer(
        "Доступные действия: нажмите /start и выберите каталог аренды.",
        reply_markup=home_keyboard(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    extra = ""
    if is_admin(message.from_user.id, message.from_user.username, settings):
        extra = (
            "\n\nАдмин: /add_item — добавить вещь, /list_items, /delete_item (id); "
            "/bookings, /add_blackout; /list_blackouts, /delete_blackout id; /delete_item — свои вещи (общие — суперадмин); "
            "/ban @name [причина], /unban @name, /list_bans."
        )
    await message.answer(
        "Привет! Выберите каталог аренды:" + extra,
        reply_markup=category_keyboard(),
    )
