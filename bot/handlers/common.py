from __future__ import annotations

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings, is_admin, is_superadmin, superadmin_roles_enabled
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


_ADMIN_HELP_FULL = """<b>Команды администратора</b>

<b>Вещи</b>
• /add_item — добавить вещь (категория, фото, платная/бесплатная, срок аренды от n до m ч.); в диалоге шаг назад: /back или «назад» (не на вводе названия)
• /list_items — ваши вещи и «общие» без владельца (старая база)
• /edit_item &lt;id&gt; — изменить название, описание, категорию, фото, сроки, цены, платная/бесплатная (своя вещь или «общая» без владельца — любой админ; иначе только суперадмин)
• /item_order &lt;id&gt; &lt;позиция&gt; — порядок в каталоге у пользователя внутри группы (платность+категория), 1 = выше всех
• /delete_item &lt;id&gt; — удалить свою вещь; общие legacy — только суперадмин (SUPERADMIN_USER_IDS)

<b>Заявки и брони</b>
• /bookings — список броней и активных аренд, отмена с причиной
• /drop_request &lt;item_id&gt; — вручную снять зависшую заявку «ожидает админа» по конкретной вещи
• /rent_stats — ваша статистика: заработок (всего, за сегодня / неделю / месяц), число выдач
• /my_invoices — ваши неоплаченные счета по неделям; выбор недели и отправка скрина оплаты

<b>Окна неактива выдачи</b>
• /add_blackout — окно «не дома» для <b>всех ваших</b> вещей (снимаются только брони/заявки, если <b>начало</b> слота в окне)
• /list_blackouts — предстоящие окна (общее /add_blackout — один id на все вещи)
• /delete_blackout &lt;id&gt; — удалить; id общего окна снимает его сразу со всех вещей

<b>Пользователи</b>
• /ban или /ban_user &lt;username&gt; [комментарий] — запретить бота: только <b>суперадмин</b>, если в .env задан <code>SUPERADMIN_USER_IDS</code> (иначе — любой админ, как раньше)
• /unban или /unban_user &lt;username&gt; — снять блокировку: те же правила
• /list_bans — кто в бане
• /warn или /warn_user &lt;@username или id&gt; [причина] — предупреждение арендатору; при заданных суперадминах автобан по 3-м только если предупреждение выдал суперадмин (иначе суперадмины получают уведомление)
• /unwarn или /unwarn_user &lt;@username или id&gt; — обнулить предупреждения и счётчик успешных выдач (бан не снимает)
• /list_warnings — у кого есть активные предупреждения

<b>Прочее</b>
• /start или кнопка «Начать» — сбросить шаги и открыть меню
• /help — это сообщение
"""

_ADMIN_HELP_SCOPED = """<b>Команды администратора</b>

<b>Вещи</b>
• /add_item — добавить вещь (категория, фото, платная/бесплатная, срок аренды от n до m ч.); в диалоге шаг назад: /back или «назад» (не на вводе названия)
• /list_items — ваши вещи и «общие» без владельца (старая база)
• /edit_item &lt;id&gt; — изменить название, описание, категорию, фото, сроки, цены, платная/бесплатная (<b>своя</b> вещь или «общая» без владельца)
• /item_order &lt;id&gt; &lt;позиция&gt; — порядок в каталоге у пользователя внутри группы (платность+категория), 1 = выше всех
• /delete_item &lt;id&gt; — удалить <b>свою</b> вещь (общие без владельца — через суперадмина)

<b>Заявки и брони</b>
• /bookings — список броней и активных аренд, отмена с причиной
• /drop_request &lt;item_id&gt; — вручную снять зависшую заявку «ожидает админа» по конкретной вещи
• /rent_stats — ваша статистика: заработок (всего, за сегодня / неделю / месяц), число выдач
• /my_invoices — ваши неоплаченные счета по неделям; выбор недели и отправка скрина оплаты

<b>Окна неактива выдачи</b>
• /add_blackout — окно «не дома» для <b>всех ваших</b> вещей (снимаются только брони/заявки, если <b>начало</b> слота в окне)
• /list_blackouts — предстоящие окна (общее /add_blackout — один id на все вещи)
• /delete_blackout &lt;id&gt; — удалить; id общего окна снимает его сразу со всех вещей

<b>Пользователи</b>
• /list_bans — посмотреть, кто в бане
• /warn или /warn_user &lt;@username или id&gt; [причина] — предупреждение арендатору (о выдаче суперадмины получают уведомление; автобан по лимиту — только если предупреждение выдал суперадмин)
• /unwarn или /unwarn_user &lt;@username или id&gt; — обнулить предупреждения и счётчик успешных выдач (бан не снимает)
• /list_warnings — у кого есть активные предупреждения

<b>Прочее</b>
• /start или кнопка «Начать» — сбросить шаги и открыть меню
• /help — это сообщение
"""

_SUPERADMIN_HELP_EXTRA = """

<b>Команды суперадмина</b> <i>(id в <code>SUPERADMIN_USER_IDS</code>)</i>

<b>Вещи</b>
• /edit_item &lt;id&gt; — также вещи других админов (не только свои и «общие»)
• /delete_item &lt;id&gt; — также «общие» вещи без владельца (legacy)

<b>Пользователи</b>
• /ban или /ban_user &lt;username&gt; [комментарий] — запретить бота (с @ или без)
• /unban или /unban_user &lt;username&gt; — снять блокировку
• Автобан при <b>3</b> предупреждениях, если третье (и бан) оформляет суперадмин; при предупреждении от обычного админа суперадмины получают уведомление в Telegram
• /issue_invoice_now [@username|user_id] — принудительно выставить недельные счета сейчас: всем или конкретному админу
• /item_logs — логи по конкретному админу и его вещи: заявки пользователей и решения «сдал/не сдал»
"""


def _help_text_for_admin_user(user_id: int, settings: Settings) -> str:
    """Суперадмин — полная справка; обычный админ — без блока суперадмина, если роли включены."""
    if is_superadmin(user_id, settings):
        if superadmin_roles_enabled(settings):
            return _ADMIN_HELP_SCOPED + _SUPERADMIN_HELP_EXTRA
        return _ADMIN_HELP_FULL
    if superadmin_roles_enabled(settings):
        return _ADMIN_HELP_SCOPED
    return _ADMIN_HELP_FULL


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    async with db_session.async_session_maker() as session:
        menu_seen = await user_main_menu_seen(session, message.from_user.id)
        await session.commit()
    uid = message.from_user.id
    un = message.from_user.username
    if is_superadmin(uid, settings) or is_admin(uid, un, settings):
        await message.answer(
            _help_text_for_admin_user(uid, settings),
            reply_markup=home_keyboard(),
            parse_mode=ParseMode.HTML,
        )
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
