"""
Finance Intelligence — APIRouter (aggregator)

Mounts domain sub-routers and hosts LLM/RAG/EDGAR/scheduler endpoints.
Domain routes are split across:
  - market_router.py   — stock info, chart, news, quotes, peers
  - signals_router.py  — signals, screeners, recommendations, volume analysis
  - broker_router.py   — broker, watchlist, picks, crawl

`init_finance(llm_engine, rag_pipeline, scraper_instance)` called from lifespan.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import broker
import fundamentals as fd
import ingestion
import market
import db as portfolio
import scheduler
import signals
import market_router
import signals_router
import broker_router
import daily_trades_router
from finance_prompts import (
    _build_finance_context,
    _build_insight_prompt,
    _insight_max_tokens,
    generate_extended_insight_text,
)

router = APIRouter()
router.include_router(market_router.router)
router.include_router(signals_router.router)
router.include_router(broker_router.router)
router.include_router(daily_trades_router.router)

# References to shared singletons — set by init_finance()
_llm = None
_rag = None
_scraper = None

# Symbols currently being generated (in-process dedup guard)
_generating: set[str] = set()


def init_finance(llm_engine, rag_pipeline, scraper_instance):
    global _llm, _rag, _scraper
    _llm     = llm_engine
    _rag     = rag_pipeline
    _scraper = scraper_instance
    scheduler.set_rag(rag_pipeline)
    scheduler.start_scheduler()
    market.build_yf_session()


def shutdown_finance():
    scheduler.stop_scheduler()


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/api/finance/health")
def finance_health():
    return {
        "status":        "ok",
        "paper_trading": broker.ALPACA_PAPER,
        "broker_ready":  broker.is_configured(),
        "llm_loaded":    _llm is not None,
        "timestamp":     datetime.now(UTC).isoformat(),
    }


# ── Symbol autocomplete ───────────────────────────────────────────────────────

@router.get("/api/finance/symbols")
async def get_symbols():
    """
    Return cached tickers + company names for autocomplete (~10k from SEC EDGAR).
    Served from SQLite symbols table. Populated on startup by scheduler.
    Falls back to static ~150-symbol list if DB is still empty (scheduler not done yet).
    """
    count = portfolio.get_symbol_count()
    if count > 0:
        return {"symbols": portfolio.get_symbols(), "count": count, "cached": True}

    # DB empty — scheduler hasn't finished yet; return fallback immediately.
    # Client should retry in a few seconds.
    fallback = signals._FALLBACK_UNIVERSE
    names    = signals._FALLBACK_NAMES
    return {
        "symbols": [{"symbol": s, "name": names.get(s, "")} for s in fallback],
        "count":   len(fallback),
        "cached":  False,
    }


# ── Log viewer ────────────────────────────────────────────────────────────────

@router.get("/api/finance/logs")
async def get_logs(level: str = "WARNING", limit: int = 200):
    """
    Return recent log entries captured in the in-memory ring buffer.
    level: WARNING | ERROR | INFO (minimum level filter)
    """
    import main as _main
    level_no = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}.get(
        level.upper(), 30
    )
    import logging
    entries = [
        e for e in _main._LOG_BUFFER
        if logging.getLevelName(e["level"]) >= level_no
    ]
    return {"entries": list(entries)[-limit:], "total": len(entries)}


# ── yfinance Cache Config ──────────────────────────────────────────────────────

@router.get("/api/finance/cache/config")
async def get_cache_config():
    """Get yfinance cache configuration."""
    try:
        config = portfolio.get_crawl_results("yfinance_cache_config") or {}
        return {
            "enabled": config.get("enabled", True),
            "ttl_minutes": config.get("ttl_minutes", 30),
        }
    except Exception as e:
        raise HTTPException(500, str(e)) from e


class CacheConfigBody(BaseModel):
    enabled: bool = True
    ttl_minutes: int = 30

@router.post("/api/finance/cache/config")
async def set_cache_config(body: CacheConfigBody):
    """Save yfinance cache config and rebuild the session immediately."""
    try:
        if not 1 <= body.ttl_minutes <= 1440:
            raise HTTPException(400, "ttl_minutes must be 1-1440")
        config = {"enabled": body.enabled, "ttl_minutes": body.ttl_minutes}
        portfolio.save_crawl_results("yfinance_cache_config", config)
        await asyncio.to_thread(market.build_yf_session)
        return config
    except Exception as e:
        raise HTTPException(500, str(e)) from e


# ── Scheduler ─────────────────────────────────────────────────────────────────

# Registry of configurable jobs (excludes volume_analysis and screeners which have dedicated endpoints)
_JOB_REGISTRY: dict[str, dict] = {
    "crawl": {
        "config_key":    "crawl_config",
        "defaults":      {"enabled": True, "interval_hours": 1.0, "interval_offset_minutes": 0},
        "interval_field": "interval_hours",
        "interval_unit":  "hours",
        "interval_min":   0.25,
        "interval_max":   168,
    },
    "refresh_picks": {
        "config_key":    "refresh_picks_config",
        "defaults":      {"enabled": True, "interval_hours": 0.25, "interval_offset_minutes": 0},
        "interval_field": "interval_hours",
        "interval_unit":  "hours",
        "interval_min":   0.25,
        "interval_max":   168,
    },
    "score_watchlist": {
        "config_key":    "score_watchlist_config",
        "defaults":      {"enabled": True, "interval_hours": 4, "interval_offset_minutes": 0},
        "interval_field": "interval_hours",
        "interval_unit":  "hours",
        "interval_min":   0.25,
        "interval_max":   168,
    },
    "ingest": {
        "config_key":    "ingest_config",
        "defaults":      {"enabled": True, "interval_hours": 24, "interval_offset_minutes": 0},
        "interval_field": "interval_hours",
        "interval_unit":  "hours",
        "interval_min":   0.25,
        "interval_max":   168,
    },
    "refresh_symbols": {
        "config_key":    "refresh_symbols_config",
        "defaults":      {"enabled": True, "interval_hours": 24, "interval_offset_minutes": 0},
        "interval_field": "interval_hours",
        "interval_unit":  "hours",
        "interval_min":   0.25,
        "interval_max":   168,
    },
    "recommendations": {
        "config_key":    "recommendations_config",
        "defaults":      {"enabled": True, "interval_hours": 6, "interval_offset_minutes": 0},
        "interval_field": "interval_hours",
        "interval_unit":  "hours",
        "interval_min":   0.25,
        "interval_max":   168,
    },
    "volume_analysis": {
        "config_key":    "volume_analysis_config",
        "defaults":      {"enabled": True, "interval_minutes": 1, "stocks_per_minute": 20, "skip_hours": 24},
        "interval_field": "interval_minutes",
        "interval_unit":  "minutes",
        "interval_min":   1,
        "interval_max":   1440,
    },
    "screeners": {
        "config_key":    "screener_config",
        "defaults":      {"enabled": True, "interval_hours": 12, "interval_offset_minutes": 0},
        "interval_field": "interval_hours",
        "interval_unit":  "hours",
        "interval_min":   0.25,
        "interval_max":   168,
    },
    "extended_insights": {
        "config_key":    "extended_insights_config",
        "defaults":      {"enabled": True, "interval_hours": 12, "skip_hours": 12, "max_per_minute": 5, "interval_offset_minutes": 0},
        "interval_field": "interval_hours",
        "interval_unit":  "hours",
        "interval_min":   0.25,
        "interval_max":   168,
    },
}


@router.get("/api/finance/scheduler/jobs")
def get_jobs():
    return {"jobs": scheduler.get_job_status()}


@router.get("/api/finance/jobs/stream")
async def stream_jobs(request: Request):
    """SSE stream of job status — emits every second."""
    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                jobs = scheduler.get_job_status()
                yield f"data: {json.dumps(jobs)}\n\n"
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/finance/jobs/{job_id}/status")
def get_job_status(job_id: str):
    """Return whether a job is currently running."""
    return {"job_id": job_id, "running": scheduler.get_job_running(job_id)}


@router.get("/api/finance/jobs/{job_id}/config")
def get_job_config(job_id: str):
    """Return persisted config for a configurable job."""
    reg = _JOB_REGISTRY.get(job_id)
    if not reg:
        raise HTTPException(404, f"Unknown job: {job_id}")
    saved = portfolio.get_crawl_results(reg["config_key"]) or {}
    return {**reg["defaults"], **saved}


class JobConfigBody(BaseModel):
    enabled: bool = True
    interval: float | None = None          # hours for hour-unit jobs, minutes for volume_analysis
    interval_offset_minutes: int | None = None
    skip_hours: int | None = None
    max_per_minute: int | None = None
    stocks_per_minute: int | None = None


@router.post("/api/finance/jobs/{job_id}/config")
def set_job_config(job_id: str, body: JobConfigBody):
    """Save config for a configurable job and reschedule it."""
    reg = _JOB_REGISTRY.get(job_id)
    if not reg:
        raise HTTPException(404, f"Unknown job: {job_id}")

    saved = portfolio.get_crawl_results(reg["config_key"]) or {}
    config = {**reg["defaults"], **saved, "enabled": body.enabled}

    if body.interval is not None:
        lo, hi = reg["interval_min"], reg["interval_max"]
        if not lo <= body.interval <= hi:
            raise HTTPException(400, f"interval must be {lo}-{hi}")
        config[reg["interval_field"]] = body.interval

    if body.interval_offset_minutes is not None:
        if not 0 <= body.interval_offset_minutes <= 55:
            raise HTTPException(400, "interval_offset_minutes must be 0-55")
        config["interval_offset_minutes"] = body.interval_offset_minutes

    if body.skip_hours is not None:
        if "skip_hours" not in reg["defaults"]:
            raise HTTPException(400, f"{job_id} does not support skip_hours")
        if body.skip_hours < 0:
            raise HTTPException(400, "skip_hours must be >= 0")
        config["skip_hours"] = body.skip_hours

    if body.max_per_minute is not None:
        if "max_per_minute" not in reg["defaults"]:
            raise HTTPException(400, f"{job_id} does not support max_per_minute")
        if not 1 <= body.max_per_minute <= 60:
            raise HTTPException(400, "max_per_minute must be 1-60")
        config["max_per_minute"] = body.max_per_minute

    if body.stocks_per_minute is not None:
        if "stocks_per_minute" not in reg["defaults"]:
            raise HTTPException(400, f"{job_id} does not support stocks_per_minute")
        if not 1 <= body.stocks_per_minute <= 500:
            raise HTTPException(400, "stocks_per_minute must be 1-500")
        config["stocks_per_minute"] = body.stocks_per_minute

    portfolio.save_crawl_results(reg["config_key"], config)

    # If disabled, stop any currently running instance immediately
    if not config.get("enabled", True):
        scheduler.stop_job(job_id)

    # Reschedule live job
    if scheduler._scheduler:
        unit   = reg["interval_unit"]
        offset = int(config.get("interval_offset_minutes", 0))
        if unit == "minutes":
            mins = int(config.get(reg["interval_field"], 1))
            scheduler.update_job_interval(job_id, minutes=mins, offset_minutes=offset)
        else:
            hrs = float(config.get(reg["interval_field"], 1))
            scheduler.update_job_interval(job_id, hours=hrs, offset_minutes=offset)

    return config


@router.post("/api/finance/jobs/{job_id}/run")
async def run_job_now(job_id: str, background_tasks: BackgroundTasks):
    """Trigger a job immediately. Returns 409 if already running."""
    if job_id not in _JOB_REGISTRY:
        raise HTTPException(404, f"Unknown job: {job_id}")
    if scheduler.get_job_running(job_id):
        raise HTTPException(409, f"{job_id} is already running")
    background_tasks.add_task(scheduler.run_job_now, job_id)
    return {"status": "running", "message": f"{job_id} started"}


@router.post("/api/finance/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    """Signal a running job to stop at its next checkpoint."""
    if job_id not in _JOB_REGISTRY:
        raise HTTPException(404, f"Unknown job: {job_id}")
    if not scheduler.get_job_running(job_id):
        raise HTTPException(409, f"{job_id} is not running")
    scheduler.stop_job(job_id)
    return {"status": "stopping", "message": f"Stop signal sent to {job_id}"}


class CrawlIntervalBody(BaseModel):
    minutes: int

@router.put("/api/finance/scheduler/crawl-interval")
async def update_crawl_interval(body: CrawlIntervalBody):
    scheduler.update_crawl_interval(body.minutes)
    return {"ok": True, "minutes": body.minutes}


# ── Extended Insights ─────────────────────────────────────────────────────────

@router.get("/api/finance/extended-insights/{symbol}")
async def get_extended_insight(symbol: str):
    """Return cached extended insight for a symbol, plus live generating flag."""
    sym = symbol.upper()
    result = portfolio.get_extended_insight(sym)
    generating = sym in _generating
    if not result:
        return {"symbol": sym, "insight_text": None, "generated_at": None, "model_name": None, "generating": generating}
    return {**result, "generating": generating}


@router.post("/api/finance/extended-insights/{symbol}/run")
async def run_extended_insight(symbol: str, background_tasks: BackgroundTasks):
    """Trigger on-demand extended insight generation for any symbol."""
    import main as _main
    sym = symbol.upper()
    if sym in _generating:
        raise HTTPException(409, f"Already generating for {sym}")
    llm = _main.llm or _llm
    if llm is None:
        raise HTTPException(503, "No model loaded")

    async def _generate():
        _generating.add(sym)
        try:
            text = await generate_extended_insight_text(sym, llm, _rag)
            if text:
                model_name = getattr(llm, "model_path", "") or ""
                import os as _os
                model_name = _os.path.basename(model_name)
                portfolio.save_extended_insight(sym, text, model_name)
        except Exception as e:
            import logging as _l
            _l.getLogger("finance_router").error(f"Extended insight {sym}: {e}")
        finally:
            _generating.discard(sym)

    background_tasks.add_task(_generate)
    return {"status": "generating", "symbol": sym}


@router.get("/api/finance/extended-insights/{symbol}/stream")
async def stream_extended_insight(symbol: str):
    """Stream extended insight tokens as SSE. Saves result to DB when done."""
    import main as _main
    sym = symbol.upper()
    llm = _main.llm or _llm
    if llm is None:
        raise HTTPException(503, "No model loaded")
    if sym in _generating:
        raise HTTPException(409, f"Already generating for {sym}")

    async def _token_generator():
        _generating.add(sym)
        full = ""
        try:
            prompt_text = await _build_insight_prompt(sym, _rag)
            async for token in llm.stream(prompt_text, max_tokens=_insight_max_tokens(llm, prompt_text)):
                full += token
                escaped = token.replace("\\", "\\\\").replace("\n", "\\n")
                yield f"data: {escaped}\n\n"
        except Exception as e:
            yield f"data: [ERROR] {e}\n\n"
        finally:
            _generating.discard(sym)
            if full:
                import os as _os
                model_name = _os.path.basename(getattr(llm, "model_path", "") or "")
                await asyncio.to_thread(portfolio.save_extended_insight, sym, full.strip(), model_name)
            yield "data: [DONE]\n\n"

    return StreamingResponse(_token_generator(), media_type="text/event-stream")


# ── EDGAR ─────────────────────────────────────────────────────────────────────

@router.get("/api/finance/edgar/filings/{symbol}")
async def get_edgar_filings(symbol: str, forms: str = "10-K,10-Q,8-K"):
    form_list = [f.strip() for f in forms.split(",")]
    try:
        return {"filings": await asyncio.to_thread(fd.get_edgar_filings, symbol, form_list, 10)}
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.get("/api/finance/edgar/financials/{symbol}")
async def get_financial_history(symbol: str):
    try:
        return {"symbol": symbol.upper(), "financials": await asyncio.to_thread(fd.get_financial_history, symbol)}
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.get("/api/finance/edgar/cik/{symbol}")
async def get_cik(symbol: str):
    cik = await asyncio.to_thread(fd.get_cik, symbol)
    return {"symbol": symbol.upper(), "cik": cik}


# ── FRED Macro ────────────────────────────────────────────────────────────────

@router.get("/api/finance/macro/snapshot")
async def get_macro_snapshot():
    try:
        return await asyncio.to_thread(fd.get_macro_snapshot)
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.get("/api/finance/macro/series/{series_id}")
async def get_fred_series(series_id: str, periods: int = 60):
    try:
        return {"series_id": series_id, "data": await asyncio.to_thread(fd.get_fred_series_history, series_id, periods)}
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.get("/api/finance/macro/series")
async def list_fred_series():
    return {"series": [{"id": sid, "name": name, "label": label} for name, (sid, label) in fd.FRED_SERIES.items()]}


# ── OpenFIGI ──────────────────────────────────────────────────────────────────

@router.get("/api/finance/figi/{symbol}")
async def get_figi(symbol: str):
    result = await asyncio.to_thread(fd.figi_lookup, symbol)
    return result or {"symbol": symbol.upper(), "figi": None}

@router.post("/api/finance/figi/batch")
async def get_figi_batch(body: dict):
    tickers = body.get("tickers", [])
    if not tickers:
        raise HTTPException(400, "tickers list required")
    return await asyncio.to_thread(fd.figi_batch, tickers)


# ── RAG Ingestion ─────────────────────────────────────────────────────────────

@router.post("/api/finance/ingest/{symbol}")
async def ingest_symbol(symbol: str, background_tasks: BackgroundTasks, force: bool = False):
    """Trigger ingestion for a single symbol (runs in background)."""
    if _rag is None:
        raise HTTPException(503, "RAG not initialized")
    async def _run():
        await ingestion.ingest_symbol(symbol, _rag, force=force)
    background_tasks.add_task(_run)
    return {"status": "ingestion started", "symbol": symbol.upper(), "force": force}

@router.post("/api/finance/ingest/run/all")
async def ingest_all(background_tasks: BackgroundTasks):
    """Trigger full ingestion pipeline for entire watchlist."""
    if _rag is None:
        raise HTTPException(503, "RAG not initialized")
    watchlist = portfolio.get_watchlist()
    async def _run():
        await ingestion.run_ingestion(watchlist, _rag)
    background_tasks.add_task(_run)
    return {"status": "ingestion started", "symbols": watchlist}

@router.get("/api/finance/ingest/sources")
async def get_ingestion_sources():
    """List all documents currently indexed in the vector DB."""
    if _rag is None:
        return {"sources": []}
    sources = _rag.list_sources()
    finance_sources = [s for s in sources if any(
        s.get("name","").startswith(kw) or s.get("id","").startswith(kw)
        for kw in ("edgar", "fred", "macrotrends", "SEC")
    )]
    return {"total": len(sources), "finance": len(finance_sources), "sources": finance_sources}


# ── LLM — finance-aware streaming Q&A ───────────────────────────────────────
# Uses the shared LLMEngine directly — no HTTP round-trip.

class LLMRequest(BaseModel):
    prompt:  str
    context: str | None = None
    symbol:  str | None = None


@router.post("/api/finance/llm/stream")
async def finance_llm_stream(body: LLMRequest):
    """
    Stream LLM response using the shared LLMEngine directly.
    Injects finance context (market data, signals, news, crawl) as system prompt.
    """
    import main as _main
    # Always read the live engine — _llm is stale after a model switch
    llm = _main.llm or _llm
    if llm is None:
        async def _no_model():
            yield f"data: {json.dumps({'error': 'No model loaded. Load a model in the Chat app first.'})}\n\n"
        return StreamingResponse(_no_model(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    system_prompt = body.context or await _build_finance_context(body.symbol)

    # Use RAG if we have a symbol — may surface relevant ingested docs
    rag_context = ""
    if _rag and body.symbol:
        with contextlib.suppress(Exception):
            rag_context = await _rag.query_async(
                f"{body.symbol} {body.prompt}", n_results=2
            )

    full_system = system_prompt
    if rag_context:
        full_system += f"\n\n## Knowledge Base\n{rag_context}"

    from types import SimpleNamespace

    from prompts import build_prompt
    prompt_text = build_prompt(
        template=getattr(_main, "active_template", "mistral"),
        system=full_system,
        messages=[SimpleNamespace(role="user", content=body.prompt)],
    )

    import logging as _logging
    _stream_logger = _logging.getLogger("finance_router")

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            async for token in llm.stream(prompt_text):
                yield f"data: {json.dumps({'content': token})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as exc:
            _stream_logger.error(f"finance LLM stream: {exc}")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _scan_models() -> list[dict]:
    """Scan models/ dir for .gguf files — mirrors main.py scan_models()."""
    import main as _main
    return _main.scan_models()


@router.get("/api/finance/llm/models")
async def get_finance_models():
    """List all available .gguf models with active flag."""
    import main as _main
    return {"models": _scan_models(), "switching": _main.switching_model}


@router.get("/api/finance/llm/model")
async def get_finance_model():
    """Current model status."""
    import main as _main
    llm = _main.llm or _llm
    if llm is None:
        return {"loaded": False, "switching": _main.switching_model}
    mp = getattr(llm, "model_path", "") or ""
    return {
        "loaded":    True,
        "name":      mp.split("/")[-1] if mp else "unknown",
        "path":      mp,
        "switching": _main.switching_model,
    }


class SwitchModelRequest(BaseModel):
    path: str


@router.post("/api/finance/llm/models/switch")
async def finance_switch_model(req: SwitchModelRequest, background_tasks: BackgroundTasks):
    """Hot-swap the LLM — delegates to main.py _do_switch."""
    from pathlib import Path

    import main as _main

    target = Path(req.path)
    if not target.is_absolute():
        target = _main.MODELS_DIR / target

    if not target.exists():
        raise HTTPException(404, f"Model not found: {target}")

    if _main.switching_model:
        raise HTTPException(409, "Already switching models, please wait.")

    llm = _main.llm or _llm
    if llm is not None and Path(getattr(llm, "model_path", "")).resolve() == target.resolve():
        return {"status": "already_loaded", "name": target.name}

    _main.switching_model = True
    background_tasks.add_task(_main._do_switch, str(target))
    return {"status": "switching", "name": target.name}


# ── Configuration ─────────────────────────────────────────────────────────────

class VolumeAnalysisConfig(BaseModel):
    """Adjustable volume analysis settings."""
    enabled: bool           # Enable/disable volume analysis
    stocks_per_minute: int  # How many stocks to score per minute (1-500)
    skip_hours: int         # Don't rescore within N hours
    max_retries: int        # Retry failed scores N times


@router.get("/api/finance/config")
async def get_config():
    """Return all configurable settings."""
    # Volume analysis config (stored as dict in crawl_cache)
    vol_config = await asyncio.to_thread(portfolio.get_crawl_results, "volume_analysis_config")
    if not vol_config:
        vol_config = {}

    # Default: 20/minute = ~8.2 hours for all 9836 stocks
    return {
        "volume_analysis": {
            "enabled": vol_config.get("enabled", True),
            "stocks_per_minute": vol_config.get("stocks_per_minute", 20),
            "skip_hours": vol_config.get("skip_hours", 24),
            "max_retries": vol_config.get("max_retries", 3),
        },
    }


@router.post("/api/finance/config")
async def update_config(config: dict):
    """Update configurable settings."""
    try:
        # Update volume analysis config if provided
        if "volume_analysis" in config:
            vol_cfg = config["volume_analysis"]
            # Validate ranges
            vol_cfg["enabled"] = bool(vol_cfg.get("enabled", True))
            vol_cfg["stocks_per_minute"] = max(1, min(vol_cfg.get("stocks_per_minute", 20), 500))
            vol_cfg["skip_hours"] = max(0, vol_cfg.get("skip_hours", 24))
            vol_cfg["max_retries"] = max(0, min(vol_cfg.get("max_retries", 3), 10))

            await asyncio.to_thread(
                portfolio.save_crawl_results,
                "volume_analysis_config",
                vol_cfg  # Save dict directly, not wrapped
            )

        return {"status": "ok", "config": await get_config()}
    except Exception as e:
        raise HTTPException(400, str(e)) from e
