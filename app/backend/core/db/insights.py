"""Per-symbol extended LLM insight cache."""
from __future__ import annotations

from datetime import UTC, datetime

from .connection import _get_conn, _write_lock

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
