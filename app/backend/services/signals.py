"""
Trading signal engine.

Scores each stock across three dimensions:
  • Value    (0-40 pts) - fundamentals vs sector / absolute thresholds
  • Technical (0-35 pts) - price action, momentum, trend
  • Analyst   (0-25 pts) - consensus targets and upgrades

Final grade:  A ≥70  B ≥50  C ≥30  D <30
Recommendation:  BUY / WATCH / HOLD / AVOID
"""

from __future__ import annotations

import time
from typing import Any

import fundamentals as _fd
import db as _portfolio
from market import _yf_is_blocked, _yf_trip_breaker, get_price_history, get_stock_info, get_fast_market_cap

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


def _grade(score: int) -> str:
    if score >= GRADE_A_MIN: return "A"
    if score >= GRADE_B_MIN: return "B"
    if score >= GRADE_C_MIN: return "C"
    return "D"


def _rec(score: int) -> str:
    if score >= REC_BUY_MIN:   return "BUY"
    if score >= REC_WATCH_MIN: return "WATCH"
    if score >= REC_HOLD_MIN:  return "HOLD"
    return "AVOID"


# ── Individual scorers ────────────────────────────────────────────────────────

def _value_score(info: dict[str, Any]) -> tuple[int, list[str]]:
    score, reasons = 0, []

    pe = info.get("pe_ratio")
    if pe and pe > 0:
        if pe < 12:
            score += 18; reasons.append(f"P/E {pe:.1f} — deeply undervalued")
        elif pe < 20:
            score += 12; reasons.append(f"P/E {pe:.1f} — reasonably valued")
        elif pe < 30:
            score += 5

    pb = info.get("pb_ratio")
    if pb and pb > 0:
        if pb < 1.0:
            score += 12; reasons.append(f"P/B {pb:.2f} — below book value")
        elif pb < 2.0:
            score += 6;  reasons.append(f"P/B {pb:.2f} — near book value")

    ev_ebitda = info.get("ev_ebitda")
    if ev_ebitda and ev_ebitda > 0:
        if ev_ebitda < 8:
            score += 6; reasons.append(f"EV/EBITDA {ev_ebitda:.1f} — cheap")
        elif ev_ebitda < 15:
            score += 3

    rev_growth = info.get("revenue_growth")
    if rev_growth:
        if rev_growth > 0.20:
            score += 4; reasons.append(f"Revenue +{rev_growth*100:.0f}% YoY")
        elif rev_growth > 0.10:
            score += 2

    fcf = info.get("free_cashflow")
    if fcf and fcf > 0:
        score += 4; reasons.append("Positive free cash flow")

    de = info.get("debt_equity")
    if de is not None:
        if de < 0.5:
            score += 3; reasons.append(f"Low debt/equity ({de:.2f})")
        elif de > 2.0:
            score -= 3; reasons.append(f"High leverage ({de:.2f})")

    insider = info.get("insider_pct")
    if insider and insider > 0.05:
        score += 3; reasons.append(f"Insiders own {insider*100:.1f}%")

    return min(score, VALUE_MAX), reasons


def _technical_score(info: dict[str, Any], symbol: str | None = None, df=None) -> tuple[int, list[str]]:
    score, reasons = 0, []

    try:
        if df is None:
            df = get_price_history(symbol, period="6mo")
        if df.empty:
            return 0, []
        latest  = df.iloc[-1]
        price   = float(latest["close"])
    except Exception:
        return 0, []

    # RSI momentum
    rsi = latest.get("rsi")
    if rsi and str(rsi) != "nan":
        rsi = float(rsi)
        if 30 <= rsi <= 45:
            score += 15; reasons.append(f"RSI {rsi:.0f} — recovering from oversold")
        elif 45 < rsi <= 55:
            score += 8;  reasons.append(f"RSI {rsi:.0f} — neutral momentum")
        elif rsi < 30:
            score += 10; reasons.append(f"RSI {rsi:.0f} — oversold (contrarian)")
        elif rsi > 70:
            score -= 5;  reasons.append(f"RSI {rsi:.0f} — overbought, risk of pullback")

    # Trend (price vs 200 SMA)
    sma200 = latest.get("sma200")
    if sma200 and str(sma200) != "nan":
        sma200 = float(sma200)
        if price > sma200:
            score += 8; reasons.append("Above 200-day SMA — uptrend")
        else:
            pct_below = (sma200 - price) / sma200 * 100
            score -= 3; reasons.append(f"{pct_below:.1f}% below 200 SMA")

    # Golden cross: 50 SMA above 200 SMA
    sma50 = latest.get("sma50")
    if sma50 and sma200 and str(sma50) != "nan" and str(sma200) != "nan" and float(sma50) > float(sma200):
        score += 5; reasons.append("Golden cross — 50 SMA above 200 SMA")

    # 52-week positioning
    low52  = info.get("52w_low")
    high52 = info.get("52w_high")
    if low52 and high52 and high52 > low52:
        pct_from_low = (price - low52) / (high52 - low52)
        if pct_from_low < 0.20:
            score += 10; reasons.append("Near 52-week low — potential reversal zone")
        elif pct_from_low > 0.85:
            score -= 3;  reasons.append("Near 52-week high — limited near-term upside")

    # Volume: recent vs average
    vol   = info.get("volume")
    avgvol = info.get("avg_volume")
    if vol and avgvol and avgvol > 0:
        ratio = vol / avgvol
        if ratio > 1.5:
            score += 5; reasons.append(f"Volume {ratio:.1f}x above average — unusual activity")

    # MACD crossover
    macd = latest.get("macd")
    sig  = latest.get("macd_signal")
    if macd and sig and str(macd) != "nan" and str(sig) != "nan" and float(macd) > float(sig):
        score += 5; reasons.append("MACD bullish crossover")

    return min(max(score, 0), TECHNICAL_MAX), reasons


def _analyst_score(info: dict[str, Any]) -> tuple[int, list[str]]:
    score, reasons = 0, []

    price  = info.get("price")
    target = info.get("analyst_target")
    n      = info.get("num_analysts", 0)

    if price and target and price > 0:
        upside = (target - price) / price
        if upside > 0.25:
            score += 15; reasons.append(f"Analysts see +{upside*100:.0f}% upside (target ${target:.2f})")
        elif upside > 0.10:
            score += 8;  reasons.append(f"Analysts see +{upside*100:.0f}% upside")
        elif upside < -0.05:
            score -= 5;  reasons.append("Analyst target below current price")

    rec = (info.get("recommendation") or "").lower()
    if rec in ("strong_buy", "buy"):
        score += 10; reasons.append(f"Analyst consensus: {rec.replace('_',' ').title()}")
    elif rec in ("hold", "neutral"):
        score += 3
    elif rec in ("sell", "strong_sell", "underperform"):
        score -= 5;  reasons.append(f"Analyst consensus: {rec.replace('_',' ').title()}")

    if n and n >= 10:
        score += 3; reasons.append(f"{n} analysts covering")

    return min(max(score, 0), ANALYST_MAX), reasons


def _macro_score(macro_snap: dict[str, Any], sector: str | None = None) -> tuple[int, list[str]]:
    """
    Macro environment score (0-100): interest rates, yield curve, VIX, unemployment, inflation, sentiment.
    Uses FRED data. Weights vary by sector—tech sensitive to rates, financials benefit, consumer to sentiment.
    """
    score, reasons = 0, []
    sector = (sector or "").lower()

    # Sector sensitivity weights (0.0-2.0x multiplier)
    # Tech/Growth = rate sensitive (hurt by high rates); Financials = rate beneficiary (help by high rates)
    sector_weights = {
        "technology": {"rate_sens": 2.0, "yield": 2.0, "consumer": 0.8},
        "consumer discretionary": {"rate_sens": 1.8, "yield": 1.5, "consumer": 2.0},
        "consumer staples": {"rate_sens": 1.0, "yield": 1.0, "consumer": 1.5},
        "financials": {"rate_sens": 2.0, "yield": 1.5, "consumer": 0.7},  # inverse scoring
        "energy": {"rate_sens": 1.0, "yield": 1.0, "consumer": 0.8},
        "industrials": {"rate_sens": 1.5, "yield": 1.5, "consumer": 1.2},
        "healthcare": {"rate_sens": 0.8, "yield": 0.8, "consumer": 0.6},
        "utilities": {"rate_sens": 0.5, "yield": 1.5, "consumer": 0.8},
        "materials": {"rate_sens": 1.5, "yield": 1.5, "consumer": 0.7},
        "real estate": {"rate_sens": 2.0, "yield": 2.0, "consumer": 0.8},
    }
    weights = sector_weights.get(sector, {"rate_sens": 1.0, "yield": 1.0, "consumer": 1.0})
    is_fin = sector == "financials"

    # Fed Funds Rate: inverse scoring for financials (higher rates = good), normal for others (lower rates = good)
    ff = macro_snap.get("FedFunds", {})
    if ff.get("value"):
        try:
            ff_val = float(ff["value"])
            if is_fin:
                # Financials: high rates = good (boost score)
                if ff_val > 5.0:
                    score += int(20 * weights["rate_sens"]); reasons.append(f"Fed Funds {ff_val:.2f}% — high rates boost bank margins")
                elif ff_val > 3.0:
                    score += int(12 * weights["rate_sens"]); reasons.append(f"Fed Funds {ff_val:.2f}% — rates support margins")
                elif ff_val > 1.0:
                    score += int(5 * weights["rate_sens"]); reasons.append(f"Fed Funds {ff_val:.2f}% — low margins")
                else:
                    score -= int(5 * weights["rate_sens"]); reasons.append(f"Fed Funds {ff_val:.2f}% — compressed margins")
            else:
                # Growth/tech: low rates = good (boost score), high rates = bad (reduce score)
                if ff_val < 2.0:
                    score += int(20 * weights["rate_sens"]); reasons.append(f"Fed Funds {ff_val:.2f}% — accommodative for growth")
                elif ff_val < 4.0:
                    score += int(10 * weights["rate_sens"]); reasons.append(f"Fed Funds {ff_val:.2f}% — moderate")
                elif ff_val < 6.0:
                    score += int(5 * weights["rate_sens"])
                else:
                    score -= int(5 * weights["rate_sens"]); reasons.append(f"Fed Funds {ff_val:.2f}% — headwind for growth")
        except (ValueError, TypeError):
            pass

    # Yield Curve (10Y-2Y; inversion = recession risk). Financials benefit from steep curve.
    yc = macro_snap.get("YieldCurve", {})
    if yc.get("value"):
        try:
            yc_val = float(yc["value"])
            yc_weight = weights["yield"] * (2.0 if is_fin else 1.0)  # Financials benefit more from steep curve
            if yc_val > 0.5:
                score += int(15 * yc_weight); reasons.append(f"Yield curve +{yc_val:.2f}% — normal/steep")
            elif yc_val > 0:
                score += int(8 * yc_weight); reasons.append(f"Yield curve +{yc_val:.2f}% — flat")
            else:
                score -= int(5 * yc_weight); reasons.append(f"Yield curve inverted {yc_val:.2f}% — recession risk")
        except (ValueError, TypeError):
            pass

    # VIX (lower = less fear; all sectors vulnerable but rates/tech more so)
    vix = macro_snap.get("VIX", {})
    if vix.get("value"):
        try:
            vix_val = float(vix["value"])
            vix_weight = 1.3 if sector in ("technology", "consumer discretionary") else 0.9 if sector == "utilities" else 1.0
            if vix_val < 15:
                score += int(15 * vix_weight); reasons.append(f"VIX {vix_val:.0f} — low fear")
            elif vix_val < 25:
                score += int(8 * vix_weight); reasons.append(f"VIX {vix_val:.0f} — normal volatility")
            elif vix_val < 35:
                score += int(3 * vix_weight)
            else:
                score -= int(5 * vix_weight); reasons.append(f"VIX {vix_val:.0f} — elevated fear")
        except (ValueError, TypeError):
            pass

    # Unemployment (more impactful for consumer/discretionary sectors)
    unemp = macro_snap.get("Unemployment", {})
    if unemp.get("value"):
        try:
            u_val = float(unemp["value"])
            if u_val < 4.0:
                score += int(15 * weights["consumer"]); reasons.append(f"Unemployment {u_val:.1f}% — tight labor market")
            elif u_val < 5.0:
                score += int(8 * weights["consumer"]); reasons.append(f"Unemployment {u_val:.1f}% — healthy")
            elif u_val > 6.0:
                score -= int(5 * weights["consumer"]); reasons.append(f"Unemployment {u_val:.1f}% — slack labor market")
        except (ValueError, TypeError):
            pass

    # Inflation (CPI; hurt all sectors but tech more sensitive)
    cpi = macro_snap.get("CPI", {})
    if cpi.get("value"):
        try:
            cpi_val = float(cpi["value"])
            cpi_weight = 1.2 if sector == "technology" else 0.9 if sector == "utilities" else 1.0
            if cpi_val < 3.0:
                score += int(10 * cpi_weight); reasons.append(f"CPI {cpi_val:.1f}% — controlled inflation")
            elif cpi_val < 5.0:
                score += int(5 * cpi_weight); reasons.append(f"CPI {cpi_val:.1f}% — elevated inflation")
            else:
                score -= int(5 * cpi_weight); reasons.append(f"CPI {cpi_val:.1f}% — high inflation")
        except (ValueError, TypeError):
            pass

    # Consumer Sentiment (key for discretionary and staples; less important for tech/financials)
    cs = macro_snap.get("ConsumerSentiment", {})
    if cs.get("value"):
        try:
            cs_val = float(cs["value"])
            if cs_val > 90:
                score += int(10 * weights["consumer"]); reasons.append(f"Consumer sentiment {cs_val:.0f} — strong spending")
            elif cs_val > 70:
                score += int(5 * weights["consumer"]); reasons.append(f"Consumer sentiment {cs_val:.0f} — moderate")
            elif cs_val < 70:
                score -= int(3 * weights["consumer"]); reasons.append(f"Consumer sentiment {cs_val:.0f} — weak spending")
        except (ValueError, TypeError):
            pass

    return min(max(score, 0), 100), reasons


def _sentiment_score(symbol: str) -> tuple[int, list[str]]:
    """
    News sentiment score (0-100): keyword analysis of crawled headlines + cached sentiment.
    Symbol-specific headlines weighted 3x over generic market headlines.
    """
    import db as portfolio

    score, reasons = 0, []

    # Positive and negative keywords
    pos_keywords = {
        "bull", "surge", "beat", "rally", "gain", "strong", "upgrade", "buy", "rise",
        "record", "growth", "profit", "upside", "breakout", "momentum", "outperform",
        "better", "bullish", "positive", "jump", "soar", "boom", "recovery"
    }
    neg_keywords = {
        "bear", "crash", "drop", "miss", "fall", "weak", "downgrade", "sell", "decline",
        "loss", "risk", "concern", "layoff", "warning", "slump", "selloff", "bearish",
        "negative", "plunge", "tumble", "collapse", "recession", "downturn"
    }

    # Generic headlines (lower weight)
    generic_headlines = []
    for source in ("reddit", "marketwatch", "zacks", "yahoo", "finviz", "google_finance"):
        cached = portfolio.get_crawl_results(source)
        if cached and cached.get("items"):
            for item in cached["items"]:
                title = (item.get("title") or item.get("text") or "").lower()
                if title:
                    generic_headlines.append(title)

    # Symbol-specific headlines (higher weight = 3x more important)
    specific_headlines = []
    for source in (f"finviz_{symbol.upper()}", f"google_finance_{symbol.upper()}", f"zacks_{symbol.upper()}"):
        cached = portfolio.get_crawl_results(source)
        if cached and cached.get("items"):
            for item in cached["items"]:
                title = (item.get("title") or "").lower()
                if title:
                    specific_headlines.append(title)

    # Count sentiment separately for generic vs specific
    generic_pos, generic_neg = 0, 0
    for headline in generic_headlines:
        for kw in pos_keywords:
            if kw in headline:
                generic_pos += 1
        for kw in neg_keywords:
            if kw in headline:
                generic_neg += 1

    specific_pos, specific_neg = 0, 0
    for headline in specific_headlines:
        for kw in pos_keywords:
            if kw in headline:
                specific_pos += 1
        for kw in neg_keywords:
            if kw in headline:
                specific_neg += 1

    # Compute scores: weight symbol-specific 3x heavier
    if specific_pos + specific_neg > 0:
        specific_ratio = (specific_pos - specific_neg) / (specific_pos + specific_neg)
        specific_score = int(max(0, min(100, (specific_ratio + 1) * 50)))
    else:
        specific_score = 50

    if generic_pos + generic_neg > 0:
        generic_ratio = (generic_pos - generic_neg) / (generic_pos + generic_neg)
        generic_score = int(max(0, min(100, (generic_ratio + 1) * 50)))
    else:
        generic_score = 50

    # Blend: 75% symbol-specific, 25% generic market sentiment
    blended_score = int(specific_score * 0.75 + generic_score * 0.25) if (specific_pos + specific_neg > 0) else generic_score

    # Blend with cached sentiment if available
    cached_sentiment = portfolio.get_sentiment(symbol)
    if cached_sentiment and cached_sentiment.get("score") is not None:
        cached_score = float(cached_sentiment["score"])
        score = int(blended_score * 0.7 + cached_score * 0.3)  # 70% live headlines, 30% cached
        reasons.append(f"{specific_pos} pos / {specific_neg} neg (symbol-specific)")
        if generic_pos + generic_neg > 0:
            reasons.append(f"{generic_pos} pos / {generic_neg} neg (market)")
        reasons.append(f"Cached sentiment: {cached_score:.0f}")
    else:
        score = blended_score
        reasons.append(f"{specific_pos} pos / {specific_neg} neg (symbol-specific)")
        if generic_pos + generic_neg > 0:
            reasons.append(f"{generic_pos} pos / {generic_neg} neg (market)")
        if specific_pos + specific_neg == 0 and generic_pos + generic_neg == 0:
            reasons.append("No recent coverage")

    return min(max(score, 0), 100), reasons


def _short_term_score(info: dict[str, Any], symbol: str | None = None, df=None) -> tuple[int, list[str]]:
    """
    Short-term signal (1-30 days): recent momentum, technicals, volume.
    Uses last ~21 rows of price history. Max 100 pts.
    """
    score, reasons = 0, []

    try:
        if df is None:
            df = get_price_history(symbol, period="1mo")
        if df.empty:
            return 0, []
        latest = df.iloc[-1]
        price  = float(latest["close"])
    except Exception:
        return 0, []

    # RSI momentum (primary short-term indicator)
    rsi = latest.get("rsi")
    if rsi and str(rsi) != "nan":
        rsi = float(rsi)
        if 30 <= rsi <= 45:
            score += 20; reasons.append(f"RSI {rsi:.0f} — recovering from oversold")
        elif 45 < rsi <= 55:
            score += 12; reasons.append(f"RSI {rsi:.0f} — neutral momentum")
        elif rsi < 30:
            score += 15; reasons.append(f"RSI {rsi:.0f} — oversold (strong reversal)")
        elif rsi > 70:
            score += 5;  reasons.append(f"RSI {rsi:.0f} — overbought")

    # MACD momentum
    macd = latest.get("macd")
    sig  = latest.get("macd_signal")
    if macd and sig and str(macd) != "nan" and str(sig) != "nan":
        macd_f, sig_f = float(macd), float(sig)
        if macd_f > sig_f:
            score += 15; reasons.append("MACD bullish (price momentum)")
        else:
            score -= 8;  reasons.append("MACD bearish")

    # SMA20 (short-term trend)
    sma20 = latest.get("sma20")
    if sma20 and str(sma20) != "nan":
        sma20 = float(sma20)
        if price > sma20:
            score += 12; reasons.append("Above 20-day SMA (short uptrend)")
        else:
            score -= 8;  reasons.append("Below 20-day SMA")

    # Volume surge (unusual activity → potential breakout)
    vol    = info.get("volume")
    avgvol = info.get("avg_volume")
    if vol and avgvol and avgvol > 0:
        ratio = vol / avgvol
        if ratio > 2.0:
            score += 15; reasons.append(f"Volume {ratio:.1f}x average — strong breakout signal")
        elif ratio > 1.5:
            score += 8;  reasons.append(f"Volume {ratio:.1f}x average")

    # 1-week price change (recent momentum)
    if len(df) >= 6:  # at least 5 trading days
        week_ago = df.iloc[-6]["close"]
        pct_change = (price - week_ago) / week_ago if week_ago > 0 else 0
        if pct_change > 0.05:
            score += 10; reasons.append(f"Up +{pct_change*100:.1f}% in 1 week")
        elif pct_change < -0.05:
            score -= 5;  reasons.append(f"Down {pct_change*100:.1f}% in 1 week")

    return min(max(score, 0), 100), reasons


def _long_term_score(info: dict[str, Any], symbol: str | None = None, df=None) -> tuple[int, list[str]]:
    """
    Long-term signal: fundamentals, longer technicals, analyst consensus.
    Uses 6-month history. Max 100 pts.
    Combines reusable scorers: _value_score + long-term technicals + _analyst_score.
    """
    score, reasons = 0, []

    # Fundamentals (value score)
    v_score, v_reasons = _value_score(info)
    score += v_score
    reasons.extend(v_reasons)

    # Long-term technicals (6-month view)
    try:
        if df is None:
            df = get_price_history(symbol, period="6mo")
        if not df.empty:
            latest = df.iloc[-1]
            price  = float(latest["close"])

            # SMA200 (long-term trend)
            sma200 = latest.get("sma200")
            if sma200 and str(sma200) != "nan":
                sma200 = float(sma200)
                if price > sma200:
                    score += 12; reasons.append("Above 200-day SMA — long uptrend")
                else:
                    score -= 5;  reasons.append("Below 200-day SMA")

            # Golden cross (50 SMA > 200 SMA)
            sma50 = latest.get("sma50")
            if sma50 and sma200 and str(sma50) != "nan" and str(sma200) != "nan" and float(sma50) > float(sma200):
                score += 8; reasons.append("Golden cross — 50 > 200 SMA")

            # 52-week positioning (medium-term reversal / trend continuation)
            low52  = info.get("52w_low")
            high52 = info.get("52w_high")
            if low52 and high52 and high52 > low52:
                pct_from_low = (price - low52) / (high52 - low52)
                if pct_from_low < 0.20:
                    score += 12; reasons.append("Near 52-week low — reversal opportunity")
                elif pct_from_low > 0.85:
                    score -= 5;  reasons.append("Near 52-week high — overbought long-term")
    except Exception:
        pass

    # Analyst consensus (longer-term targets)
    a_score, a_reasons = _analyst_score(info)
    score += a_score
    reasons.extend(a_reasons)

    return min(max(score, 0), 100), reasons


# ── Main entry point ──────────────────────────────────────────────────────────

def score_stock(symbol: str, macro_snap: dict | None = None) -> dict:
    """
    Return a full signal analysis dict for one symbol.
    Includes both short-term (1-30d) and long-term scores.
    Raises on network failure — callers should catch.
    Pass macro_snap to skip the per-call FRED fetch when scoring in bulk.
    """
    info = get_stock_info(symbol)

    # Skip if no price data — ticker doesn't exist or yfinance returned nothing
    if not info.get("price") and not info.get("name"):
        return None

    df_6mo = get_price_history(symbol, period="6mo")

    v_score, v_reasons = _value_score(info)
    t_score, t_reasons = _technical_score(info, df=df_6mo)
    a_score, a_reasons = _analyst_score(info)

    total = v_score + t_score + a_score
    grade = _grade(total)
    rec   = _rec(total)

    st_score, st_reasons = _short_term_score(info, df=df_6mo)
    st_grade = _grade(st_score)

    lt_score, lt_reasons = _long_term_score(info, df=df_6mo)
    lt_grade = _grade(lt_score)

    if macro_snap is None:
        try:
            macro_snap = _fd.get_macro_snapshot()
        except Exception:
            macro_snap = {}
    sector = info.get("sector", "")
    mo_score, mo_reasons = _macro_score(macro_snap, sector)
    mo_grade = _grade(mo_score)

    try:
        se_score, se_reasons = _sentiment_score(symbol)
        se_grade = _grade(se_score)
    except Exception:
        se_score, se_reasons, se_grade = 0, [], "D"

    return {
        "symbol":          symbol.upper(),
        "name":            info.get("name", ""),
        "price":           info.get("price"),
        "score":           total,
        "grade":           grade,
        "recommendation":  rec,
        "breakdown": {
            "value":     {"score": v_score, "max": VALUE_MAX,     "reasons": v_reasons},
            "technical": {"score": t_score, "max": TECHNICAL_MAX, "reasons": t_reasons},
            "analyst":   {"score": a_score, "max": ANALYST_MAX,   "reasons": a_reasons},
        },
        "all_reasons": v_reasons + t_reasons + a_reasons,
        "short_term": {
            "score": st_score,
            "grade": st_grade,
            "reasons": st_reasons,
        },
        "long_term": {
            "score": lt_score,
            "grade": lt_grade,
            "reasons": lt_reasons,
        },
        "macro": {
            "score": mo_score,
            "grade": mo_grade,
            "reasons": mo_reasons,
        },
        "sentiment": {
            "score": se_score,
            "grade": se_grade,
            "reasons": se_reasons,
        },
        "sector":           info.get("sector", ""),
        "industry":         info.get("industry", ""),
        "pe_ratio":         info.get("pe_ratio"),
        "pb_ratio":         info.get("pb_ratio"),
        "52w_low":          info.get("52w_low"),
        "52w_high":         info.get("52w_high"),
        "analyst_target":   info.get("analyst_target"),
        "revenue_growth":   info.get("revenue_growth"),
        "earnings_growth":  info.get("earnings_growth"),
        "profit_margin":    info.get("profit_margin"),
        "roe":              info.get("roe"),
        "dividend_yield":   info.get("dividend_yield"),
        "debt_equity":      info.get("debt_equity"),
        "volume":           info.get("volume"),
        "avg_volume":       info.get("avg_volume"),
    }


def score_watchlist(symbols: list[str]) -> list[dict]:
    """Score all watchlist symbols; skip on error or missing data."""
    results = []
    for sym in symbols:
        try:
            score = score_stock(sym)
            if score:
                results.append(score)
        except Exception as e:
            results.append({"symbol": sym, "error": str(e), "score": 0, "grade": "?", "recommendation": "ERROR"})
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results


# ── Universe ──────────────────────────────────────────────────────────────────

import logging as _logging
import threading as _threading
import time as _time

import httpx as _httpx
from http_retry import retry_sync as _retry_sync, WEB_RETRY as _WEB_RETRY

_universe_logger = _logging.getLogger("signals")
_SP500_CACHE: tuple[float, list[str]] | None = None
_NYSE_CACHE:  tuple[float, list[str]] | None = None
_UNIVERSE_TTL = 86400  # refresh once per day
_NYSE_FALLBACK_TTL = 900  # 15 min — retry real fetch sooner after fallback

_SHORTLIST_CACHE: tuple[float, list[str]] | None = None
_SHORTLIST_TTL = 4 * 3600  # 4 hours
_SHORTLIST_LOCK = _threading.Lock()

_FALLBACK_UNIVERSE = [
    "AAPL","MSFT","GOOGL","META","AMZN","NVDA","AMD","INTC","CRM","ADBE","ORCL","CSCO","QCOM","TXN","AMAT",
    "JPM","BAC","GS","MS","WFC","V","MA","BRK-B","AXP","C","SCHW","BLK","USB","PNC","TFC",
    "JNJ","UNH","PFE","ABT","MRK","LLY","AMGN","GILD","BMY","CVS","HCA","CI","ELV","HUM","ISRG",
    "XOM","CVX","COP","SLB","OXY","PSX","VLO","MPC","HAL","BKR","EOG","DVN","FANG",
    "CAT","GE","HON","UPS","RTX","BA","LMT","DE","MMM","ETN","EMR","ITW","FDX","NOC","GD",
    "WMT","TGT","HD","COST","MCD","SBUX","NKE","LOW","TJX","BKNG","ABNB","MAR","YUM",
    "PG","KO","PEP","CL","GIS","MO","PM","KHC","STZ","EL","MDLZ",
    "NEE","DUK","SO","D","AEP","EXC","SRE","PCG","ED","FE","ES","PPL",
    "T","VZ","CMCSA","DIS","NFLX","CHTR","FOX","OMC","WBD",
    "AMT","PLD","CCI","EQIX","SPG","O","DLR","PSA","WELL","VTR",
    "LIN","APD","SHW","FCX","NEM","DD","NUE","ALB","MOS","CF",
    "TSLA","F","GM","TM","RIVN","LCID","STLA",
]

# Static name map for fallback universe — no network calls required
_FALLBACK_NAMES: dict[str, str] = {
    "AAPL":"Apple Inc.","MSFT":"Microsoft Corp.","GOOGL":"Alphabet Inc.","META":"Meta Platforms",
    "AMZN":"Amazon.com Inc.","NVDA":"NVIDIA Corp.","AMD":"Advanced Micro Devices","INTC":"Intel Corp.",
    "CRM":"Salesforce Inc.","ADBE":"Adobe Inc.","ORCL":"Oracle Corp.","CSCO":"Cisco Systems",
    "QCOM":"Qualcomm Inc.","TXN":"Texas Instruments","AMAT":"Applied Materials",
    "JPM":"JPMorgan Chase","BAC":"Bank of America","GS":"Goldman Sachs","MS":"Morgan Stanley",
    "WFC":"Wells Fargo","V":"Visa Inc.","MA":"Mastercard Inc.","BRK-B":"Berkshire Hathaway",
    "AXP":"American Express","C":"Citigroup Inc.","SCHW":"Charles Schwab","BLK":"BlackRock Inc.",
    "USB":"U.S. Bancorp","PNC":"PNC Financial","TFC":"Truist Financial",
    "JNJ":"Johnson & Johnson","UNH":"UnitedHealth Group","PFE":"Pfizer Inc.",
    "ABT":"Abbott Laboratories","MRK":"Merck & Co.","LLY":"Eli Lilly","AMGN":"Amgen Inc.",
    "GILD":"Gilead Sciences","BMY":"Bristol-Myers Squibb","CVS":"CVS Health",
    "HCA":"HCA Healthcare","CI":"Cigna Group","ELV":"Elevance Health","HUM":"Humana Inc.",
    "ISRG":"Intuitive Surgical",
    "XOM":"Exxon Mobil","CVX":"Chevron Corp.","COP":"ConocoPhillips","SLB":"SLB (Schlumberger)",
    "OXY":"Occidental Petroleum","PSX":"Phillips 66","VLO":"Valero Energy",
    "MPC":"Marathon Petroleum","HAL":"Halliburton Co.","BKR":"Baker Hughes",
    "EOG":"EOG Resources","DVN":"Devon Energy","FANG":"Diamondback Energy",
    "CAT":"Caterpillar Inc.","GE":"GE Aerospace","HON":"Honeywell International",
    "UPS":"United Parcel Service","RTX":"RTX Corp.","BA":"Boeing Co.","LMT":"Lockheed Martin",
    "DE":"Deere & Co.","MMM":"3M Co.","ETN":"Eaton Corp.","EMR":"Emerson Electric",
    "ITW":"Illinois Tool Works","FDX":"FedEx Corp.","NOC":"Northrop Grumman","GD":"General Dynamics",
    "WMT":"Walmart Inc.","TGT":"Target Corp.","HD":"Home Depot","COST":"Costco Wholesale",
    "MCD":"McDonald's Corp.","SBUX":"Starbucks Corp.","NKE":"Nike Inc.","LOW":"Lowe's Companies",
    "TJX":"TJX Companies","BKNG":"Booking Holdings","ABNB":"Airbnb Inc.","MAR":"Marriott International",
    "YUM":"Yum! Brands",
    "PG":"Procter & Gamble","KO":"Coca-Cola Co.","PEP":"PepsiCo Inc.","CL":"Colgate-Palmolive",
    "GIS":"General Mills","MO":"Altria Group","PM":"Philip Morris International",
    "KHC":"Kraft Heinz Co.","STZ":"Constellation Brands","EL":"Estée Lauder","MDLZ":"Mondelez International",
    "NEE":"NextEra Energy","DUK":"Duke Energy","SO":"Southern Co.","D":"Dominion Energy",
    "AEP":"American Electric Power","EXC":"Exelon Corp.","SRE":"Sempra","PCG":"PG&E Corp.",
    "ED":"Consolidated Edison","FE":"FirstEnergy Corp.","ES":"Eversource Energy","PPL":"PPL Corp.",
    "T":"AT&T Inc.","VZ":"Verizon Communications","CMCSA":"Comcast Corp.",
    "DIS":"Walt Disney Co.","NFLX":"Netflix Inc.","CHTR":"Charter Communications",
    "FOX":"Fox Corp.","OMC":"Omnicom Group","WBD":"Warner Bros. Discovery",
    "AMT":"American Tower Corp.","PLD":"Prologis Inc.","CCI":"Crown Castle Inc.",
    "EQIX":"Equinix Inc.","SPG":"Simon Property Group","O":"Realty Income Corp.",
    "DLR":"Digital Realty Trust","PSA":"Public Storage","WELL":"Welltower Inc.","VTR":"Ventas Inc.",
    "LIN":"Linde plc","APD":"Air Products & Chemicals","SHW":"Sherwin-Williams",
    "FCX":"Freeport-McMoRan","NEM":"Newmont Corp.","DD":"DuPont de Nemours",
    "NUE":"Nucor Corp.","ALB":"Albemarle Corp.","MOS":"Mosaic Co.","CF":"CF Industries",
    "TSLA":"Tesla Inc.","F":"Ford Motor Co.","GM":"General Motors","TM":"Toyota Motor",
    "RIVN":"Rivian Automotive","LCID":"Lucid Group","STLA":"Stellantis N.V.",
}


def _portfolio_symbol_cache() -> list[str]:
    """Return symbols already cached in the portfolio DB (from refresh_symbols job)."""
    try:
        import db as _portfolio
        rows = _portfolio.get_symbols()
        if rows:
            return [r["symbol"] for r in rows]
    except Exception:
        pass
    return []


def get_sp500_symbols() -> list[str]:
    """
    Fetch current S&P 500 constituents from Wikipedia.
    Falls back to portfolio DB cache, then hardcoded subset on failure.
    Cached for 24 hours.
    """
    global _SP500_CACHE
    now = _time.time()
    if _SP500_CACHE and now - _SP500_CACHE[0] < _UNIVERSE_TTL:
        return _SP500_CACHE[1]

    try:
        import pandas as pd
        html = _retry_sync(
            lambda: _httpx.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; finance-bot/1.0)"},
            ).text,
            _WEB_RETRY,
        )
        tables = pd.read_html(html, attrs={"id": "constituents"})
        syms = tables[0]["Symbol"].tolist()
        syms = [s.replace(".", "-") for s in syms if isinstance(s, str)]
        _universe_logger.info(f"S&P 500 universe loaded: {len(syms)} symbols")
        _SP500_CACHE = (now, syms)
        return syms
    except Exception as e:
        _universe_logger.warning(f"S&P 500 fetch failed ({e}), using fallback universe")

    cached = _portfolio_symbol_cache()
    if cached:
        _universe_logger.info(f"S&P 500 fallback: using {len(cached)} portfolio-cached symbols")
        _SP500_CACHE = (now, cached)
        return cached

    _SP500_CACHE = (now, _FALLBACK_UNIVERSE)
    return _FALLBACK_UNIVERSE


def get_nyse_symbols() -> list[str]:
    """
    Fetch all NYSE-listed common stocks from NASDAQ's symbol directory.
    Filters out ETFs, preferred shares, and illiquid names.
    Falls back to portfolio DB cache, then S&P 500 on failure. Cached 24 hours.
    """
    global _NYSE_CACHE
    now = _time.time()
    if _NYSE_CACHE and now - _NYSE_CACHE[0] < _UNIVERSE_TTL:
        return _NYSE_CACHE[1]

    try:
        r = _retry_sync(
            lambda: _httpx.get(
                "https://ftp.nasdaqtrader.com/dynamic/SymbolDirectory/otherlisted.txt",
                timeout=15, follow_redirects=True,
            ),
            _WEB_RETRY,
        )
        r.raise_for_status()
        syms = []
        for line in r.text.splitlines()[1:]:     # skip header
            parts = line.split("|")
            if len(parts) < 7:
                continue
            sym      = parts[0].strip()
            exchange = parts[2].strip()           # N=NYSE, A=AMEX, P=Arca
            etf      = parts[4].strip()           # Y if ETF
            test     = parts[6].strip()           # Y if test issue
            # Keep NYSE/ARCA common stocks with clean ticker symbols only
            if exchange in ("N", "P") and etf != "Y" and test != "Y" and sym and sym.isalpha() and len(sym) <= 5:
                syms.append(sym)
        _universe_logger.info(f"NYSE universe loaded: {len(syms)} symbols")
        _NYSE_CACHE = (now, syms)
        return syms
    except Exception as e:
        _universe_logger.warning(f"NYSE fetch failed ({e}), falling back to portfolio cache")

    cached = _portfolio_symbol_cache()
    if cached:
        _universe_logger.info(f"NYSE fallback: using {len(cached)} portfolio-cached symbols")
        # Short TTL so we retry the real NASDAQ fetch soon
        _NYSE_CACHE = (now - _UNIVERSE_TTL + _NYSE_FALLBACK_TTL, cached)
        return cached

    return get_sp500_symbols()


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


# ── Named screeners ───────────────────────────────────────────────────────────

def _screen_undervalued(sym: str) -> dict | None:
    info = get_stock_info(sym)
    v, vr = _value_score(info)
    a, ar = _analyst_score(info)
    total = v + a
    if total < 30:
        return None
    return {
        "symbol": sym, "name": info.get("name", ""), "price": info.get("price"),
        "score": total, "grade": "A" if total >= 55 else "B",
        "recommendation": "BUY" if total >= 55 else "WATCH",
        "reasons": (vr + ar)[:4],
        "pe_ratio": info.get("pe_ratio"), "analyst_target": info.get("analyst_target"),
        "sector": info.get("sector", ""),
    }


def _screen_momentum(sym: str) -> dict | None:
    info = get_stock_info(sym)
    t, tr = _technical_score(info, sym)
    a, ar = _analyst_score(info)
    total = t + a
    if total < 35:
        return None
    return {
        "symbol": sym, "name": info.get("name", ""), "price": info.get("price"),
        "score": total, "grade": "A" if total >= 50 else "B",
        "recommendation": "BUY" if total >= 50 else "WATCH",
        "reasons": (tr + ar)[:4],
        "pe_ratio": info.get("pe_ratio"), "analyst_target": info.get("analyst_target"),
        "sector": info.get("sector", ""),
    }


def _screen_growth(sym: str) -> dict | None:
    info = get_stock_info(sym)
    rev_g = info.get("revenue_growth") or 0
    earn_g = info.get("earnings_growth") or 0
    margin = info.get("profit_margin") or 0
    roe    = info.get("roe") or 0
    score = 0
    reasons = []
    if rev_g > 0.20:  score += 25; reasons.append(f"Revenue +{rev_g*100:.0f}% YoY")
    elif rev_g > 0.10: score += 15; reasons.append(f"Revenue +{rev_g*100:.0f}% YoY")
    if earn_g > 0.25: score += 20; reasons.append(f"Earnings +{earn_g*100:.0f}% YoY")
    elif earn_g > 0.10: score += 10
    if margin > 0.20: score += 15; reasons.append(f"Margin {margin*100:.0f}%")
    elif margin > 0.10: score += 8
    if roe > 0.20:    score += 10; reasons.append(f"ROE {roe*100:.0f}%")
    a, ar = _analyst_score(info)
    score += a // 2
    reasons += ar[:1]
    if score < 35:
        return None
    return {
        "symbol": sym, "name": info.get("name", ""), "price": info.get("price"),
        "score": score, "grade": "A" if score >= 55 else "B",
        "recommendation": "BUY" if score >= 55 else "WATCH",
        "reasons": reasons[:4],
        "pe_ratio": info.get("pe_ratio"), "analyst_target": info.get("analyst_target"),
        "sector": info.get("sector", ""),
        "revenue_growth": rev_g, "earnings_growth": earn_g,
    }


def _screen_dividend(sym: str) -> dict | None:
    info = get_stock_info(sym)
    div_yield = info.get("dividend_yield") or 0
    de        = info.get("debt_equity") or 999
    margin    = info.get("profit_margin") or 0
    if div_yield < 0.02:
        return None
    score = 0
    reasons = []
    if div_yield > 0.05:  score += 30; reasons.append(f"Dividend yield {div_yield*100:.1f}%")
    elif div_yield > 0.03: score += 20; reasons.append(f"Dividend yield {div_yield*100:.1f}%")
    else:                  score += 10; reasons.append(f"Dividend yield {div_yield*100:.1f}%")
    if de < 1.0:   score += 10; reasons.append(f"Conservative leverage ({de:.1f})")
    if margin > 0.10: score += 10; reasons.append(f"Healthy margins {margin*100:.0f}%")
    v, vr = _value_score(info)
    score += v // 3
    reasons += vr[:1]
    return {
        "symbol": sym, "name": info.get("name", ""), "price": info.get("price"),
        "score": score, "grade": "A" if score >= 40 else "B",
        "recommendation": "BUY" if score >= 40 else "WATCH",
        "reasons": reasons[:4],
        "pe_ratio": info.get("pe_ratio"), "analyst_target": info.get("analyst_target"),
        "sector": info.get("sector", ""),
        "dividend_yield": div_yield,
    }


def _screen_beaten_down(sym: str) -> dict | None:
    """Near 52w low + oversold — contrarian reversal candidates."""
    info = get_stock_info(sym)
    price = info.get("price") or 0
    low52 = info.get("52w_low") or 0
    high52 = info.get("52w_high") or 1
    if not (price and low52 and high52 > low52):
        return None
    pct_from_low = (price - low52) / (high52 - low52)
    if pct_from_low > 0.30:  # not beaten down enough
        return None
    score = 0
    reasons = []
    score += int((1 - pct_from_low) * 30)
    reasons.append(f"{pct_from_low*100:.0f}% off 52w low (low=${low52:.2f})")
    v, vr = _value_score(info)
    a, ar = _analyst_score(info)
    score += v // 2 + a // 2
    reasons += (vr + ar)[:2]
    if score < 25:
        return None
    return {
        "symbol": sym, "name": info.get("name", ""), "price": info.get("price"),
        "score": score, "grade": "A" if score >= 45 else "B",
        "recommendation": "WATCH",
        "reasons": reasons[:4],
        "pe_ratio": info.get("pe_ratio"), "analyst_target": info.get("analyst_target"),
        "sector": info.get("sector", ""),
        "pct_from_52w_low": round(pct_from_low * 100, 1),
    }


_GREEN_ENERGY_SYMBOLS = [
    "ENPH", "SEDG", "RUN", "FSLR", "SPWR", "NEE", "BEP", "CWEN", "AES",
    "PLUG", "BE", "BLDP", "ITRI", "GNRC", "HASI", "ARRY", "SHLS", "NOVA",
    "CSIQ", "JKS", "DQ", "SOL", "ORA", "EVA", "GPRE", "REX", "AMRC",
]


def _screen_green_energy(sym: str) -> dict | None:
    """Green / clean energy — solar, wind, storage, utilities."""
    if sym not in _GREEN_ENERGY_SYMBOLS:
        return None
    info = get_stock_info(sym)
    v, vr = _value_score(info)
    t, tr = _technical_score(info, sym)
    a, ar = _analyst_score(info)
    score = v + t // 2 + a
    if score < 15:
        return None
    return {
        "symbol": sym, "name": info.get("name", ""), "price": info.get("price"),
        "score": score, "grade": "A" if score >= 55 else "B",
        "recommendation": "BUY" if score >= 55 else "WATCH",
        "reasons": (vr + ar + tr)[:4],
        "pe_ratio": info.get("pe_ratio"), "analyst_target": info.get("analyst_target"),
        "sector": info.get("sector", ""),
    }


def _screen_moonshots(sym: str) -> dict | None:
    """
    Moonshots — stocks coiled to launch, not ones that already ran.
    Looks for: strong revenue/earnings growth, early technical setup
    (price recovering but well below 52w high), MACD just turning bullish,
    RSI building from neutral, and volume picking up.
    """
    # ── Blocklist gate (instant, no API) ─────────────────────────────────────
    if sym in _LARGE_CAP_BLOCKLIST:
        return None

    info = get_stock_info(sym)
    price = info.get("price") or 0
    if not price:
        return None

    # ── Market cap gate: secondary check via API ──────────────────────────────
    # > $10B (large cap): physically impossible to 1000%; exclude entirely
    # $2B–$10B (mid cap): very hard; allow but penalise
    # < $2B (small/micro): prime moonshot territory; bonus
    mcap = get_fast_market_cap(sym)  # bypasses info cache
    if mcap > 10_000_000_000:  # > $10B — hard no
        return None

    score = 0
    reasons: list[str] = []

    if mcap > 0:
        if mcap < 300_000_000:  # micro cap < $300M
            score += 15; reasons.append(f"Micro-cap ${mcap/1e6:.0f}M — high upside potential")
        elif mcap < 2_000_000_000:  # small cap < $2B
            score += 10; reasons.append(f"Small-cap ${mcap/1e6:.0f}M — moonshot range")
        elif mcap < 10_000_000_000:  # mid cap $2B–$10B
            score -= 10; reasons.append(f"Mid-cap ${mcap/1e9:.1f}B — harder to 10x")

    # ── Fundamentals first: must have real growth (the catalyst) ─────────────
    rev_g  = info.get("revenue_growth") or 0
    earn_g = info.get("earnings_growth") or 0

    if rev_g >= 0.30:
        score += 25; reasons.append(f"Revenue +{rev_g*100:.0f}% YoY — hypergrowth")
    elif rev_g >= 0.15:
        score += 15; reasons.append(f"Revenue +{rev_g*100:.0f}% YoY")
    elif rev_g >= 0.05:
        score += 5
    else:
        return None  # no growth = not a moonshot

    if earn_g >= 0.25:
        score += 15; reasons.append(f"Earnings +{earn_g*100:.0f}% YoY")
    elif earn_g >= 0.10:
        score += 8;  reasons.append(f"Earnings +{earn_g*100:.0f}% YoY")

    # ── Technical setup: early stage, not already extended ───────────────────
    try:
        df = get_price_history(sym, period="6mo")
        if df.empty:
            return None
        latest = df.iloc[-1]

        sma20  = latest.get("sma20")
        sma50  = latest.get("sma50")
        sma200 = latest.get("sma200")
        rsi    = latest.get("rsi")
        macd   = latest.get("macd")
        sig    = latest.get("macd_signal")

        # Price above SMA20 and SMA50 (short-term recovery underway)
        # but NOT required to be above SMA200 — still early stage
        above_sma20 = sma20  and str(sma20)  != "nan" and price > float(sma20)
        above_sma50 = sma50  and str(sma50)  != "nan" and price > float(sma50)
        above_sma200 = sma200 and str(sma200) != "nan" and price > float(sma200)

        if above_sma20 and above_sma50:
            if above_sma200:
                score += 12; reasons.append("Above SMA20/50/200 — uptrend building")
            else:
                score += 18; reasons.append("Above SMA20 & SMA50, reclaiming SMA200 — early launch")
        elif above_sma50:
            score += 8
        else:
            return None  # no momentum at all

        # RSI: building momentum but not overbought (40-65 sweet spot)
        if rsi and str(rsi) != "nan":
            rsi_f = float(rsi)
            if 45 <= rsi_f <= 65:
                score += 15; reasons.append(f"RSI {rsi_f:.0f} — building momentum, room to run")
            elif 35 <= rsi_f < 45:
                score += 8;  reasons.append(f"RSI {rsi_f:.0f} — recovering, early signal")
            elif rsi_f > 75:
                score -= 10; reasons.append(f"RSI {rsi_f:.0f} — already extended")
            elif rsi_f < 35:
                return None  # still in downtrend

        # MACD: bullish crossover = timing signal
        if macd and sig and str(macd) != "nan" and str(sig) != "nan":
            if float(macd) > float(sig):
                score += 12; reasons.append("MACD bullish crossover — momentum turning")
            else:
                score -= 5

    except Exception:
        return None

    # ── 52-week positioning: should have room left to run ────────────────────
    low52  = info.get("52w_low")
    high52 = info.get("52w_high")
    if low52 and high52 and high52 > low52:
        pct_from_low = (price - low52) / (high52 - low52)
        if 0.20 <= pct_from_low <= 0.55:
            score += 15; reasons.append(f"{pct_from_low*100:.0f}% off 52w low — bouncing with room to run")
        elif pct_from_low < 0.20:
            score += 8;  reasons.append("Near 52w low — contrarian setup")
        elif pct_from_low > 0.80:
            score -= 10; reasons.append("Near 52w high — already ran")

    # ── Volume: accumulation signal ───────────────────────────────────────────
    vol    = info.get("volume")
    avgvol = info.get("avg_volume")
    if vol and avgvol and avgvol > 0:
        ratio = vol / avgvol
        if ratio >= 2.0:
            score += 12; reasons.append(f"Volume {ratio:.1f}x average — accumulation")
        elif ratio >= 1.5:
            score += 6;  reasons.append(f"Volume {ratio:.1f}x average")

    # ── Analyst target: significant upside still priced in ───────────────────
    target = info.get("analyst_target")
    if target and price > 0:
        upside = (target - price) / price
        if upside >= 0.30:
            score += 12; reasons.append(f"Analyst target +{upside*100:.0f}% upside")
        elif upside >= 0.15:
            score += 6;  reasons.append(f"Analyst target +{upside*100:.0f}% upside")
        elif upside < 0:
            score -= 5

    if score < 45:
        return None

    return {
        "symbol": sym, "name": info.get("name", ""), "price": price,
        "score": score, "grade": "A" if score >= 70 else "B",
        "recommendation": "BUY" if score >= 70 else "WATCH",
        "reasons": reasons[:4],
        "pe_ratio": info.get("pe_ratio"), "analyst_target": info.get("analyst_target"),
        "sector": info.get("sector", ""),
        "revenue_growth": rev_g, "earnings_growth": earn_g,
        "market_cap": mcap,
    }


_SCREENERS: dict[str, tuple[Any, int, str]] = {
    # name → (fn, min_score, description)
    "undervalued":   (_screen_undervalued,   30, "Low P/E & P/B with analyst upside"),
    "momentum":      (_screen_momentum,      35, "Strong technicals & analyst consensus"),
    "growth":        (_screen_growth,        35, "High revenue & earnings growth"),
    "dividend":      (_screen_dividend,       1, "Dividend yield ≥2% with healthy fundamentals"),
    "beaten_down":   (_screen_beaten_down,   25, "Near 52-week low — contrarian reversal"),
    "green_energy":  (_screen_green_energy,  15, "Solar, wind, storage & clean utilities"),
    "moonshots":     (_screen_moonshots,     45, "Breakout momentum — full uptrend + growth surge"),
}

# Sector aliases → map to a sector filter on top of the undervalued screener
_SECTORS = {
    "tech":        "Technology",
    "technology":  "Technology",
    "finance":     "Financial Services",
    "healthcare":  "Healthcare",
    "energy":      "Energy",
    "industrials": "Industrials",
    "consumer":    "Consumer Cyclical",
    "utilities":   "Utilities",
    "realestate":  "Real Estate",
    "materials":   "Materials",
    "telecom":     "Communication Services",
}


def _screener_from_volume_cache(screen_name: str, cached: dict) -> dict | None:
    """
    Apply screener filter to a cached score_stock() result.
    No yfinance calls — uses pre-computed subscores and stored fundamentals.
    """
    sym    = cached.get("symbol", "")
    price  = cached.get("price") or 0
    name   = cached.get("name", "")
    sector = cached.get("sector", "")

    bd       = cached.get("breakdown", {})
    v_score  = bd.get("value",     {}).get("score", 0)
    v_rsns   = bd.get("value",     {}).get("reasons", [])
    t_score  = bd.get("technical", {}).get("score", 0)
    t_rsns   = bd.get("technical", {}).get("reasons", [])
    a_score  = bd.get("analyst",   {}).get("score", 0)
    a_rsns   = bd.get("analyst",   {}).get("reasons", [])

    pe_ratio      = cached.get("pe_ratio")
    analyst_target = cached.get("analyst_target")
    low52  = cached.get("52w_low")
    high52 = cached.get("52w_high")

    rev_g   = cached.get("revenue_growth")  or 0
    earn_g  = cached.get("earnings_growth") or 0
    margin  = cached.get("profit_margin")   or 0
    roe     = cached.get("roe")             or 0
    div_yld = cached.get("dividend_yield")  or 0
    de      = cached.get("debt_equity")     or 999

    base = {
        "symbol": sym, "name": name, "price": price,
        "pe_ratio": pe_ratio, "analyst_target": analyst_target, "sector": sector,
    }

    if screen_name == "undervalued":
        total = v_score + a_score
        if total < 30:
            return None
        return {**base, "score": total,
                "grade": "A" if total >= 55 else "B",
                "recommendation": "BUY" if total >= 55 else "WATCH",
                "reasons": (v_rsns + a_rsns)[:4]}

    if screen_name == "momentum":
        total = t_score + a_score
        if total < 35:
            return None
        return {**base, "score": total,
                "grade": "A" if total >= 50 else "B",
                "recommendation": "BUY" if total >= 50 else "WATCH",
                "reasons": (t_rsns + a_rsns)[:4]}

    if screen_name == "growth":
        score = 0; reasons = []
        if rev_g > 0.20:   score += 25; reasons.append(f"Revenue +{rev_g*100:.0f}% YoY")
        elif rev_g > 0.10: score += 15; reasons.append(f"Revenue +{rev_g*100:.0f}% YoY")
        if earn_g > 0.25:  score += 20; reasons.append(f"Earnings +{earn_g*100:.0f}% YoY")
        elif earn_g > 0.10: score += 10
        if margin > 0.20:  score += 15; reasons.append(f"Margin {margin*100:.0f}%")
        elif margin > 0.10: score += 8
        if roe > 0.20:     score += 10; reasons.append(f"ROE {roe*100:.0f}%")
        score += a_score // 2
        reasons += a_rsns[:1]
        if score < 35:
            return None
        return {**base, "score": score,
                "grade": "A" if score >= 55 else "B",
                "recommendation": "BUY" if score >= 55 else "WATCH",
                "reasons": reasons[:4],
                "revenue_growth": rev_g, "earnings_growth": earn_g}

    if screen_name == "dividend":
        if div_yld < 0.02:
            return None
        score = 0; reasons = []
        if div_yld > 0.05:   score += 30; reasons.append(f"Dividend yield {div_yld*100:.1f}%")
        elif div_yld > 0.03: score += 20; reasons.append(f"Dividend yield {div_yld*100:.1f}%")
        else:                 score += 10; reasons.append(f"Dividend yield {div_yld*100:.1f}%")
        if de < 1.0:    score += 10; reasons.append(f"Conservative leverage ({de:.1f})")
        if margin > 0.10: score += 10; reasons.append(f"Healthy margins {margin*100:.0f}%")
        score += v_score // 3
        reasons += v_rsns[:1]
        return {**base, "score": score,
                "grade": "A" if score >= 40 else "B",
                "recommendation": "BUY" if score >= 40 else "WATCH",
                "reasons": reasons[:4], "dividend_yield": div_yld}

    if screen_name == "beaten_down":
        if not (price and low52 and high52 and high52 > low52):
            return None
        pct_from_low = (price - low52) / (high52 - low52)
        if pct_from_low > 0.30:
            return None
        score = int((1 - pct_from_low) * 30) + v_score // 2 + a_score // 2
        if score < 25:
            return None
        return {**base, "score": score,
                "grade": "A" if score >= 45 else "B",
                "recommendation": "WATCH",
                "reasons": [f"{pct_from_low*100:.0f}% off 52w low (low=${low52:.2f})", *(v_rsns + a_rsns)[:2]],
                "pct_from_52w_low": round(pct_from_low * 100, 1)}

    if screen_name == "green_energy":
        if sym not in _GREEN_ENERGY_SYMBOLS:
            return None
        score = v_score + t_score // 2 + a_score
        if score < 15:
            return None
        return {**base, "score": score,
                "grade": "A" if score >= 55 else "B",
                "recommendation": "BUY" if score >= 55 else "WATCH",
                "reasons": (v_rsns + a_rsns + t_rsns)[:4]}

    if screen_name == "moonshots":
        if sym in _LARGE_CAP_BLOCKLIST:
            return None
        if rev_g < 0.05:
            return None
        score = 0; reasons = []
        if rev_g >= 0.30:   score += 25; reasons.append(f"Revenue +{rev_g*100:.0f}% YoY — hypergrowth")
        elif rev_g >= 0.15: score += 15; reasons.append(f"Revenue +{rev_g*100:.0f}% YoY")
        elif rev_g >= 0.05: score += 5
        if earn_g >= 0.25:  score += 15; reasons.append(f"Earnings +{earn_g*100:.0f}% YoY")
        elif earn_g >= 0.10: score += 8; reasons.append(f"Earnings +{earn_g*100:.0f}% YoY")
        score += t_score // 2
        reasons += t_rsns[:2]
        if low52 and high52 and high52 > low52:
            pct_from_low = (price - low52) / (high52 - low52)
            if 0.20 <= pct_from_low <= 0.55:
                score += 15; reasons.append(f"{pct_from_low*100:.0f}% off 52w low — room to run")
            elif pct_from_low < 0.20:
                score += 8
            elif pct_from_low > 0.80:
                score -= 10
        if analyst_target and price > 0:
            upside = (analyst_target - price) / price
            if upside >= 0.30:  score += 12; reasons.append(f"Analyst target +{upside*100:.0f}% upside")
            elif upside >= 0.15: score += 6; reasons.append(f"Analyst target +{upside*100:.0f}% upside")
            elif upside < 0:    score -= 5
        if score < 45:
            return None
        return {**base, "score": score,
                "grade": "A" if score >= 70 else "B",
                "recommendation": "BUY" if score >= 70 else "WATCH",
                "reasons": reasons[:4],
                "revenue_growth": rev_g, "earnings_growth": earn_g}

    return None


def run_screen(name: str, top_n: int = 15) -> dict:
    """
    Run a named screener using volume-analysis cached data only — no yfinance calls.
    Falls back to empty results if no cached data available yet.
    name: undervalued | momentum | growth | dividend | beaten_down | green_energy | moonshots
          OR sector:<name>  e.g. sector:tech
    Returns {screen, description, results, universe_size, shortlist_size}.
    """
    sector_filter = None

    if name.startswith("sector:"):
        sector_key = name.split(":", 1)[1].lower().replace(" ", "")
        sector_filter = _SECTORS.get(sector_key, sector_key.title())
        screen_name = "undervalued"
        min_score   = 20
        desc        = f"Undervalued — {sector_filter} sector"
    elif name in _SCREENERS:
        _, min_score, desc = _SCREENERS[name]
        screen_name = name
    else:
        screen_name = "undervalued"
        min_score   = 30
        desc        = "Undervalued"

    # Load all volume-analysis cached scores (scored by background job — no yfinance here)
    volume_cache = _portfolio.get_all_volume_scores()  # {symbol: scores_dict}

    if name == "green_energy":
        candidates = {s: volume_cache[s] for s in _GREEN_ENERGY_SYMBOLS if s in volume_cache}
        raw_universe_size = len(_GREEN_ENERGY_SYMBOLS)
    else:
        candidates = volume_cache
        raw_universe_size = len(volume_cache)

    import random as _random
    syms = list(candidates.keys())
    _random.shuffle(syms)

    results = []
    for sym in syms:
        try:
            row = _screener_from_volume_cache(screen_name, candidates[sym])
            if row is None or row.get("score", 0) < min_score:
                continue
            if sector_filter and row.get("sector", "").lower() != sector_filter.lower():
                continue
            results.append(row)
        except Exception:
            continue

    results.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Enforce sector diversity for broad screens
    diversified: list[dict] = []
    if not sector_filter:
        sector_cap = max(2, top_n // 3)
        sector_counts: dict[str, int] = {}
        for row in results:
            sec = row.get("sector") or "Unknown"
            if sector_counts.get(sec, 0) < sector_cap:
                diversified.append(row)
                sector_counts[sec] = sector_counts.get(sec, 0) + 1
            if len(diversified) >= top_n:
                break
        if len(diversified) < top_n:
            seen = {r["symbol"] for r in diversified}
            for row in results:
                if row["symbol"] not in seen:
                    diversified.append(row)
                if len(diversified) >= top_n:
                    break
    else:
        diversified = results[:top_n]

    return {
        "screen":         name,
        "description":    desc,
        "universe_size":  raw_universe_size,
        "shortlist_size": len(candidates),
        "results":        diversified,
    }


# ── Recommendations ───────────────────────────────────────────────────────────

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
