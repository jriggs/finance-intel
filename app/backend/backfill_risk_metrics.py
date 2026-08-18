#!/usr/bin/env python3
"""
Backfill the pre-entry risk metrics (volatility + dollar_volume) into the
existing volume-analysis cache so the daily-trade filters bite immediately
instead of waiting for the background worker to re-score every name.

Run from app/backend with the venv:  ./venv/bin/python backfill_risk_metrics.py

IMPORTANT: run this only when the server is on the NEW scorer code. The old
scorer rewrites scores_json without these fields on every re-score, so a
backfill done against the old code gets erased.

Two passes:
  1. dollar_volume — instant, no network: price × avg_volume (already cached)
     for every row, via one json_set UPDATE.
  2. volatility (+ precise median dollar_volume) — batch-download 3mo close/volume
     for the top-N names by score (the only ones that can become picks) and
     compute both with the same signals helpers used by the live scorer.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
for _pkg in ("core", "services", "ai", "routers"):
    sys.path.insert(0, str(_BACKEND / _pkg))
sys.path.insert(0, str(_BACKEND))

import pandas as pd
import yfinance as yf

import db
import market  # pyright: ignore[reportMissingImports]
import signals

DEFAULT_TOP_N = 1200   # cover Pool-1 (top ~500 by score) + margin for screener/overall picks
BATCH_SIZE    = 250


def _instant_dollar_volume() -> int:
    """Pass 1 — dollar_volume = price × avg_volume for every row (no network)."""
    conn = db._get_conn()
    with db._write_lock:
        cur = conn.execute("""
            UPDATE volume_analysis_results
            SET scores_json = json_set(
                    scores_json, '$.dollar_volume',
                    json_extract(scores_json, '$.price') * json_extract(scores_json, '$.avg_volume'))
            WHERE json_extract(scores_json, '$.price')         IS NOT NULL
              AND json_extract(scores_json, '$.avg_volume')    IS NOT NULL
              AND json_extract(scores_json, '$.dollar_volume') IS NULL
        """)
        conn.commit()
    return cur.rowcount


def _volatility_topn(top_n: int | None, batch_size: int) -> int:
    """
    Pass 2 — fetch close/volume for the top-N by score (or all names when top_n
    is None) and compute vol + median $-vol. Incremental: names that already
    have volatility are skipped, so a deeper re-run only fetches the new tail.
    """
    ranked = [r["symbol"] for r in db.get_all_volume_results()]        # sorted by overall_score desc
    if top_n is not None:
        ranked = ranked[:top_n]
    have   = {s for s, v in db.get_all_volume_scores().items() if v.get("volatility") is not None}
    syms   = [s for s in ranked if s not in have and re.match(r"^[A-Z]{1,5}$", s)]
    conn   = db._get_conn()
    updated = 0
    if not syms:
        print("  nothing to fill — all requested names already have volatility.")
        return 0

    for i in range(0, len(syms), batch_size):
        if market._yf_is_blocked():
            print("  yfinance circuit breaker tripped — stopping (re-run later to resume).")
            break
        batch = syms[i:i + batch_size]
        try:
            raw = yf.download(batch, period="3mo", interval="1d", progress=False, auto_adjust=True)
        except Exception as e:
            print(f"  batch {i // batch_size}: download failed ({e}); skipping")
            continue
        if raw is None or raw.empty:
            continue

        multi   = isinstance(raw.columns, pd.MultiIndex)
        close_d = raw["Close"]  if multi else raw[["Close"]].rename(columns={"Close": batch[0]})
        vol_d   = raw["Volume"] if multi else raw[["Volume"]].rename(columns={"Volume": batch[0]})

        rows = []
        for sym in batch:
            if sym not in close_d.columns:
                continue
            df = pd.DataFrame({"close": close_d[sym], "volume": vol_d.get(sym)}).dropna()
            if df.empty:
                continue
            vol = signals.annualized_volatility(df)
            dv  = signals.median_dollar_volume(df)
            if vol is not None or dv is not None:
                rows.append((vol, dv, sym))

        if rows:
            with db._write_lock:
                conn.executemany(
                    "UPDATE volume_analysis_results "
                    "SET scores_json = json_set(json_set(scores_json,'$.volatility',?),'$.dollar_volume',?) "
                    "WHERE symbol = ?",
                    rows,
                )
                conn.commit()
            updated += len(rows)
        print(f"  batch {i // batch_size + 1}/{(len(syms) + batch_size - 1) // batch_size}: "
              f"+{len(rows)} names (total {updated})")
        time.sleep(1.0)   # be gentle on yfinance / the running scheduler

    return updated


def _coverage() -> tuple[int, int, int]:
    scores = db.get_all_volume_scores()
    n   = len(scores)
    dv  = sum(1 for s in scores.values() if s.get("dollar_volume") is not None)
    vol = sum(1 for s in scores.values() if s.get("volatility") is not None)
    return n, dv, vol


def _parse_depth() -> int | None:
    """Depth from argv[1]: an integer count, or 'all' for the whole cache."""
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip().lower()
        if arg in ("all", "full", "-1"):
            return None
        try:
            return int(arg)
        except ValueError:
            print(f"  (ignoring unrecognised depth {sys.argv[1]!r}; using {DEFAULT_TOP_N})")
    return DEFAULT_TOP_N


def main() -> None:
    depth = _parse_depth()

    print("Pass 1 — instant dollar_volume (price × avg_volume)…")
    print(f"  updated {_instant_dollar_volume()} rows")

    label = "ALL names" if depth is None else f"top {depth} by score"
    print(f"Pass 2 — volatility + median $-vol for {label}…")
    print(f"  filled {_volatility_topn(depth, BATCH_SIZE)} names")

    n, dv, vol = _coverage()
    print(f"\nCoverage now: {n} scored | dollar_volume {dv} | volatility {vol}")


if __name__ == "__main__":
    main()
