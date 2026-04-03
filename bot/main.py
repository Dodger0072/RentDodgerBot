from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import load_settings
from bot.telegram_session import build_telegram_session
from bot.db.session import init_db, setup_engine
from bot.handlers import admin, common, user
from bot.middlewares import BanMiddleware, SettingsMiddleware
from bot.services.reservation_reminders import reservation_reminder_loop

logging.basicConfig(level=logging.INFO)


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

    asyncio.create_task(reservation_reminder_loop(bot, settings))

    await dp.start_polling(bot)


if __name__ == "__main__":
    # Обход aiohttp/WinError 121 на старых Windows; в Python 3.14+ политика помечена deprecated.
    if sys.platform == "win32" and sys.version_info < (3, 14):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
