"""
Web Search + Finance Data
  - News queries   → Google News RSS (actual headlines, deduplicated)
  - Finance queries → yfinance (live prices) + RSS news, fetched in parallel
  - Weather queries → wttr.in JSON API
  - General queries → DuckDuckGo web search

All network I/O is async. yfinance and DDGS (sync libraries) run in a thread
pool so they never block the event loop. Multiple finance tickers are fetched
concurrently.

No API key required for any source.
"""

import asyncio
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
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

# Thread pool only for sync libraries (yfinance, DDGS) — HTTP I/O is now async
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


# ── Real-time necessity detection ─────────────────────────────────────────────

_REALTIME_SIGNALS = {
    "today", "tonight", "yesterday",
    "right now", "at the moment",
    "this week", "this month", "this year",
    "latest", "recent", "recently", "breaking", "headlines", "news",
    "stock price", "share price",
    "bitcoin", "ethereum", "btc", "eth",
    "nasdaq", "dow jones", "s&p 500", "sp500",
    "earnings report", "ipo",
    "weather", "forecast",
    "standings", "who won", "who is winning", "game score",
    "search the web", "browse the web", "look it up online",
}

_REALTIME_WORDS = {
    "today", "tonight", "yesterday", "headlines", "news",
    "bitcoin", "ethereum", "btc", "eth", "nasdaq", "ipo",
    "weather", "forecast", "standings",
}

# Pre-compiled — avoids repeated compilation on every needs_search() call
_REALTIME_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b202[4-9]\b"),
    re.compile(r"\bhow much (does|is|are)\b"),
    re.compile(r"\bwhat (is|are) the (current |latest |live )?(price|cost|rate|value)\b"),
    re.compile(r"\b(current|live|real.?time) (price|rate|data|score|standings)\b"),
    re.compile(r"\bwho (is|are) (the )?(current(ly)?\s+|now\s+)?(president|ceo|winner|champion)\b"),
    re.compile(r"\bwhat happened (today|yesterday|this week|recently)\b"),
]

# Time/date queries — excluded from needs_search() because the current UTC time
# is already injected into every system prompt. The model can compute any local
# time directly from that context without a web search.
_TIME_QUERY_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bwhat\s+time\s+is\s+it\b", re.IGNORECASE),
    re.compile(r"\btime\s+in\s+\w", re.IGNORECASE),
    re.compile(r"\bcurrent\s+time\s+in\b", re.IGNORECASE),
    re.compile(r"\btime\s+zone\b|\btimezone\b", re.IGNORECASE),
    re.compile(r"\bwhat('?s| is) the time\b", re.IGNORECASE),
    re.compile(r"\bwhat (day|date) is (it|today)\b", re.IGNORECASE),
]

_COMMON_TLDS = (
    "com|net|org|io|co|dev|app|ai|me|us|uk|edu|gov|ca|au|de|fr|jp|ru|"
    "info|biz|tv|fm|gg|xyz|tech|site|online|store|shop|blog|news|media"
)
_URL_RE = re.compile(
    r"https?:/{0,2}[^\s>\"')\]]+"
    r"|www\.[^\s>\"')\]]+"
    r"|\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)"
    r"+(?:" + _COMMON_TLDS + r")\b(?:/[^\s>\"')\]]*)?",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    """Return all URLs found in text."""
    return _URL_RE.findall(text)


def needs_search(query: str) -> bool:
    """
    Return True only when the query genuinely requires real-time / live data.
    Never triggers when a URL is present — the caller should fetch it directly.

    Time/date queries are intentionally excluded: the current UTC time is already
    injected into every system prompt, so the model can answer 'what time is it
    in Tokyo?' directly from that context without any web search.
    """
    if _URL_RE.search(query):
        return False

    # Time/date: model already knows UTC time — no search needed
    for pattern in _TIME_QUERY_PATTERNS:
        if pattern.search(query):
            return False

    q = query.lower()
    words = set(re.findall(r"\w+", q))

    if words & _REALTIME_WORDS:
        return True

    for phrase in _REALTIME_SIGNALS:
        if " " in phrase and phrase in q:
            return True

    for pattern in _REALTIME_PATTERNS:
        if pattern.search(q):
            return True

    # Bare ticker symbols like $AAPL or "TSLA stock"
    if re.search(r"\$[A-Z]{1,5}\b", query):
        return True
    return bool(re.search(r"\b[A-Z]{2,5}\s+stock\b", query))


# ── Routing classifiers ────────────────────────────────────────────────────────

_WEATHER_TERMS = {
    "weather", "forecast", "temperature", "humidity", "rain", "snow",
    "sunny", "cloudy", "storm", "wind", "windy", "hot", "cold",
    "raining", "snowing", "thunder", "lightning", "hail", "fog", "foggy",
    "drizzle", "overcast", "partly cloudy",
}

# Patterns that route to the weather/time handler (wttr.in returns local time too)
_TIME_IN_LOCATION_RE = re.compile(
    r"\btime\s+in\s+(?P<loc>.+)|"
    r"\bcurrent\s+time\s+in\s+(?P<loc2>.+)|"
    r"\bwhat\s+time\s+is\s+it\s+in\s+(?P<loc3>.+)",
    re.IGNORECASE,
)

_NEWS_TERMS = {
    "news", "headlines", "happening", "today", "latest", "breaking",
    "current", "update", "updates", "recent", "now",
}

_FINANCE_TERMS = {
    "stock", "stocks", "share", "shares",
    "nasdaq", "nyse", "s&p", "sp500",
    "crypto", "bitcoin", "ethereum", "btc", "eth",
    "etf", "invest", "investing", "portfolio",
    "earnings", "dividend", "ipo", "futures",
    "stonks", "bullish", "bearish", "ticker",
}

_FINANCE_SIGNALS = {
    "stock", "stocks", "share", "shares", "price", "prices",
    "market", "trading", "invest", "investing", "etf", "dividend",
    "earnings", "portfolio", "crypto", "coin",
}

_FINANCE_NAMES_UNAMBIGUOUS = {
    "bitcoin": "BTC-USD", "ethereum": "ETH-USD", "btc": "BTC-USD",
    "eth": "ETH-USD", "solana": "SOL-USD", "dogecoin": "DOGE-USD",
    "ripple": "XRP-USD", "coinbase": "COIN", "robinhood": "HOOD",
    "s&p 500": "^GSPC", "sp500": "^GSPC",
    "dow jones": "^DJI", "nasdaq": "^IXIC",
}

_FINANCE_NAMES_AMBIGUOUS = {
    "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL",
    "alphabet": "GOOGL", "amazon": "AMZN", "tesla": "TSLA",
    "nvidia": "NVDA", "meta": "META", "netflix": "NFLX",
    "intel": "INTC", "amd": "AMD", "palantir": "PLTR",
    "shopify": "SHOP", "spotify": "SPOT", "uber": "UBER",
    "lyft": "LYFT", "airbnb": "ABNB",
    "gold": "GC=F", "silver": "SI=F", "oil": "CL=F",
    "dow": "^DJI", "s&p": "^GSPC",
}

_NAME_TO_TICKER = {**_FINANCE_NAMES_UNAMBIGUOUS, **_FINANCE_NAMES_AMBIGUOUS}


def _words(text: str) -> set:
    return set(re.findall(r"\w+", text.lower()))


def _is_time_query(query: str) -> bool:
    """'What time is it in Tokyo?' — location required, no weather terms."""
    return bool(_TIME_IN_LOCATION_RE.search(query)) and not bool(_words(query) & _WEATHER_TERMS)


def _is_weather_query(query: str) -> bool:
    return bool(_words(query) & _WEATHER_TERMS) or bool(_TIME_IN_LOCATION_RE.search(query))


def _is_news_query(query: str) -> bool:
    if _is_weather_query(query):
        return False
    return bool(_words(query) & _NEWS_TERMS)


def _is_finance_query(query: str) -> bool:
    q = query.lower()
    w = _words(q)

    if w & _FINANCE_TERMS:
        return True

    for name in _FINANCE_NAMES_UNAMBIGUOUS:
        if name in q:
            return True

    if w & _FINANCE_SIGNALS:
        for name in _FINANCE_NAMES_AMBIGUOUS:
            if name in q:
                return True

    return bool(re.search(r"\$[A-Z]{1,5}\b", query))


# ── Deduplication ──────────────────────────────────────────────────────────────

_STOPWORDS = {"the", "a", "an", "in", "of", "to", "and", "for",
              "is", "are", "on", "at", "by", "with", "from"}


def _deduplicate(items: list[dict], title_key: str, url_key: str) -> list[dict]:
    """Remove items that share a URL or have >70% title word overlap."""
    seen_urls: set[str] = set()
    seen_title_word_sets: list[set] = []
    unique = []

    for item in items:
        url        = item.get(url_key, "")
        raw_title  = item.get(title_key, "").lower()
        title_words = {w for w in re.findall(r"\w+", raw_title) if w not in _STOPWORDS}

        if url and url in seen_urls:
            continue

        too_similar = any(
            prev and title_words and
            len(title_words & prev) / min(len(title_words), len(prev)) > 0.7
            for prev in seen_title_word_sets
        )
        if too_similar:
            continue

        if url:
            seen_urls.add(url)
        seen_title_word_sets.append(title_words)
        unique.append(item)

    return unique


# ── RSS parsing (pure, no I/O) ─────────────────────────────────────────────────

def _parse_rss(xml_text: str, max_results: int) -> list[dict]:
    """Parse Google News RSS XML into a list of article dicts."""
    root = ET.fromstring(xml_text)
    parsed = []
    for item in root.findall(".//item"):
        title  = (item.findtext("title") or "").strip()
        link   = (item.findtext("link")  or "").strip()
        desc   = re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()
        pub    = (item.findtext("pubDate") or "").strip()
        source = ""
        src_el = item.find("source")
        if src_el is not None:
            source = src_el.text or ""
        parsed.append({"title": title, "url": link, "desc": desc,
                        "pub": pub, "source": source})
    return _deduplicate(parsed, title_key="title", url_key="url")[:max_results]


# Matches "20 articles", "top 5 headlines", "give me 10 stories", etc.
_COUNT_RE = re.compile(
    r"(?:top|latest|give\s+me|show\s+me|find|get|fetch|list)?\s*(\d+)\s*"
    r"(?:news|stories|story|articles?|headlines?|results?|updates?)\b"
    r"|\b(a\s+few|several|some)\s+(?:news|stories|articles?|headlines?)\b",
    re.IGNORECASE,
)


def _parse_requested_count(query: str, default: int = 5, maximum: int = 25) -> int:
    """Extract an explicit article count from the user's query."""
    m = _COUNT_RE.search(query)
    if m:
        if m.group(1):
            return min(int(m.group(1)), maximum)
        if m.group(2):
            return 5  # "a few" / "several" / "some"
    return default


def _format_articles(articles: list[dict], desc_cap: int = 200) -> str:
    lines = []
    for i, r in enumerate(articles, 1):
        src = f" — {r['source']}" if r["source"] else ""
        # Trim pub date to just "Day, DD Mon YYYY" (drop time + timezone)
        pub = r["pub"]
        if pub:
            pub = ", ".join(pub.split(", ")[:2]) if ", " in pub else pub
        desc = r["desc"][:desc_cap] if r["desc"] else ""
        line = f"[{i}] {r['title']}{src} ({pub})"
        if desc:
            line += f"\n{desc}"
        lines.append(line)
    return "\n\n".join(lines)


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


# ── Lifecycle helpers (call from app lifespan) ────────────────────────────────

async def close_http_client() -> None:
    """Close the persistent HTTP client. Call on app shutdown."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


def shutdown_executor() -> None:
    """Drain the thread-pool executor. Call on app shutdown."""
    _executor.shutdown(wait=True)
