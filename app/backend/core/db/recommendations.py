"""Persisted recommendation snapshots."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from .connection import _get_conn, _write_lock

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
