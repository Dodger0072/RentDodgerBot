from __future__ import annotations

from datetime import UTC, datetime

from bot.config import Settings


def format_local_time(dt: datetime, settings: Settings) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    body = dt.astimezone(settings.display_tz).strftime("%d.%m.%Y %H:%M")
    lab = settings.time_zone_label.strip()
    if lab:
        return f"{body} {lab}"
    return body
