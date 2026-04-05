from aiogram.fsm.state import State, StatesGroup


class AddItemStates(StatesGroup):
    name = State()
    description = State()
    category = State()
    photos = State()
    is_paid = State()
    rent_hours_min = State()
    rent_hours_max = State()
    price_hour = State()
    price_day = State()
    price_week = State()


class EditItemStates(StatesGroup):
    """Пошаговое изменение полей существующей вещи (/edit_item)."""

    name = State()
    description = State()
    photos = State()
    rent_hours_min = State()
    rent_hours_max = State()
    price_hour = State()
    price_day = State()
    price_week = State()


class UserRentStates(StatesGroup):
    waiting_hours = State()
    waiting_confirm = State()


class UserBookStates(StatesGroup):
    waiting_start_datetime = State()
    waiting_hours = State()
    waiting_confirm = State()


class AdminRentalStates(StatesGroup):
    """После «Вещь сдана» — ждём срок в часах (кнопка или текст)."""
    waiting_handover_hours = State()


class AdminReservationStates(StatesGroup):
    """Отмена брони — ждём текст причины для пользователя."""
    waiting_cancel_reason = State()


class AdminBlackoutStates(StatesGroup):
    waiting_start = State()
    waiting_end = State()
