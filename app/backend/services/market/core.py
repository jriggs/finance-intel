"""
Market data core — yfinance session + in-process TTL cache, rate-limit backoff,
the 401 circuit breaker, and the "not found" symbol blocklist. Shared
infrastructure consumed by the data accessors.
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



# Explicit surface (incl. private names) so `from .core import *` gives the
# accessors every shared helper — no silent missing-import gaps.
__all__ = [
    "logger",
    "_cache", "_cache_lock", "_yf_not_found_lock",
    "_to_num", "_get_cache_config", "bust_symbol_cache", "build_yf_session",
    "_cache_get", "_cache_set", "_yf_ticker", "_yf_download",
    "_retry_with_backoff",
    "_yf_blocked_until", "_YF_COOLDOWN", "_yf_is_blocked", "_yf_trip_breaker",
    "_yf_check", "_YFRateLimitHandler", "_yf_not_found_pending",
    "_yf_logger", "_yf_handler", "get_yf_status", "clear_not_found_blocklist",
    "_flatten_columns",
    "_NOT_FOUND_KEY", "_NOT_FOUND_TTL_DAYS",
    "_not_found_load", "_not_found_save", "_not_found_flush_pending",
    "_not_found_block", "is_not_found_blocked",
]
