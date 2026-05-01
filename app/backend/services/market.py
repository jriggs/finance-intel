"""
Market data layer — yfinance for price history, fundamentals, news.
All functions are synchronous; call via asyncio.to_thread() from async handlers.
Includes exponential backoff retry for rate-limit resilience.
"""

from __future__ import annotations

import logging
import threading
import time
import warnings
from datetime import datetime
from typing import Any

import pandas as pd
import yfinance as yf

import broker
import db as _portfolio
from http_retry import exponential_backoff as _exponential_backoff

logger = logging.getLogger("market")
warnings.filterwarnings("ignore", category=FutureWarning)


# ── In-process TTL cache (replaces requests-cache — incompatible with curl_cffi) ──

_cache: dict[str, tuple[float, Any]] = {}   # key → (expires_at, value)
_cache_lock = threading.Lock()
_yf_not_found_lock = threading.Lock()


def _to_num(v: Any) -> float | None:
    """Coerce yfinance field to float; returns None for strings/non-numeric."""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _get_cache_config() -> dict:
    cfg = _portfolio.get_crawl_results("yfinance_cache_config") or {}
    return {
        "enabled": cfg.get("enabled", True),
        "ttl_minutes": cfg.get("ttl_minutes", 30),
    }

def bust_symbol_cache(symbol: str) -> None:
    """Remove all cached entries for a symbol so the next fetch goes live."""
    sym = symbol.upper()
    with _cache_lock:
        keys = [k for k in list(_cache.keys()) if ":" in k and k.split(":")[1] == sym]
        for k in keys:
            _cache.pop(k, None)


def build_yf_session():
    """Re-read cache config and clear stale entries. Called on startup and config save."""
    global _cache
    cfg = _get_cache_config()
    with _cache_lock:
        _cache.clear()
    if cfg["enabled"]:
        logger.info(f"yfinance cache enabled (TTL={cfg['ttl_minutes']}min, cache cleared)")
    else:
        logger.info("yfinance cache disabled (cache cleared)")

def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        entry = _cache.get(key)
    if entry and time.time() < entry[0]:
        return entry[1]
    return None

def _cache_set(key: str, value: Any, ttl_seconds: float):
    with _cache_lock:
        _cache[key] = (time.time() + ttl_seconds, value)

def _yf_ticker(symbol: str) -> yf.Ticker:
    return yf.Ticker(symbol.upper())

def _yf_download(symbol: str, **kwargs):
    return yf.download(symbol, **kwargs)


# ── Rate limit handling ───────────────────────────────────────────────────────

def _retry_with_backoff(fn, max_retries: int = 3, base_delay: float = 0.5):
    """
    yfinance-specific retry with circuit-breaker integration.
    Uses shared exponential_backoff for delay calculation.
    Trips circuit breaker on 401/rate-limit; does not retry auth errors.
    """
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            error_str = str(e).lower()
            if "401" in error_str or "unauthorized" in error_str or "invalid crumb" in error_str:
                _yf_trip_breaker()
                raise
            is_rate_limit = "rate" in error_str or "too many" in error_str or "429" in error_str
            if attempt < max_retries and (is_rate_limit or "timeout" in error_str):
                delay = _exponential_backoff(attempt, base_delay)
                logger.debug(f"Rate limited, retry {attempt+1}/{max_retries} after {delay:.1f}s")
                time.sleep(delay)
            else:
                if is_rate_limit:
                    _yf_trip_breaker()
                raise

# ── Circuit breaker — stop yfinance calls after 401 until cooldown expires ────

_yf_blocked_until: float = 0.0   # epoch seconds; 0 = not blocked
_YF_COOLDOWN = 900                # 15 minutes

def _yf_is_blocked() -> bool:
    return time.time() < _yf_blocked_until

def _yf_trip_breaker():
    global _yf_blocked_until
    if not _yf_is_blocked():
        _yf_blocked_until = time.time() + _YF_COOLDOWN
        logger.warning(f"yfinance rate-limited — pausing all requests for {_YF_COOLDOWN//60} min")
        # Stop all scheduler jobs so nothing keeps hammering the API
        try:
            import scheduler as _sched
            _sched.stop_all_jobs()
        except Exception:
            pass

def _yf_check():
    """Raise if circuit breaker is open."""
    if _yf_is_blocked():
        remaining = int(_yf_blocked_until - time.time())
        raise RuntimeError(f"yfinance paused after rate limit — {remaining}s remaining")


class _YFRateLimitHandler(logging.Handler):
    """
    Intercepts yfinance's internal logger.
    When it logs a rate-limit error (which yf.download raises internally but
    swallows before returning to the caller), trip the circuit breaker.
    Also detects 404 "No fundamentals data" errors and blocks those symbols.
    """
    _RATE_LIMIT_PHRASES = ("too many requests", "ratelimit", "rate limit", "429")
    _NOT_FOUND_PHRASES  = ("no fundamentals data found for symbol", "possibly delisted", "no price data found")

    def emit(self, record: logging.LogRecord) -> None:
        import re
        msg = record.getMessage()
        msg_lower = msg.lower()
        if any(p in msg_lower for p in self._RATE_LIMIT_PHRASES):
            _yf_trip_breaker()
        elif any(p in msg_lower for p in self._NOT_FOUND_PHRASES) and not _yf_is_blocked():
            # Extract symbol from message
            # Matches both "No fundamentals data found for symbol: DZZ" and "$ATCHW: possibly delisted"
            sym = None
            m = re.search(r"no fundamentals data found for symbol:\s*([A-Z0-9.\-^]+)", msg, re.IGNORECASE)
            if m:
                sym = m.group(1).upper()
            else:
                # Try pattern for "SYMBOL: error" format (e.g., "$ATCHW: possibly delisted")
                m = re.search(r"^\$?([A-Z0-9.\-^]+):\s+(?:possibly delisted|no price data)", msg, re.IGNORECASE)
                if m:
                    sym = m.group(1).upper()

            if not sym:
                # Try pattern for "['SYMBOL']: error" format from yfinance multi-line errors
                m = re.search(r"\[?'?([A-Z0-9.\-^]+)'?\]?:\s+(?:possibly delisted|no price data)", msg, re.IGNORECASE)
                if m:
                    sym = m.group(1).upper()

            if sym:
                with _yf_not_found_lock:
                    _yf_not_found_pending.add(sym)


# Symbols detected via log handler before _not_found_block is defined
_yf_not_found_pending: set[str] = set()


# Attach once at import time to the yfinance logger (and its children)
_yf_logger = logging.getLogger("yfinance")
_yf_handler = _YFRateLimitHandler()
_yf_logger.addHandler(_yf_handler)
_yf_logger.setLevel(logging.WARNING)  # Only capture warnings/errors; DEBUG floods the console
_yf_logger.propagate = False  # Don't double-emit through root logger

# Also attach to common yfinance submodules
for submodule in ["yfinance.download", "yfinance.ticker", "yfinance.data"]:
    sublogger = logging.getLogger(submodule)
    sublogger.addHandler(_yf_handler)
    sublogger.setLevel(logging.WARNING)


def get_yf_status() -> dict:
    """Return current yfinance circuit-breaker state for the settings UI."""
    blocked = _yf_is_blocked()
    remaining = max(0, int(_yf_blocked_until - time.time())) if blocked else 0
    return {
        "rate_limited": blocked,
        "cooldown_seconds_remaining": remaining,
        "cooldown_minutes": _YF_COOLDOWN // 60,
    }


def clear_not_found_blocklist(symbols: list[str] | None = None) -> dict[str, float]:
    """Remove symbols from the 404 blocklist. Clears all if symbols is None."""
    data = _not_found_load()
    if symbols is None:
        data = {}
    else:
        for sym in symbols:
            data.pop(sym.upper(), None)
    _not_found_save(data)
    return data


# ── Helpers ───────────────────────────────────────────────────────────────────

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse MultiIndex columns from yfinance downloads to lowercase strings."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    return df


# ── Fundamentals ──────────────────────────────────────────────────────────────

def _get_price_current(symbol: str) -> float | None:
    """Get current price from Alpaca, fallback to yfinance."""
    # Try Alpaca first (less rate-limiting)
    if broker.is_configured():
        quote = broker.get_quote(symbol)
        if quote and quote.get("price"):
            return quote.get("price")

    # Fallback to yfinance
    def _fetch():
        ticker = _yf_ticker(symbol)
        info = ticker.info or {}
        return (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("navPrice")
        )

    try:
        return _retry_with_backoff(_fetch, max_retries=1, base_delay=0.2)
    except Exception:
        return None


# ── 404 / "No fundamentals data" blocklist ────────────────────────────────────
_NOT_FOUND_KEY = "yf_not_found_symbols"
_NOT_FOUND_TTL_DAYS = 10


def _not_found_load() -> dict[str, float]:
    """Return {SYMBOL: expiry_epoch} from persistent store."""
    return _portfolio.get_crawl_results(_NOT_FOUND_KEY) or {}


def _not_found_save(data: dict[str, float]) -> None:
    _portfolio.save_crawl_results(_NOT_FOUND_KEY, data)


def _not_found_flush_pending() -> None:
    """Persist any symbols queued by the log handler into the blocklist."""
    with _yf_not_found_lock:
        pending = list(_yf_not_found_pending)
        _yf_not_found_pending.clear()
    if not pending:
        return
    data = _not_found_load()
    expiry = time.time() + _NOT_FOUND_TTL_DAYS * 86400
    for sym in pending:
        data[sym] = expiry
        logger.info(f"Blocked {sym} for {_NOT_FOUND_TTL_DAYS}d — no fundamentals data found (404)")
    _not_found_save(data)


def _not_found_block(symbol: str) -> None:
    """Mark symbol as not-found; blocks scoring for _NOT_FOUND_TTL_DAYS days."""
    sym = symbol.upper()
    _not_found_flush_pending()
    data = _not_found_load()
    data[sym] = time.time() + _NOT_FOUND_TTL_DAYS * 86400
    _not_found_save(data)
    logger.info(f"Blocked {sym} for {_NOT_FOUND_TTL_DAYS}d — no fundamentals data found (404)")


def is_not_found_blocked(symbol: str) -> bool:
    """Return True if symbol is currently blocked due to a prior 404."""
    sym = symbol.upper()
    data = _not_found_load()
    expiry = data.get(sym)
    if expiry is None:
        return False
    if time.time() > expiry:
        # Expired — remove and allow retry
        del data[sym]
        _not_found_save(data)
        return False
    return True


def get_stock_info(symbol: str) -> dict[str, Any]:
    """Return fundamentals + quote data for a symbol. Cached per config TTL."""
    _yf_check()
    sym = symbol.upper()

    # Flush any 404s detected via log handler
    if _yf_not_found_pending:
        _not_found_flush_pending()

    if is_not_found_blocked(sym):
        remaining_days = int((_not_found_load().get(sym, 0) - time.time()) / 86400) + 1
        raise RuntimeError(f"{sym} blocked for {remaining_days}d — no fundamentals data (404)")

    cfg = _get_cache_config()
    cache_key = f"info:{sym}"
    if cfg["enabled"]:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    def _fetch():
        ticker = _yf_ticker(sym)
        return ticker

    ticker = _retry_with_backoff(_fetch, max_retries=2, base_delay=0.5)
    try:
        info = ticker.info or {}
    except Exception as e:
        err = str(e)
        if "404" in err or "not found" in err.lower():
            _not_found_block(sym)
        raise

    # Detect 404 / "No fundamentals data" from yfinance error payload
    if info.get("trailingPegRatio") is None and not info.get("symbol") and not info.get("shortName") and not info.get("quoteType") and not info.get("regularMarketPrice"):
        # yfinance returned a near-empty dict — check quoteType to distinguish ETFs/valid tickers
        _not_found_block(sym)
        raise RuntimeError(f"No fundamentals data found for symbol: {sym}")

    # Use Alpaca-sourced price if available; recompute change_pct against prev close so
    # the two values are always consistent with each other.
    alpaca_price = _get_price_current(symbol)
    price = alpaca_price or (
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("navPrice")
    )
    prev_close = _to_num(info.get("regularMarketPreviousClose") or info.get("previousClose"))
    if alpaca_price and prev_close:
        change_pct = (alpaca_price - prev_close) / prev_close * 100
        change     = alpaca_price - prev_close
    else:
        change_pct = _to_num(info.get("regularMarketChangePercent"))
        change     = _to_num(info.get("regularMarketChange"))

    result = {
        "symbol":            symbol.upper(),
        "name":              info.get("longName") or info.get("shortName", ""),
        "price":             _to_num(price),
        "change":            change,
        "change_pct":        change_pct,
        "volume":            _to_num(info.get("regularMarketVolume")),
        "avg_volume":        _to_num(info.get("averageVolume")),
        "market_cap":        _to_num(info.get("marketCap")),
        "pe_ratio":          _to_num(info.get("trailingPE")),
        "forward_pe":        _to_num(info.get("forwardPE")),
        "pb_ratio":          _to_num(info.get("priceToBook")),
        "ev_ebitda":         _to_num(info.get("enterpriseToEbitda")),
        "ev_revenue":        _to_num(info.get("enterpriseToRevenue")),
        "debt_equity":       _to_num(info.get("debtToEquity")),
        "current_ratio":     _to_num(info.get("currentRatio")),
        "roe":               _to_num(info.get("returnOnEquity")),
        "roa":               _to_num(info.get("returnOnAssets")),
        "profit_margin":     _to_num(info.get("profitMargins")),
        "revenue_growth":    _to_num(info.get("revenueGrowth")),
        "earnings_growth":   _to_num(info.get("earningsGrowth")),
        "free_cashflow":     _to_num(info.get("freeCashflow")),
        "total_cash":        _to_num(info.get("totalCash")),
        "total_debt":        _to_num(info.get("totalDebt")),
        "sector":            info.get("sector", ""),
        "industry":          info.get("industry", ""),
        "52w_high":          _to_num(info.get("fiftyTwoWeekHigh")),
        "52w_low":           _to_num(info.get("fiftyTwoWeekLow")),
        "analyst_target":    _to_num(info.get("targetMeanPrice")),
        "analyst_low":       _to_num(info.get("targetLowPrice")),
        "analyst_high":      _to_num(info.get("targetHighPrice")),
        "recommendation":    info.get("recommendationKey", ""),
        "num_analysts":      _to_num(info.get("numberOfAnalystOpinions")),
        "short_ratio":       _to_num(info.get("shortRatio")),
        "insider_pct":       _to_num(info.get("heldPercentInsiders")),
        "institution_pct":   _to_num(info.get("heldPercentInstitutions")),
        "beta":              _to_num(info.get("beta")),
        "dividend_yield":    _to_num(info.get("dividendYield")),
        "ex_dividend_date":  info.get("exDividendDate"),
        "earnings_date":     _next_earnings(ticker),
        "website":           info.get("website", ""),
        "description":       info.get("longBusinessSummary", ""),
    }
    if cfg["enabled"]:
        _cache_set(cache_key, result, cfg["ttl_minutes"] * 60)
    return result


def _next_earnings(ticker: yf.Ticker) -> str | None:
    try:
        cal = ticker.calendar
        if cal is not None and not cal.empty:
            col = "Earnings Date"
            if col in cal.columns:
                val = cal[col].dropna()
                if len(val):
                    return str(val.iloc[0].date())
    except Exception:
        pass
    return None


# ── Price history + technical indicators ──────────────────────────────────────

def get_price_history(symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """
    Download OHLCV and compute RSI, SMAs, MACD, Bollinger Bands.
    Uses yfinance (with exponential backoff for rate limit resilience).
    Returns empty DataFrame on failure.
    """
    cfg = _get_cache_config()
    cache_key = f"history:{symbol.upper()}:{period}:{interval}"
    if cfg["enabled"]:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    df = pd.DataFrame()

    # Try Alpaca first (daily bars only — no intraday)
    # Validate coverage: Alpaca free tier caps history so we fall back to yfinance
    # if fewer than 70% of expected trading days were returned.
    _period_min_bars = {
        "1mo": 15, "3mo": 45, "6mo": 90, "1y": 175, "2y": 350,
        "5y": 875, "10y": 1750, "ytd": 1, "max": 1,
    }
    if interval == "1d" and broker.is_configured():
        df = broker.get_bars(symbol, period)
        if not df.empty:
            min_bars = _period_min_bars.get(period, 90)
            if len(df) >= min_bars:
                logger.debug(f"Fetched {symbol} bars from Alpaca ({len(df)} bars)")
            else:
                logger.debug(
                    f"Alpaca returned only {len(df)} bars for {period} "
                    f"(expected >={min_bars}), falling back to yfinance"
                )
                df = pd.DataFrame()

    # Fall back to yfinance
    if df.empty:
        _yf_check()
        def _download():
            return _yf_download(symbol, period=period, interval=interval,
                                progress=False, auto_adjust=True)
        try:
            df = _retry_with_backoff(_download, max_retries=2, base_delay=0.5)
            logger.debug(f"Fetched {symbol} bars from yfinance")
        except Exception as e:
            # Re-raise delisted/not-found errors so they can be properly handled
            err_str = str(e).lower()
            if "delisted" in err_str or "no price data" in err_str or "404" in err_str:
                raise
            return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    # Normalize columns (yfinance MultiIndex / adj close naming)
    df = _flatten_columns(df)
    df = df.rename(columns={"adj close": "close"}) if "adj close" in df.columns else df

    # ── RSI (14) ──────────────────────────────────────────────────────────────
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    # ── SMAs ──────────────────────────────────────────────────────────────────
    for n in (20, 50, 200):
        df[f"sma{n}"] = df["close"].rolling(n).mean()

    # ── EMA 12/26 + MACD ─────────────────────────────────────────────────────
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]        = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]

    # ── Bollinger Bands (20, 2σ) ──────────────────────────────────────────────  # noqa: RUF003
    sma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    df["bb_upper"] = sma20 + 2 * std20
    df["bb_mid"]   = sma20
    df["bb_lower"] = sma20 - 2 * std20

    # ── Average True Range (ATR 14) ───────────────────────────────────────────
    hi, lo, cl = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([hi - lo, (hi - cl).abs(), (lo - cl).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    df = df.round(4)
    if cfg["enabled"]:
        _cache_set(cache_key, df, cfg["ttl_minutes"] * 60)
    return df


def get_chart_data(symbol: str, period: str = "6mo") -> dict:
    """Return chart-ready OHLCV + indicators keyed by date string."""
    # Select appropriate interval for short periods
    interval = "5m" if period == "1d" else "1h" if period == "5d" else "1d"

    # For daily periods < 2y, fetch 2y so SMA200 is fully computed, then trim.
    # Trim sizes (trading days) per period.
    _trim: dict[str, int | None] = {
        "1mo": 23, "3mo": 66, "6mo": 132, "1y": 253,
    }
    fetch_period = "2y" if (interval == "1d" and period in _trim) else period
    df = get_price_history(symbol, fetch_period, interval)
    if df.empty:
        return {"error": f"No price data for {symbol}"}

    df = df.reset_index()

    # Find date column — could be 'date', 'Date', 'Datetime', or index name
    date_col = None
    for col in ['date', 'Date', 'Datetime', 'datetime']:
        if col in df.columns:
            date_col = col
            break

    fmt = "%Y-%m-%dT%H:%M:%S" if interval != "1d" else "%Y-%m-%d"

    def _strip_tz(series: pd.Series) -> pd.Series:
        s = pd.to_datetime(series)
        if s.dt.tz is not None:
            return s.dt.tz_convert(None)
        return s

    if date_col:
        df["date"] = _strip_tz(df[date_col]).dt.strftime(fmt)
    else:
        df["date"] = _strip_tz(pd.Series(df.index)).dt.strftime(fmt)

    # Trim to requested display window (we may have fetched more for SMA200 warmup)
    trim_rows = _trim.get(period) if interval == "1d" else None
    if trim_rows is not None and len(df) > trim_rows:
        df = df.tail(trim_rows).reset_index(drop=True)

    records = (
        df[["date", "open", "high", "low", "close", "volume",
            "rsi", "sma20", "sma50", "sma200",
            "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_mid", "bb_lower", "atr14"]]
        .fillna("")
        .to_dict("records")
    )

    result: dict = {"symbol": symbol.upper(), "period": period, "candles": records}

    # For 1d, include prev_close so the frontend can show day-over-day % (same as watchlist)
    if period == "1d":
        try:
            daily = get_price_history(symbol, "5d", "1d")
            if not daily.empty and len(daily) >= 2:
                result["prev_close"] = float(daily["close"].iloc[-2])
        except Exception:
            pass

    return result


# ── News ──────────────────────────────────────────────────────────────────────

def get_news(symbol: str, limit: int = 15) -> list[dict]:
    """Fetch recent news articles for a symbol via yfinance."""
    ticker = _yf_ticker(symbol)
    news = ticker.news or []
    out = []
    for n in news[:limit]:
        ct = n.get("content", {})
        pub_time = ct.get("pubDate") or n.get("providerPublishTime")
        try:
            pub_iso = (
                pub_time if isinstance(pub_time, str)
                else datetime.utcfromtimestamp(int(pub_time)).isoformat() + "Z"
            )
        except Exception:
            pub_iso = ""

        thumb = ""
        if ct:
            thumbs = (ct.get("thumbnail") or {}).get("resolutions", [])
            if thumbs:
                thumb = thumbs[-1].get("url", "")

        out.append({
            "title":     ct.get("title") or n.get("title", ""),
            "publisher": (ct.get("provider") or {}).get("displayName") or n.get("publisher", ""),
            "url":       (ct.get("canonicalUrl") or {}).get("url") or n.get("link", ""),
            "published": pub_iso,
            "summary":   ct.get("summary", ""),
            "thumbnail": thumb,
        })
    return out


# ── Sector peers ─────────────────────────────────────────────────────────────

_SECTOR_PEERS: dict[str, list[str]] = {
    "Technology":             ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "AMD", "CRM", "ADBE"],
    "Financial Services":     ["JPM", "BAC", "GS", "MS", "WFC", "BRK-B", "V", "MA"],
    "Healthcare":             ["JNJ", "UNH", "PFE", "ABT", "MRK", "LLY", "AMGN", "GILD"],
    "Consumer Cyclical":      ["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "TGT", "BKNG"],
    "Communication Services": ["GOOGL", "META", "DIS", "NFLX", "T", "VZ", "CMCSA"],
    "Energy":                 ["XOM", "CVX", "COP", "SLB", "OXY", "PSX", "VLO"],
    "Industrials":            ["CAT", "GE", "HON", "UPS", "RTX", "BA", "LMT", "DE"],
    "Consumer Defensive":     ["PG", "KO", "PEP", "WMT", "COST", "CL", "GIS"],
    "Utilities":              ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE"],
    "Real Estate":            ["AMT", "PLD", "CCI", "EQIX", "SPG", "O", "DLR"],
    "Materials":              ["LIN", "APD", "SHW", "FCX", "NEM", "DD", "NUE"],
    "Basic Materials":        ["LIN", "APD", "SHW", "FCX", "NEM", "DD"],
}


def get_sector_peers(symbol: str) -> list[str]:
    try:
        info   = _yf_ticker(symbol).info or {}
        sector = info.get("sector", "")
        return [s for s in _SECTOR_PEERS.get(sector, []) if s != symbol.upper()][:5]
    except Exception:
        return []


# ── Batch quote (watchlist) ───────────────────────────────────────────────────

def get_batch_quotes(symbols: list[str]) -> list[dict]:
    """
    Light quote fetch for a list of symbols (price, change%).
    Uses a single yf.download(period=2d) batch call instead of N .info calls,
    then overrides price with Alpaca where configured.
    """
    if not symbols:
        return []

    # Seed results so every symbol gets an entry even on total failure
    price_map: dict[str, float | None] = {s: None for s in symbols}
    chg_map:   dict[str, float | None] = {s: None for s in symbols}
    vol_map:   dict[str, float | None] = {s: None for s in symbols}

    # Try Alpaca snapshot first (single batch call, no rate limits)
    if broker.is_configured():
        try:
            snaps = broker.get_snapshot(symbols)
            for sym in symbols:
                s = snaps.get(sym.upper())
                if s:
                    price_map[sym] = s["price"]
                    chg_map[sym]   = s["change_pct"]
                    vol_map[sym]   = s["volume"]
        except Exception as e:
            logger.debug("Alpaca snapshot batch failed: %s", e)

    # Fill any gaps with yfinance batch download
    missing = [s for s in symbols if price_map[s] is None]
    if missing and not _yf_is_blocked():
        try:
            raw = _yf_download(" ".join(missing), period="2d", progress=False, auto_adjust=True)
            if not raw.empty:
                multi = isinstance(raw.columns, pd.MultiIndex)
                for sym in missing:
                    sym_u = sym.upper()
                    try:
                        closes = (raw[("Close", sym_u)] if multi else raw["Close"]).dropna()
                        if len(closes) >= 2:
                            prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
                            price_map[sym] = last
                            chg_map[sym]   = (last - prev) / prev * 100 if prev else None
                        elif len(closes) == 1:
                            price_map[sym] = float(closes.iloc[-1])
                        vols = (raw[("Volume", sym_u)] if multi else raw["Volume"]).dropna()
                        if not vols.empty:
                            vol_map[sym] = float(vols.iloc[-1])
                    except Exception:
                        pass
        except Exception as e:
            err = str(e).lower()
            if "401" in err or "unauthorized" in err or "invalid crumb" in err:
                _yf_trip_breaker()
            logger.debug("batch quote yfinance fallback failed: %s", e)

    name_map = _portfolio.get_symbol_names(symbols)
    return [
        {
            "symbol":     sym,
            "name":       name_map.get(sym.upper(), sym),
            "price":      price_map[sym],
            "change_pct": chg_map[sym],
            "volume":     vol_map[sym],
        }
        for sym in symbols
    ]
