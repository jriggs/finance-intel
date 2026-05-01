"""
RAG ingestion pipeline for financial documents.

Feeds the SQLite FTS5 knowledge base (via rag.add_text_async) with:
  - SEC EDGAR 10-K / 10-Q filing text
  - SEC EDGAR 8-K earnings releases
  - EDGAR XBRL financial history summaries
  - FRED macro snapshots
  - Macrotrends historical ratios (Playwright, best-effort → EDGAR fallback)

Uses rag.has_source(source_id) so nothing is re-indexed on repeated runs.
source_id convention: {type}_{symbol}_{date_or_period}
  e.g.  edgar_10k_AAPL_2024-09-28
        edgar_facts_AAPL_2024-Q3
        fred_macro_2024-01-15
        macrotrends_AAPL_revenue_2024
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, date, datetime

import fundamentals as fd

logger = logging.getLogger("ingestion")


# ── EDGAR filings ─────────────────────────────────────────────────────────────

async def ingest_filings(symbol: str, rag, forms: list[str] | None = None) -> dict:
    """
    Fetch and ingest recent EDGAR filings for a symbol.
    Skips any filing already in the vector DB.
    Returns summary: {ingested, skipped, errors}.
    """
    forms = forms or ["10-K", "10-Q", "8-K"]
    filings = await asyncio.to_thread(fd.get_edgar_filings, symbol, forms, limit=6)
    ingested = skipped = errors = 0

    for filing in filings:
        source_id = f"edgar_{filing['form'].replace('-','').lower()}_{symbol.upper()}_{filing['date']}"

        if rag.has_source(source_id):
            skipped += 1
            continue

        logger.info(f"Ingesting {filing['form']} {symbol} {filing['date']}")
        try:
            text = await asyncio.to_thread(fd.get_edgar_filing_text, filing["doc_url"])
            if not text or len(text) < 200:
                errors += 1
                continue

            header = (
                f"SEC Filing: {filing['form']} — {symbol.upper()}\n"
                f"Date: {filing['date']}\n"
                f"Source: SEC EDGAR\n\n"
            )
            await rag.add_text_async(header + text, name=f"{symbol} {filing['form']} {filing['date']}", source_id=source_id)
            ingested += 1
            await asyncio.sleep(0.5)  # be gentle with SEC servers (10 req/s limit)
        except Exception as e:
            logger.warning(f"ingest filing {source_id}: {e}")
            errors += 1

    return {"symbol": symbol, "ingested": ingested, "skipped": skipped, "errors": errors}


# ── EDGAR XBRL financial history ──────────────────────────────────────────────

async def ingest_financial_history(symbol: str, rag) -> bool:
    """
    Ingest a plain-text XBRL financial history summary (revenue, NI, EPS, etc.).
    Re-ingests quarterly to stay fresh.
    """
    quarter   = f"{date.today().year}-Q{(date.today().month - 1) // 3 + 1}"
    source_id = f"edgar_facts_{symbol.upper()}_{quarter}"

    if rag.has_source(source_id):
        return False

    text = await asyncio.to_thread(fd.format_financial_history_text, symbol)
    if not text:
        return False

    await rag.add_text_async(text, name=f"{symbol} Financial History (EDGAR XBRL)", source_id=source_id)
    logger.info(f"Ingested XBRL history for {symbol}")
    return True


# ── Macrotrends historical ratios ─────────────────────────────────────────────

async def ingest_macrotrends(symbol: str, rag, metrics: list[str] | None = None) -> dict:
    """
    Scrape Macrotrends for historical financial ratios and ingest into RAG.
    Falls back to EDGAR XBRL text on scrape failure.
    """
    metrics   = metrics or ["revenue", "net-income", "eps", "free-cash-flow", "pe-ratio"]
    ingested  = skipped = 0

    for metric in metrics:
        source_id = f"macrotrends_{symbol.upper()}_{metric}_{date.today().year}"
        if rag.has_source(source_id):
            skipped += 1
            continue

        # Try Playwright scrape first
        rows = await fd.scrape_macrotrends(symbol, metric)

        if rows:
            lines = [f"# {symbol.upper()} — {metric.replace('-',' ').title()} (Macrotrends)\n"]
            for r in rows:
                lines.append(f"{r['date']}: {r['value']}")
            text = "\n".join(lines)
        else:
            # Fallback: EDGAR XBRL summary (only for fundamentals metrics)
            text = await asyncio.to_thread(fd.format_financial_history_text, symbol)

        if text:
            await rag.add_text_async(text, name=f"{symbol} {metric} history", source_id=source_id)
            ingested += 1

    return {"symbol": symbol, "ingested": ingested, "skipped": skipped}


# ── FRED macro snapshot ───────────────────────────────────────────────────────

async def ingest_macro_snapshot(rag) -> bool:
    """
    Ingest current FRED macro snapshot into RAG.
    Re-ingests daily.
    """
    source_id = f"fred_macro_{date.today().isoformat()}"
    if rag.has_source(source_id):
        return False

    text = await asyncio.to_thread(fd.format_macro_text)
    if not text:
        logger.warning("FRED macro text empty — check FRED_API_KEY")
        return False

    await rag.add_text_async(text, name=f"US Macro Snapshot {date.today().isoformat()}", source_id=source_id)
    logger.info("Ingested FRED macro snapshot")
    return True


# ── Full ingestion run ────────────────────────────────────────────────────────

async def run_ingestion(watchlist: list[str], rag) -> dict:
    """
    Run full ingestion pipeline for the watchlist:
      1. FRED macro snapshot (once/day)
      2. For each symbol: XBRL history, latest 10-K + 10-Q + 8-K, Macrotrends
    Returns summary dict.
    """
    logger.info(f"Starting ingestion for {len(watchlist)} symbols…")
    summary: dict = {"macro": False, "symbols": {}}

    # Macro first — shared context for all stocks
    try:
        summary["macro"] = await ingest_macro_snapshot(rag)
    except Exception as e:
        logger.error(f"macro ingestion: {e}")

    # Per-symbol — stagger to avoid hammering SEC
    # Each step is isolated: one failure does not block the others.
    for sym in watchlist:
        sym_result: dict = {}
        for step_name, coro in [
            ("xbrl",        ingest_financial_history(sym, rag)),
            ("filings",     ingest_filings(sym, rag, ["10-K", "10-Q", "8-K"])),
            ("macrotrends", ingest_macrotrends(sym, rag, ["revenue", "net-income", "pe-ratio"])),
        ]:
            try:
                sym_result[step_name] = await coro
            except Exception as e:
                sym_result[step_name] = {"error": str(e), "ingested": 0}
                logger.error(f"ingestion {sym}/{step_name}: {e} — skipping, not retrying")
        summary["symbols"][sym] = sym_result
        await asyncio.sleep(1)  # SEC rate limit courtesy pause

    summary["completed_at"] = datetime.now(UTC).isoformat()
    logger.info(f"Ingestion complete: {summary}")
    return summary


# ── On-demand single symbol ingestion ────────────────────────────────────────

async def ingest_symbol(symbol: str, rag, force: bool = False) -> dict:
    """
    Ingest all data for a single symbol immediately.
    force=True clears existing sources for this symbol first (re-index).
    """
    sym = symbol.upper()
    if force:
        # Delete existing sources for this symbol
        sources = rag.list_sources()
        for src in sources:
            src_id = src.get("id", "").upper()
            if f"_{sym}_" in src_id or src_id.startswith(f"{sym}_") or src_id.endswith(f"_{sym}"):
                with contextlib.suppress(Exception):
                    rag.delete_source(src["id"])

    result = {}
    result["xbrl"]        = await ingest_financial_history(sym, rag)
    result["filings"]     = await ingest_filings(sym, rag)
    result["macrotrends"] = await ingest_macrotrends(sym, rag)
    return result
