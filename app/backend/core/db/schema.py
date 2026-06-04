"""Schema creation, incremental migrations, and one-time data bootstraps."""
from __future__ import annotations

import json

from .connection import _get_conn, _write_lock

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
            sector           TEXT,
            industry         TEXT,
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

        -- Daily automated trading tables
        CREATE TABLE IF NOT EXISTS daily_trade_sessions (
            id          TEXT PRIMARY KEY,
            trade_date  TEXT NOT NULL UNIQUE,
            status      TEXT DEFAULT 'recommended',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_trade_positions (
            id              TEXT PRIMARY KEY,
            session_id      TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            signal_score    REAL,
            screener_score  REAL,
            volume_score    REAL,
            aggregate_score REAL NOT NULL,
            allocation_usd  REAL NOT NULL,
            rank            INTEGER,
            status          TEXT DEFAULT 'recommended',
            sell_reason     TEXT,
            sell_date       TEXT,
            entry_price     REAL,
            current_price   REAL,
            exit_price      REAL,
            pnl_pct         REAL,
            pnl_usd         REAL,
            sentiment_score REAL,
            source          TEXT DEFAULT 'overall',
            UNIQUE(trade_date, symbol)
        );

        CREATE INDEX IF NOT EXISTS idx_dtp_status
            ON daily_trade_positions(status);
        CREATE INDEX IF NOT EXISTS idx_dtp_trade_date
            ON daily_trade_positions(trade_date);
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

    var_cols = {row[1] for row in conn.execute("PRAGMA table_info(volume_analysis_results)")}
    if "sector" not in var_cols:
        conn.execute("ALTER TABLE volume_analysis_results ADD COLUMN sector TEXT")
        # Back-fill from existing scores_json so current rows aren't left blank
        conn.execute("""
            UPDATE volume_analysis_results
            SET sector = json_extract(scores_json, '$.sector')
            WHERE sector IS NULL AND scores_json IS NOT NULL
        """)
        conn.commit()
    if "industry" not in var_cols:
        conn.execute("ALTER TABLE volume_analysis_results ADD COLUMN industry TEXT")
        conn.execute("""
            UPDATE volume_analysis_results
            SET industry = json_extract(scores_json, '$.industry')
            WHERE industry IS NULL AND scores_json IS NOT NULL
        """)
        conn.commit()

    dtp_cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_trade_positions)")}
    if "sentiment_score" not in dtp_cols:
        conn.execute("ALTER TABLE daily_trade_positions ADD COLUMN sentiment_score REAL")
        conn.commit()
    if "source" not in dtp_cols:
        conn.execute("ALTER TABLE daily_trade_positions ADD COLUMN source TEXT DEFAULT 'overall'")
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

