from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import load_settings
from bot.telegram_session import build_telegram_session
from bot.db.session import init_db, setup_engine
from bot.handlers import admin, common, user
from bot.middlewares import BanMiddleware, SettingsMiddleware
from bot.services.reservation_reminders import reservation_reminder_loop

logging.basicConfig(level=logging.INFO)


def _public_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Открыть каталог аренды"),
        BotCommand(command="help", description="Список команд и справка"),
        BotCommand(command="my_bookings", description="Мои брони и их отмена"),
    ]


def _admin_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Открыть каталог аренды"),
        BotCommand(command="help", description="Список команд и справка"),
        BotCommand(command="my_bookings", description="Мои брони и их отмена"),
        BotCommand(command="bookings", description="Брони и аренды (админ)"),
        BotCommand(command="drop_request", description="Снять зависшую заявку по вещи"),
        BotCommand(command="rent_stats", description="Статистика выдач и дохода"),
        BotCommand(command="add_item", description="Добавить вещь"),
        BotCommand(command="list_items", description="Список ваших вещей"),
        BotCommand(command="edit_item", description="Редактировать вещь по id"),
        BotCommand(command="item_order", description="Поменять порядок вещи"),
        BotCommand(command="delete_item", description="Удалить вещь по id"),
        BotCommand(command="add_blackout", description="Добавить окно недоступности"),
        BotCommand(command="list_blackouts", description="Список окон недоступности"),
        BotCommand(command="delete_blackout", description="Удалить окно по id"),
        BotCommand(command="warn", description="Выдать предупреждение пользователю"),
        BotCommand(command="unwarn", description="Снять предупреждения"),
        BotCommand(command="list_warnings", description="Список предупреждений"),
        BotCommand(command="list_bans", description="Список заблокированных"),
        BotCommand(command="ban", description="Заблокировать пользователя"),
        BotCommand(command="unban", description="Снять блокировку"),
    ]


async def _setup_bot_commands(bot: Bot, settings) -> None:
    # Команды в меню по "/" для всех приватных чатов.
    await bot.set_my_commands(
        _public_commands(),
        scope=BotCommandScopeAllPrivateChats(),
    )
    # Расширяем набор команд адресно для админов по их chat_id.
    admin_ids = sorted(settings.admin_user_ids | settings.superadmin_user_ids)
    if not admin_ids:
        return
    admin_cmds = _admin_commands()
    for uid in admin_ids:
        await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=uid))


async def main() -> None:
    settings = load_settings()
    setup_engine(settings)
    await init_db()

    bot_kwargs: dict = {
        "default": DefaultBotProperties(parse_mode=ParseMode.HTML),
    }
    session = build_telegram_session(
        settings.telegram_proxy,
        socks_rdns=settings.telegram_socks_rdns,
        request_timeout=settings.telegram_request_timeout,
    )
    if session is not None:
        bot_kwargs["session"] = session
    bot = Bot(settings.bot_token, **bot_kwargs)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(SettingsMiddleware(settings))
    dp.update.middleware(BanMiddleware())

    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(user.router)

    await _setup_bot_commands(bot, settings)
    asyncio.create_task(reservation_reminder_loop(bot, settings))

    await dp.start_polling(bot)


if __name__ == "__main__":
    # Обход aiohttp/WinError 121 на старых Windows; в Python 3.14+ политика помечена deprecated.
    if sys.platform == "win32" and sys.version_info < (3, 14):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
