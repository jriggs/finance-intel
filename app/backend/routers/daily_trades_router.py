"""
Daily automated trades — pick 10 stocks from signal/screener/volume data.
Sell review runs first: flag positions 30+ days old that show AVOID or weak LT/sentiment.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time as dt_time, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import logging

import db as portfolio
import market

logger = logging.getLogger("daily_trades")

# ── Constants ─────────────────────────────────────────────────────────────────

_DAILY_BUDGET    = 10.00
_HOLD_DAYS       = 30

# Pool sizes: how many picks come from each source
_POOL_VOLUME     = 4   # top N by volume analysis overall_score
_POOL_SCREENER   = 4   # top N by screener score (no volume overlap)
_POOL_OVERALL    = 2   # top N by aggregate from remaining universe

# Volume analysis candidate pool — only the top-ranked stocks compete for Pool 1
_VOL_POOL_LIMIT  = 500

# Sell-review thresholds
_SELL_THRESHOLD  = 30.0   # composite below this → flag for sell
_LT_WEIGHT       = 0.6    # long-term score weight in composite
_SE_WEIGHT       = 0.4    # sentiment score weight in composite
_DEFAULT_SCORE   = 50.0   # neutral fallback when one component is missing

# Allocation weighting: only score above this floor drives position sizing.
# Stocks at or below the floor receive zero weight (falls back to equal-weight
# if every pick scores ≤ floor, which shouldn't happen in practice).
_ALLOC_FLOOR = 50.0

# Screener imputation: when a candidate has no screener score, substitute this
# value so it is penalised against candidates with real (possibly mediocre)
# screener data.  Computed at run-time as the median of actual screener scores;
# this constant is only the fallback for edge cases where sc_map is empty.
_SCREENER_MISSING_DEFAULT = 60.0

# Minimum dollar allocation.  Picks that would receive less than this after
# weighting are evicted from the book (they're rounding errors, not positions).
_MIN_ALLOCATION = 0.50

# ── Pre-entry risk filters (backtest-driven) ──────────────────────────────────
# Ordered by measured impact on forward return. Trailing volatility is the one
# variable with real explanatory power (r ≈ −0.34); the score itself has ~zero
# correlation with return, so it gates (in/out) rather than sizing positions.
_MAX_VOLATILITY    = 0.55         # reject trailing-60d annualized vol above this
_MIN_DOLLAR_VOLUME = 5_000_000    # require median 60d dollar volume ≥ $5M
_SCORE_MIN         = 0.0          # declared score range; out-of-bounds = bug, not a trade
_SCORE_MAX         = 100.0
_MAX_POSITION_PCT  = 0.15         # cap any single position at 15% of the budget
_STOP_LOSS_PCT     = -20.0        # hard exit once a position is down ≥ 20%

# Persistence in the rankings has been an inverse signal (repeat picks
# underperformed), so fade a name's selection score by this factor per recent
# appearance. Sizing is equal-weight, so repeats can no longer compound weight.
_APPEARANCE_DECAY    = 0.85
_APPEARANCE_LOOKBACK = 5          # sessions to scan for repeat appearances

# Industry dedup: max picks allowed per GICS industry (or sector if industry unknown).
# Populated from yfinance via score_stock() → volume_analysis_results.sector/industry.
_INDUSTRY_MAX = 1

# Buffer multiplier: each pool collects this many times its target size so the
# per-pool dedup pass has headroom without breaking the hard pool caps.
_POOL_BUFFER = 2

# Macro-correlation overrides — pairs that GICS classifies under *different* industries
# but move on the same macro variable.  Keep small; only add when you observe a
# confirmed recurring pair that the GICS dedup misses.
_MACRO_GROUPS: tuple[frozenset[str], ...] = (
    # Rate/housing-sensitive: all benefit or suffer together on mortgage-rate headlines
    frozenset({"ANGI", "TREE", "LDI", "UWMC", "PFSI", "RKT", "HMPT"}),
    # Chinese ADRs: correlated by regulatory/geopolitical risk irrespective of sector
    frozenset({"TME", "FUTU", "BABA", "BIDU", "JD", "PDD", "BILI", "NIO", "XPEV", "LI", "IQ"}),
)

# Data freshness
_DATA_STALE_HOURS = 26    # warn if volume or screener cache is older than this

# Market calendar
_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = dt_time(9, 30)
_MARKET_CLOSE = dt_time(16, 0)

# NYSE holidays (ISO date strings). Add the next year each December.
_NYSE_HOLIDAYS: frozenset[str] = frozenset([
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
])

# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter()


# ── Market calendar helpers ───────────────────────────────────────────────────

def _is_trading_day(d: date | None = None) -> bool:
    """True if d (default: today ET) is a NYSE trading day."""
    d = d or datetime.now(_ET).date()
    return d.weekday() < 5 and d.isoformat() not in _NYSE_HOLIDAYS


def _is_market_open() -> bool:
    """True only during NYSE trading hours (9:30–16:00 ET) on a trading day."""
    now_et = datetime.now(_ET)
    return _is_trading_day(now_et.date()) and _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE


# ── Data-source builders ──────────────────────────────────────────────────────

def _build_signal_map() -> dict[str, dict]:
    """Watchlist signal scores from crawl cache → {symbol: {signal_score, recommendation, lt_score, se_score}}."""
    raw = portfolio.get_crawl_results("signal_scores")
    items: list[dict] = raw if isinstance(raw, list) else (raw.get("items") or [])
    out: dict[str, dict] = {}
    for s in items:
        sym = (s.get("symbol") or "").upper()
        if not sym:
            continue
        out[sym] = {
            "signal_score":   s.get("score"),
            "recommendation": s.get("recommendation"),
            "lt_score":       (s.get("long_term")  or {}).get("score"),
            "se_score":       (s.get("sentiment")  or {}).get("score"),
        }
    return out


def _build_screener_map() -> dict[str, float]:
    """Max score per symbol across all cached screener results."""
    all_sc = portfolio.get_all_screener_results()
    out: dict[str, float] = {}
    for _name, sc in all_sc.items():
        for r in (sc.get("data", {}).get("results") or []):
            sym   = (r.get("symbol") or "").upper()
            score = float(r.get("score") or 0)
            if sym and score > out.get(sym, 0.0):
                out[sym] = score
    return out


def _build_volume_data() -> tuple[dict[str, dict], dict[str, float]]:
    """
    Single DB read → (vol_map, rec_map).

    vol_map: top _VOL_POOL_LIMIT symbols by overall_score for Pool 1 selection.
    rec_map: every scored symbol → overall_score, used as signal_score fallback.
    """
    rows = portfolio.get_all_volume_results()
    vol_map: dict[str, dict] = {}
    rec_map: dict[str, float] = {}
    for i, v in enumerate(rows):
        sym = (v.get("symbol") or "").upper()
        if not sym:
            continue
        score = v.get("overall_score")
        if score is not None:
            rec_map[sym] = float(score)
        if i < _VOL_POOL_LIMIT:
            vol_map[sym] = {
                "volume_score":   score,
                "recommendation": v.get("recommendation"),
                "lt_score":       v.get("long_term_score"),
                "se_score":       v.get("sentiment_score"),
                "sector":         v.get("sector"),
                "industry":       v.get("industry"),
            }
    return vol_map, rec_map


def _build_sentiment_map() -> dict[str, float]:
    """Per-symbol sentiment_score from volume_analysis_results (display only, not scored)."""
    return portfolio.get_all_volume_sentiments()


# ── Candidate construction ────────────────────────────────────────────────────

def _make_candidate(
    sym: str,
    source: str,
    sig_map: dict,
    sc_map: dict,
    vol_map: dict,
    sent_map: dict,
    rec_map: dict,
    score_map: dict,
    sc_impute: float = _SCREENER_MISSING_DEFAULT,
) -> dict:
    sig  = sig_map.get(sym, {})
    sc   = sc_map.get(sym)
    vol  = vol_map.get(sym, {})
    sent = sent_map.get(sym)
    full = score_map.get(sym) or {}   # full score_stock output → volatility / dollar_volume

    # signal_score: watchlist scorer preferred; fall back to volume_analysis overall_score
    watchlist_sig = sig.get("signal_score")
    sig_score = watchlist_sig if watchlist_sig is not None else rec_map.get(sym)

    # Aggregate = mean(fundamental, screener_effective).
    # "Fundamental" is the volume-analysis overall_score (value+technical+analyst),
    # sourced from the watchlist scorer when available (fresher), else rec_map (all ~10k
    # stocks).  Using rec_map here instead of vol_map["volume_score"] ensures screener-
    # only picks (outside the top-500 vol pool) still get their fundamental score blended
    # in rather than the aggregate collapsing to the screener score alone.
    # Sentiment excluded: generic market crawl (~99% of stocks share the same value).
    #
    # Missing-screener imputation: when sc is None, substitute sc_impute (the
    # median of actual screener scores for this run) so that stocks without
    # screener coverage are penalised relative to stocks with a real score.
    # Without this, a stock with Signal=81 and no screener gets agg=81, beating
    # a stock with Signal=95 and Screener=60 whose agg is only (95+60)/2=77.5.
    fundamental = watchlist_sig if watchlist_sig is not None else rec_map.get(sym)
    sc_effective = float(sc) if sc is not None else (sc_impute if fundamental is not None else None)
    scores: list[float] = []
    if fundamental is not None:
        scores.append(float(fundamental))
    if sc_effective is not None:
        scores.append(sc_effective)
    agg = round(sum(scores) / len(scores), 2) if scores else 0.0

    return {
        "symbol":          sym,
        "signal_score":    sig_score,
        "screener_score":  sc,
        "volume_score":    vol.get("volume_score"),
        "sentiment_score": sent,
        "aggregate_score": agg,
        "source":          source,
        "recommendation":  sig.get("recommendation") or vol.get("recommendation"),
        "lt_score":        sig.get("lt_score") if sig.get("lt_score") is not None else vol.get("lt_score"),
        "se_score":        sig.get("se_score") if sig.get("se_score") is not None else vol.get("se_score"),
        "sector":          vol.get("sector"),
        "industry":        vol.get("industry"),
        "volatility":      full.get("volatility"),      # trailing-60d annualized
        "dollar_volume":   full.get("dollar_volume"),   # median 60d close×volume
    }


# ── Pick selection ────────────────────────────────────────────────────────────

def _constraint_keys(candidate: dict) -> list[str]:
    """
    Return all dedup constraint keys for a candidate.

    Two keys are checked per candidate:
      1. GICS industry (or sector if industry unknown) — catches same-industry pairs
         like gold miners, ad-tech, insurance, etc.
      2. Macro group index — catches cross-GICS pairs that share a macro driver
         (e.g. ANGI/TREE are Communication Services vs Financial Services but
         both live and die on mortgage-rate headlines).

    A candidate is blocked if ANY of its keys is already at capacity.
    """
    keys: list[str] = []

    gics = candidate.get("industry") or candidate.get("sector")
    if gics:
        keys.append(f"gics:{gics}")

    sym = candidate["symbol"]
    for i, group in enumerate(_MACRO_GROUPS):
        if sym in group:
            keys.append(f"macro:{i}")
            break  # symbol belongs to at most one macro group

    return keys


def _take_from_pool(
    candidates: list[dict],
    target: int,
    counts: dict[str, int],
) -> list[dict]:
    """
    Greedy pick: iterate candidates in score order, skip any whose constraint
    keys are full, stop when `target` accepted or list exhausted.
    Writes accepted keys into the shared `counts` dict so constraints are
    enforced across all subsequent pool calls in the same session.
    """
    result: list[dict] = []
    for c in candidates:
        if len(result) >= target:
            break
        keys = _constraint_keys(c)
        if all(counts.get(k, 0) < _INDUSTRY_MAX for k in keys):
            result.append(c)
            for k in keys:
                counts[k] = counts.get(k, 0) + 1
    return result


# ── Pre-entry filters & equal-weight sizing (backtest-driven) ─────────────────

def _entry_rejection(candidate: dict) -> str | None:
    """
    Reason a candidate fails a pre-entry gate, or None if it clears them all.

    Volatility and liquidity fail *closed*: a name whose risk metrics aren't in
    the volume cache yet (never scored, or mid-rescore — the worker mutates the
    cache continuously) can't be confirmed safe, so it is skipped rather than
    traded blind. The picker simply draws the next clean name from the pool, so
    this is deterministic regardless of worker timing. An out-of-range score is
    a bug and is always rejected.
    """
    agg = candidate.get("aggregate_score")
    if agg is None or not (_SCORE_MIN <= agg <= _SCORE_MAX):
        return f"score {agg} outside declared range [{_SCORE_MIN:.0f}, {_SCORE_MAX:.0f}]"

    vol = candidate.get("volatility")
    if vol is None:
        return "volatility unavailable (not yet scored)"
    if vol > _MAX_VOLATILITY:
        return f"volatility {vol:.0%} > {_MAX_VOLATILITY:.0%} ceiling"

    dv = candidate.get("dollar_volume")
    if dv is None:
        return "liquidity unavailable (not yet scored)"
    if dv < _MIN_DOLLAR_VOLUME:
        return f"liquidity ${dv:,.0f}/day < ${_MIN_DOLLAR_VOLUME:,.0f} floor"

    return None


def _appearance_counts(sessions: list[dict]) -> dict[str, int]:
    """How many of the recent sessions each symbol appeared in."""
    counts: dict[str, int] = {}
    for s in sessions:
        for sym in {(p.get("symbol") or "").upper() for p in s.get("positions", [])}:
            if sym:
                counts[sym] = counts.get(sym, 0) + 1
    return counts


def _decay_factor(appearances: int) -> float:
    """Selection-score multiplier that fades repeat picks (see _APPEARANCE_DECAY)."""
    return _APPEARANCE_DECAY ** max(0, appearances)


def _allocate(picks: list[dict]) -> None:
    """
    Equal-weight sizing with a per-position cap (in-place).

    Score-to-return correlation is ~0, so the score gates entry (in/out) and
    every survivor is sized equally rather than by score — equal-weight beat
    score-weighting across the backtest. Each name is then capped at
    _MAX_POSITION_PCT of the budget as a bug guard that bounds the damage if a
    bad score ever slips past the sanity gate.
    """
    n = len(picks)
    if n == 0:
        return
    equal = _DAILY_BUDGET / n
    cap   = _DAILY_BUDGET * _MAX_POSITION_PCT
    for i, c in enumerate(picks):
        c["allocation_usd"] = round(min(equal, cap), 4)
        c["rank"] = i + 1


def _compute_picks() -> list[dict]:
    """
    Select today's picks from three independent source pools (volume / screener /
    aggregate), apply the backtest-driven pre-entry gates, and size equal-weight.

    Pre-entry gates, highest measured impact first:
      1. volatility ceiling — reject trailing-60d annualized vol > _MAX_VOLATILITY
      2. liquidity floor     — require median 60d dollar volume ≥ _MIN_DOLLAR_VOLUME
      3. score sanity        — drop (never trade) any out-of-declared-range score
    Repeat appearances fade a name's selection score (_APPEARANCE_DECAY — an
    inverse signal in the backtest). Sizing is equal-weight with a
    _MAX_POSITION_PCT cap: the score gates entry, it does not size the position.

    Pool caps (4/4/2) and the industry/macro dedup are unchanged; the filters
    reduce the book deliberately — a tighter, cleaner book beats a padded one.
    """
    sig_map          = _build_signal_map()
    sc_map           = _build_screener_map()
    vol_map, rec_map = _build_volume_data()
    sent_map         = _build_sentiment_map()
    score_map        = portfolio.get_all_volume_scores()   # volatility / dollar_volume live here

    appears = _appearance_counts(portfolio.get_daily_history(_APPEARANCE_LOOKBACK))

    sc_vals   = list(sc_map.values())
    sc_median = float(median(sc_vals)) if sc_vals else _SCREENER_MISSING_DEFAULT

    def _candidate(sym: str, source: str) -> dict:
        return _make_candidate(sym, source, sig_map, sc_map, vol_map, sent_map, rec_map, score_map, sc_median)

    def _eligible(c: dict) -> bool:
        reason = _entry_rejection(c)
        if reason is None:
            return True
        if "declared range" in reason:
            logger.warning("Daily-trades: DROPPED %s — %s (bug, not traded)", c["symbol"], reason)
        else:
            logger.debug("Daily-trades: filtered %s — %s", c["symbol"], reason)
        return False

    def _sel(base, sym: str) -> float:
        """Ranking key: raw metric faded by repeat-appearance decay."""
        return float(base or 0.0) * _decay_factor(appears.get(sym, 0))

    def _fill(sorted_syms, source: str, buffer: int) -> list[dict]:
        """Build candidates down the ranking, keeping the first `buffer` that pass the gates."""
        out: list[dict] = []
        for s in sorted_syms:
            c = _candidate(s, source)
            if _eligible(c):
                out.append(c)
                if len(out) >= buffer:
                    break
        return out

    # Shared constraint state — written by each pool, read by the next.
    counts: dict[str, int] = {}

    # ── Pool 1: volume analysis score ────────────────────────────────────────
    vol_pool = sorted(
        [s for s in vol_map if vol_map[s].get("volume_score") is not None],
        key=lambda s: _sel(vol_map[s]["volume_score"], s),
        reverse=True,
    )
    chosen_vol  = _take_from_pool(_fill(vol_pool, "volume", _POOL_VOLUME * _POOL_BUFFER), _POOL_VOLUME, counts)
    chosen_syms = {c["symbol"] for c in chosen_vol}

    # ── Pool 2: screener score, no overlap with Pool 1 ───────────────────────
    sc_pool = sorted(
        [s for s in sc_map if s not in chosen_syms],
        key=lambda s: _sel(sc_map[s], s),
        reverse=True,
    )
    chosen_sc = _take_from_pool(_fill(sc_pool, "screener", _POOL_SCREENER * _POOL_BUFFER), _POOL_SCREENER, counts)
    chosen_syms |= {c["symbol"] for c in chosen_sc}

    # ── Pool 3: best aggregate from remaining symbols ─────────────────────────
    all_syms = set(sc_map) | set(vol_map)
    overall = [
        c for c in (
            _candidate(s, "overall")
            for s in all_syms
            if s not in chosen_syms
            and (sc_map.get(s) is not None or vol_map.get(s, {}).get("volume_score") is not None)
        )
        if _eligible(c)
    ]
    overall.sort(key=lambda c: _sel(c["aggregate_score"], c["symbol"]), reverse=True)
    chosen_overall = _take_from_pool(overall[: _POOL_OVERALL * _POOL_BUFFER], _POOL_OVERALL, counts)

    chosen = chosen_vol + chosen_sc + chosen_overall
    if not chosen:
        return []

    # Rank by aggregate for display; size equally with a per-name cap.
    chosen.sort(key=lambda c: c["aggregate_score"], reverse=True)
    _allocate(chosen)
    return chosen


# ── Sell-review logic ─────────────────────────────────────────────────────────

def _sell_reason(sym: str, sig_map: dict, vol_map: dict) -> str | None:
    """Return a sell reason if the position should be flagged, else None."""
    sig = sig_map.get(sym, {})
    vol = vol_map.get(sym, {})

    recommendation = sig.get("recommendation") or vol.get("recommendation")
    if recommendation == "AVOID":
        return "AVOID signal"

    lt = sig.get("lt_score") if sig.get("lt_score") is not None else vol.get("lt_score")
    se = sig.get("se_score") if sig.get("se_score") is not None else vol.get("se_score")

    if lt is not None or se is not None:
        lt_val    = float(lt) if lt is not None else _DEFAULT_SCORE
        se_val    = float(se) if se is not None else _DEFAULT_SCORE
        composite = lt_val * _LT_WEIGHT + se_val * _SE_WEIGHT
        if composite < _SELL_THRESHOLD:
            return f"Weak LT/sentiment composite ({composite:.0f})"

    return None


def _stop_loss_flags(positions: list[dict]) -> dict[str, dict]:
    """
    Flag open positions down ≥ _STOP_LOSS_PCT for a hard exit, keyed by position id.

    A hard stop is the price-responsive backstop for a stale score: names whose
    score never moved while the price fell (e.g. −86%) get cut here regardless of
    how the scorer rates them or how long they've been held.
    """
    flagged: dict[str, dict] = {}
    for p in positions:
        pnl = p.get("pnl_pct")
        if pnl is not None and pnl <= _STOP_LOSS_PCT:
            flagged[p["id"]] = {**p, "suggested_sell_reason": f"Stop loss ({pnl:.0f}%)"}
    return flagged


# ── Data-freshness warnings ───────────────────────────────────────────────────

def _data_freshness_warnings() -> list[str]:
    """Return warning strings if cached volume or screener data is stale."""
    warnings: list[str] = []
    now  = datetime.now(UTC)
    ages = portfolio.get_data_ages()

    vol_ts = ages["volume_last_updated"]
    if vol_ts:
        try:
            age_h = (now - datetime.fromisoformat(vol_ts)).total_seconds() / 3600
            if age_h > _DATA_STALE_HOURS:
                warnings.append(f"Volume analysis data is {age_h:.0f}h old (last updated {vol_ts[:16]})")
        except Exception:
            pass
    else:
        warnings.append("No volume analysis data found — run volume analysis first")

    sc_ts = ages["screener_last_updated"]
    if sc_ts:
        try:
            age_h = (now - datetime.fromisoformat(sc_ts)).total_seconds() / 3600
            if age_h > _DATA_STALE_HOURS:
                warnings.append(f"Screener data is {age_h:.0f}h old (last updated {sc_ts[:16]})")
        except Exception:
            pass
    else:
        warnings.append("No screener data found — run a screener first")

    return warnings


# ── Price refresh ─────────────────────────────────────────────────────────────

async def _refresh_position_prices(positions: list[dict]) -> dict[str, float]:
    """Fetch live prices for a list of positions; return {symbol: price}."""
    syms = list({p["symbol"] for p in positions})
    if not syms:
        return {}
    try:
        quotes = await asyncio.to_thread(market.get_batch_quotes, syms)
        return {q["symbol"]: q["price"] for q in quotes if q.get("price")}
    except Exception:
        return {}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/finance/daily-trades/today")
async def get_today(force: bool = False):
    """
    Return today's picks and sell review.
    Picks are computed once per day from cached data; subsequent calls return the
    saved session unless force=True.
    """
    today = date.today().isoformat()

    session = portfolio.get_daily_session(today)
    if session and not force:
        picks = portfolio.get_daily_positions_for_date(today)
    else:
        picks = await asyncio.to_thread(_compute_picks)
        await asyncio.to_thread(portfolio.save_daily_session, today, picks)
        picks = portfolio.get_daily_positions_for_date(today)

    # ── Sell review: hard −20% stop on ALL open positions (any age), then the
    #    weak long-term/sentiment review of positions held _HOLD_DAYS+ days ────
    all_open = await asyncio.to_thread(portfolio.get_open_daily_positions)
    sell_review: list[dict] = []
    if all_open:
        price_map = await _refresh_position_prices(all_open)
        if price_map:
            await asyncio.to_thread(portfolio.update_daily_position_prices, price_map)
            all_open = await asyncio.to_thread(portfolio.get_open_daily_positions)  # refreshed pnl

        # 1) Hard stop-loss takes priority and applies at any holding age.
        flagged = _stop_loss_flags(all_open)

        # 2) Weak LT/sentiment review for aged positions not already stopped out.
        cutoff     = (datetime.now(UTC) - timedelta(days=_HOLD_DAYS)).date().isoformat()
        sig_map    = await asyncio.to_thread(_build_signal_map)
        vol_map, _ = await asyncio.to_thread(_build_volume_data)
        for pos in all_open:
            if pos["id"] in flagged or pos.get("trade_date", "") > cutoff:
                continue
            reason = _sell_reason(pos["symbol"], sig_map, vol_map)
            if reason:
                flagged[pos["id"]] = {**pos, "suggested_sell_reason": reason}

        sell_review = list(flagged.values())

    # ── Refresh prices for confirmed picks ────────────────────────────────────
    confirmed = [p for p in picks if p.get("status") == "purchased"]
    if confirmed:
        price_map = await _refresh_position_prices(confirmed)
        if price_map:
            await asyncio.to_thread(portfolio.update_daily_position_prices, price_map)
            picks = portfolio.get_daily_positions_for_date(today)

    session       = portfolio.get_daily_session(today)
    data_warnings = await asyncio.to_thread(_data_freshness_warnings)
    return {
        "date":           today,
        "session":        session,
        "picks":          picks,
        "sell_review":    sell_review,
        "budget":         _DAILY_BUDGET,
        "data_warnings":  data_warnings,
        "is_market_open": _is_market_open(),
    }


class ConfirmBody(BaseModel):
    prices: dict[str, float]  # {symbol: entry_price}


@router.post("/api/finance/daily-trades/today/confirm")
async def confirm_today(body: ConfirmBody):
    """Mark today's picks as purchased with user-supplied fill prices."""
    today = date.today().isoformat()
    if not portfolio.get_daily_session(today):
        raise HTTPException(404, "No session for today — call /today first")
    prices = {sym.upper(): price for sym, price in body.prices.items()}
    await asyncio.to_thread(portfolio.confirm_daily_session, today, prices)
    picks = portfolio.get_daily_positions_for_date(today)
    return {"date": today, "status": "confirmed", "picks": picks}


class SellBody(BaseModel):
    exit_price: float | None = None


@router.put("/api/finance/daily-trades/position/{position_id}/sell")
async def sell_position(position_id: str, body: SellBody):
    """Close a position (mark as sold)."""
    await asyncio.to_thread(portfolio.close_daily_position, position_id, body.exit_price)
    return {"status": "sold", "id": position_id}


@router.get("/api/finance/daily-trades/history")
async def get_history(limit: int = 60):
    """Return all sessions newest-first with their positions."""
    sessions = await asyncio.to_thread(portfolio.get_daily_history, limit)

    # Enrich purchased positions with live prices (best-effort)
    all_purchased = [
        p for s in sessions
        for p in s.get("positions", [])
        if p.get("status") == "purchased"
    ]
    if all_purchased:
        try:
            price_map = await _refresh_position_prices(all_purchased)
            if price_map:
                await asyncio.to_thread(portfolio.update_daily_position_prices, price_map)
                sessions = await asyncio.to_thread(portfolio.get_daily_history, limit)
        except Exception:
            pass

    stats = await asyncio.to_thread(portfolio.get_daily_portfolio_stats)
    return {"sessions": sessions, "stats": stats}


@router.get("/api/finance/daily-trades/stats")
async def get_stats():
    return await asyncio.to_thread(portfolio.get_daily_portfolio_stats)
