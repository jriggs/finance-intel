"""Shared HTTP client and thread pool for the search providers, with the
lifecycle helpers to drain them on app shutdown."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx

# Thread pool only for sync libraries (yfinance, DDGS) — HTTP I/O is async.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="search")

_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}
_HTTP_TIMEOUT = 10.0

# Persistent client — reuses TCP/TLS connections across calls instead of
# paying the handshake cost on every search request.
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            headers=_HTTP_HEADERS,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
        )
    return _http_client


async def close_http_client() -> None:
    """Close the persistent HTTP client. Call on app shutdown."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


def shutdown_executor() -> None:
    """Drain the thread-pool executor. Call on app shutdown."""
    _executor.shutdown(wait=True)
