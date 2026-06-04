"""Per-symbol sentiment cache."""
from __future__ import annotations

from datetime import UTC, datetime

from .connection import _get_conn, _write_lock

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
