"""
Market data layer — yfinance for price history, fundamentals, and news.

All functions are synchronous; call via ``asyncio.to_thread()`` from async
handlers. Formerly a single ~770-line ``market.py``; now split into shared
infrastructure (:mod:`core`) and the data :mod:`accessors`, with the full public
surface re-exported here so ``from market import get_stock_info`` and
``market.get_price_history`` keep working unchanged.
"""
from __future__ import annotations

from .core import (  # noqa: F401
    _not_found_load,
    _yf_check,
    _yf_is_blocked,
    _yf_trip_breaker,
    build_yf_session,
    bust_symbol_cache,
    clear_not_found_blocklist,
    get_yf_status,
    is_not_found_blocked,
    logger,
)
from .accessors import (  # noqa: F401
    get_batch_quotes,
    get_chart_data,
    get_fast_market_cap,
    get_news,
    get_price_history,
    get_sector_peers,
    get_stock_info,
)

__all__ = [
    "get_stock_info",
    "get_price_history",
    "get_chart_data",
    "get_fast_market_cap",
    "get_news",
    "get_sector_peers",
    "get_batch_quotes",
    "build_yf_session",
    "bust_symbol_cache",
    "get_yf_status",
    "clear_not_found_blocklist",
    "is_not_found_blocked",
]
