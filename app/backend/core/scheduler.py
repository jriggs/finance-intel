"""
APScheduler background jobs.
Started once at app startup; jobs run on a configurable interval.
All intervals stored and configured in hours (float). Volume analysis uses minutes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import crawler
import market
import db as portfolio

# Shared RAG instance — set by init_finance via finance_router
_rag = None

def set_rag(rag_instance):
    global _rag
    _rag = rag_instance

logger = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None


# ── Trigger factory ───────────────────────────────────────────────────────────

def _make_trigger(hours: float = 0, minutes: int = 0, offset_minutes: int = 0) -> IntervalTrigger:
    """Build an IntervalTrigger with optional phase offset so jobs don't all fire on the hour."""
    total_seconds = max(60, int(hours * 3600 + minutes * 60))
    # Phase-align: start_date at a fixed epoch point + offset so the trigger
    # fires at offset, offset+interval, offset+2*interval, …
    start = datetime(2000, 1, 1, 0, 0, 0, tzinfo=UTC) + timedelta(minutes=offset_minutes)
    return IntervalTrigger(seconds=total_seconds, start_date=start)


# ── Volume analysis continuous worker state ───────────────────────────────────

_va_task:         asyncio.Task | None  = None
_va_force_rescore: bool                = False
_va_wake:         asyncio.Event | None = None  # set inside worker coroutine


# ── Stop flags (one per job) ──────────────────────────────────────────────────

_stop_flags: dict[str, bool] = {
    "crawl":                   False,
    "refresh_picks":           False,
    "score_watchlist":         False,
    "ingest":                  False,
    "refresh_symbols":         False,
    "volume_analysis":         False,
    "recommendations":         False,
    "screeners":               False,
    "extended_insights":       False,
    "refresh_symbol_volumes":  False,
}


def stop_job(job_id: str) -> None:
    if job_id in _stop_flags:
        _stop_flags[job_id] = True
        logger.info(f"Stop requested for {job_id}")


# Keep backward-compat alias used by existing router endpoints
def stop_volume_analysis():
    stop_job("volume_analysis")


# ── Running flags ─────────────────────────────────────────────────────────────

_running: dict[str, bool] = {k: False for k in _stop_flags}

# ── Last-run timestamps ───────────────────────────────────────────────────────

_last_run: dict[str, str | None] = {k: None for k in _stop_flags}


def _mark_running(job_id: str) -> bool:
    """Set running=True. Returns False if already running."""
    if _running.get(job_id):
        return False
    _stop_flags[job_id] = False  # clear any stale stop flag so job can proceed
    _running[job_id] = True
    return True


def _yf_is_blocked_now() -> bool:
    try:
        import market as _market
        return _market._yf_is_blocked()
    except Exception:
        return False


def _mark_done(job_id: str) -> None:
    _running[job_id] = False
    _stop_flags[job_id] = False  # reset so next scheduled run proceeds
    _last_run[job_id] = datetime.now(UTC).isoformat()


def stop_all_jobs() -> None:
    """Set stop flag on every job. Called when yfinance rate-limits to avoid pile-on."""
    for k in _stop_flags:
        _stop_flags[k] = True
    logger.warning("All jobs stopped (rate limit / circuit breaker tripped)")


# ── Jobs ──────────────────────────────────────────────────────────────────────

def _crawl_is_fresh(crawl_hours: float) -> bool:
    cached = portfolio.get_crawl_results("reddit")
    fetched_at = cached.get("fetched_at") if cached else None
    if not fetched_at:
        return False
    try:
        last = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        age  = datetime.now(UTC) - last
        return age < timedelta(hours=crawl_hours)
    except Exception:
        return False


async def _job_crawl():
    if not _mark_running("crawl"):
        return
    try:
        config = portfolio.get_crawl_results("crawl_config") or {}
        if not config.get("enabled", True):
            return
        crawl_hours = config.get("interval_hours", config.get("interval_minutes", 60) / 60)
        if _crawl_is_fresh(crawl_hours):
            logger.info("Crawl skipped — cache is fresh")
            return
        if _stop_flags["crawl"]:
            return
        logger.info("Crawl job started")
        watchlist = portfolio.get_watchlist()
        try:
            summary = await crawler.run_full_crawl(watchlist)
            logger.info(f"Crawl done: {summary}")
        except Exception as e:
            logger.error(f"Crawl job error: {e}")
    finally:
        _mark_done("crawl")


async def _job_refresh_picks():
    if not _mark_running("refresh_picks"):
        return
    try:
        config = portfolio.get_crawl_results("refresh_picks_config") or {}
        if not config.get("enabled", True):
            return

        open_picks = await asyncio.to_thread(portfolio.get_open_picks)
        if not open_picks:
            return

        symbols = list({p["symbol"] for p in open_picks})
        logger.info(f"Refreshing prices for {len(symbols)} open pick(s)")

        def _fetch_and_update():
            quotes = market.get_batch_quotes(symbols)
            prices = {q["symbol"]: q["price"] for q in quotes if q.get("price")}
            if prices:
                portfolio.update_pick_prices(prices)

        await asyncio.wait_for(asyncio.to_thread(_fetch_and_update), timeout=60.0)
        logger.info("Open pick prices refreshed")
    except asyncio.TimeoutError:
        logger.warning("refresh_picks: timed out after 60s")
    except Exception as e:
        logger.error(f"Pick refresh error: {e}")
    finally:
        _mark_done("refresh_picks")


async def _job_score_watchlist():
    if not _mark_running("score_watchlist"):
        return
    try:
        if _yf_is_blocked_now():
            logger.info("score_watchlist: skipping — yfinance rate-limited")
            return
        config = portfolio.get_crawl_results("score_watchlist_config") or {}
        if not config.get("enabled", True):
            return
        logger.info("Scoring watchlist…")
        try:
            import signals
            syms    = portfolio.get_watchlist()
            results = await asyncio.to_thread(signals.score_watchlist, syms)
            portfolio.save_crawl_results("signal_scores", results)
            logger.info(f"Scored {len(results)} symbols")
        except Exception as e:
            logger.error(f"Score job error: {e}")
    finally:
        _mark_done("score_watchlist")


async def _job_ingest():
    if not _mark_running("ingest"):
        return
    try:
        config = portfolio.get_crawl_results("ingest_config") or {}
        if not config.get("enabled", True):
            return
        if _rag is None:
            return
        logger.info("Ingestion job started")
        try:
            import ingestion
            watchlist = portfolio.get_watchlist()
            summary   = await ingestion.run_ingestion(watchlist, _rag)
            logger.info(f"Ingestion done: {summary}")
        except Exception as e:
            logger.error(f"Ingestion job error: {e}")
    finally:
        _mark_done("ingest")


async def _job_refresh_symbols():
    if not _mark_running("refresh_symbols"):
        return
    try:
        config = portfolio.get_crawl_results("refresh_symbols_config") or {}
        if not config.get("enabled", True):
            return
        logger.info("Symbol list refresh started")
        try:
            import fundamentals
            import signals

            result = await asyncio.to_thread(fundamentals.get_all_tickers)
            if result:
                portfolio.save_symbols(result)
                logger.info(f"Cached {len(result)} symbols with names from SEC EDGAR")
            else:
                result = [{"symbol": s, "name": signals._FALLBACK_NAMES.get(s, "")} for s in signals._FALLBACK_UNIVERSE]
                portfolio.save_symbols(result)
                logger.info(f"Cached {len(result)} fallback symbols with static names")

            va_config = portfolio.get_crawl_results("volume_analysis_config") or {}
            if va_config.get("enabled", True):
                portfolio.init_volume_analysis_tasks()
                logger.info("Volume analysis tasks initialized after symbol refresh")
        except Exception as e:
            logger.error(f"Symbol refresh error: {e}")
    finally:
        _mark_done("refresh_symbols")


async def _job_refresh_symbol_volumes():
    if not _mark_running("refresh_symbol_volumes"):
        return
    try:
        config = portfolio.get_crawl_results("refresh_symbol_volumes_config") or {}
        if not config.get("enabled", True):
            return
        logger.info("Symbol volume refresh started")
        import signals
        updated = await asyncio.to_thread(signals.refresh_all_symbol_volumes)
        logger.info(f"Symbol volume refresh done: {updated} symbols updated")
        added, removed = await asyncio.to_thread(portfolio.sync_volume_analysis_tasks)
        logger.info(f"Volume task queue synced: +{added} added, -{removed} removed")
    except Exception as e:
        logger.error(f"Symbol volume refresh error: {e}")
    finally:
        _mark_done("refresh_symbol_volumes")


async def _volume_analysis_worker() -> None:
    """Continuous worker — runs forever, scoring stocks at the configured rate.

    Reads config fresh each iteration so changes to stocks_per_minute / enabled
    take effect immediately without a restart.  Rate is enforced by staggering
    each stock's start time by 60 / stocks_per_minute seconds so throughput
    never exceeds the user-configured ceiling.
    """
    global _va_wake, _va_force_rescore
    _va_wake = asyncio.Event()

    import signals, fundamentals

    async def _score_one(
        task: dict,
        start_delay: float,
        semaphore: asyncio.Semaphore,
        macro_snap: dict,
        max_retries: int,
    ) -> None:
        if _stop_flags["volume_analysis"] or _yf_is_blocked_now():
            return
        await asyncio.sleep(start_delay)
        if _stop_flags["volume_analysis"] or _yf_is_blocked_now():
            return
        symbol = task["symbol"]
        retry  = task.get("retry_count", 0)

        if await asyncio.to_thread(market.is_not_found_blocked, symbol):
            def _remaining_days() -> int:
                import time
                data   = market._not_found_load()
                expiry = data.get(symbol.upper(), 0)
                return int((expiry - time.time()) / 86400) + 1
            remaining = await asyncio.to_thread(_remaining_days)
            logger.info(f"Volume analysis {symbol} skipped — blocked {remaining}d")
            await asyncio.to_thread(portfolio.update_volume_task, symbol, "error", 0)
            return

        try:
            async with semaphore:
                scores = await asyncio.wait_for(
                    asyncio.to_thread(signals.score_stock, symbol, macro_snap),
                    timeout=45.0,
                )
            if not scores:
                raise RuntimeError(f"no price data for {symbol}")
            await asyncio.to_thread(portfolio.save_volume_result, symbol, scores)
            await asyncio.to_thread(portfolio.update_volume_task, symbol, "done", 0)
            logger.info(f"Volume analysis scored {symbol}: {scores.get('grade', '?')}")
        except Exception as score_error:
            err = str(score_error).lower()
            if "401" in err or "unauthorized" in err or "invalid crumb" in err:
                await asyncio.to_thread(portfolio.update_volume_task, symbol, "error", retry)
                logger.warning(f"Volume analysis {symbol} auth error: {score_error}")
            elif "404" in err or "not found" in err or "delisted" in err or "no price data" in err:
                await asyncio.to_thread(portfolio.set_volume_task_cooldown, symbol, 10)
                logger.warning(f"Volume analysis {symbol} not found/delisted — cooldown 10d: {score_error}")
            elif retry < max_retries:
                await asyncio.to_thread(portfolio.update_volume_task, symbol, "pending", retry + 1)
                logger.debug(f"Volume analysis {symbol} retry {retry + 1}: {score_error}")
            else:
                await asyncio.to_thread(portfolio.update_volume_task, symbol, "error", retry)
                logger.warning(f"Volume analysis {symbol} failed after {max_retries} retries: {score_error}")

    while True:
        try:
            # ── Stop signal: abort current work, short cooldown ───────────────
            if _stop_flags["volume_analysis"]:
                _running["volume_analysis"] = False
                _stop_flags["volume_analysis"] = False
                logger.info("Volume analysis: stop signal — pausing 30s")
                await asyncio.sleep(30)
                continue

            # ── Read fresh config every iteration ─────────────────────────────
            config = portfolio.get_crawl_results("volume_analysis_config") or {}

            if not config.get("enabled", True):
                _running["volume_analysis"] = False
                await asyncio.sleep(15)
                continue

            if _yf_is_blocked_now():
                _running["volume_analysis"] = False
                logger.info("volume_analysis: yfinance blocked — backing off 30s")
                await asyncio.sleep(30)
                continue

            stocks_per_minute = max(1, min(int(config.get("stocks_per_minute", 20)), 500))
            skip_hours        = int(config.get("skip_hours", 24))
            max_retries       = int(config.get("max_retries", 3))
            max_concurrent    = min(stocks_per_minute, 40)
            # Stagger starts so N stocks launch over ~60 s → ≤ N/min throughput
            delay             = 60.0 / stocks_per_minute

            # Honour force-rescore requested by "Run Now"
            force             = _va_force_rescore
            _va_force_rescore = False
            effective_skip    = 0 if force else skip_hours

            await asyncio.to_thread(portfolio.mark_volume_tasks_for_rescore, effective_skip)
            tasks = await asyncio.to_thread(portfolio.get_next_volume_tasks, stocks_per_minute)

            if not tasks:
                # All caught up — idle until next task becomes stale or Run Now fires
                _running["volume_analysis"] = False
                _last_run["volume_analysis"] = datetime.now(UTC).isoformat()
                logger.debug(f"volume_analysis: all up-to-date within {effective_skip}h — waiting")
                try:
                    await asyncio.wait_for(_va_wake.wait(), timeout=60.0)
                    _va_wake.clear()
                except asyncio.TimeoutError:
                    pass
                continue

            # ── Score batch ───────────────────────────────────────────────────
            _running["volume_analysis"] = True
            try:
                macro_snap = await asyncio.to_thread(fundamentals.get_macro_snapshot)
            except Exception:
                macro_snap = {}

            semaphore = asyncio.Semaphore(max_concurrent)
            logger.info(
                f"Volume analysis: scoring {len(tasks)} stocks "
                f"at {stocks_per_minute}/min (delay={delay:.2f}s)"
            )
            coros = [
                _score_one(task, i * delay, semaphore, macro_snap, max_retries)
                for i, task in enumerate(tasks)
            ]
            await asyncio.gather(*coros)
            _last_run["volume_analysis"] = datetime.now(UTC).isoformat()
            # No sleep — loop immediately to fetch the next batch

        except asyncio.CancelledError:
            logger.info("Volume analysis worker shut down")
            _running["volume_analysis"] = False
            raise
        except Exception as exc:
            logger.error(f"Volume analysis worker error: {exc}")
            _running["volume_analysis"] = False
            await asyncio.sleep(5)


async def _job_recommendations():
    if not _mark_running("recommendations"):
        return
    try:
        if _yf_is_blocked_now():
            logger.info("recommendations: skipping — yfinance rate-limited")
            return
        config = portfolio.get_crawl_results("recommendations_config") or {}
        if not config.get("enabled", True):
            return
        logger.info("Recommendations job started")
        try:
            import signals
            watchlist = portfolio.get_watchlist()
            result = await asyncio.to_thread(signals.get_recommendations, watchlist, 12)

            if not result.get("recommendations"):
                logger.info("No recommendations found, using watchlist scores as fallback")
                fallback_recs = []
                for sym in watchlist:
                    try:
                        score = await asyncio.to_thread(signals.score_stock, sym)
                        if score:
                            fallback_recs.append(score)
                    except Exception:
                        pass
                result = {"recommendations": fallback_recs[:12], "total_evaluated": len(watchlist)}

            await asyncio.to_thread(portfolio.save_recommendations, result)
            logger.info(f"Recommendations cached: {result.get('total_evaluated', 0)} evaluated, {len(result.get('recommendations', []))} recs")
        except Exception as e:
            logger.error(f"Recommendations job error: {e}")
    finally:
        _mark_done("recommendations")


async def _job_screeners():
    if not _mark_running("screeners"):
        return
    try:
        await _run_screeners()
    finally:
        _mark_done("screeners")


async def _run_screeners():
    if _yf_is_blocked_now():
        logger.info("screeners: skipping — yfinance rate-limited")
        return
    config = portfolio.get_crawl_results("screener_config") or {}
    if not config.get("enabled", True):
        logger.info("Screeners disabled")
        return

    delay_secs = max(1, min(config.get("delay_secs", 5), 300))
    logger.info(f"Screeners job started (delay={delay_secs}s between screeners)")
    try:
        import signals
        screeners = [
            "undervalued", "momentum", "growth", "dividend", "beaten_down",
            "moonshots",
            "sector:tech", "sector:healthcare", "sector:financials", "sector:energy"
        ]
        for i, screener_name in enumerate(screeners):
            if _stop_flags["screeners"] or _yf_is_blocked_now():
                logger.info("Screeners: stop signal received")
                break
            try:
                if i > 0:
                    await asyncio.sleep(delay_secs)
                result = await asyncio.to_thread(signals.run_screen, screener_name, 15)

                if not result.get("results"):
                    logger.info(f"Screener {screener_name} returned 0 results, using fallback")
                    raw_universe = await asyncio.to_thread(signals.get_nyse_symbols)
                    results = []
                    for sym in raw_universe[:100]:
                        try:
                            score = await asyncio.to_thread(signals.score_stock, sym)
                            if score and score.get("score", 0) >= 10:
                                results.append(score)
                        except Exception:
                            pass
                    results.sort(key=lambda x: x.get("score", 0), reverse=True)
                    result["results"] = results[:15]

                await asyncio.to_thread(portfolio.save_screener_result, screener_name, result)
                logger.info(f"Screener {screener_name} cached: {len(result.get('results', []))} results")
            except Exception as e:
                logger.warning(f"Screener {screener_name} failed: {e}")
    except Exception as e:
        logger.error(f"Screeners job error: {e}")


async def _job_extended_insights():
    if not _mark_running("extended_insights"):
        return
    try:
        if _yf_is_blocked_now():
            logger.info("extended_insights: skipping — yfinance rate-limited")
            return
        config = portfolio.get_crawl_results("extended_insights_config") or {}
        if not config.get("enabled", True):
            return
        if _rag is None:
            logger.warning("Extended insights: RAG not ready, skipping")
            return

        skip_hours         = config.get("skip_hours", 12)
        max_per_minute     = max(1, min(config.get("max_per_minute", 5), 60))
        inter_symbol_delay = 60.0 / max_per_minute
        watchlist          = portfolio.get_watchlist()
        logger.info(f"Extended insights job: {len(watchlist)} symbols")

        import finance_router as _fr
        import main as _main

        for sym in watchlist:
            if _stop_flags["extended_insights"] or _yf_is_blocked_now():
                logger.info("Extended insights: stop signal received")
                break
            existing = portfolio.get_extended_insight(sym)
            if existing:
                try:
                    gen_at = datetime.fromisoformat(existing["generated_at"].replace("Z", "+00:00"))
                    age = datetime.now(UTC) - gen_at
                    if age < timedelta(hours=skip_hours):
                        logger.info(f"Extended insights: {sym} is fresh ({age}), skipping")
                        continue
                except Exception:
                    pass

            llm = _main.llm
            if llm is None:
                logger.warning("Extended insights: no LLM loaded, aborting job")
                break

            try:
                logger.info(f"Extended insights: generating for {sym}")
                text = await _fr.generate_extended_insight_text(sym, llm, _rag)
                if text:
                    model_name = getattr(llm, "model_path", "") or ""
                    import os as _os
                    model_name = _os.path.basename(model_name)
                    portfolio.save_extended_insight(sym, text, model_name)
                    logger.info(f"Extended insights: saved {sym} ({len(text)} chars)")
                else:
                    logger.warning(f"Extended insights: empty result for {sym}")
            except Exception as e:
                logger.error(f"Extended insights: error for {sym}: {e}")

            await asyncio.sleep(inter_symbol_delay)
    except Exception as e:
        logger.error(f"Extended insights job error: {e}")
    finally:
        _mark_done("extended_insights")


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _read_cfg(key: str, defaults: dict) -> dict:
    saved = portfolio.get_crawl_results(key) or {}
    return {**defaults, **saved}


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    try:
        portfolio.init_volume_analysis_tasks()
    except Exception as e:
        logger.warning(f"Volume analysis init error: {e}")

    _scheduler = AsyncIOScheduler(
        timezone="America/New_York",
        job_defaults={"misfire_grace_time": 3600, "coalesce": True},
    )

    # Read intervals (all in hours now; compat: fall back to interval_minutes / 60)
    def hours_from_cfg(cfg: dict, minutes_default: int = 60) -> float:
        if "interval_hours" in cfg:
            return float(cfg["interval_hours"])
        return cfg.get("interval_minutes", minutes_default) / 60.0

    crawl_cfg  = _read_cfg("crawl_config",                    {"enabled": True, "interval_hours": 1.0})
    picks_cfg  = _read_cfg("refresh_picks_config",             {"enabled": True, "interval_hours": 0.25})
    wl_cfg     = _read_cfg("score_watchlist_config",           {"enabled": True, "interval_hours": 4})
    ing_cfg    = _read_cfg("ingest_config",                    {"enabled": True, "interval_hours": 24})
    sym_cfg    = _read_cfg("refresh_symbols_config",           {"enabled": True, "interval_hours": 24})
    rec_cfg    = _read_cfg("recommendations_config",           {"enabled": True, "interval_hours": 6})
    scr_cfg    = _read_cfg("screener_config",                  {"enabled": True, "interval_hours": 12})
    ei_cfg     = _read_cfg("extended_insights_config",         {"enabled": True, "interval_hours": 12})
    svol_cfg   = _read_cfg("refresh_symbol_volumes_config",    {"enabled": True, "interval_hours": 48})

    crawl_h  = hours_from_cfg(crawl_cfg,  60)
    picks_h  = hours_from_cfg(picks_cfg,  15)
    wl_h     = float(wl_cfg.get("interval_hours", 4))
    ing_h    = float(ing_cfg.get("interval_hours", 24))
    sym_h    = float(sym_cfg.get("interval_hours", 24))
    rec_h    = float(rec_cfg.get("interval_hours", 6))
    scr_h    = float(scr_cfg.get("interval_hours", 12))
    ei_h     = float(ei_cfg.get("interval_hours", 12))
    svol_h   = float(svol_cfg.get("interval_hours", 48))

    def offset(cfg):
        return int(cfg.get("interval_offset_minutes", 0))

    _scheduler.add_job(
        _job_crawl,
        trigger=_make_trigger(hours=crawl_h, offset_minutes=offset(crawl_cfg)),
        id="crawl", name="Web Crawl", replace_existing=True,
        next_run_time=datetime.now(UTC),
    )
    _scheduler.add_job(
        _job_refresh_picks,
        trigger=_make_trigger(hours=picks_h, offset_minutes=offset(picks_cfg)),
        id="refresh_picks", name="Refresh Pick Prices", replace_existing=True,
    )
    _scheduler.add_job(
        _job_score_watchlist,
        trigger=_make_trigger(hours=wl_h, offset_minutes=offset(wl_cfg)),
        id="score_watchlist", name="Score Watchlist", replace_existing=True,
        next_run_time=datetime.now(UTC),
    )
    _scheduler.add_job(
        _job_ingest,
        trigger=_make_trigger(hours=ing_h, offset_minutes=offset(ing_cfg)),
        id="ingest", name="RAG Ingestion", replace_existing=True,
    )
    _scheduler.add_job(
        _job_refresh_symbols,
        trigger=_make_trigger(hours=sym_h, offset_minutes=offset(sym_cfg)),
        id="refresh_symbols", name="Refresh Symbol Cache", replace_existing=True,
        next_run_time=datetime.now(UTC),
    )
    _scheduler.add_job(
        _job_recommendations,
        trigger=_make_trigger(hours=rec_h, offset_minutes=offset(rec_cfg)),
        id="recommendations", name="Top Recommendations", replace_existing=True,
        next_run_time=datetime.now(UTC),
    )
    _scheduler.add_job(
        _job_screeners,
        trigger=_make_trigger(hours=scr_h, offset_minutes=offset(scr_cfg)),
        id="screeners", name="Screeners", replace_existing=True,
        next_run_time=datetime.now(UTC),
    )
    _scheduler.add_job(
        _job_extended_insights,
        trigger=_make_trigger(hours=ei_h, offset_minutes=offset(ei_cfg)),
        id="extended_insights", name="Extended Insights", replace_existing=True,
    )
    _scheduler.add_job(
        _job_refresh_symbol_volumes,
        trigger=_make_trigger(hours=svol_h, offset_minutes=offset(svol_cfg)),
        id="refresh_symbol_volumes", name="Refresh Symbol Volumes", replace_existing=True,
        next_run_time=datetime.now(UTC),
    )

    _scheduler.start()

    # Volume analysis runs as a continuous asyncio task (not an APScheduler job)
    # so it scores stocks without gaps and respects stocks_per_minute as a live rate cap.
    global _va_task
    _va_task = asyncio.get_running_loop().create_task(
        _volume_analysis_worker(), name="volume_analysis_worker"
    )

    logger.info(
        f"Scheduler started — crawl {crawl_h}h, picks {picks_h}h, "
        f"scores {wl_h}h, ingestion {ing_h}h, symbols {sym_h}h, "
        f"volume analysis continuous, recs {rec_h}h, screeners {scr_h}h, "
        f"symbol volumes {svol_h}h"
    )
    return _scheduler


def stop_scheduler():
    global _scheduler, _va_task
    if _va_task and not _va_task.done():
        _va_task.cancel()
        _va_task = None
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def get_job_running(job_id: str) -> bool:
    return _running.get(job_id, False)


_CONFIG_KEYS: dict[str, str] = {
    "crawl":                  "crawl_config",
    "refresh_picks":          "refresh_picks_config",
    "score_watchlist":        "score_watchlist_config",
    "ingest":                 "ingest_config",
    "refresh_symbols":        "refresh_symbols_config",
    "volume_analysis":        "volume_analysis_config",
    "recommendations":        "recommendations_config",
    "screeners":              "screener_config",
    "extended_insights":      "extended_insights_config",
    "refresh_symbol_volumes": "refresh_symbol_volumes_config",
}


def get_job_status() -> list[dict]:
    if not _scheduler:
        return []
    jobs = []
    for job in _scheduler.get_jobs():
        nrt = job.next_run_time
        cfg_key = _CONFIG_KEYS.get(job.id)
        cfg = portfolio.get_crawl_results(cfg_key) or {} if cfg_key else {}
        jobs.append({
            "id":       job.id,
            "name":     job.name,
            "next_run": nrt.isoformat() if nrt else None,
            "running":  _running.get(job.id, False),
            "last_run": _last_run.get(job.id),
            "enabled":  cfg.get("enabled", True),
        })
    # Volume analysis is a continuous asyncio task, not an APScheduler job —
    # inject a synthetic status entry so the UI sees it alongside the others.
    va_cfg = portfolio.get_crawl_results("volume_analysis_config") or {}
    jobs.append({
        "id":       "volume_analysis",
        "name":     "Volume Analysis",
        "next_run": None,  # continuous — no fixed next-run time
        "running":  _running.get("volume_analysis", False),
        "last_run": _last_run.get("volume_analysis"),
        "enabled":  va_cfg.get("enabled", True),
    })
    return jobs


def update_job_interval(
    job_id: str,
    hours: float | None = None,
    minutes: int | None = None,
    offset_minutes: int = 0,
) -> None:
    global _scheduler
    if not _scheduler:
        return
    trigger = _make_trigger(
        hours=hours or 0,
        minutes=minutes or 0,
        offset_minutes=offset_minutes,
    )
    _scheduler.reschedule_job(job_id, trigger=trigger)
    logger.info(f"Job {job_id} rescheduled (hours={hours}, minutes={minutes}, offset={offset_minutes}min)")


async def run_job_now(job_id: str) -> None:
    """Trigger a job by ID immediately. Clears stop flag so manual runs always proceed."""
    if job_id in _stop_flags:
        _stop_flags[job_id] = False
    if job_id == "volume_analysis":
        global _va_force_rescore
        _va_force_rescore = True
        if _va_wake:
            _va_wake.set()  # wake idle worker immediately
        return
    fn_map = {
        "crawl":             _job_crawl,
        "refresh_picks":     _job_refresh_picks,
        "score_watchlist":   _job_score_watchlist,
        "ingest":            _job_ingest,
        "refresh_symbols":   _job_refresh_symbols,
        "recommendations":   _job_recommendations,
        "screeners":         _job_screeners,
        "extended_insights": _job_extended_insights,
    }
    fn = fn_map.get(job_id)
    if fn:
        await fn()


# Keep backward-compat aliases
def update_crawl_interval(minutes: int):
    update_job_interval("crawl", hours=max(5, minutes) / 60.0)


def update_screener_interval(hours: int):
    update_job_interval("screeners", hours=max(1, hours))
