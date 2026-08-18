"""
Signal, screener, recommendation, and volume-analysis routes.
Mounted into the main finance router via include_router().
No LLM or RAG dependency.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

import db as portfolio
import scheduler  # circular-safe: scheduler imports this module only at call time
import signals

router = APIRouter()

# Candidate pool size for recommendations/screeners. The Signals page's
# pre-entry gate filter (vol/liquidity, mirrors daily_trades_router) fails
# closed — it hides any candidate whose risk metrics haven't been scored yet,
# not just ones that breach the thresholds — so the pool needs real headroom
# to keep at least ~12 clean results on screen after filtering.
_SIGNALS_POOL_SIZE = 60


async def _screener_fallback(name: str, top_n: int) -> dict:
    """Score a sample of NYSE symbols when the screener returns no results."""
    raw_universe = signals.get_nyse_symbols()[:100]
    results = []
    for sym in raw_universe:
        try:
            score = await asyncio.to_thread(signals.score_stock, sym)
            if score and score.get("score", 0) >= 10:
                results.append(score)
        except Exception:
            pass
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return {"results": results[:top_n]}


async def _recommendations_fallback(watchlist: list[str], limit: int) -> dict:
    """Score each watchlist symbol when get_recommendations returns nothing."""
    fallback_recs = []
    for sym in watchlist:
        try:
            score = await asyncio.to_thread(signals.score_stock, sym)
            if score:
                fallback_recs.append(score)
        except Exception:
            pass
    return {"recommendations": fallback_recs[:limit], "total_evaluated": len(watchlist)}


def _sanitize(obj):
    """Recursively replace inf/nan floats with None so JSON serialization never fails."""
    if isinstance(obj, float):
        return None if (math.isinf(obj) or math.isnan(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


# ── Signals ───────────────────────────────────────────────────────────────────

@router.get("/api/finance/signal/{symbol}")
async def get_signal(symbol: str):
    try:
        return await asyncio.to_thread(signals.score_stock, symbol)
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.get("/api/finance/signals/watchlist")
async def get_watchlist_signals(force: bool = False):
    watchlist = portfolio.get_watchlist()
    if not force:
        cached = portfolio.get_crawl_results("signal_scores")
        if cached and cached.get("items"):
            cached_syms = {s.get("symbol") for s in cached["items"]}
            if set(watchlist) <= cached_syms:
                return {"scores": cached["items"], "cached_at": cached.get("fetched_at")}
    results = await asyncio.to_thread(signals.score_watchlist, watchlist)
    portfolio.save_crawl_results("signal_scores", results)
    return {"scores": results, "cached_at": datetime.now(UTC).isoformat()}


# ── Screeners ─────────────────────────────────────────────────────────────────

@router.get("/api/finance/screeners")
def list_screeners():
    return {
        "screeners": [
            {"name": "undervalued",        "label": "Undervalued",   "desc": "Low P/E & P/B with analyst upside"},
            {"name": "momentum",           "label": "Momentum",      "desc": "Strong technicals & analyst consensus"},
            {"name": "growth",             "label": "Growth",        "desc": "High revenue & earnings growth"},
            {"name": "dividend",           "label": "Dividend",      "desc": "Yield ≥2% with healthy fundamentals"},
            {"name": "beaten_down",        "label": "Beaten Down",   "desc": "Near 52-week low — contrarian reversal"},
            {"name": "sector:tech",        "label": "Tech",          "desc": "Undervalued Technology"},
            {"name": "sector:healthcare",  "label": "Healthcare",    "desc": "Undervalued Healthcare"},
            {"name": "sector:financials",  "label": "Financials",    "desc": "Undervalued Financials"},
            {"name": "sector:energy",      "label": "Energy",        "desc": "Undervalued Energy"},
            {"name": "sector:industrials", "label": "Industrials",   "desc": "Undervalued Industrials"},
            {"name": "green_energy",       "label": "Green Energy",  "desc": "Solar, wind, storage & clean utilities"},
            {"name": "moonshots",          "label": "Moonshots",     "desc": "Breakout momentum — full uptrend + growth surge"},
        ]
    }


@router.get("/api/finance/screeners-cached")
async def get_screeners_cached():
    try:
        cached = await asyncio.to_thread(portfolio.get_all_screener_results)
        return {"results": _sanitize(cached)}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.get("/api/finance/screen/{name}")
async def run_screen(name: str, top_n: int = _SIGNALS_POOL_SIZE):
    try:
        cached = await asyncio.to_thread(portfolio.get_screener_result, name)
        if cached:
            data = cached["data"]
            if not data.get("results"):
                result = await asyncio.to_thread(signals.run_screen, name, top_n)
                if result.get("results"):
                    await asyncio.to_thread(portfolio.save_screener_result, name, result)
                    return {**result, "generated_at": datetime.now(UTC).isoformat(), "cached": False}
            return _sanitize({**data, "generated_at": cached["generated_at"], "cached": True})

        result = await asyncio.to_thread(signals.run_screen, name, top_n)

        if not result.get("results"):
            result.update(await _screener_fallback(name, top_n))

        await asyncio.to_thread(portfolio.save_screener_result, name, result)
        return _sanitize({**result, "generated_at": datetime.now(UTC).isoformat(), "cached": False})
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/api/finance/screen/{name}/run")
async def run_screen_now(name: str, background_tasks: BackgroundTasks):
    try:
        async def run_it():
            result = await asyncio.to_thread(signals.run_screen, name, _SIGNALS_POOL_SIZE)
            if not result.get("results"):
                result.update(await _screener_fallback(name, _SIGNALS_POOL_SIZE))
            await asyncio.to_thread(portfolio.save_screener_result, name, result)

        background_tasks.add_task(run_it)
        return {"status": "running", "message": f"Screener {name} started"}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


# ── Recommendations ───────────────────────────────────────────────────────────

@router.get("/api/finance/recommendations")
async def get_recommendations_cached():
    try:
        cached = await asyncio.to_thread(portfolio.get_recommendations)
        if cached:
            data = cached["data"]
            if not data.get("recommendations"):
                watchlist = portfolio.get_watchlist()
                result = await asyncio.to_thread(signals.get_recommendations, watchlist, _SIGNALS_POOL_SIZE)
                if result.get("recommendations"):
                    await asyncio.to_thread(portfolio.save_recommendations, result)
                    return {"results": result, "generated_at": datetime.now(UTC).isoformat(), "cached": False}
            return {"results": data, "generated_at": cached["generated_at"], "cached": True}

        watchlist = portfolio.get_watchlist()
        result = await asyncio.to_thread(signals.get_recommendations, watchlist, _SIGNALS_POOL_SIZE)

        if not result.get("recommendations"):
            result = await _recommendations_fallback(watchlist, _SIGNALS_POOL_SIZE)

        await asyncio.to_thread(portfolio.save_recommendations, result)
        return {"results": result, "generated_at": datetime.now(UTC).isoformat(), "cached": False}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/api/finance/recommendations/run")
async def run_recommendations_now(background_tasks: BackgroundTasks):
    try:
        async def gen_recs():
            watchlist = portfolio.get_watchlist()
            result = await asyncio.to_thread(signals.get_recommendations, watchlist, _SIGNALS_POOL_SIZE)
            if not result.get("recommendations"):
                result = await _recommendations_fallback(watchlist, _SIGNALS_POOL_SIZE)
            await asyncio.to_thread(portfolio.save_recommendations, result)

        background_tasks.add_task(gen_recs)
        return {"status": "running", "message": "Recommendations generation started"}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


# ── Volume Analysis ───────────────────────────────────────────────────────────

@router.get("/api/finance/volume-analysis/results")
async def get_volume_results():
    try:
        results, _ = await asyncio.to_thread(portfolio.get_volume_results)
        return {"results": results, "count": len(results)}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.get("/api/finance/volume-analysis/tasks")
async def get_volume_tasks():
    try:
        counts = await asyncio.to_thread(portfolio.get_volume_task_counts)
        return counts
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.get("/api/finance/volume-analysis/config")
async def get_volume_config():
    try:
        config = portfolio.get_crawl_results("volume_analysis_config") or {}
        defaults = {"enabled": True, "stocks_per_minute": 20, "skip_hours": 24, "max_retries": 3}
        return {**defaults, **config}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


class VolumeConfigBody(BaseModel):
    enabled: bool = True
    stocks_per_minute: int = 20
    skip_hours: int = 24
    max_retries: int = 3


@router.post("/api/finance/volume-analysis/config")
async def set_volume_config(body: VolumeConfigBody):
    try:
        if not 1 <= body.stocks_per_minute <= 500:
            raise HTTPException(400, "stocks_per_minute must be 1-500")
        if body.skip_hours < 0:
            raise HTTPException(400, "skip_hours must be >= 0")
        if not 0 <= body.max_retries <= 10:
            raise HTTPException(400, "max_retries must be 0-10")
        config = {
            "enabled": body.enabled,
            "stocks_per_minute": body.stocks_per_minute,
            "skip_hours": body.skip_hours,
            "max_retries": body.max_retries,
        }
        portfolio.save_crawl_results("volume_analysis_config", config)
        if not body.enabled:
            scheduler.stop_job("volume_analysis")
        return config
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/api/finance/volume-analysis/run")
async def run_volume_analysis_now():
    try:
        # Volume analysis is a continuous worker — "Run Now" sets the force-rescore
        # flag and wakes the worker if idle. Safe to call at any time.
        await scheduler.run_job_now("volume_analysis")
        return {"status": "running", "message": "Volume analysis force-rescore queued"}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/api/finance/volume-analysis/stop")
async def stop_volume_analysis_now():
    if not scheduler.get_job_running("volume_analysis"):
        raise HTTPException(409, "Volume analysis is not running")
    scheduler.stop_job("volume_analysis")
    return {"status": "stopping", "message": "Stop signal sent"}


@router.post("/api/finance/volume-analysis/rescore/{symbol}")
async def rescore_volume_symbol(symbol: str, background_tasks: BackgroundTasks):
    try:
        await asyncio.to_thread(portfolio.set_volume_task_priority, symbol.upper(), 10)

        async def score_one():
            import logging as _l
            try:
                scores = await asyncio.to_thread(signals.score_stock, symbol.upper())
                await asyncio.to_thread(portfolio.save_volume_result, symbol.upper(), scores)
                await asyncio.to_thread(portfolio.set_volume_task_priority, symbol.upper(), 0)
            except Exception as e:
                _l.getLogger("signals_router").error(f"Rescore {symbol} failed: {e}")

        background_tasks.add_task(score_one)
        return {"status": "running", "message": f"Rescoring {symbol}"}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


# ── Screener Config ────────────────────────────────────────────────────────────

@router.get("/api/finance/screeners/config")
async def get_screener_config():
    try:
        config = portfolio.get_crawl_results("screener_config") or {}
        return {**{"enabled": True, "interval_hours": 12, "delay_secs": 5}, **config}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


class ScreenerConfigBody(BaseModel):
    enabled: bool = True
    interval_hours: int = 12
    delay_secs: int = 5


@router.post("/api/finance/screeners/config")
async def set_screener_config(body: ScreenerConfigBody):
    try:
        if not 1 <= body.interval_hours <= 168:
            raise HTTPException(400, "interval_hours must be 1-168")
        if not 1 <= body.delay_secs <= 300:
            raise HTTPException(400, "delay_secs must be 1-300")
        config = {"enabled": body.enabled, "interval_hours": body.interval_hours, "delay_secs": body.delay_secs}
        portfolio.save_crawl_results("screener_config", config)
        if not body.enabled:
            scheduler.stop_job("screeners")
        scheduler.update_screener_interval(body.interval_hours)
        return config
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e)) from e


# ── Sentiment ─────────────────────────────────────────────────────────────────

@router.get("/api/finance/sentiment/{symbol}")
def get_sentiment(symbol: str):
    return portfolio.get_sentiment(symbol) or {"symbol": symbol.upper(), "score": None}


@router.get("/api/finance/sentiment")
def get_all_sentiment():
    return portfolio.get_sentiment()
