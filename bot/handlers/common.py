from __future__ import annotations

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings, is_admin
from bot.keyboards.inline import home_keyboard
from bot.keyboards.reply import start_reply_keyboard
from bot.main_menu import send_main_menu

router = Router(name="common")

_ADMIN_HELP = """<b>Команды администратора</b>

<b>Вещи</b>
• /add_item — добавить вещь (категория, фото, платная/бесплатная); закрепляется за вами
• /list_items — ваши вещи и «общие» без владельца (старая база)
• /item_order &lt;id&gt; &lt;позиция&gt; — порядок в каталоге у пользователя внутри группы (платность+категория), 1 = выше всех
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
• /start или кнопка «Начать» — сбросить шаги и открыть меню
• /help — это сообщение
"""


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    if is_admin(message.from_user.id, message.from_user.username, settings):
        await message.answer(_ADMIN_HELP, reply_markup=home_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(
            "Доступные действия: /start или кнопка «Начать», затем выберите каталог аренды.",
            reply_markup=home_keyboard(),
        )
    await message.answer(
        "Быстрый возврат в каталог — кнопка «Начать» под полем ввода.",
        reply_markup=start_reply_keyboard(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, settings: Settings) -> None:
    await send_main_menu(message, state, settings)
