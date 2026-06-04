"""RAG source registry (replaces the legacy sources.json)."""
from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime

from .connection import _get_conn, _write_lock

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
