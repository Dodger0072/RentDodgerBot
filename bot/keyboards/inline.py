from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def category_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="Платная аренда", callback_data="cat:paid"),
        InlineKeyboardButton(text="Бесплатная аренда", callback_data="cat:free"),
    )
    return b.as_markup()


def home_keyboard() -> InlineKeyboardMarkup:
    """Выбор платная/бесплатная (как после /start). Callback: u:home."""
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="« Главное меню", callback_data="u:home"))
    return b.as_markup()


def item_list_keyboard(items: list[tuple[int, str]], prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item_id, name in items:
        b.row(InlineKeyboardButton(text=name, callback_data=f"{prefix}:item:{item_id}"))
    b.row(InlineKeyboardButton(text="« Главное меню", callback_data="u:home"))
    return b.as_markup()


def confirm_keyboard(action: str, item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="Да", callback_data=f"{action}:yes:{item_id}"),
        InlineKeyboardButton(text="Нет", callback_data=f"{action}:no:{item_id}"),
    )
    b.row(InlineKeyboardButton(text="« Главное меню", callback_data="u:home"))
    return b.as_markup()


def admin_rental_decision_keyboard(rental_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="Вещь сдана", callback_data=f"adm:r:{rental_id}:ok"),
        InlineKeyboardButton(text="Вещь не сдана", callback_data=f"adm:r:{rental_id}:no"),
    )
    return b.as_markup()


def admin_hours_keyboard(rental_id: int) -> InlineKeyboardMarkup:
    hours = [1, 3, 6, 12, 24, 48, 72, 168]
    b = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for h in hours:
        row.append(InlineKeyboardButton(text=str(h), callback_data=f"adm:r:{rental_id}:h:{h}"))
        if len(row) == 4:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(InlineKeyboardButton(text="Отмена", callback_data=f"adm:r:{rental_id}:cancel"))
    return b.as_markup()
