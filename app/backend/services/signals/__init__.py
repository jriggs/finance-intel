"""
Trading signal engine.

Scores each stock across value, technical, analyst, macro and sentiment
dimensions, manages the tradable universe, runs named screeners, and produces
ranked recommendations.

This package replaces the former monolithic ``signals.py``. It is split into
focused, single-responsibility modules — :mod:`constants`, :mod:`scoring`,
:mod:`universe`, :mod:`volume`, :mod:`screeners`, :mod:`recommendations` — and
re-exports the full public (and historically reached-into) surface here so
existing callers such as ``import signals; signals.score_stock(...)`` and the
test-suite continue to work unchanged.
"""
from __future__ import annotations

# Market helpers the module has always re-exposed (callers use
# ``signals.get_price_history`` / ``signals.get_stock_info`` directly).
from market import (  # noqa: F401
    _yf_is_blocked,
    _yf_trip_breaker,
    get_fast_market_cap,
    get_price_history,
    get_stock_info,
)

# Exposed as ``signals._portfolio`` for back-compat (tests reach into it).
import db as _portfolio  # noqa: F401

from .constants import (  # noqa: F401
    ANALYST_MAX,
    GRADE_A_MIN,
    GRADE_B_MIN,
    GRADE_C_MIN,
    REC_BUY_MIN,
    REC_HOLD_MIN,
    REC_WATCH_MIN,
    TECHNICAL_MAX,
    VALUE_MAX,
    _LARGE_CAP_BLOCKLIST,
)
from .scoring import (  # noqa: F401
    _analyst_score,
    _grade,
    _long_term_score,
    _macro_score,
    _rec,
    _sentiment_score,
    _short_term_score,
    _technical_score,
    _value_score,
    score_stock,
    score_watchlist,
)
from .universe import (  # noqa: F401
    _FALLBACK_NAMES,
    _FALLBACK_UNIVERSE,
    _portfolio_symbol_cache,
    get_nyse_symbols,
    get_sp500_symbols,
)
from .volume import (  # noqa: F401
    _refresh_volumes_alpaca,
    _refresh_volumes_yfinance,
    _shortlist_by_volume_uncached,
    refresh_all_symbol_volumes,
    shortlist_by_volume,
)
from .screeners import (  # noqa: F401
    _screener_from_volume_cache,
    run_screen,
)
from .recommendations import get_recommendations  # noqa: F401

__all__ = [
    # Scoring
    "score_stock",
    "score_watchlist",
    # Screening & recommendations
    "run_screen",
    "get_recommendations",
    # Universe
    "get_sp500_symbols",
    "get_nyse_symbols",
    # Volume
    "shortlist_by_volume",
    "refresh_all_symbol_volumes",
    # Re-exported market helpers
    "get_price_history",
    "get_stock_info",
    "get_fast_market_cap",
]
