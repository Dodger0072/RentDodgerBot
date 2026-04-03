from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import List

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserBan(Base):
    """Запрет доступа к боту по @username (и при наличии — по telegram user id)."""
    __tablename__ = "user_bans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username_norm: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    user_id = mapped_column(BigInteger, nullable=True, unique=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RentalState(str, enum.Enum):
    pending_admin = "pending_admin"
    active = "active"


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    photos_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    price_hour = mapped_column(Numeric(12, 2), nullable=True)
    price_day = mapped_column(Numeric(12, 2), nullable=True)
    price_week = mapped_column(Numeric(12, 2), nullable=True)
    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    owner_user_id = mapped_column(BigInteger, nullable=True)
    owner_username = mapped_column(String(255), nullable=True)
    item_category = mapped_column(String(64), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rent_hours_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rent_hours_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    rentals: Mapped[List["Rental"]] = relationship(back_populates="item")
    reservations: Mapped[List["Reservation"]] = relationship(back_populates="item")
    blackouts: Mapped[List["ItemBlackout"]] = relationship(back_populates="item")


class ItemBlackout(Base):
    """Окно, когда арендодатель не может выдать конкретную вещь."""

    __tablename__ = "item_blackouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    item: Mapped["Item"] = relationship(back_populates="blackouts")


class Rental(Base):
    __tablename__ = "rentals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    start_at = mapped_column(DateTime(timezone=True), nullable=True)
    end_at = mapped_column(DateTime(timezone=True), nullable=True)
    requested_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    admin_message_chat_id = mapped_column(BigInteger, nullable=True)
    admin_message_id = mapped_column(BigInteger, nullable=True)

    item: Mapped["Item"] = relationship(back_populates="rentals")


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username = mapped_column(String(255), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notified_before_1h: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notified_before_15m: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    item: Mapped["Item"] = relationship(back_populates="reservations")
