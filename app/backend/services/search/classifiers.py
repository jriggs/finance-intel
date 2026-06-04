"""Query routing: URL extraction, real-time-necessity detection, and the
weather/news/finance/time classifiers (pure, regex-based)."""
from __future__ import annotations

import re

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


