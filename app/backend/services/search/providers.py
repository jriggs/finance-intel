"""Data-source providers: Google News RSS, wttr.in weather, yfinance finance,
and DuckDuckGo web search. Sync libraries run in the shared thread pool."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote_plus

import httpx

try:
    from ddgs import DDGS
    HAS_DDG = True
except ImportError:
    HAS_DDG = False
    print("⚠️  ddgs not installed — run: pip install ddgs")

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False
    print("⚠️  yfinance not installed — run: pip install yfinance")

from .classifiers import _NAME_TO_TICKER, _TIME_IN_LOCATION_RE
from .client import _executor
from .parsing import _deduplicate, _format_articles, _parse_rss

# ── News via Google News RSS (async) ──────────────────────────────────────────

async def _news_rss(
    client: httpx.AsyncClient,
    query: str,
    max_results: int = 8,
    desc_cap: int = 200,
) -> tuple[str, list[str]]:
    """Fetch and parse Google News RSS for `query`. Restricts to past 24 h.
    Returns (formatted_text, list_of_article_urls)."""
    q_dated = query + " when:1d"
    url = (
        f"https://news.google.com/rss/search"
        f"?q={quote_plus(q_dated)}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        articles = _parse_rss(resp.text, max_results)
        if not articles:
            return "", []
        print(f"[Search] {len(articles)} unique news headlines for {query!r}")
        urls = [a["url"] for a in articles if a.get("url")]
        return _format_articles(articles, desc_cap=desc_cap), urls
    except Exception as e:
        print(f"[Search] RSS error: {type(e).__name__}: {e}")
        return "", []


# ── Weather via wttr.in (async) ───────────────────────────────────────────────

_WEATHER_STRIP = re.compile(
    r"\b(weather|forecast|temperature|today|tonight|tomorrow|this week|"
    r"time|current|local|right now|currently|"
    r"in|for|at|the|what|is|it|how|will|be|like|o'clock)\b",
    re.IGNORECASE,
)


def _extract_location(query: str) -> str:
    """Isolate the location from a weather or time query.

    Tries to pull the location directly from the _TIME_IN_LOCATION_RE capture
    group first (e.g. "what time is it in Cincinnati" → "Cincinnati").
    Falls back to stripping common weather/time words if no regex match.
    """
    m = _TIME_IN_LOCATION_RE.search(query)
    if m:
        # Only one of the three named groups will be non-None
        loc = m.group("loc") or m.group("loc2") or m.group("loc3")
        if loc:
            return loc.strip(" ?.,!")
    loc = _WEATHER_STRIP.sub(" ", query)
    return re.sub(r"\s{2,}", " ", loc).strip(" ?,.") or query


async def _weather_search(client: httpx.AsyncClient, query: str) -> str:
    """Fetch current conditions + today/tomorrow forecast from wttr.in."""
    location = _extract_location(query)
    url = f"https://wttr.in/{quote_plus(location)}?format=j1"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

        current  = data["current_condition"][0]
        area     = data["nearest_area"][0]
        today    = data["weather"][0]
        tomorrow = data["weather"][1] if len(data["weather"]) > 1 else None

        city    = area["areaName"][0]["value"]
        country = area["country"][0]["value"]
        region  = area.get("region", [{}])[0].get("value", "")
        place   = f"{city}, {region}, {country}".strip(", ")

        local_time = current.get("localObsDateTime", "")
        lines = [
            f"Weather for {place}:",
            *([ f"  Local time: {local_time}"] if local_time else []),
            f"  Now: {current['temp_F']}°F (feels like {current['FeelsLikeF']}°F),"
            f" {current['weatherDesc'][0]['value']}",
            f"  Humidity: {current['humidity']}%  |"
            f"  Wind: {current['windspeedMiles']} mph {current['winddir16Point']}  |"
            f"  Visibility: {current['visibility']} mi",
            f"  Today: High {today['maxtempF']}°F / Low {today['mintempF']}°F  |"
            f"  Sunrise {today['astronomy'][0]['sunrise']}"
            f" · Sunset {today['astronomy'][0]['sunset']}",
        ]
        if today.get("hourly"):
            lines.append(f"  Conditions: {today['hourly'][4]['weatherDesc'][0]['value']}")

        if tomorrow:
            t_desc = (
                tomorrow["hourly"][4]["weatherDesc"][0]["value"]
                if tomorrow.get("hourly") else ""
            )
            line = f"  Tomorrow: High {tomorrow['maxtempF']}°F / Low {tomorrow['mintempF']}°F"
            if t_desc:
                line += f", {t_desc}"
            lines.append(line)

        print(f"[Search] Weather fetched for '{location}'")
        return "\n".join(lines)
    except Exception as e:
        print(f"[Search] Weather error for '{location}': {type(e).__name__}: {e}")
        return ""


# ── Finance via yfinance (parallel async) ─────────────────────────────────────

def _fetch_ticker_sync(sym: str) -> dict | None:
    """
    Fetch price data for a single ticker symbol.
    Runs in a thread pool — yfinance is a sync library.
    """
    try:
        t     = yf.Ticker(sym)
        fast  = t.fast_info
        price = getattr(fast, "last_price", None)
        prev  = getattr(fast, "previous_close", None)
        # fast_info also exposes a display name — avoid a second t.info HTTP round trip
        name  = getattr(fast, "display_name", None) or sym
        return {"sym": sym, "name": name, "price": price, "prev": prev}
    except Exception as e:
        print(f"[Finance] {sym}: {type(e).__name__}: {e}")
        return None


async def _fetch_ticker(sym: str) -> dict | None:
    """Async wrapper — runs _fetch_ticker_sync in the thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _fetch_ticker_sync, sym)


async def _finance_search(query: str) -> str:
    """
    Fetch live price data for all identified tickers in parallel.
    Returns a formatted string with current prices and % change.
    """
    if not HAS_YF:
        return ""

    q_lower = query.lower()

    candidates: list[str] = []
    for name, sym in _NAME_TO_TICKER.items():
        if name in q_lower:
            candidates.append(sym)
    for m in re.finditer(r"\$([A-Z]{1,5})\b", query):
        sym = m.group(1)
        if sym not in candidates:
            candidates.append(sym)
    if not candidates:
        candidates = ["^GSPC", "^DJI", "^IXIC", "BTC-USD", "GC=F"]

    # Deduplicate while preserving order
    seen: set[str] = set()
    tickers: list[str] = []
    for t in candidates:
        if t not in seen:
            seen.add(t)
            tickers.append(t)
    tickers = tickers[:8]

    # Fetch all tickers concurrently
    results = await asyncio.gather(*(_fetch_ticker(sym) for sym in tickers))

    lines = []
    for r in results:
        if r is None:
            continue
        price, prev, sym, name = r["price"], r["prev"], r["sym"], r["name"]
        if price is not None and prev:
            change = price - prev
            pct    = (change / prev) * 100
            arrow  = "▲" if change >= 0 else "▼"
            lines.append(
                f"{name} ({sym}): ${price:,.2f}  "
                f"{arrow} {abs(change):.2f} ({abs(pct):.2f}%)"
            )
        elif price is not None:
            lines.append(f"{name} ({sym}): ${price:,.2f}")

    if not lines:
        return ""
    print(f"[Search] {len(lines)} finance quote(s) fetched (parallel)")
    return "Live market data (real-time):\n" + "\n".join(lines)


# ── DDG general web search (sync → thread pool) ───────────────────────────────

def _ddg_search_sync(query: str, max_results: int) -> tuple[str, list[str]]:
    if not HAS_DDG:
        return "", []
    try:
        raw    = list(DDGS(timeout=10).text(query, max_results=max_results + 5))
        unique = _deduplicate(
            [{"title": r.get("title", ""), "url": r.get("href", ""), "body": r.get("body", "")}
             for r in raw],
            title_key="title", url_key="url",
        )[:max_results]
        if not unique:
            print("[Search] DDG returned no results")
            return "", []
        lines = [
            f"[{i}] {r['title']}\nURL: {r['url']}\n{r['body']}"
            for i, r in enumerate(unique, 1)
        ]
        urls = [r["url"] for r in unique if r.get("url")]
        print(f"[Search] {len(unique)} unique DDG result(s) (from {len(raw)} raw)")
        return "\n\n".join(lines), urls
    except Exception as e:
        print(f"[Search] DDG error: {type(e).__name__}: {e}")
        return "", []


async def _ddg_search(query: str, max_results: int) -> tuple[str, list[str]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _ddg_search_sync, query, max_results)


