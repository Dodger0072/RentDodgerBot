from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from aiogram.client.session.aiohttp import AiohttpSession
from python_socks import ProxyType

logger = logging.getLogger(__name__)


class TelProxySession(AiohttpSession):
    """SOCKS-прокси с настраиваемым rdns (у aiogram по умолчанию rdns=True — часто ломает TLS)."""

    def __init__(
        self,
        proxy: str,
        *,
        socks_rdns: bool = True,
        limit: int = 100,
        timeout: float = 90.0,
        **kwargs: Any,
    ) -> None:
        self._socks_rdns = socks_rdns
        super().__init__(proxy=proxy, limit=limit, timeout=timeout, **kwargs)

    def _setup_proxy_connector(self, proxy: Any) -> None:
        from aiohttp_socks import ProxyConnector
        from aiohttp_socks.utils import parse_proxy_url

        if isinstance(proxy, str):
            proxy_type, host, port, username, password = parse_proxy_url(proxy)
            if proxy_type in (ProxyType.SOCKS4, ProxyType.SOCKS5):
                self._connector_type = ProxyConnector
                self._connector_init = {
                    "proxy_type": proxy_type,
                    "host": host,
                    "port": port,
                    "username": username,
                    "password": password,
                    "rdns": self._socks_rdns,
                }
                self._proxy = proxy
                return
        super()._setup_proxy_connector(proxy)


def build_telegram_session(
    proxy_url: str | None,
    *,
    socks_rdns: bool = True,
    request_timeout: float = 90.0,
) -> AiohttpSession | None:
    if not proxy_url:
        return None
    u = proxy_url.strip()
    if u.lower().startswith("socks5h://"):
        u = "socks5://" + u.split("://", 1)[1]

    if u.lower().startswith("socks5://") or u.lower().startswith("socks4://"):
        logger.info("Telegram: SOCKS-прокси, rdns=%s, timeout=%ss", socks_rdns, request_timeout)
        return TelProxySession(
            u,
            socks_rdns=socks_rdns,
            timeout=request_timeout,
        )

    logger.info("Telegram: HTTP(S)-прокси, timeout=%ss", request_timeout)
    return AiohttpSession(proxy=u, timeout=request_timeout)


def proxy_line_to_url(line: str, scheme: str = "socks5") -> str:
    parts = line.strip().split(":", 3)
    if len(parts) != 4:
        raise ValueError("TELEGRAM_PROXY_LINE must be host:port:user:pass (exactly 4 segments)")
    host, port, user, password = parts
    if not host or not port:
        raise ValueError("Invalid host or port in TELEGRAM_PROXY_LINE")
    u = quote(user, safe="")
    p = quote(password, safe="")
    s = scheme.lower().rstrip(":/")
    if s == "socks5h":
        s = "socks5"
    if s not in ("socks5", "http", "https"):
        raise ValueError("TELEGRAM_PROXY_SCHEME must be socks5, socks5h, http, or https")
    return f"{s}://{u}:{p}@{host}:{port}"
