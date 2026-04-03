from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from bot.config import Settings, is_admin
from bot.db import session as db_session
from bot.services.user_bans import is_user_banned


def _user_from_update(update: Update):
    if update.message and update.message.from_user:
        return update.message.from_user
    if update.edited_message and update.edited_message.from_user:
        return update.edited_message.from_user
    if update.callback_query and update.callback_query.from_user:
        return update.callback_query.from_user
    if update.inline_query and update.inline_query.from_user:
        return update.inline_query.from_user
    if update.chosen_inline_result and update.chosen_inline_result.from_user:
        return update.chosen_inline_result.from_user
    if update.shipping_query and update.shipping_query.from_user:
        return update.shipping_query.from_user
    if update.pre_checkout_query and update.pre_checkout_query.from_user:
        return update.pre_checkout_query.from_user
    if update.poll_answer and update.poll_answer.user:
        return update.poll_answer.user
    return None


class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings: Settings | None = data.get("settings")
        if settings is None or not isinstance(event, Update):
            return await handler(event, data)

        user = _user_from_update(event)
        if user is None:
            return await handler(event, data)

        if is_admin(user.id, user.username, settings):
            return await handler(event, data)

        async with db_session.async_session_maker() as session:
            banned = await is_user_banned(session, user_id=user.id, username=user.username)
            await session.commit()

        if not banned:
            return await handler(event, data)

        if event.message:
            msg: Message = event.message
            await msg.answer("Доступ к боту для вас закрыт (блокировка администратором).")
        elif event.callback_query:
            cq: CallbackQuery = event.callback_query
            await cq.answer("Доступ запрещён. Вы заблокированы.", show_alert=True)
        return None
