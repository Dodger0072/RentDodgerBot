from __future__ import annotations

# slug хранится в Item.item_category; порядок — как в интерфейсе.
ITEM_CATEGORIES: list[tuple[str, str]] = [
    ("transport", "Транспорт"),
    ("lodging", "Подселение"),
    ("accessories", "Аксессуары"),
    ("skins", "Скины"),
    ("guards", "Охранники"),
    ("work_sets", "Сета для работ"),
]

ITEM_CATEGORY_SLUGS: frozenset[str] = frozenset(s for s, _ in ITEM_CATEGORIES)
UNCATEGORIZED_SLUG = "other"


def item_category_label(slug: str | None) -> str:
    if slug is None or (isinstance(slug, str) and not slug.strip()):
        return "Без категории"
    s = slug.strip()
    for key, lab in ITEM_CATEGORIES:
        if key == s:
            return lab
    return s
