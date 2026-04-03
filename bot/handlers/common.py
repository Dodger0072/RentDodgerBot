from __future__ import annotations

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings, is_admin
from bot.db import session as db_session
from bot.services.booking_schedule import MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START
from bot.services.user_bot_state import user_main_menu_seen
from bot.keyboards.inline import home_keyboard
from bot.keyboards.reply import remove_reply_keyboard, start_reply_keyboard
from bot.main_menu import send_main_menu

router = Router(name="common")


class ReplyKeyboardStartFilter(BaseFilter):
    """Текст с кнопки «Начать» (reply keyboard), регистр не важен."""

    async def __call__(self, message: Message) -> bool:
        return (message.text or "").strip().casefold() == "начать"


@router.message(ReplyKeyboardStartFilter())
async def cmd_start_reply_button(message: Message, state: FSMContext, settings: Settings) -> None:
    await send_main_menu(message, state, settings)


_ADMIN_HELP = """<b>Команды администратора</b>

<b>Вещи</b>
• /add_item — добавить вещь (категория, фото, платная/бесплатная, срок аренды от n до m ч.); в диалоге шаг назад: /back или «назад» (не на вводе названия)
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
• /warn или /warn_user &lt;@username или id&gt; [причина] — предупреждение арендатору (3 = бан, как в правилах брони)
• /list_warnings — у кого есть активные предупреждения

<b>Прочее</b>
• /start или кнопка «Начать» — сбросить шаги и открыть меню
• /help — это сообщение
"""


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    async with db_session.async_session_maker() as session:
        menu_seen = await user_main_menu_seen(session, message.from_user.id)
        await session.commit()
    if is_admin(message.from_user.id, message.from_user.username, settings):
        await message.answer(_ADMIN_HELP, reply_markup=home_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(
            "Доступные действия: /start или кнопка «Начать», затем каталог аренды. "
            f"/my_bookings — ваши брони; отмена не позднее чем за {MIN_HOURS_USER_CANCEL_RESERVATION_BEFORE_START} ч до начала.",
            reply_markup=home_keyboard(),
        )
    if menu_seen:
        await message.answer(
            "Вернуться в каталог: /start.",
            reply_markup=remove_reply_keyboard(),
        )
    else:
        await message.answer(
            "Быстрый возврат в каталог — кнопка «Начать» под полем ввода.",
            reply_markup=start_reply_keyboard(),
        )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, settings: Settings) -> None:
    await send_main_menu(message, state, settings)
