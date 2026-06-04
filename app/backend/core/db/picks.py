"""Picks: model-suggested trades with outcome tracking and win-rate stats."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from .connection import _get_conn, _write_lock

# ── Picks ─────────────────────────────────────────────────────────────────────

def _insert_pick(p: dict) -> None:
    _get_conn().execute("""
        INSERT OR REPLACE INTO picks
            (id, symbol, direction, entry_price, entry_date, target_price, stop_loss,
             horizon_days, signal_score, reasoning, status, current_price,
             unrealized_pnl, unrealized_pnl_pct, outcome, exit_price,
             exit_date, final_return, was_correct)
        VALUES
            (:id,:symbol,:direction,:entry_price,:entry_date,:target_price,:stop_loss,
             :horizon_days,:signal_score,:reasoning,:status,:current_price,
             :unrealized_pnl,:unrealized_pnl_pct,:outcome,:exit_price,
             :exit_date,:final_return,:was_correct)
    """, {
        "id":                 p.get("id", str(uuid.uuid4())[:8]),
        "symbol":             p.get("symbol", ""),
        "direction":          p.get("direction", "buy"),
        "entry_price":        p.get("entry_price"),
        "entry_date":         p.get("entry_date"),
        "target_price":       p.get("target_price"),
        "stop_loss":          p.get("stop_loss"),
        "horizon_days":       p.get("horizon_days", 30),
        "signal_score":       p.get("signal_score"),
        "reasoning":          p.get("reasoning", ""),
        "status":             p.get("status", "open"),
        "current_price":      p.get("current_price"),
        "unrealized_pnl":     p.get("unrealized_pnl", 0.0),
        "unrealized_pnl_pct": p.get("unrealized_pnl_pct", 0.0),
        "outcome":            p.get("outcome"),
        "exit_price":         p.get("exit_price"),
        "exit_date":          p.get("exit_date"),
        "final_return":       p.get("final_return"),
        "was_correct":        int(p["was_correct"]) if p.get("was_correct") is not None else None,
    })


def _row_to_pick(row: sqlite3.Row) -> dict:
    p = dict(row)
    if p.get("was_correct") is not None:
        p["was_correct"] = bool(p["was_correct"])
    return p


def get_picks() -> list[dict]:
    """Return all picks from DB."""
    rows = _get_conn().execute("SELECT * FROM picks ORDER BY entry_date DESC").fetchall()
    return [_row_to_pick(r) for r in rows]


def get_open_picks() -> list[dict]:
    """Return only open (not closed) picks."""
    rows = _get_conn().execute(
        "SELECT * FROM picks WHERE status='open' ORDER BY entry_date DESC"
    ).fetchall()
    return [_row_to_pick(r) for r in rows]


def update_pick_prices(prices: dict[str, float]) -> None:
    """Persist refreshed current prices for open picks. Called by the router layer."""
    if not prices:
        return
    now = datetime.now(UTC).isoformat()
    rows = _get_conn().execute(
        "SELECT id, symbol, direction, entry_price FROM picks WHERE status='open'"
    ).fetchall()
    updates = []
    for row in rows:
        price = prices.get(row["symbol"])
        if price is None:
            continue
        if row["direction"] == "buy":
            pnl = (price - row["entry_price"]) / row["entry_price"] * 100
        else:
            pnl = (row["entry_price"] - price) / row["entry_price"] * 100
        updates.append((round(price, 4), round(pnl, 2), round(pnl, 2), row["id"]))
    if updates:
        with _write_lock:
            _get_conn().executemany(
                "UPDATE picks SET current_price=?, unrealized_pnl=?, unrealized_pnl_pct=? WHERE id=?",
                updates,
            )
            _get_conn().commit()


def add_pick(
    symbol: str,
    entry_price: float,
    direction: str,
    reasoning: str,
    target_price: float | None = None,
    stop_loss: float | None = None,
    horizon_days: int = 30,
    signal_score: int | None = None,
) -> dict:
    pick = {
        "id":                 str(uuid.uuid4())[:8],
        "symbol":             symbol.upper(),
        "direction":          direction.lower(),
        "entry_price":        round(entry_price, 4),
        "entry_date":         datetime.now(UTC).isoformat(),
        "target_price":       round(target_price, 4) if target_price else None,
        "stop_loss":          round(stop_loss, 4) if stop_loss else None,
        "horizon_days":       horizon_days,
        "signal_score":       signal_score,
        "reasoning":          reasoning,
        "status":             "open",
        "current_price":      entry_price,
        "unrealized_pnl":     0.0,
        "unrealized_pnl_pct": 0.0,
        "outcome":            None,
        "exit_price":         None,
        "exit_date":          None,
        "final_return":       None,
        "was_correct":        None,
    }
    with _write_lock:
        _insert_pick(pick)
        _get_conn().commit()
    return pick


def close_pick(pick_id: str, exit_price: float, outcome: str) -> dict | None:
    row = _get_conn().execute("SELECT * FROM picks WHERE id=?", (pick_id,)).fetchone()
    if not row:
        return None
    p = _row_to_pick(row)
    p["status"]     = "closed"
    p["exit_price"] = round(exit_price, 4)
    p["exit_date"]  = datetime.now(UTC).isoformat()
    p["outcome"]    = outcome
    if p["direction"] == "buy":
        p["final_return"] = round((exit_price - p["entry_price"]) / p["entry_price"] * 100, 2)
    else:
        p["final_return"] = round((p["entry_price"] - exit_price) / p["entry_price"] * 100, 2)
    p["was_correct"] = p["final_return"] > 0
    with _write_lock:
        _insert_pick(p)
        _get_conn().commit()
    return p


def get_pick_stats() -> dict:
    rows   = _get_conn().execute("SELECT * FROM picks").fetchall()
    picks  = [_row_to_pick(r) for r in rows]
    closed = [p for p in picks if p.get("status") == "closed" and p.get("final_return") is not None]
    if not closed:
        return {"total": len(picks), "closed": 0, "win_rate": None, "avg_return": None}
    wins    = [p for p in closed if p.get("was_correct")]
    returns = [p["final_return"] for p in closed]
    best    = max(closed, key=lambda p: p["final_return"])
    worst   = min(closed, key=lambda p: p["final_return"])
    return {
        "total":        len(picks),
        "open":         len([p for p in picks if p.get("status") == "open"]),
        "closed":       len(closed),
        "wins":         len(wins),
        "win_rate":     round(len(wins) / len(closed) * 100, 1),
        "avg_return":   round(sum(returns) / len(returns), 2),
        "best_pick":    {"symbol": best["symbol"], "return": best["final_return"]},
        "worst_pick":   {"symbol": worst["symbol"], "return": worst["final_return"]},
        "total_return": round(sum(returns), 2),
    }
