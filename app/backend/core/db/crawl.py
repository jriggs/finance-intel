"""Crawl cache: scraped/scored content keyed by source. The special
``nyse_symbols`` key is delegated to the symbols table."""
from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

from .connection import _get_conn, _write_lock
from .symbols import get_symbols, save_symbols

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
