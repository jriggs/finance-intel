"""Search orchestration: per-source async route handlers, the OCP route table,
and the public search_with_urls / search / search_sync entry points."""
from __future__ import annotations

import asyncio
from urllib.parse import quote_plus

import httpx

from .classifiers import (
    _is_finance_query,
    _is_news_query,
    _is_time_query,
    _is_weather_query,
)
from .client import _get_http_client
from .parsing import _parse_requested_count
from .providers import (
    _ddg_search,
    _extract_location,
    _finance_search,
    _news_rss,
    _weather_search,
)

# ── Route handlers (all async) ────────────────────────────────────────────────

async def _handle_time(
    client: httpx.AsyncClient, query: str, max_results: int
) -> tuple[str, list[str]]:
    """Return only the local time for a location — no weather data."""
    location = _extract_location(query)
    url = f"https://wttr.in/{quote_plus(location)}?format=j1"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        current     = data["current_condition"][0]
        area        = data["nearest_area"][0]
        city        = area["areaName"][0]["value"]
        region      = area.get("region", [{}])[0].get("value", "")
        country     = area["country"][0]["value"]
        place       = ", ".join(p for p in [city, region, country] if p)
        local_time  = current.get("localObsDateTime", "")
        if local_time:
            print(f"[Search] Local time for '{location}': {local_time}")
            return f"The current local time in {place} is: {local_time}", []
        return "", []
    except Exception as e:
        print(f"[Search] Time lookup error for '{location}': {type(e).__name__}: {e}")
        return "", []


async def _handle_weather(
    client: httpx.AsyncClient, query: str, max_results: int
) -> tuple[str, list[str]]:
    return await _weather_search(client, query), []


async def _handle_news(
    client: httpx.AsyncClient, query: str, max_results: int
) -> tuple[str, list[str]]:
    count = _parse_requested_count(query, default=max_results)
    # Fewer articles → richer snippets; many articles → shorter to keep context lean
    desc_cap = 300 if count <= 5 else 150 if count <= 15 else 80
    return await _news_rss(client, query, max_results=count, desc_cap=desc_cap)


async def _handle_finance(
    client: httpx.AsyncClient, query: str, max_results: int
) -> tuple[str, list[str]]:
    """Fetch live prices and related news simultaneously."""
    finance_text, (news_text, news_urls) = await asyncio.gather(
        _finance_search(query),
        _news_rss(client, query + " finance", max_results=4),
    )
    text = "\n\n".join(p for p in [finance_text, news_text] if p)
    return text, news_urls


# Route table — (predicate, async_handler, fallback_label).
# Time before weather: "what time is it in Paris" should return the time,
# not a weather report. Weather before news so "weather today" skips RSS.
# Add new sources here only (OCP).
_SEARCH_ROUTES: list[tuple] = [
    (_is_time_query,    _handle_time,    "Time lookup failed"),
    (_is_weather_query, _handle_weather, "Weather failed"),
    (_is_finance_query, _handle_finance, "Finance/news failed"),
    (_is_news_query,    _handle_news,    "RSS failed"),
]


# ── Public API ─────────────────────────────────────────────────────────────────

async def search_with_urls(query: str, max_results: int = 10) -> tuple[str, list[str]]:
    """
    Async entry point. Returns (formatted_results, source_urls).
    source_urls can be used by the caller to index content into the knowledge base.
    All I/O runs concurrently:
    - HTTP fetches (RSS, weather) use a shared async client
    - yfinance / DDGS (sync) run in the thread pool
    """
    print(f"[Search] Query: {query!r}")

    client = _get_http_client()
    for predicate, handler, fallback_label in _SEARCH_ROUTES:
        if predicate(query):
            text, urls = await handler(client, query, max_results)
            if text:
                return text, urls
            print(f"[Search] {fallback_label}, falling back to DDG")
            break

    return await _ddg_search(query, max_results)


async def search(query: str, max_results: int = 10) -> str:
    """Backward-compatible wrapper — returns only the formatted text."""
    text, _ = await search_with_urls(query, max_results)
    return text


def search_sync(query: str, max_results: int = 10) -> str:
    """Synchronous wrapper for use outside an async context (e.g. scripts/tests)."""
    return asyncio.run(search(query, max_results))
