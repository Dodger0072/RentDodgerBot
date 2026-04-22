from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from html import escape

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import Settings
from bot.db import session as db_session
from bot.db.models import Item, ItemBlackout, PaymentProof, RentalHandoverStat, WeeklyInvoice, WeeklyInvoiceItem
from bot.services.rental import format_money
from bot.time_format import format_local_time

COMMISSION_RATE = Decimal("0.10")
SYSTEM_BLACKOUT_REASON = "subscription_debt"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClosedWeekRange:
    start_utc: datetime
    end_utc: datetime


def current_week_range(settings: Settings, ref_utc: datetime | None = None) -> ClosedWeekRange:
    now_utc = ref_utc or datetime.now(UTC)
    local = now_utc.astimezone(settings.display_tz)
    this_week_start_local = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_week_start_local = this_week_start_local + timedelta(days=7)
    return ClosedWeekRange(
        start_utc=this_week_start_local.astimezone(UTC),
        end_utc=next_week_start_local.astimezone(UTC),
    )


def _closed_week_range(settings: Settings, ref_utc: datetime | None = None) -> ClosedWeekRange:
    now_utc = ref_utc or datetime.now(UTC)
    local = now_utc.astimezone(settings.display_tz)
    this_week_start_local = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    prev_week_start_local = this_week_start_local - timedelta(days=7)
    return ClosedWeekRange(
        start_utc=prev_week_start_local.astimezone(UTC),
        end_utc=this_week_start_local.astimezone(UTC),
    )


def _calc_due(amount: Decimal) -> Decimal:
    return (amount * COMMISSION_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _invoice_status_open(status: str) -> bool:
    return status in {"awaiting_payment", "pending_review", "rework_required"}


async def _ensure_system_blackouts_for_invoice(session: AsyncSession, invoice: WeeklyInvoice) -> None:
    now_utc = datetime.now(UTC)
    blackout_end = now_utc + timedelta(days=365)
    q_items = await session.execute(
        select(Item).where(
            Item.owner_user_id == int(invoice.owner_user_id),
            Item.is_paid.is_(True),
        )
    )
    paid_items = list(q_items.scalars().unique())
    if not paid_items:
        return
    q_existing = await session.execute(
        select(ItemBlackout.item_id).where(
            ItemBlackout.invoice_id == invoice.id,
            ItemBlackout.created_by_system.is_(True),
            ItemBlackout.end_at > now_utc,
        )
    )
    existing_item_ids = {int(x) for x in q_existing.scalars().all()}
    for item in paid_items:
        if item.id in existing_item_ids:
            continue
        session.add(
            ItemBlackout(
                item_id=item.id,
                window_id=None,
                invoice_id=invoice.id,
                created_by_system=True,
                reason_code=SYSTEM_BLACKOUT_REASON,
                start_at=now_utc,
                end_at=blackout_end,
                created_at=now_utc,
            )
        )


def _invoice_text(settings: Settings, invoice: WeeklyInvoice, item_rows: list[WeeklyInvoiceItem]) -> str:
    lines = [
        "<b>Недельный счёт (комиссия 10%)</b>",
        (
            f"Период: {format_local_time(invoice.week_start_at, settings)} — "
            f"{format_local_time(invoice.week_end_at, settings)}"
        ),
        "",
        f"Заработано за неделю (платные вещи): {format_money(invoice.total_earned)}",
        f"К оплате владельцу бота (10%): <b>{format_money(invoice.total_due)}</b>",
        "",
        "<b>По вещам:</b>",
    ]
    for row in item_rows:
        name = escape(row.item_name or f"Вещь #{row.item_id or '?'}")
        lines.append(
            f"• {name}: доход {format_money(Decimal(row.earned))}, комиссия {format_money(Decimal(row.due))}"
        )
    lines += [
        "",
        "Оплата: можно перевести на счёт семьи (если вы в Dodger) "
        "или отправить перевод Luis_Monte / Angela_Monte.",
        "После оплаты сразу отправьте сюда <b>скрин перевода</b> одним фото.",
        "До подтверждения суперадмином платные вещи временно недоступны.",
    ]
    return "\n".join(lines)


async def _notify_owner_invoice(
    bot: Bot,
    settings: Settings,
    invoice: WeeklyInvoice,
    item_rows: list[WeeklyInvoiceItem],
) -> bool:
    text = _invoice_text(settings, invoice, item_rows)
    try:
        await bot.send_message(int(invoice.owner_user_id), text, parse_mode=ParseMode.HTML)
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def build_weekly_invoices_and_notify(bot: Bot, settings: Settings, ref_utc: datetime | None = None) -> None:
    week = _closed_week_range(settings, ref_utc=ref_utc)
    await build_invoices_for_range_and_notify(
        bot,
        settings,
        start_utc=week.start_utc,
        end_utc=week.end_utc,
        force_notify=False,
    )


async def build_invoices_for_range_and_notify(
    bot: Bot,
    settings: Settings,
    *,
    start_utc: datetime,
    end_utc: datetime,
    force_notify: bool = False,
    owner_user_id: int | None = None,
) -> None:
    async with db_session.async_session_maker() as session:
        where_clauses = [
            Item.is_paid.is_(True),
            Item.owner_user_id.is_not(None),
            RentalHandoverStat.handed_over_at >= start_utc,
            RentalHandoverStat.handed_over_at < end_utc,
        ]
        if owner_user_id is not None:
            where_clauses.append(Item.owner_user_id == int(owner_user_id))
        rows = await session.execute(
            select(
                Item.owner_user_id,
                RentalHandoverStat.item_id,
                Item.name,
                func.coalesce(func.sum(RentalHandoverStat.amount), 0),
            )
            .select_from(RentalHandoverStat)
            .join(Item, RentalHandoverStat.item_id == Item.id)
            .where(*where_clauses)
            .group_by(Item.owner_user_id, RentalHandoverStat.item_id, Item.name)
        )
        grouped: dict[int, list[tuple[int | None, str, Decimal]]] = {}
        for owner_user_id, item_id, item_name, earned in rows.all():
            if owner_user_id is None:
                continue
            grouped.setdefault(int(owner_user_id), []).append(
                (int(item_id) if item_id is not None else None, str(item_name or ""), Decimal(earned or 0))
            )

        for owner_user_id, item_rows in grouped.items():
            total_earned = sum((x[2] for x in item_rows), Decimal("0"))
            total_due = _calc_due(total_earned)
            if total_due <= Decimal("0"):
                continue
            q_inv = await session.execute(
                select(WeeklyInvoice).where(
                    WeeklyInvoice.owner_user_id == owner_user_id,
                    WeeklyInvoice.week_start_at == start_utc,
                    WeeklyInvoice.week_end_at == end_utc,
                )
            )
            invoice = q_inv.scalar_one_or_none()
            if invoice is None:
                invoice = WeeklyInvoice(
                    owner_user_id=owner_user_id,
                    week_start_at=start_utc,
                    week_end_at=end_utc,
                    total_earned=total_earned,
                    total_due=total_due,
                    status="awaiting_payment",
                    created_at=datetime.now(UTC),
                    finalized_at=None,
                    notified_at=None,
                )
                session.add(invoice)
                await session.flush()
            elif invoice.status == "paid":
                continue
            else:
                invoice.total_earned = total_earned
                invoice.total_due = total_due

            await session.execute(delete(WeeklyInvoiceItem).where(WeeklyInvoiceItem.invoice_id == invoice.id))
            for item_id, item_name, earned in item_rows:
                session.add(
                    WeeklyInvoiceItem(
                        invoice_id=invoice.id,
                        item_id=item_id,
                        item_name=item_name or f"Вещь #{item_id or '?'}",
                        earned=earned,
                        due=_calc_due(earned),
                    )
                )

            if _invoice_status_open(invoice.status):
                await _ensure_system_blackouts_for_invoice(session, invoice)

            q_items = await session.execute(
                select(WeeklyInvoiceItem).where(WeeklyInvoiceItem.invoice_id == invoice.id).order_by(WeeklyInvoiceItem.id)
            )
            persisted_items = list(q_items.scalars().unique())
            if force_notify or invoice.notified_at is None:
                if await _notify_owner_invoice(bot, settings, invoice, persisted_items):
                    invoice.notified_at = datetime.now(UTC)

        await session.commit()


async def register_payment_proof(
    session: AsyncSession,
    *,
    owner_user_id: int,
    screenshot_file_id: str,
    note: str = "",
    invoice_id: int | None = None,
) -> tuple[WeeklyInvoice | None, PaymentProof | None, str | None]:
    if invoice_id is None:
        q = await session.execute(
            select(WeeklyInvoice)
            .where(
                WeeklyInvoice.owner_user_id == int(owner_user_id),
                WeeklyInvoice.status.in_(["awaiting_payment", "rework_required"]),
            )
            .order_by(WeeklyInvoice.week_end_at.desc())
        )
        invoice = q.scalar_one_or_none()
    else:
        q = await session.execute(
            select(WeeklyInvoice).where(
                WeeklyInvoice.id == int(invoice_id),
                WeeklyInvoice.owner_user_id == int(owner_user_id),
            )
        )
        invoice = q.scalar_one_or_none()
    if invoice is None:
        return None, None, "Нет открытого недельного долга для оплаты."
    if invoice.status == "paid":
        return None, None, "Этот счёт уже закрыт."
    q_proof = await session.execute(
        select(PaymentProof).where(
            PaymentProof.invoice_id == invoice.id,
            PaymentProof.status == "pending_review",
        )
    )
    if q_proof.scalar_one_or_none() is not None:
        return None, None, "Скрин уже отправлен и ожидает проверку суперадмином."
    proof = PaymentProof(
        invoice_id=invoice.id,
        owner_user_id=int(owner_user_id),
        screenshot_file_id=screenshot_file_id,
        note=note.strip(),
        status="pending_review",
        created_at=datetime.now(UTC),
        reviewed_by_user_id=None,
        reviewed_at=None,
        review_comment="",
    )
    session.add(proof)
    invoice.status = "pending_review"
    await session.flush()
    return invoice, proof, None


def payment_review_keyboard(proof_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Подтвердить", callback_data=f"adm:inv:approve:{proof_id}"))
    kb.row(InlineKeyboardButton(text="Отклонить", callback_data=f"adm:inv:reject:{proof_id}"))
    kb.row(InlineKeyboardButton(text="На доработку", callback_data=f"adm:inv:rework:{proof_id}"))
    return kb


async def notify_superadmins_about_proof(
    bot: Bot,
    settings: Settings,
    invoice: WeeklyInvoice,
    proof: PaymentProof,
    item_rows: list[WeeklyInvoiceItem],
) -> None:
    review_targets = sorted(settings.superadmin_user_ids or settings.admin_user_ids)
    if not review_targets:
        return
    lines = [
        "<b>Новый скрин оплаты по недельному счёту</b>",
        f"Админ: <code>{invoice.owner_user_id}</code>",
        (
            f"Период: {format_local_time(invoice.week_start_at, settings)} — "
            f"{format_local_time(invoice.week_end_at, settings)}"
        ),
        f"К оплате: <b>{format_money(Decimal(invoice.total_due))}</b>",
        "",
        "<b>Разбивка:</b>",
    ]
    for row in item_rows:
        name = escape(row.item_name)
        lines.append(f"• {name}: {format_money(Decimal(row.due))}")
    if proof.note:
        lines += ["", f"Комментарий: {escape(proof.note)}"]
    text = "\n".join(lines)
    kb = payment_review_keyboard(proof.id).as_markup()
    for uid in review_targets:
        try:
            await bot.send_photo(
                uid,
                photo=proof.screenshot_file_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            continue


async def apply_payment_review(
    session: AsyncSession,
    *,
    proof_id: int,
    reviewer_user_id: int,
    action: str,
) -> tuple[PaymentProof | None, WeeklyInvoice | None, str | None]:
    q = await session.execute(
        select(PaymentProof)
        .options(selectinload(PaymentProof.invoice))
        .where(PaymentProof.id == int(proof_id))
    )
    proof = q.scalar_one_or_none()
    if proof is None:
        return None, None, "Скрин оплаты не найден."
    if proof.status != "pending_review":
        return None, None, "Этот скрин уже обработан."
    invoice = proof.invoice
    if invoice is None:
        return None, None, "Инвойс для скрина не найден."
    proof.reviewed_by_user_id = int(reviewer_user_id)
    proof.reviewed_at = datetime.now(UTC)
    if action == "approve":
        proof.status = "approved"
        proof.review_comment = "Оплата подтверждена."
        invoice.status = "paid"
        invoice.finalized_at = datetime.now(UTC)
        await session.execute(
            delete(ItemBlackout).where(
                ItemBlackout.invoice_id == invoice.id,
                ItemBlackout.created_by_system.is_(True),
                ItemBlackout.reason_code == SYSTEM_BLACKOUT_REASON,
            )
        )
    elif action == "reject":
        proof.status = "rejected"
        proof.review_comment = "Оплата отклонена."
        invoice.status = "awaiting_payment"
    elif action == "rework":
        proof.status = "rework_required"
        proof.review_comment = "Нужно доработать оплату."
        invoice.status = "rework_required"
    else:
        return None, None, "Неизвестное действие."
    await session.flush()
    return proof, invoice, None


async def subscription_billing_loop(bot: Bot, settings: Settings, interval_sec: float = 300.0) -> None:
    while True:
        try:
            await asyncio.sleep(interval_sec)
            await build_weekly_invoices_and_notify(bot, settings)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("subscription_billing_loop tick failed")
