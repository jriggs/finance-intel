"""
SQLite persistence for all app state:
  - Watchlist      (symbols to track)
  - Picks          (model-suggested trades with outcome tracking)
  - Crawl cache    (scraped content / scored results per key)
  - Symbols        (ticker + company name for autocomplete)
  - Sentiment      (per-symbol sentiment cache)
  - App settings   (key-value: model pref, etc.)
  - RAG sources    (replaces sources.json)

Single DB at app/data/finance.db

Connection strategy: one sqlite3 connection per thread (thread-local pool).
Reads need no Python-level lock — WAL journal handles concurrent readers.
Writes serialize through _write_lock to prevent "database is locked" under load.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("portfolio")


# ── Typed setting keys ────────────────────────────────────────────────────────

class AppSettings:
    """Typed constants for app_settings keys. Use instead of raw strings."""
    MODEL_PATH               = "model_path"
    YFINANCE_CACHE_CONFIG    = "yfinance_cache_config"
    VOLUME_ANALYSIS_CONFIG   = "volume_analysis_config"
    SCREENER_CONFIG          = "screener_config"
    CRAWL_CONFIG             = "crawl_config"
    SIGNAL_SCORES            = "signal_scores"
    REFRESH_PICKS_CONFIG     = "refresh_picks_config"
    SCORE_WATCHLIST_CONFIG   = "score_watchlist_config"
    INGEST_CONFIG            = "ingest_config"
    REFRESH_SYMBOLS_CONFIG   = "refresh_symbols_config"
    RECOMMENDATIONS_CONFIG   = "recommendations_config"
    EXTENDED_INSIGHTS_CONFIG = "extended_insights_config"
    YF_NOT_FOUND_SYMBOLS     = "yf_not_found_symbols"


# ── Paths ─────────────────────────────────────────────────────────────────────

_DEFAULT_DB = Path(__file__).parent.parent.parent / "data" / "finance.db"
DB_PATH = Path(os.environ.get("FINANCE_DB_PATH", str(_DEFAULT_DB)))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ── Connection pool (thread-local) ────────────────────────────────────────────

_local      = threading.local()
_write_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Return this thread's sqlite3 connection, creating it on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL: readers never block writers, writers never block readers
        conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL is safe with WAL and avoids a fsync per write
        conn.execute("PRAGMA synchronous=NORMAL")
        # 32 MB page cache per connection
        conn.execute("PRAGMA cache_size=-32000")
        # Keep temp tables in memory
        conn.execute("PRAGMA temp_store=MEMORY")
        # Memory-mapped I/O for large sequential scans (256 MB)
        conn.execute("PRAGMA mmap_size=268435456")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


# ── Schema ────────────────────────────────────────────────────────────────────

def _init_schema() -> None:
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS picks (
            id                 TEXT PRIMARY KEY,
            symbol             TEXT NOT NULL,
            direction          TEXT NOT NULL,
            entry_price        REAL,
            entry_date         TEXT,
            target_price       REAL,
            stop_loss          REAL,
            horizon_days       INTEGER,
            signal_score       INTEGER,
            reasoning          TEXT,
            status             TEXT DEFAULT 'open',
            current_price      REAL,
            unrealized_pnl     REAL DEFAULT 0,
            unrealized_pnl_pct REAL DEFAULT 0,
            outcome            TEXT,
            exit_price         REAL,
            exit_date          TEXT,
            final_return       REAL,
            was_correct        INTEGER
        );

        CREATE TABLE IF NOT EXISTS crawl_cache (
            key        TEXT PRIMARY KEY,
            data       TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS symbols (
            symbol          TEXT PRIMARY KEY,
            name            TEXT DEFAULT '',
            fetch_failed_at TEXT,
            avg_volume      REAL
        );

        CREATE TABLE IF NOT EXISTS sentiment (
            symbol     TEXT PRIMARY KEY,
            score      REAL,
            summary    TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS volume_analysis_tasks (
            id             TEXT PRIMARY KEY,
            symbol         TEXT NOT NULL UNIQUE,
            status         TEXT DEFAULT 'pending',
            created_at     TEXT NOT NULL,
            last_scored_at TEXT,
            next_run_at    TEXT,
            retry_count    INTEGER DEFAULT 0,
            needs_rescore  INTEGER DEFAULT 0,
            priority       INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS volume_analysis_results (
            symbol           TEXT PRIMARY KEY,
            overall_score    REAL,
            macro_score      REAL,
            sentiment_score  REAL,
            short_term_score REAL,
            long_term_score  REAL,
            recommendation   TEXT,
            grade            TEXT,
            scores_json      TEXT,
            last_updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recommendations (
            id              TEXT PRIMARY KEY,
            data_json       TEXT NOT NULL,
            generated_at    TEXT NOT NULL,
            total_evaluated INTEGER
        );

        CREATE TABLE IF NOT EXISTS screener_results (
            screener_name  TEXT PRIMARY KEY,
            data_json      TEXT NOT NULL,
            generated_at   TEXT NOT NULL,
            universe_size  INTEGER,
            shortlist_size INTEGER
        );

        CREATE TABLE IF NOT EXISTS extended_insights (
            symbol       TEXT PRIMARY KEY,
            insight_text TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            model_name   TEXT
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rag_sources (
            id         TEXT PRIMARY KEY,
            data_json  TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rag_chunks (
            id         TEXT PRIMARY KEY,
            source_id  TEXT NOT NULL,
            source_name TEXT NOT NULL,
            content    TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts
            USING fts5(content, source_id UNINDEXED, source_name UNINDEXED,
                       content='rag_chunks', content_rowid='rowid');

        -- Indexes for hot query paths
        CREATE INDEX IF NOT EXISTS idx_picks_status
            ON picks(status);
        CREATE INDEX IF NOT EXISTS idx_vat_rescore
            ON volume_analysis_tasks(needs_rescore, priority, last_scored_at);
        CREATE INDEX IF NOT EXISTS idx_vat_symbol
            ON volume_analysis_tasks(symbol);
    """)
    conn.commit()


def _migrate() -> None:
    """Apply incremental schema changes to existing databases."""
    conn = _get_conn()

    existing_vat = {row[1] for row in conn.execute("PRAGMA table_info(volume_analysis_tasks)")}
    if "needs_rescore" not in existing_vat:
        conn.execute("ALTER TABLE volume_analysis_tasks ADD COLUMN needs_rescore INTEGER DEFAULT 0")
        conn.commit()
    if "priority" not in existing_vat:
        conn.execute("ALTER TABLE volume_analysis_tasks ADD COLUMN priority INTEGER DEFAULT 0")
        conn.commit()

    sym_cols = {row[1] for row in conn.execute("PRAGMA table_info(symbols)")}
    if "fetch_failed_at" not in sym_cols:
        conn.execute("ALTER TABLE symbols ADD COLUMN fetch_failed_at TEXT")
        conn.commit()
    if "avg_volume" not in sym_cols:
        conn.execute("ALTER TABLE symbols ADD COLUMN avg_volume REAL")
        conn.commit()

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "app_settings" not in tables:
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
    if "rag_sources" not in tables:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_sources (
                id TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        conn.commit()


def _bootstrap_avg_volumes() -> None:
    """Seed symbols.avg_volume from already-scored volume_analysis_results on first run."""
    conn = _get_conn()
    null_count = conn.execute("SELECT COUNT(*) FROM symbols WHERE avg_volume IS NULL").fetchone()[0]
    if null_count == 0:
        return  # already populated

    rows = conn.execute("SELECT symbol, scores_json FROM volume_analysis_results WHERE scores_json IS NOT NULL").fetchall()
    updates = []
    for r in rows:
        try:
            scores = json.loads(r["scores_json"])
            avg_vol = scores.get("avg_volume")
            if avg_vol is not None:
                updates.append((float(avg_vol), r["symbol"]))
        except Exception:
            pass

    if updates:
        with _write_lock:
            conn.executemany("UPDATE symbols SET avg_volume = ? WHERE symbol = ?", updates)
            conn.commit()


# Initialise on import (runs in the main thread)
_init_schema()
_migrate()
_bootstrap_avg_volumes()


# ── Watchlist ─────────────────────────────────────────────────────────────────

def get_watchlist() -> list[str]:
    rows = _get_conn().execute("SELECT symbol FROM watchlist ORDER BY rowid").fetchall()
    if not rows:
        defaults = ["AAPL", "MSFT", "GOOGL", "NVDA", "AMZN"]
        with _write_lock:
            _get_conn().executemany(
                "INSERT OR IGNORE INTO watchlist(symbol) VALUES(?)",
                [(s,) for s in defaults],
            )
            _get_conn().commit()
        return defaults
    return [r["symbol"] for r in rows]


def add_to_watchlist(symbol: str) -> list[str]:
    sym = symbol.upper().strip()
    with _write_lock:
        _get_conn().execute("INSERT OR IGNORE INTO watchlist(symbol) VALUES(?)", (sym,))
        _get_conn().commit()
    return get_watchlist()


def remove_from_watchlist(symbol: str) -> list[str]:
    sym = symbol.upper().strip()
    with _write_lock:
        _get_conn().execute("DELETE FROM watchlist WHERE symbol=?", (sym,))
        _get_conn().commit()
    return get_watchlist()


# ── Picks ─────────────────────────────────────────────────────────────────────

def _insert_pick(p: dict) -> None:
    _get_conn().execute("""
        INSERT OR REPLACE INTO picks
            (id, symbol, direction, entry_price, entry_date, target_price, stop_loss,
             horizon_days, signal_score, reasoning, status, current_price,
             unrealized_pnl, unrealized_pnl_pct, outcome, exit_price,
             exit_date, final_return, was_correct)
        VALUES
            (:id,:symbol,:direction,:entry_price,:entry_date,:target_price,:stop_loss,
             :horizon_days,:signal_score,:reasoning,:status,:current_price,
             :unrealized_pnl,:unrealized_pnl_pct,:outcome,:exit_price,
             :exit_date,:final_return,:was_correct)
    """, {
        "id":                 p.get("id", str(uuid.uuid4())[:8]),
        "symbol":             p.get("symbol", ""),
        "direction":          p.get("direction", "buy"),
        "entry_price":        p.get("entry_price"),
        "entry_date":         p.get("entry_date"),
        "target_price":       p.get("target_price"),
        "stop_loss":          p.get("stop_loss"),
        "horizon_days":       p.get("horizon_days", 30),
        "signal_score":       p.get("signal_score"),
        "reasoning":          p.get("reasoning", ""),
        "status":             p.get("status", "open"),
        "current_price":      p.get("current_price"),
        "unrealized_pnl":     p.get("unrealized_pnl", 0.0),
        "unrealized_pnl_pct": p.get("unrealized_pnl_pct", 0.0),
        "outcome":            p.get("outcome"),
        "exit_price":         p.get("exit_price"),
        "exit_date":          p.get("exit_date"),
        "final_return":       p.get("final_return"),
        "was_correct":        int(p["was_correct"]) if p.get("was_correct") is not None else None,
    })


def _row_to_pick(row: sqlite3.Row) -> dict:
    p = dict(row)
    if p.get("was_correct") is not None:
        p["was_correct"] = bool(p["was_correct"])
    return p


def get_picks() -> list[dict]:
    """Return all picks from DB."""
    rows = _get_conn().execute("SELECT * FROM picks ORDER BY entry_date DESC").fetchall()
    return [_row_to_pick(r) for r in rows]


def get_open_picks() -> list[dict]:
    """Return only open (not closed) picks."""
    rows = _get_conn().execute(
        "SELECT * FROM picks WHERE status='open' ORDER BY entry_date DESC"
    ).fetchall()
    return [_row_to_pick(r) for r in rows]


def update_pick_prices(prices: dict[str, float]) -> None:
    """Persist refreshed current prices for open picks. Called by the router layer."""
    if not prices:
        return
    now = datetime.now(UTC).isoformat()
    rows = _get_conn().execute(
        "SELECT id, symbol, direction, entry_price FROM picks WHERE status='open'"
    ).fetchall()
    updates = []
    for row in rows:
        price = prices.get(row["symbol"])
        if price is None:
            continue
        if row["direction"] == "buy":
            pnl = (price - row["entry_price"]) / row["entry_price"] * 100
        else:
            pnl = (row["entry_price"] - price) / row["entry_price"] * 100
        updates.append((round(price, 4), round(pnl, 2), round(pnl, 2), row["id"]))
    if updates:
        with _write_lock:
            _get_conn().executemany(
                "UPDATE picks SET current_price=?, unrealized_pnl=?, unrealized_pnl_pct=? WHERE id=?",
                updates,
            )
            _get_conn().commit()


def add_pick(
    symbol: str,
    entry_price: float,
    direction: str,
    reasoning: str,
    target_price: float | None = None,
    stop_loss: float | None = None,
    horizon_days: int = 30,
    signal_score: int | None = None,
) -> dict:
    pick = {
        "id":                 str(uuid.uuid4())[:8],
        "symbol":             symbol.upper(),
        "direction":          direction.lower(),
        "entry_price":        round(entry_price, 4),
        "entry_date":         datetime.now(UTC).isoformat(),
        "target_price":       round(target_price, 4) if target_price else None,
        "stop_loss":          round(stop_loss, 4) if stop_loss else None,
        "horizon_days":       horizon_days,
        "signal_score":       signal_score,
        "reasoning":          reasoning,
        "status":             "open",
        "current_price":      entry_price,
        "unrealized_pnl":     0.0,
        "unrealized_pnl_pct": 0.0,
        "outcome":            None,
        "exit_price":         None,
        "exit_date":          None,
        "final_return":       None,
        "was_correct":        None,
    }
    with _write_lock:
        _insert_pick(pick)
        _get_conn().commit()
    return pick


def close_pick(pick_id: str, exit_price: float, outcome: str) -> dict | None:
    row = _get_conn().execute("SELECT * FROM picks WHERE id=?", (pick_id,)).fetchone()
    if not row:
        return None
    p = _row_to_pick(row)
    p["status"]     = "closed"
    p["exit_price"] = round(exit_price, 4)
    p["exit_date"]  = datetime.now(UTC).isoformat()
    p["outcome"]    = outcome
    if p["direction"] == "buy":
        p["final_return"] = round((exit_price - p["entry_price"]) / p["entry_price"] * 100, 2)
    else:
        p["final_return"] = round((p["entry_price"] - exit_price) / p["entry_price"] * 100, 2)
    p["was_correct"] = p["final_return"] > 0
    with _write_lock:
        _insert_pick(p)
        _get_conn().commit()
    return p


def get_pick_stats() -> dict:
    rows   = _get_conn().execute("SELECT * FROM picks").fetchall()
    picks  = [_row_to_pick(r) for r in rows]
    closed = [p for p in picks if p.get("status") == "closed" and p.get("final_return") is not None]
    if not closed:
        return {"total": len(picks), "closed": 0, "win_rate": None, "avg_return": None}
    wins    = [p for p in closed if p.get("was_correct")]
    returns = [p["final_return"] for p in closed]
    best    = max(closed, key=lambda p: p["final_return"])
    worst   = min(closed, key=lambda p: p["final_return"])
    return {
        "total":        len(picks),
        "open":         len([p for p in picks if p.get("status") == "open"]),
        "closed":       len(closed),
        "wins":         len(wins),
        "win_rate":     round(len(wins) / len(closed) * 100, 1),
        "avg_return":   round(sum(returns) / len(returns), 2),
        "best_pick":    {"symbol": best["symbol"], "return": best["final_return"]},
        "worst_pick":   {"symbol": worst["symbol"], "return": worst["final_return"]},
        "total_return": round(sum(returns), 2),
    }


# ── Symbols (autocomplete) ────────────────────────────────────────────────────

def save_symbols(symbols: list[dict]) -> None:
    """Replace entire symbols table with fresh data."""
    with _write_lock:
        _get_conn().execute("DELETE FROM symbols")
        _get_conn().executemany(
            "INSERT INTO symbols(symbol, name) VALUES(?,?)",
            [(s.get("symbol", ""), s.get("name", "")) for s in symbols if s.get("symbol")],
        )
        _get_conn().commit()


def save_symbol_avg_volumes(volumes: dict[str, float]) -> None:
    """Update avg_volume for a batch of symbols."""
    if not volumes:
        return
    with _write_lock:
        _get_conn().executemany(
            "UPDATE symbols SET avg_volume = ? WHERE symbol = ?",
            [(v, s) for s, v in volumes.items()],
        )
        _get_conn().commit()


def get_symbols() -> list[dict]:
    rows = _get_conn().execute("SELECT symbol, name FROM symbols ORDER BY symbol").fetchall()
    return [{"symbol": r["symbol"], "name": r["name"]} for r in rows]


def get_symbol_count() -> int:
    return _get_conn().execute("SELECT COUNT(*) FROM symbols").fetchone()[0]


def get_symbol_names(symbols: list[str]) -> dict[str, str]:
    """Return {symbol: name} for the given list. Unknown symbols omitted."""
    if not symbols:
        return {}
    placeholders = ",".join("?" * len(symbols))
    rows = _get_conn().execute(
        f"SELECT symbol, name FROM symbols WHERE symbol IN ({placeholders})",
        [s.upper() for s in symbols],
    ).fetchall()
    return {r["symbol"]: r["name"] for r in rows}


def mark_symbol_fetch_failed(symbols: list[str]) -> None:
    """Record that yfinance returned no data for these symbols (e.g. delisted)."""
    if not symbols:
        return
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        _get_conn().executemany(
            "UPDATE symbols SET fetch_failed_at=? WHERE symbol=?",
            [(now, s.upper()) for s in symbols],
        )
        _get_conn().commit()


def get_fetch_failed_symbols(within_days: int = 7) -> set[str]:
    """Return symbols that failed within the last `within_days` days."""
    rows   = _get_conn().execute(
        "SELECT symbol, fetch_failed_at FROM symbols WHERE fetch_failed_at IS NOT NULL"
    ).fetchall()
    cutoff = datetime.now(UTC).timestamp() - within_days * 86400
    result: set[str] = set()
    for r in rows:
        with contextlib.suppress(Exception):
            if datetime.fromisoformat(r["fetch_failed_at"]).timestamp() >= cutoff:
                result.add(r["symbol"])
    return result


# ── Crawl cache ───────────────────────────────────────────────────────────────

def save_crawl_results(source: str, results: Any) -> None:
    if source == "nyse_symbols":
        syms = results.get("symbols", []) if isinstance(results, dict) else []
        if syms:
            save_symbols(syms)
        return
    data = {"items": results} if isinstance(results, list) else results
    now  = datetime.now(UTC).isoformat()
    with _write_lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO crawl_cache(key, data, fetched_at) VALUES(?,?,?)",
            (source, json.dumps(data), now),
        )
        _get_conn().commit()


def get_crawl_results(source: str | None = None) -> Any:
    if source == "nyse_symbols":
        syms  = get_symbols()
        count = len(syms)
        return {"symbols": syms, "count": count, "fetched_at": datetime.now(UTC).isoformat()} if count else {}

    if source:
        row = _get_conn().execute(
            "SELECT data, fetched_at FROM crawl_cache WHERE key=?", (source,)
        ).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["data"])
        except Exception:
            data = {}
        if isinstance(data, dict):
            data["fetched_at"] = row["fetched_at"]
        return data

    rows = _get_conn().execute("SELECT key, data, fetched_at FROM crawl_cache").fetchall()
    out: dict[str, Any] = {}
    for r in rows:
        with contextlib.suppress(Exception):
            val = json.loads(r["data"])
            if isinstance(val, dict):
                val["fetched_at"] = r["fetched_at"]
            out[r["key"]] = val
    return out


# ── Sentiment cache ───────────────────────────────────────────────────────────

def save_sentiment(symbol: str, score: float, summary: str) -> None:
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO sentiment(symbol, score, summary, updated_at) VALUES(?,?,?,?)",
            (symbol.upper(), score, summary, now),
        )
        _get_conn().commit()


def get_sentiment(symbol: str | None = None) -> dict:
    if symbol:
        row = _get_conn().execute(
            "SELECT score, summary, updated_at FROM sentiment WHERE symbol=?",
            (symbol.upper(),),
        ).fetchone()
        return dict(row) if row else {}
    rows = _get_conn().execute(
        "SELECT symbol, score, summary, updated_at FROM sentiment"
    ).fetchall()
    return {
        r["symbol"]: {"score": r["score"], "summary": r["summary"], "updated_at": r["updated_at"]}
        for r in rows
    }


# ── Volume Analysis Task Queue ─────────────────────────────────────────────────

def init_volume_analysis_tasks(limit: int = 3000) -> None:
    """
    Queue symbols for volume analysis.
    When avg_volume data covers ≥50% of symbols, queues the top N by volume.
    Otherwise queues ALL symbols so scoring naturally builds volume data across
    the full alphabet — sync_volume_analysis_tasks() will trim to top N once
    coverage is sufficient.
    """
    total = _get_conn().execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    known = _get_conn().execute("SELECT COUNT(*) FROM symbols WHERE avg_volume IS NOT NULL").fetchone()[0]
    have_sufficient_data = total > 0 and (known / total) >= 0.5

    if have_sufficient_data:
        rows = _get_conn().execute(
            "SELECT symbol, name FROM symbols ORDER BY avg_volume DESC NULLS LAST, symbol LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = _get_conn().execute(
            "SELECT symbol, name FROM symbols ORDER BY symbol",
        ).fetchall()

    syms = [{"symbol": r["symbol"], "name": r["name"]} for r in rows]
    existing = {r["symbol"] for r in _get_conn().execute("SELECT symbol FROM volume_analysis_tasks").fetchall()}
    # Use epoch for created_at so never-scored symbols sort before stale existing ones
    _epoch = "1970-01-01T00:00:00+00:00"
    new_tasks = [
        (str(uuid.uuid4()), s["symbol"], _epoch, _epoch)
        for s in syms if s["symbol"] not in existing
    ]
    if new_tasks:
        with _write_lock:
            _get_conn().executemany(
                "INSERT INTO volume_analysis_tasks(id, symbol, created_at, next_run_at) VALUES(?,?,?,?)",
                new_tasks,
            )
            _get_conn().commit()


def sync_volume_analysis_tasks(limit: int = 3000) -> tuple[int, int]:
    """
    Re-evaluate which symbols belong in the top N by avg_volume.
    Adds tasks for newly-qualified symbols and removes tasks for symbols that
    have dropped out.  Returns (added, removed).
    Skips removals if fewer than 50% of symbols have avg_volume data — avoids
    incorrectly evicting stocks before the first full volume refresh completes.
    """
    total_syms = _get_conn().execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    known_vol  = _get_conn().execute("SELECT COUNT(*) FROM symbols WHERE avg_volume IS NOT NULL").fetchone()[0]
    have_sufficient_data = total_syms > 0 and (known_vol / total_syms) >= 0.5

    rows = _get_conn().execute(
        "SELECT symbol FROM symbols ORDER BY avg_volume DESC NULLS LAST, symbol LIMIT ?",
        (limit,),
    ).fetchall()
    top_symbols = {r["symbol"] for r in rows}

    existing = {r["symbol"] for r in _get_conn().execute("SELECT symbol FROM volume_analysis_tasks").fetchall()}

    to_add    = top_symbols - existing
    to_remove = existing - top_symbols if have_sufficient_data else set()

    _epoch = "1970-01-01T00:00:00+00:00"
    new_tasks = [
        (str(uuid.uuid4()), sym, _epoch, _epoch)
        for sym in to_add
    ]

    with _write_lock:
        if new_tasks:
            _get_conn().executemany(
                "INSERT INTO volume_analysis_tasks(id, symbol, created_at, next_run_at) VALUES(?,?,?,?)",
                new_tasks,
            )
        if to_remove:
            _get_conn().executemany(
                "DELETE FROM volume_analysis_tasks WHERE symbol = ?",
                [(sym,) for sym in to_remove],
            )
        _get_conn().commit()

    return len(to_add), len(to_remove)


def get_next_volume_task(skip_hours: int = 24) -> dict | None:
    now = datetime.now(UTC).isoformat()
    row = _get_conn().execute("""
        SELECT id, symbol, status, last_scored_at, retry_count
        FROM volume_analysis_tasks
        WHERE status = 'pending'
          AND (last_scored_at IS NULL OR
               datetime(last_scored_at) < datetime(?, '-' || ? || ' hours'))
        ORDER BY COALESCE(last_scored_at, created_at) ASC
        LIMIT 1
    """, (now, skip_hours)).fetchone()
    return dict(row) if row else None


def mark_volume_tasks_for_rescore(skip_hours: int = 24) -> int:
    """Flag stale tasks. Returns count of newly flagged rows."""
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        cur = _get_conn().execute("""
            UPDATE volume_analysis_tasks
            SET needs_rescore = 1
            WHERE needs_rescore = 0 AND (
                last_scored_at IS NULL
                OR (status = 'pending' AND last_scored_at IS NOT NULL
                    AND datetime(last_scored_at) < datetime(?, '-1 hours'))
                OR (status IN ('done', 'error')
                    AND datetime(last_scored_at) < datetime(?, '-' || ? || ' hours'))
            )
        """, (now, now, skip_hours))
        _get_conn().commit()
    return cur.rowcount


def get_next_volume_tasks(limit: int = 40) -> list[dict]:
    """Get next batch of flagged tasks, high priority first."""
    rows = _get_conn().execute("""
        SELECT id, symbol, status, last_scored_at, retry_count, priority
        FROM volume_analysis_tasks
        WHERE needs_rescore = 1
        ORDER BY priority DESC, COALESCE(last_scored_at, created_at) ASC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_volume_task_priority(symbol: str, priority: int) -> None:
    with _write_lock:
        _get_conn().execute("""
            UPDATE volume_analysis_tasks SET priority=?, needs_rescore=1 WHERE symbol=?
        """, (priority, symbol.upper()))
        _get_conn().commit()


def update_volume_task(symbol: str, status: str, retry_count: int = 0) -> None:
    """Update task status. Clears needs_rescore on done/error."""
    now        = datetime.now(UTC).isoformat()
    clear_flag = 1 if status in ("done", "error") else 0
    with _write_lock:
        _get_conn().execute("""
            UPDATE volume_analysis_tasks
            SET status=?, last_scored_at=?, next_run_at=?, retry_count=?,
                needs_rescore = CASE WHEN ? THEN 0 ELSE needs_rescore END
            WHERE symbol=?
        """, (status, now, now, retry_count, clear_flag, symbol.upper()))
        _get_conn().commit()


def set_volume_task_cooldown(symbol: str, days: int = 10) -> None:
    now      = datetime.now(UTC)
    next_run = (now + timedelta(days=days)).isoformat()
    with _write_lock:
        _get_conn().execute("""
            UPDATE volume_analysis_tasks
            SET status='error', last_scored_at=?, next_run_at=?, needs_rescore=0
            WHERE symbol=?
        """, (now.isoformat(), next_run, symbol.upper()))
        _get_conn().commit()


def save_volume_result(symbol: str, scores: dict) -> None:
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        _get_conn().execute("""
            INSERT OR REPLACE INTO volume_analysis_results
            (symbol, overall_score, macro_score, sentiment_score,
             short_term_score, long_term_score, recommendation, grade,
             scores_json, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol.upper(),
            scores.get("score", 0),
            scores.get("macro", {}).get("score", 0),
            scores.get("sentiment", {}).get("score", 0),
            scores.get("short_term", {}).get("score", 0),
            scores.get("long_term", {}).get("score", 0),
            scores.get("recommendation", ""),
            scores.get("grade", ""),
            json.dumps(scores),
            now,
        ))
        avg_vol = scores.get("avg_volume")
        if avg_vol is not None:
            _get_conn().execute(
                "UPDATE symbols SET avg_volume = ? WHERE symbol = ?",
                (avg_vol, symbol.upper()),
            )
        _get_conn().commit()


def get_volume_result(symbol: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT scores_json FROM volume_analysis_results WHERE symbol=?",
        (symbol.upper(),),
    ).fetchone()
    if row and row[0]:
        with contextlib.suppress(Exception):
            return json.loads(row[0])
    return None


def get_all_volume_scores() -> dict[str, dict]:
    rows   = _get_conn().execute(
        "SELECT symbol, scores_json FROM volume_analysis_results WHERE scores_json IS NOT NULL"
    ).fetchall()
    result = {}
    for row in rows:
        with contextlib.suppress(Exception):
            result[row[0]] = json.loads(row[1])
    return result


def get_volume_results(limit: int = 500) -> tuple[list[dict], int]:
    """Return (top-N results by score, total count)."""
    total = _get_conn().execute("SELECT COUNT(*) FROM volume_analysis_results").fetchone()[0]
    rows  = _get_conn().execute("""
        SELECT symbol, overall_score, macro_score, sentiment_score,
               short_term_score, long_term_score, recommendation, grade, last_updated_at
        FROM volume_analysis_results
        ORDER BY overall_score DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows], total


def get_volume_task_counts() -> dict:
    row = _get_conn().execute("""
        SELECT
            COUNT(*)              AS total,
            SUM(needs_rescore)    AS pending,
            COUNT(last_scored_at) AS ever_scored
        FROM volume_analysis_tasks
    """).fetchone()
    return {
        "total":       row["total"]       if row else 0,
        "pending":     row["pending"]     if row else 0,
        "ever_scored": row["ever_scored"] if row else 0,
    }


# ── Recommendations ───────────────────────────────────────────────────────────

def save_recommendations(data: dict) -> None:
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        _get_conn().execute("""
            INSERT OR REPLACE INTO recommendations(id, data_json, generated_at, total_evaluated)
            VALUES(?, ?, ?, ?)
        """, ("recommendations", json.dumps(data), now, data.get("total_evaluated")))
        _get_conn().commit()


def get_recommendations() -> dict | None:
    row = _get_conn().execute(
        "SELECT data_json, generated_at FROM recommendations WHERE id=?",
        ("recommendations",),
    ).fetchone()
    if not row:
        return None
    return {"data": json.loads(row["data_json"]), "generated_at": row["generated_at"]}


# ── Screener Results ──────────────────────────────────────────────────────────

def save_screener_result(screener_name: str, data: dict) -> None:
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        _get_conn().execute("""
            INSERT OR REPLACE INTO screener_results
            (screener_name, data_json, generated_at, universe_size, shortlist_size)
            VALUES(?, ?, ?, ?, ?)
        """, (screener_name, json.dumps(data), now, data.get("universe_size"), data.get("shortlist_size")))
        _get_conn().commit()


def get_screener_result(screener_name: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT data_json, generated_at FROM screener_results WHERE screener_name=?",
        (screener_name,),
    ).fetchone()
    if not row:
        return None
    return {"data": json.loads(row["data_json"]), "generated_at": row["generated_at"]}


def get_all_screener_results() -> dict:
    rows = _get_conn().execute(
        "SELECT screener_name, data_json, generated_at FROM screener_results ORDER BY screener_name"
    ).fetchall()
    return {
        r["screener_name"]: {"data": json.loads(r["data_json"]), "generated_at": r["generated_at"]}
        for r in rows
    }


# ── Extended Insights ─────────────────────────────────────────────────────────

def save_extended_insight(symbol: str, text: str, model_name: str = "") -> None:
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        _get_conn().execute("""
            INSERT OR REPLACE INTO extended_insights(symbol, insight_text, generated_at, model_name)
            VALUES(?, ?, ?, ?)
        """, (symbol.upper(), text, now, model_name))
        _get_conn().commit()


def get_extended_insight(symbol: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT insight_text, generated_at, model_name FROM extended_insights WHERE symbol=?",
        (symbol.upper(),),
    ).fetchone()
    if not row:
        return None
    return {
        "symbol":       symbol.upper(),
        "insight_text": row["insight_text"],
        "generated_at": row["generated_at"],
        "model_name":   row["model_name"] or "",
    }


def get_all_extended_insights() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT symbol, generated_at, model_name FROM extended_insights ORDER BY generated_at DESC"
    ).fetchall()
    return [
        {"symbol": r["symbol"], "generated_at": r["generated_at"], "model_name": r["model_name"] or ""}
        for r in rows
    ]


# ── App settings (key-value) ──────────────────────────────────────────────────

def get_setting(key: str, default: Any = None) -> Any:
    row = _get_conn().execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    with contextlib.suppress(Exception):
        return json.loads(row["value"])
    return row["value"]


def set_setting(key: str, value: Any) -> None:
    with _write_lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO app_settings(key, value) VALUES(?, ?)",
            (key, json.dumps(value)),
        )
        _get_conn().commit()


# ── RAG sources ───────────────────────────────────────────────────────────────

def get_rag_sources() -> dict[str, dict]:
    rows   = _get_conn().execute("SELECT id, data_json FROM rag_sources").fetchall()
    result = {}
    for row in rows:
        with contextlib.suppress(Exception):
            result[row["id"]] = json.loads(row["data_json"])
    return result


def save_rag_source(source_id: str, data: dict) -> None:
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO rag_sources(id, data_json, updated_at) VALUES(?, ?, ?)",
            (source_id, json.dumps(data), now),
        )
        _get_conn().commit()


def delete_rag_source(source_id: str) -> None:
    with _write_lock:
        _get_conn().execute("DELETE FROM rag_sources WHERE id=?", (source_id,))
        _get_conn().commit()


def save_all_rag_sources(sources: dict[str, dict]) -> None:
    """Replace all RAG sources atomically."""
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        _get_conn().execute("DELETE FROM rag_sources")
        _get_conn().executemany(
            "INSERT INTO rag_sources(id, data_json, updated_at) VALUES(?, ?, ?)",
            [(sid, json.dumps(data), now) for sid, data in sources.items()],
        )
        _get_conn().commit()
