"""
Ranked recommendations: merges full watchlist signal scores with the top hits
from the volume-cache screeners, de-duplicated and ranked by score.
"""
from __future__ import annotations

import time

import db as _portfolio

from .scoring import score_stock
from .screeners import _screener_from_volume_cache

def get_recommendations(watchlist: list[str], top_n: int = 10) -> dict:
    """
    Produce ranked recommendations by merging:
      1. Full signal scores for the watchlist (BUY/WATCH only)
      2. Top hits from the undervalued + momentum screeners
    Deduplicates by symbol, returns top_n ranked by score.
    """
    seen: dict[str, dict] = {}

    # Score the watchlist fully
    for i, sym in enumerate(watchlist):
        if i > 0:
            time.sleep(0.3)  # Avoid yfinance rate limits
        try:
            r = score_stock(sym)
            if r and r.get("recommendation") in ("BUY", "WATCH"):
                seen[sym] = r
        except Exception:
            continue

    # Broad scan using volume-analysis cached data — no yfinance calls
    volume_cache = _portfolio.get_all_volume_scores()
    for sym, cached in volume_cache.items():
        if sym in seen:
            continue
        try:
            r = _screener_from_volume_cache("undervalued", cached)
            if r and r.get("score", 0) >= 45:
                seen[sym] = r
        except Exception:
            continue

    for sym, cached in volume_cache.items():
        if sym in seen:
            continue
        try:
            r = _screener_from_volume_cache("momentum", cached)
            if r and r.get("score", 0) >= 50:
                seen[sym] = r
        except Exception:
            continue

    ranked = sorted(seen.values(), key=lambda x: x.get("score", 0), reverse=True)
    return {"recommendations": ranked[:top_n], "total_evaluated": len(seen)}
