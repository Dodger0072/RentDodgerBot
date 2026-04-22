from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Item, RentalDecisionLog


async def log_rental_event(
    session: AsyncSession,
    *,
    item_id: int | None,
    owner_user_id: int,
    rental_id: int | None,
    renter_user_id: int,
    renter_username: str | None,
    event_type: str,
    requested_hours: int | None,
    chosen_hours: int | None = None,
    note: str = "",
) -> None:
    session.add(
        RentalDecisionLog(
            item_id=item_id,
            owner_user_id=int(owner_user_id),
            rental_id=rental_id,
            renter_user_id=int(renter_user_id),
            renter_username=renter_username,
            event_type=event_type.strip(),
            requested_hours=requested_hours,
            chosen_hours=chosen_hours,
            note=(note or "").strip(),
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()


@dataclass(frozen=True)
class AdminLogItemRow:
    item_id: int | None
    item_name: str
    events_count: int


@dataclass(frozen=True)
class AdminLogOwnerRow:
    user_id: int
    username: str


async def admins_with_log_activity(session: AsyncSession) -> list[AdminLogOwnerRow]:
    q = await session.execute(
        select(
            RentalDecisionLog.owner_user_id,
            func.coalesce(func.max(Item.owner_username), ""),
        )
        .select_from(RentalDecisionLog)
        .outerjoin(Item, Item.owner_user_id == RentalDecisionLog.owner_user_id)
        .group_by(RentalDecisionLog.owner_user_id)
        .order_by(RentalDecisionLog.owner_user_id.asc())
    )
    out: list[AdminLogOwnerRow] = []
    for uid, uname in q.all():
        out.append(
            AdminLogOwnerRow(
                user_id=int(uid),
                username=str(uname or "").strip().lstrip("@"),
            )
        )
    return out


async def items_with_logs_for_admin(session: AsyncSession, admin_user_id: int) -> list[AdminLogItemRow]:
    q = await session.execute(
        select(
            RentalDecisionLog.item_id,
            func.coalesce(Item.name, "Удалённая вещь"),
            func.count(),
        )
        .select_from(RentalDecisionLog)
        .outerjoin(Item, Item.id == RentalDecisionLog.item_id)
        .where(RentalDecisionLog.owner_user_id == int(admin_user_id))
        .group_by(RentalDecisionLog.item_id, Item.name)
        .order_by(func.count().desc(), RentalDecisionLog.item_id.asc())
    )
    out: list[AdminLogItemRow] = []
    for item_id, item_name, count_rows in q.all():
        out.append(
            AdminLogItemRow(
                item_id=int(item_id) if item_id is not None else None,
                item_name=str(item_name or "Удалённая вещь"),
                events_count=int(count_rows or 0),
            )
        )
    return out


async def latest_logs_for_admin_item(
    session: AsyncSession, *, admin_user_id: int, item_id: int | None, limit: int = 60
) -> list[RentalDecisionLog]:
    q = select(RentalDecisionLog).where(RentalDecisionLog.owner_user_id == int(admin_user_id))
    if item_id is None:
        q = q.where(RentalDecisionLog.item_id.is_(None))
    else:
        q = q.where(RentalDecisionLog.item_id == int(item_id))
    q = q.order_by(RentalDecisionLog.created_at.desc(), RentalDecisionLog.id.desc()).limit(int(limit))
    r = await session.execute(q)
    return list(r.scalars().unique())
