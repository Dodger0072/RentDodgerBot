from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def start_reply_keyboard() -> ReplyKeyboardMarkup:
    """Под клавиатурой чата: то же действие, что и команда /start."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Начать")]],
        resize_keyboard=True,
    )
