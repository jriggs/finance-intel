"""
Web Search + Finance Data.

  - News queries   → Google News RSS (deduplicated headlines)
  - Finance queries → yfinance (live prices) + RSS news, fetched in parallel
  - Weather queries → wttr.in JSON API
  - Time queries    → wttr.in (local time only)
  - General queries → DuckDuckGo web search

All network I/O is async; the sync libraries (yfinance, DDGS) run in a thread
pool so they never block the event loop. No API key required.

Formerly a single ~680-line ``search.py``; now split into focused modules
(classifiers, parsing, providers, client, engine) with the full public surface
re-exported here so ``from search import needs_search`` keeps working.
"""
from __future__ import annotations

from .classifiers import (  # noqa: F401
    _is_finance_query,
    _is_news_query,
    _is_time_query,
    _is_weather_query,
    _words,
    extract_urls,
    needs_search,
)
from .client import (  # noqa: F401
    _executor,
    _get_http_client,
    close_http_client,
    shutdown_executor,
)
from .parsing import (  # noqa: F401
    _deduplicate,
    _format_articles,
    _parse_requested_count,
    _parse_rss,
)
from .providers import (  # noqa: F401
    HAS_DDG,
    HAS_YF,
    _ddg_search,
    _extract_location,
    _fetch_ticker,
    _finance_search,
    _news_rss,
    _weather_search,
)
from .engine import (  # noqa: F401
    _SEARCH_ROUTES,
    _handle_finance,
    _handle_news,
    _handle_time,
    _handle_weather,
    search,
    search_sync,
    search_with_urls,
)

__all__ = [
    "search",
    "search_sync",
    "search_with_urls",
    "needs_search",
    "extract_urls",
    "close_http_client",
    "shutdown_executor",
]
