"""
Alpaca broker integration.

Paper trading by default (ALPACA_PAPER=true).
Gracefully degrades when no API keys are configured.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd

logger = logging.getLogger("broker")

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() != "false"
ALPACA_BASE_URL   = (
    "https://paper-api.alpaca.markets" if ALPACA_PAPER
    else "https://api.alpaca.markets"
)

_client = None
_configured = bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)


def _get_client():
    global _client
    if _client is None:
        from alpaca.trading.client import TradingClient
        _client = TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
            paper=ALPACA_PAPER,
        )
    return _client


# ── Account ───────────────────────────────────────────────────────────────────

def get_account() -> dict:
    if not _configured:
        return {"error": "Alpaca API keys not configured", "paper": True}
    try:
        acct = _get_client().get_account()
        return {
            "id":               str(acct.id),
            "status":           str(acct.status),
            "equity":           float(acct.equity),
            "cash":             float(acct.cash),
            "buying_power":     float(acct.buying_power),
            "portfolio_value":  float(acct.portfolio_value),
            "pnl_today":        float(acct.equity) - float(acct.last_equity),
            "pnl_today_pct":    (float(acct.equity) - float(acct.last_equity)) / float(acct.last_equity) * 100
                                 if float(acct.last_equity) else 0,
            "paper":            ALPACA_PAPER,
        }
    except Exception as e:
        return {"error": str(e), "paper": ALPACA_PAPER}


# ── Positions ─────────────────────────────────────────────────────────────────

def get_positions() -> list[dict]:
    if not _configured:
        return []
    try:
        positions = _get_client().get_all_positions()
        return [
            {
                "symbol":       str(p.symbol),
                "qty":          float(p.qty),
                "side":         str(p.side),
                "avg_cost":     float(p.avg_entry_price),
                "market_value": float(p.market_value),
                "unrealized_pnl":     float(p.unrealized_pl),
                "unrealized_pnl_pct": float(p.unrealized_plpc) * 100,
                "current_price":      float(p.current_price),
                "cost_basis":         float(p.cost_basis),
            }
            for p in positions
        ]
    except Exception as e:
        return [{"error": str(e)}]


# ── Orders ────────────────────────────────────────────────────────────────────

def place_market_order(symbol: str, qty: float, side: str) -> dict:
    """
    Place a market order.
    side: 'buy' or 'sell'
    """
    if not _configured:
        return {"error": "Alpaca API keys not configured"}
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        order = _get_client().submit_order(req)
        return {
            "id":        str(order.id),
            "symbol":    str(order.symbol),
            "qty":       float(order.qty),
            "side":      str(order.side),
            "status":    str(order.status),
            "submitted": datetime.now(UTC).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


def place_limit_order(symbol: str, qty: float, side: str, limit_price: float) -> dict:
    if not _configured:
        return {"error": "Alpaca API keys not configured"}
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = LimitOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=order_side,
            limit_price=round(limit_price, 2),
            time_in_force=TimeInForce.GTC,
        )
        order = _get_client().submit_order(req)
        return {
            "id":          str(order.id),
            "symbol":      str(order.symbol),
            "qty":         float(order.qty),
            "side":        str(order.side),
            "limit_price": float(order.limit_price),
            "status":      str(order.status),
            "submitted":   datetime.now(UTC).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


def get_orders(status: str = "all", limit: int = 50) -> list[dict]:
    if not _configured:
        return []
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        status_map = {
            "open":   QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all":    QueryOrderStatus.ALL,
        }
        req = GetOrdersRequest(
            status=status_map.get(status, QueryOrderStatus.ALL),
            limit=limit,
        )
        orders = _get_client().get_orders(req)
        return [
            {
                "id":          str(o.id),
                "symbol":      str(o.symbol),
                "qty":         float(o.qty or 0),
                "filled_qty":  float(o.filled_qty or 0),
                "side":        str(o.side),
                "type":        str(o.order_type),
                "status":      str(o.status),
                "limit_price": float(o.limit_price) if o.limit_price else None,
                "filled_avg":  float(o.filled_avg_price) if o.filled_avg_price else None,
                "created_at":  str(o.created_at),
                "filled_at":   str(o.filled_at) if o.filled_at else None,
            }
            for o in orders
        ]
    except Exception as e:
        return [{"error": str(e)}]


def cancel_order(order_id: str) -> dict:
    if not _configured:
        return {"error": "Alpaca API keys not configured"}
    try:
        _get_client().cancel_order_by_id(order_id)
        return {"ok": True, "id": order_id}
    except Exception as e:
        return {"error": str(e)}


# ── Market status ─────────────────────────────────────────────────────────────

def get_market_clock() -> dict:
    if not _configured:
        # Fallback: derive from NYSE hours
        now = datetime.now(UTC)
        return {"is_open": False, "note": "Keys not configured", "timestamp": now.isoformat()}
    try:
        clock = _get_client().get_clock()
        return {
            "is_open":    clock.is_open,
            "next_open":  str(clock.next_open),
            "next_close": str(clock.next_close),
            "timestamp":  str(clock.timestamp),
        }
    except Exception as e:
        return {"error": str(e), "is_open": False}


def is_configured() -> bool:
    return _configured


# ── Market data ───────────────────────────────────────────────────────────────

def get_quote(symbol: str) -> dict | None:
    """
    Fetch latest quote for a symbol using Alpaca Market Data API v2.
    Returns {symbol, price, bid, ask, ...} or None if unavailable.
    Always uses the live data endpoint regardless of paper-trading mode.
    """
    if not _configured:
        return None
    try:
        headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        url = f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/latest/quote"
        with httpx.Client(headers=headers, timeout=10) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != "ok" and "quote" not in data:
            return None

        quote = data.get("quote", {})
        return {
            "symbol":    symbol.upper(),
            "price":     quote.get("lp"),
            "bid":       quote.get("bp"),
            "ask":       quote.get("ap"),
            "bid_size":  quote.get("bs"),
            "ask_size":  quote.get("as"),
            "timestamp": quote.get("t"),
        }
    except Exception as e:
        logger.debug("Alpaca quote %s failed: %s", symbol, e)
        return None


def get_bars(symbol: str, period: str = "6mo") -> pd.DataFrame:
    """
    Fetch daily OHLCV bars from Alpaca Market Data.
    period: '1mo', '3mo', '6mo', '1y', '2y'
    Returns DataFrame with columns [open, high, low, close, volume] indexed by date,
    or empty DataFrame on failure.
    """
    if not _configured:
        return pd.DataFrame()
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        period_days = {
            "1mo": 35, "3mo": 95, "6mo": 185, "1y": 370, "2y": 740,
            "5y": 1825, "10y": 3650,
        }
        days = period_days.get(period, 185)
        start = datetime.now(UTC) - timedelta(days=days)

        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req = StockBarsRequest(
            symbol_or_symbols=symbol.upper(),
            timeframe=TimeFrame.Day,
            start=start,
        )
        bars = client.get_stock_bars(req)
        df = bars.df

        if df.empty:
            return pd.DataFrame()

        # Drop symbol level from MultiIndex, normalize index to date
        if isinstance(df.index, pd.MultiIndex):
            df = df.droplevel(0)
        idx = pd.to_datetime(df.index)
        df.index = idx.tz_convert(None) if idx.tz is not None else idx
        df.index.name = "Date"

        # Keep only OHLCV columns
        df = df[["open", "high", "low", "close", "volume"]].copy()
        logger.debug("Alpaca bars %s (%s): %d rows", symbol, period, len(df))
        return df
    except Exception as e:
        logger.debug("Alpaca bars %s failed: %s", symbol, e)
        return pd.DataFrame()


def get_snapshot(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch latest snapshot (daily bar + prev day) for multiple symbols.
    Returns {symbol: {price, prev_close, change_pct, volume}} or {} on failure.
    """
    if not _configured or not symbols:
        return {}
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest

        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req = StockSnapshotRequest(symbol_or_symbols=[s.upper() for s in symbols])
        snaps = client.get_stock_snapshot(req)

        result = {}
        for sym, s in snaps.items():
            bar  = s.daily_bar
            prev = s.previous_daily_bar
            if not bar:
                continue
            price      = float(bar.close)
            prev_close = float(prev.close) if prev else None
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else None
            result[sym] = {
                "price":      price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "volume":     float(bar.volume) if bar.volume else None,
            }
        return result
    except Exception as e:
        logger.debug("Alpaca snapshot failed: %s", e)
        return {}
