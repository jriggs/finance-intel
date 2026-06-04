"""
Scoring constants and the large-cap blocklist for the signal engine.

Pure data with no imports — depended on by the scoring and screener modules.
"""
from __future__ import annotations

# ── Moonshot: large-cap blocklist ─────────────────────────────────────────────
# Companies with market cap >> $10B that can never realistically 10x.
# API-based cap checks are flaky (rate limits → exception → cap=0 → passes).
# This list is the hard gate; get_fast_market_cap() is a secondary check.
_LARGE_CAP_BLOCKLIST: frozenset[str] = frozenset({
    # Mega cap (>$500B)
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "BRK.A", "BRK.B",
    "LLY", "AVGO", "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "WMT",
    "PG", "JNJ", "ORCL", "BAC", "ABBV", "KO", "CRM", "CVX", "MRK", "NFLX",
    "AMD", "PEP", "TMO", "ACN", "ADBE", "DIS", "ABT", "WFC", "MCD", "CSCO",
    "PM", "GE", "TXN", "IBM", "QCOM", "INTU", "AMGN", "DHR", "CAT", "ISRG",
    # Large cap ($50B–$500B) — still can't 10x
    "NOW", "UBER", "GS", "BKNG", "BLK", "SPGI", "AXP", "SYK", "VRTX", "GILD",
    "PFE", "T", "VZ", "RTX", "HON", "MMM", "UPS", "BA", "LMT", "NEE",
    "SCHW", "CME", "USB", "C", "MS", "REGN", "ZTS", "BSX", "EOG", "SLB",
    "CI", "COP", "SO", "DUK", "PLD", "AMT", "WELL", "CCI", "PSA", "O",
    "PYPL", "SNAP", "PINS", "TWTR", "LYFT", "DASH", "ABNB", "COIN", "HOOD",
    "INTC", "MU", "AMAT", "KLAC", "LRCX", "MCHP", "SNPS", "CDNS",
})

# ── Scoring constants ─────────────────────────────────────────────────────────

# Maximum points per dimension (must sum to 100 for the main score)
VALUE_MAX     = 40
TECHNICAL_MAX = 35
ANALYST_MAX   = 25

# Grade thresholds (main score and all sub-scores)
GRADE_A_MIN = 70
GRADE_B_MIN = 50
GRADE_C_MIN = 30

# Recommendation thresholds (mirrors grade thresholds)
REC_BUY_MIN   = GRADE_A_MIN
REC_WATCH_MIN = GRADE_B_MIN
REC_HOLD_MIN  = GRADE_C_MIN
