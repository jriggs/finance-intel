"""Volume analysis: the scoring task queue and the per-symbol scored results."""
from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from .connection import _get_conn, _write_lock

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
             sector, industry, scores_json, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol.upper(),
            scores.get("score", 0),
            scores.get("macro", {}).get("score", 0),
            scores.get("sentiment", {}).get("score", 0),
            scores.get("short_term", {}).get("score", 0),
            scores.get("long_term", {}).get("score", 0),
            scores.get("recommendation", ""),
            scores.get("grade", ""),
            scores.get("sector") or None,
            scores.get("industry") or None,
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


def get_all_volume_sentiments() -> dict[str, float]:
    """Return {symbol: sentiment_score} for every scored symbol — no limit."""
    rows = _get_conn().execute(
        "SELECT symbol, sentiment_score FROM volume_analysis_results WHERE sentiment_score IS NOT NULL"
    ).fetchall()
    return {r["symbol"].upper(): float(r["sentiment_score"]) for r in rows}


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


def get_all_volume_results() -> list[dict]:
    """Return every scored symbol ordered by overall_score descending."""
    rows = _get_conn().execute("""
        SELECT symbol, overall_score, macro_score, sentiment_score,
               short_term_score, long_term_score, recommendation, grade,
               sector, industry, last_updated_at
        FROM volume_analysis_results
        ORDER BY overall_score DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_data_ages() -> dict[str, str | None]:
    """Return the most-recent update timestamps for volume and screener data."""
    vol_row = _get_conn().execute(
        "SELECT MAX(last_updated_at) AS t FROM volume_analysis_results"
    ).fetchone()
    sc_row = _get_conn().execute(
        "SELECT MAX(generated_at) AS t FROM screener_results"
    ).fetchone()
    return {
        "volume_last_updated":   vol_row["t"] if vol_row else None,
        "screener_last_updated": sc_row["t"] if sc_row else None,
    }


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
