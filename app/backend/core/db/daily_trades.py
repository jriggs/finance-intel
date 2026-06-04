"""Daily automated trading: sessions and their positions, with confirm/close
lifecycle and portfolio stats."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from .connection import _get_conn, _write_lock

# ── Daily Automated Trades ────────────────────────────────────────────────────

def get_daily_session(trade_date: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM daily_trade_sessions WHERE trade_date=?", (trade_date,)
    ).fetchone()
    return dict(row) if row else None


def get_daily_positions_for_date(trade_date: str) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM daily_trade_positions WHERE trade_date=? ORDER BY rank",
        (trade_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_daily_session(trade_date: str, positions: list[dict]) -> dict:
    """
    Upsert today's recommended picks. Always replaces recommended positions so a
    force-refresh can't accumulate stale rows. Purchased/sold positions are untouched.
    """
    now = datetime.now(UTC).isoformat()
    with _write_lock:
        # Ensure session row exists
        existing = _get_conn().execute(
            "SELECT id FROM daily_trade_sessions WHERE trade_date=?", (trade_date,)
        ).fetchone()
        if existing:
            session_id = existing["id"]
        else:
            session_id = str(uuid.uuid4())[:12]
            _get_conn().execute(
                "INSERT INTO daily_trade_sessions(id, trade_date, status, created_at) VALUES(?,?,?,?)",
                (session_id, trade_date, "recommended", now),
            )
        # Delete only recommended positions — keep purchased/sold rows intact
        _get_conn().execute(
            "DELETE FROM daily_trade_positions WHERE trade_date=? AND status='recommended'",
            (trade_date,),
        )
        for i, pos in enumerate(positions):
            pos_id = str(uuid.uuid4())[:12]
            _get_conn().execute("""
                INSERT INTO daily_trade_positions
                (id, session_id, trade_date, symbol, signal_score, screener_score,
                 volume_score, aggregate_score, allocation_usd, rank, status,
                 sentiment_score, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                pos_id, session_id, trade_date,
                pos["symbol"], pos.get("signal_score"), pos.get("screener_score"),
                pos.get("volume_score"), pos["aggregate_score"], pos["allocation_usd"],
                i + 1, "recommended",
                pos.get("sentiment_score"), pos.get("source", "overall"),
            ))
        _get_conn().commit()
    row = _get_conn().execute(
        "SELECT * FROM daily_trade_sessions WHERE trade_date=?", (trade_date,)
    ).fetchone()
    return dict(row)


def confirm_daily_session(trade_date: str, price_map: dict[str, float]) -> bool:
    """Mark session as confirmed and record entry prices + purchased status."""
    with _write_lock:
        _get_conn().execute(
            "UPDATE daily_trade_sessions SET status='confirmed' WHERE trade_date=?",
            (trade_date,),
        )
        for sym, price in price_map.items():
            _get_conn().execute("""
                UPDATE daily_trade_positions
                SET status='purchased', entry_price=?, current_price=?, pnl_pct=0, pnl_usd=0
                WHERE trade_date=? AND symbol=? AND status='recommended'
            """, (price, price, trade_date, sym.upper()))
        _get_conn().commit()
    return True


def get_open_daily_positions(cutoff_date: str | None = None) -> list[dict]:
    """Return purchased positions. If cutoff_date given, only those on or before that date."""
    if cutoff_date:
        rows = _get_conn().execute(
            "SELECT * FROM daily_trade_positions WHERE status='purchased' AND trade_date<=? ORDER BY trade_date DESC",
            (cutoff_date,),
        ).fetchall()
    else:
        rows = _get_conn().execute(
            "SELECT * FROM daily_trade_positions WHERE status='purchased' ORDER BY trade_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_daily_position_prices(price_map: dict[str, float]) -> None:
    """Refresh current_price and recompute pnl for all purchased positions."""
    with _write_lock:
        for sym, price in price_map.items():
            rows = _get_conn().execute(
                "SELECT id, entry_price, allocation_usd FROM daily_trade_positions WHERE symbol=? AND status='purchased'",
                (sym.upper(),),
            ).fetchall()
            for row in rows:
                if row["entry_price"]:
                    pnl_pct = (price - row["entry_price"]) / row["entry_price"] * 100
                    pnl_usd = pnl_pct / 100 * (row["allocation_usd"] or 0)
                    _get_conn().execute(
                        "UPDATE daily_trade_positions SET current_price=?, pnl_pct=?, pnl_usd=? WHERE id=?",
                        (round(price, 4), round(pnl_pct, 2), round(pnl_usd, 4), row["id"]),
                    )
        _get_conn().commit()


def flag_daily_position_sell(position_id: str, reason: str) -> None:
    with _write_lock:
        _get_conn().execute(
            "UPDATE daily_trade_positions SET status='sell_flagged', sell_reason=? WHERE id=?",
            (reason, position_id),
        )
        _get_conn().commit()


def close_daily_position(position_id: str, exit_price: float | None = None) -> None:
    now = datetime.now(UTC).date().isoformat()
    with _write_lock:
        row = _get_conn().execute(
            "SELECT entry_price, allocation_usd FROM daily_trade_positions WHERE id=?",
            (position_id,),
        ).fetchone()
        pnl_pct = pnl_usd = None
        if row and row["entry_price"] and exit_price:
            pnl_pct = (exit_price - row["entry_price"]) / row["entry_price"] * 100
            pnl_usd = pnl_pct / 100 * (row["allocation_usd"] or 0)
        _get_conn().execute("""
            UPDATE daily_trade_positions
            SET status='sold', sell_date=?, exit_price=?, pnl_pct=?, pnl_usd=?
            WHERE id=?
        """, (
            now, exit_price,
            round(pnl_pct, 2) if pnl_pct is not None else None,
            round(pnl_usd, 4) if pnl_usd is not None else None,
            position_id,
        ))
        _get_conn().commit()


def get_daily_history(limit: int = 60) -> list[dict]:
    """Return sessions newest-first with their positions."""
    sessions = _get_conn().execute(
        "SELECT * FROM daily_trade_sessions ORDER BY trade_date DESC LIMIT ?", (limit,)
    ).fetchall()
    result = []
    for s in sessions:
        positions = get_daily_positions_for_date(s["trade_date"])
        result.append({**dict(s), "positions": positions})
    return result


def get_daily_portfolio_stats() -> dict:
    rows = _get_conn().execute("""
        SELECT
            COUNT(*) FILTER (WHERE status='purchased')    AS open_count,
            COUNT(*) FILTER (WHERE status='sold')         AS closed_count,
            COUNT(*) FILTER (WHERE status='sell_flagged') AS flagged_count,
            SUM(allocation_usd) FILTER (WHERE status='purchased') AS total_invested,
            SUM(pnl_usd) FILTER (WHERE status='purchased')        AS open_pnl_usd,
            AVG(pnl_pct) FILTER (WHERE status='sold')             AS avg_closed_pnl_pct
        FROM daily_trade_positions
    """).fetchone()
    return dict(rows) if rows else {}
