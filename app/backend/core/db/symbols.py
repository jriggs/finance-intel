"""Symbols: ticker + company name for autocomplete, plus avg-volume and
fetch-failure bookkeeping."""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from .connection import _get_conn, _write_lock

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
