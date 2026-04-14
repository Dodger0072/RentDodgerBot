from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Item
from bot.item_categories import ITEM_CATEGORIES, UNCATEGORIZED_SLUG, item_category_label
from bot.services.item_owner import admin_manages_item


def _group_clause(*, is_paid: bool, item_category: str | None):
    parts = [Item.is_paid.is_(is_paid)]
    if item_category is None or (isinstance(item_category, str) and item_category.strip() == ""):
        parts.append(or_(Item.item_category.is_(None), Item.item_category == ""))
    else:
        parts.append(Item.item_category == item_category.strip())
    return and_(*parts)


def _uncategorized_db_key(k: object) -> bool:
    return k is None or (isinstance(k, str) and not str(k).strip())


async def non_empty_rental_category_menu_rows(
    session: AsyncSession, *, is_paid: bool
) -> list[tuple[str, str]]:
    """Пары (slug для callback u:grp, подпись кнопки) — только категории, где есть вещи."""
    r = await session.execute(
        select(Item.item_category, func.count(Item.id))
        .where(Item.is_paid.is_(is_paid), Item.is_visible.is_(True))
        .group_by(Item.item_category)
    )
    counts: dict[object, int] = {}
    for cat, cnt in r.all():
        counts[cat] = int(cnt)

    out: list[tuple[str, str]] = []
    known_slugs = {s for s, _ in ITEM_CATEGORIES}
    for slug, label in ITEM_CATEGORIES:
        if counts.get(slug, 0) > 0:
            out.append((slug, label))

    uncat = sum(cnt for k, cnt in counts.items() if _uncategorized_db_key(k))
    if uncat > 0:
        out.append((UNCATEGORIZED_SLUG, "Без категории"))

    extra: list[tuple[str, str]] = []
    for k, cnt in counts.items():
        if cnt <= 0 or _uncategorized_db_key(k):
            continue
        sk = str(k).strip()
        if sk in known_slugs:
            continue
        if len(sk) > 48 or ":" in sk:
            continue
        extra.append((sk, item_category_label(sk)))
    extra.sort(key=lambda x: x[1].casefold())
    out.extend(extra)
    return out


async def next_display_order_for_group(
    session: AsyncSession, *, is_paid: bool, item_category: str | None
) -> int:
    """Следующий шаг сортировки в группе (платная/бесплатная + категория)."""
    r = await session.execute(
        select(func.coalesce(func.max(Item.display_order), 0)).where(
            _group_clause(is_paid=is_paid, item_category=item_category)
        )
    )
    mx = r.scalar_one()
    return int(mx) + 10


async def reorder_item_to_position(
    session: AsyncSession,
    *,
    item_id: int,
    position_1based: int,
    acting_user_id: int,
) -> tuple[bool, str]:
    """
    Позиция — среди вещей с тем же is_paid и item_category (1 = первая в списке у пользователя).
    """
    r = await session.execute(select(Item).where(Item.id == item_id))
    item = r.scalar_one_or_none()
    if item is None:
        return False, "Вещь не найдена."
    if not admin_manages_item(acting_user_id, item):
        return False, "У вас нет прав менять порядок этой вещи."

    r2 = await session.execute(
        select(Item)
        .where(_group_clause(is_paid=item.is_paid, item_category=item.item_category))
        .order_by(Item.display_order.asc(), Item.id.asc())
    )
    siblings = list(r2.scalars().all())
    if not siblings:
        return False, "Группа пуста."
    target = next((x for x in siblings if x.id == item_id), None)
    if target is None:
        return False, "Вещь не в группе."

    others = [x for x in siblings if x.id != item_id]
    pos0 = max(0, min(position_1based - 1, len(siblings) - 1))
    new_list = others[:pos0] + [target] + others[pos0:]
    for i, it in enumerate(new_list):
        it.display_order = (i + 1) * 10
    await session.flush()
    return True, (
        f"Готово. В каталоге для пользователя эта вещь теперь на месте <b>{pos0 + 1}</b> "
        f"из {len(new_list)} (та же платность и категория)."
    )
