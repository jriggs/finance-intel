#!/usr/bin/env python3
"""
Refresh symbols list from top market cap stocks.
Clears old data and rebuilds.
"""

from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("refresh_symbols")

import fundamentals
import db as portfolio


def main():
    # Fetch NYSE + NASDAQ stocks (deduped, ~3000 largest)
    logger.info("Fetching liquid US stocks (NYSE + NASDAQ, top 3000)...")
    symbols = fundamentals.get_liquid_us_stocks(limit=3000)

    if not symbols:
        logger.error("Failed to fetch symbols")
        sys.exit(1)

    logger.info(f"Fetched {len(symbols)} symbols")

    # Convert to symbol/name format for portfolio.save_symbols
    symbol_list = [
        {"symbol": s["symbol"], "name": s["name"]}
        for s in symbols
    ]

    # Clear and reload symbols table
    logger.info("Clearing and reloading symbols table...")
    portfolio.save_symbols(symbol_list)
    logger.info(f"Saved {len(symbol_list)} symbols to DB")

    # Clear volume analysis tasks to force reinit
    logger.info("Clearing volume analysis tasks...")
    with portfolio._write_lock:
        conn = portfolio._get_conn()
        conn.execute("DELETE FROM volume_analysis_tasks")
        conn.execute("DELETE FROM volume_analysis_results")
        conn.execute("DELETE FROM screener_results")
        conn.execute("DELETE FROM recommendations")
        conn.commit()
    logger.info("Cleared volume analysis tasks, results, screener cache, and recommendations")

    # Reinit volume analysis tasks (all 3000 symbols)
    logger.info("Reinitializing volume analysis tasks (3000 symbols)...")
    portfolio.init_volume_analysis_tasks(limit=3000)
    task_count = portfolio.get_volume_task_counts()["total"]
    logger.info(f"Created {task_count} volume analysis tasks")

    logger.info("Done! Symbols refreshed and DB reset.")
    logger.info(f"Total symbols: {len(symbol_list)}")

if __name__ == "__main__":
    main()
