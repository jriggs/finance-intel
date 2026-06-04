"""Typed app-settings keys and the key-value settings store."""
from __future__ import annotations

import contextlib
import json
from typing import Any

from .connection import _get_conn, _write_lock


class AppSettings:
    """Typed constants for app_settings keys. Use instead of raw strings."""
    MODEL_PATH               = "model_path"
    YFINANCE_CACHE_CONFIG    = "yfinance_cache_config"
    VOLUME_ANALYSIS_CONFIG   = "volume_analysis_config"
    SCREENER_CONFIG          = "screener_config"
    CRAWL_CONFIG             = "crawl_config"
    SIGNAL_SCORES            = "signal_scores"
    REFRESH_PICKS_CONFIG     = "refresh_picks_config"
    SCORE_WATCHLIST_CONFIG   = "score_watchlist_config"
    INGEST_CONFIG            = "ingest_config"
    REFRESH_SYMBOLS_CONFIG   = "refresh_symbols_config"
    RECOMMENDATIONS_CONFIG   = "recommendations_config"
    EXTENDED_INSIGHTS_CONFIG = "extended_insights_config"
    YF_NOT_FOUND_SYMBOLS     = "yf_not_found_symbols"


def get_setting(key: str, default: Any = None) -> Any:
    row = _get_conn().execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    with contextlib.suppress(Exception):
        return json.loads(row["value"])
    return row["value"]


def set_setting(key: str, value: Any) -> None:
    with _write_lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO app_settings(key, value) VALUES(?, ?)",
            (key, json.dumps(value)),
        )
        _get_conn().commit()
