from aiogram.fsm.state import State, StatesGroup


class AddItemStates(StatesGroup):
    name = State()
    description = State()
    category = State()
    photos = State()
    is_paid = State()
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
