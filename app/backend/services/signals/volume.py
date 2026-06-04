"""
Volume-based pre-filtering and bulk average-volume refresh (Alpaca preferred,
yfinance fallback). Trims the raw universe before the expensive per-stock
scoring pass, with a thread-safe shortlist cache.
"""
from __future__ import annotations

import logging
import threading
import time as _time

import db as _portfolio
from market import _yf_is_blocked, _yf_trip_breaker

_universe_logger = logging.getLogger("signals.volume")

_SHORTLIST_CACHE: tuple[float, list[str]] | None = None
_SHORTLIST_TTL = 4 * 3600      # 4 hours
_SHORTLIST_LOCK = threading.Lock()

def shortlist_by_volume(
    symbols: list[str],
    min_avg_volume: int   = 300_000,
    min_price:      float = 2.0,
    batch_size:     int   = 200,
) -> list[str]:
    """
    Pass 1 — quick pre-filter.
    Batch-download 5-day OHLCV; keep symbols where avg_volume >= min_avg_volume
    and last close >= min_price. Typically trims NYSE (~1 000 symbols) to ~200-400
    before running the more expensive full score_stock() on each.

    Result is cached for _SHORTLIST_TTL. Concurrent callers wait on the lock
    and reuse the cached result rather than all hammering yfinance independently.
    """
    global _SHORTLIST_CACHE

    now = _time.time()
    # Fast path — check cache before acquiring lock
    if _SHORTLIST_CACHE and now - _SHORTLIST_CACHE[0] < _SHORTLIST_TTL:
        _universe_logger.info(f"Shortlist cache hit: {len(_SHORTLIST_CACHE[1])} symbols")
        return _SHORTLIST_CACHE[1]

    with _SHORTLIST_LOCK:
        # Re-check after acquiring lock (another thread may have just computed it)
        now = _time.time()
        if _SHORTLIST_CACHE and now - _SHORTLIST_CACHE[0] < _SHORTLIST_TTL:
            _universe_logger.info(f"Shortlist cache hit (post-lock): {len(_SHORTLIST_CACHE[1])} symbols")
            return _SHORTLIST_CACHE[1]

        result = _shortlist_by_volume_uncached(symbols, min_avg_volume, min_price, batch_size)
        _SHORTLIST_CACHE = (_time.time(), result)
        return result


def _shortlist_by_volume_uncached(
    symbols: list[str],
    min_avg_volume: int,
    min_price: float,
    batch_size: int,
) -> list[str]:
    import pandas as pd
    import yfinance as yf

    # Skip symbols that failed a fetch within the last 7 days
    recently_failed = _portfolio.get_fetch_failed_symbols(within_days=7)
    if recently_failed:
        before = len(symbols)
        symbols = [s for s in symbols if s not in recently_failed]
        _universe_logger.info(
            f"Skipping {before - len(symbols)} recently-failed symbols (delisted/no data)"
        )

    shortlisted: list[str] = []
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    try:
        import scheduler as _scheduler_mod
    except Exception:
        _scheduler_mod = None  # type: ignore

    for batch_idx in range(total_batches):
        # Abort if circuit breaker tripped or any job stop requested
        if _yf_is_blocked():
            _universe_logger.warning("shortlist_by_volume: yfinance blocked — aborting")
            break
        if _scheduler_mod and any(_scheduler_mod._stop_flags.values()):
            _universe_logger.info("shortlist_by_volume: stop flag set — aborting")
            break

        batch = symbols[batch_idx * batch_size : (batch_idx + 1) * batch_size]
        try:
            raw = yf.download(
                batch, period="5d", interval="1d",
                progress=False, auto_adjust=True,
            )
            if raw.empty:
                _portfolio.mark_symbol_fetch_failed(batch)
                continue

            if isinstance(raw.columns, pd.MultiIndex):
                # columns = (Metric, Ticker)
                vol_df   = raw.get("Volume", pd.DataFrame())
                close_df = raw.get("Close",  pd.DataFrame())
                newly_failed: list[str] = []
                for sym in batch:
                    try:
                        has_close = sym in close_df.columns and not close_df[sym].dropna().empty
                        has_vol   = sym in vol_df.columns   and not vol_df[sym].dropna().empty
                        if not has_close or not has_vol:
                            newly_failed.append(sym)
                            continue
                        avg_vol = float(vol_df[sym].mean())
                        price   = float(close_df[sym].dropna().iloc[-1])
                        if avg_vol >= min_avg_volume and price >= min_price:
                            shortlisted.append(sym)
                    except Exception:
                        newly_failed.append(sym)
                if newly_failed:
                    _portfolio.mark_symbol_fetch_failed(newly_failed)
                    _universe_logger.debug(f"Marked {len(newly_failed)} symbols as fetch-failed: {newly_failed[:5]}{'...' if len(newly_failed) > 5 else ''}")
            else:
                # Single ticker or yfinance returned flat columns
                avg_vol = float(raw["Volume"].mean())               if "Volume" in raw.columns else 0.0
                price   = float(raw["Close"].dropna().iloc[-1])     if "Close"  in raw.columns else 0.0
                if avg_vol >= min_avg_volume and price >= min_price:
                    shortlisted.extend(batch)
                elif "Close" not in raw.columns:
                    _portfolio.mark_symbol_fetch_failed(batch)
        except Exception as e:
            _universe_logger.warning(f"shortlist batch {batch_idx}: {e}")
        _time.sleep(0.5)

    _universe_logger.info(
        f"Shortlist: {len(shortlisted)}/{len(symbols)} passed "
        f"(vol≥{min_avg_volume:,}, price≥${min_price})"
    )
    return shortlisted


def refresh_all_symbol_volumes(batch_size: int = 1000) -> int:
    """
    Fetch 5-day avg_volume for every symbol and persist to the symbols table.
    Uses Alpaca bars (no rate limits, large batches) when configured,
    falling back to yfinance otherwise.
    Returns the number of symbols successfully updated.
    """
    import broker
    if broker.is_configured():
        return _refresh_volumes_alpaca(batch_size)
    return _refresh_volumes_yfinance(min(batch_size, 200))


def _refresh_volumes_alpaca(batch_size: int = 1000) -> int:
    """Fetch avg_volume for all symbols via Alpaca multi-symbol bars request."""
    import re
    from datetime import date, timedelta as td
    import broker

    all_symbols = [s["symbol"] for s in _portfolio.get_symbols()]
    # Alpaca only accepts plain ticker symbols — drop preferred shares, warrants,
    # units, rights etc. that contain hyphens, dots, or other special characters.
    symbols = [s for s in all_symbols if re.match(r'^[A-Z]{1,5}$', s)]
    skipped = len(all_symbols) - len(symbols)
    if skipped:
        _universe_logger.info(f"refresh_volumes_alpaca: skipping {skipped} non-standard symbols")

    end   = date.today()
    start = end - td(days=7)  # 7 calendar days → ~5 trading days

    updated = 0
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    client = StockHistoricalDataClient(broker.ALPACA_API_KEY, broker.ALPACA_SECRET_KEY)

    for batch_idx in range(total_batches):
        batch = symbols[batch_idx * batch_size : (batch_idx + 1) * batch_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
            )
            bars_by_sym = client.get_stock_bars(req).data

            batch_volumes: dict[str, float] = {}
            for sym, bars in bars_by_sym.items():
                if bars:
                    vols = [float(b.volume) for b in bars if b.volume]
                    if vols:
                        batch_volumes[sym] = sum(vols) / len(vols)

            if batch_volumes:
                _portfolio.save_symbol_avg_volumes(batch_volumes)
                updated += len(batch_volumes)

        except Exception as e:
            _universe_logger.warning(f"refresh_volumes_alpaca batch {batch_idx}: {e}")

    _universe_logger.info(f"refresh_volumes_alpaca: updated {updated}/{len(symbols)} symbols")
    return updated


def _refresh_volumes_yfinance(batch_size: int = 200) -> int:
    """Fallback: fetch avg_volume via yfinance when Alpaca is not configured."""
    import pandas as pd
    import yfinance as yf

    symbols = [s["symbol"] for s in _portfolio.get_symbols()]
    recently_failed = _portfolio.get_fetch_failed_symbols(within_days=10)
    symbols = [s for s in symbols if s not in recently_failed]

    updated = 0
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        if _yf_is_blocked():
            _universe_logger.warning("refresh_volumes_yfinance: yfinance blocked — aborting")
            break

        batch = symbols[batch_idx * batch_size : (batch_idx + 1) * batch_size]
        try:
            raw = yf.download(batch, period="5d", interval="1d", progress=False, auto_adjust=True)
            if raw.empty:
                _portfolio.mark_symbol_fetch_failed(batch)
                continue

            batch_volumes: dict[str, float] = {}
            newly_failed: list[str] = []

            if isinstance(raw.columns, pd.MultiIndex):
                vol_df = raw.get("Volume", pd.DataFrame())
                for sym in batch:
                    try:
                        if sym in vol_df.columns and not vol_df[sym].dropna().empty:
                            batch_volumes[sym] = float(vol_df[sym].mean())
                        else:
                            newly_failed.append(sym)
                    except Exception:
                        newly_failed.append(sym)
            else:
                if "Volume" in raw.columns and len(batch) == 1:
                    batch_volumes[batch[0]] = float(raw["Volume"].mean())
                else:
                    newly_failed.extend(batch)

            if batch_volumes:
                _portfolio.save_symbol_avg_volumes(batch_volumes)
                updated += len(batch_volumes)
            if newly_failed:
                _portfolio.mark_symbol_fetch_failed(newly_failed)

        except Exception as e:
            err = str(e).lower()
            if "401" in err or "unauthorized" in err or "invalid crumb" in err:
                _yf_trip_breaker()
                _universe_logger.warning(f"refresh_volumes_yfinance: auth error — aborting: {e}")
                break
            _universe_logger.warning(f"refresh_volumes_yfinance batch {batch_idx}: {e}")
        _time.sleep(0.5)

    _universe_logger.info(f"refresh_volumes_yfinance: updated {updated}/{len(symbols)} symbols")
    return updated
