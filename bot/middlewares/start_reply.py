from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from bot.config import Settings
from bot.main_menu import send_main_menu


class StartReplyButtonMiddleware(BaseMiddleware):
    """Текст «Начать» (reply-кнопка) = полный сброс и главное меню, из любого состояния FSM."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)
        if (event.text or "").strip().casefold() != "начать":
            return await handler(event, data)
        settings = data.get("settings")
        state = data.get("state")
        if not isinstance(settings, Settings) or not isinstance(state, FSMContext):
            return await handler(event, data)
        await send_main_menu(event, state, settings)
        return None
