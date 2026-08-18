"""
Named stock screeners (undervalued, momentum, growth, dividend, beaten-down,
green-energy, moonshots) plus the volume-cache-backed run_screen entry point.
"""
from __future__ import annotations

from typing import Any

import db as _portfolio
from market import get_fast_market_cap, get_price_history, get_stock_info

from .constants import _LARGE_CAP_BLOCKLIST
from .scoring import _analyst_score, _technical_score, _value_score

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
        "volatility": cached.get("volatility"), "dollar_volume": cached.get("dollar_volume"),
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
