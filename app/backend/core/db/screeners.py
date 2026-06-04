"""Persisted named-screener results."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from .connection import _get_conn, _write_lock

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
