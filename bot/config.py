from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from bot.telegram_session import proxy_line_to_url

load_dotenv()


def _parse_int_list(raw: str | None) -> set[int]:
    if not raw or not raw.strip():
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def _parse_username_list(raw: str | None) -> set[str]:
    if not raw or not raw.strip():
        return set()
    return {p.strip().lstrip("@").lower() for p in raw.split(",") if p.strip()}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    low = raw.strip().lower()
    if low in ("0", "false", "no", "off"):
        return False
    if low in ("1", "true", "yes", "on"):
        return True
    return default


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_user_ids: set[int]
    admin_usernames: set[str]
    superadmin_user_ids: set[int]
    database_url: str
    display_tz: ZoneInfo
    time_zone_label: str
    telegram_proxy: str | None = None
    telegram_socks_rdns: bool = True
    telegram_request_timeout: float = 90.0


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is required")

    tz_name = os.getenv("DISPLAY_TZ", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise RuntimeError(f"Invalid DISPLAY_TZ: {tz_name}") from exc

    db = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./rent_bot.db").strip()
    if not db:
        db = "sqlite+aiosqlite:///./rent_bot.db"

    proxy = os.getenv("TELEGRAM_PROXY", "").strip() or None
    line = os.getenv("TELEGRAM_PROXY_LINE", "").strip()
    if line:
        scheme = os.getenv("TELEGRAM_PROXY_SCHEME", "socks5").strip() or "socks5"
        try:
            proxy = proxy_line_to_url(line, scheme=scheme)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    timeout_raw = os.getenv("TELEGRAM_REQUEST_TIMEOUT", "").strip()
    req_timeout = 90.0
    if timeout_raw:
        try:
            req_timeout = float(timeout_raw.replace(",", "."))
        except ValueError:
            raise RuntimeError("TELEGRAM_REQUEST_TIMEOUT must be a number (seconds)") from None
        if req_timeout < 10:
            req_timeout = 10.0

    socks_rdns = _env_bool("TELEGRAM_SOCKS_RDNS", True)

    tz_label = os.getenv("TIME_ZONE_LABEL", "МСК").strip()

    return Settings(
        bot_token=token,
        admin_user_ids=_parse_int_list(os.getenv("ADMIN_USER_IDS")),
        admin_usernames=_parse_username_list(os.getenv("ADMIN_USERNAMES")),
        superadmin_user_ids=_parse_int_list(os.getenv("SUPERADMIN_USER_IDS")),
        database_url=db,
        display_tz=tz,
        time_zone_label=tz_label,
        telegram_proxy=proxy,
        telegram_socks_rdns=socks_rdns,
        telegram_request_timeout=req_timeout,
    )


def is_admin(user_id: int, username: str | None, settings: Settings) -> bool:
    if user_id in settings.admin_user_ids:
        return True
    if username:
        un = username.lstrip("@").lower()
        if un in settings.admin_usernames:
            return True
    return False


def is_superadmin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.superadmin_user_ids


def superadmin_roles_enabled(settings: Settings) -> bool:
    """В .env задан хотя бы один SUPERADMIN_USER_IDS — включены отдельные права суперадмина."""
    return bool(settings.superadmin_user_ids)


def can_ban_via_bot_commands(user_id: int, settings: Settings) -> bool:
    """Бан/разбан командами: любой админ, если суперадмины не заданы; иначе только суперадмин."""
    if not superadmin_roles_enabled(settings):
        return True
    return is_superadmin(user_id, settings)


def can_autoban_from_warnings(issuer_user_id: int, settings: Settings) -> bool:
    """Автобан при 3-м предупреждении только от суперадмина, если роли включены."""
    if not superadmin_roles_enabled(settings):
        return True
    return is_superadmin(issuer_user_id, settings)
