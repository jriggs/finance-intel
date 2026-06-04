"""Watchlist: the user's tracked symbols (seeded with defaults on first read)."""
from __future__ import annotations

from .connection import _get_conn, _write_lock

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

