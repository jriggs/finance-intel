"""
Per-stock signal scoring: value, technical, analyst, macro and sentiment
dimensions, the composite short/long-term scores, and the score_stock /
score_watchlist entry points.
"""
from __future__ import annotations

from typing import Any

import fundamentals as _fd
from market import get_price_history, get_stock_info

from .constants import (
    ANALYST_MAX,
    GRADE_A_MIN,
    GRADE_B_MIN,
    GRADE_C_MIN,
    REC_BUY_MIN,
    REC_HOLD_MIN,
    REC_WATCH_MIN,
    TECHNICAL_MAX,
    VALUE_MAX,
)

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

# ── Pre-entry risk metrics (liquidity & volatility) ───────────────────────────

_TRADING_DAYS = 252
_RISK_WINDOW  = 60          # trailing sessions used for vol / dollar-volume


def annualized_volatility(df, window: int = _RISK_WINDOW) -> float | None:
    """
    Annualized volatility of daily returns over the trailing `window` sessions.
    Returns e.g. 0.55 for 55%; None when there isn't enough price history.
    """
    try:
        closes = df["close"].dropna()
    except Exception:
        return None
    if len(closes) < 20:
        return None
    rets = closes.pct_change().dropna().tail(window)
    if len(rets) < 10:
        return None
    # Winsorize daily returns: a single bad tick / near-zero print (common in
    # thin names) shouldn't let one day dominate and produce an absurd annualized
    # figure. ±50% bounds each day's leverage while still reading genuinely wild.
    rets = rets.clip(-0.5, 0.5)
    return round(float(rets.std()) * (_TRADING_DAYS ** 0.5), 4)


def median_dollar_volume(df, info: dict | None = None, window: int = _RISK_WINDOW) -> float | None:
    """
    Median daily dollar volume (close × volume) over the trailing `window`
    sessions. Falls back to price × avg_volume from `info` when the OHLCV frame
    is unavailable; None when neither source has data.
    """
    try:
        if df is not None and "close" in df.columns and "volume" in df.columns:
            dv = (df["close"] * df["volume"]).dropna().tail(window)
            if len(dv) >= 10:
                return round(float(dv.median()), 2)
    except Exception:
        pass
    if info:
        price, avg_vol = info.get("price"), info.get("avg_volume")
        if price and avg_vol:
            return round(float(price) * float(avg_vol), 2)
    return None


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

    # Pre-entry risk metrics consumed by the daily-trade filters.
    volatility    = annualized_volatility(df_6mo)
    dollar_volume = median_dollar_volume(df_6mo, info)

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
        "volatility":       volatility,     # trailing-60d annualized, e.g. 0.55
        "dollar_volume":    dollar_volume,  # median 60d close×volume
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
