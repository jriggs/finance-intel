"""
Broker, watchlist, and picks routes.
Mounted into the main finance router via include_router().
No LLM or RAG dependency.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

import broker
import crawler
import market
import db as portfolio

router = APIRouter()


# ── Watchlist ─────────────────────────────────────────────────────────────────

@router.get("/api/finance/watchlist")
def get_watchlist():
    return {"symbols": portfolio.get_watchlist()}


class WatchlistBody(BaseModel):
    symbol: str


@router.post("/api/finance/watchlist")
async def add_watchlist(body: WatchlistBody):
    return {"symbols": portfolio.add_to_watchlist(body.symbol)}


@router.delete("/api/finance/watchlist/{symbol}")
async def remove_watchlist(symbol: str):
    return {"symbols": portfolio.remove_from_watchlist(symbol)}


# ── Picks ─────────────────────────────────────────────────────────────────────

@router.get("/api/finance/picks")
async def get_picks():
    picks = portfolio.get_picks()
    # Refresh current prices for open picks via market layer (not DB)
    open_syms = list({p["symbol"] for p in picks if p.get("status") == "open"})
    if open_syms:
        try:
            quotes    = await asyncio.to_thread(market.get_batch_quotes, open_syms)
            price_map = {q["symbol"]: q["price"] for q in quotes if q.get("price")}
            if price_map:
                # Update in-memory picks for this response
                for p in picks:
                    price = price_map.get(p["symbol"])
                    if p.get("status") == "open" and price:
                        p["current_price"] = round(price, 4)
                        if p["direction"] == "buy":
                            pnl = (price - p["entry_price"]) / p["entry_price"] * 100
                        else:
                            pnl = (p["entry_price"] - price) / p["entry_price"] * 100
                        p["unrealized_pnl_pct"] = round(pnl, 2)
                        p["unrealized_pnl"]     = round(pnl, 2)
                # Persist refreshed prices back to DB
                await asyncio.to_thread(portfolio.update_pick_prices, price_map)
        except Exception:
            pass  # stale prices better than a 500
    return {"picks": picks}


@router.get("/api/finance/picks/stats")
def get_pick_stats():
    return portfolio.get_pick_stats()


class PickBody(BaseModel):
    symbol:       str
    entry_price:  float
    direction:    str = "buy"
    reasoning:    str = ""
    target_price: float | None = None
    stop_loss:    float | None = None
    horizon_days: int = 30
    signal_score: int | None = None


@router.post("/api/finance/picks", status_code=201)
async def add_pick(body: PickBody):
    return portfolio.add_pick(
        symbol=body.symbol, entry_price=body.entry_price,
        direction=body.direction, reasoning=body.reasoning,
        target_price=body.target_price, stop_loss=body.stop_loss,
        horizon_days=body.horizon_days, signal_score=body.signal_score,
    )


class ClosePickBody(BaseModel):
    exit_price: float
    outcome:    str = "held"


@router.put("/api/finance/picks/{pick_id}/close")
async def close_pick(pick_id: str, body: ClosePickBody):
    pick = portfolio.close_pick(pick_id, body.exit_price, body.outcome)
    if not pick:
        raise HTTPException(404, "Pick not found")
    return pick


# ── Broker ────────────────────────────────────────────────────────────────────

@router.get("/api/finance/broker/account")
async def get_account():
    return await asyncio.to_thread(broker.get_account)


@router.get("/api/finance/broker/positions")
async def get_positions():
    return {"positions": await asyncio.to_thread(broker.get_positions)}


@router.get("/api/finance/broker/orders")
async def get_orders(status: str = "all"):
    return {"orders": await asyncio.to_thread(broker.get_orders, status)}


@router.get("/api/finance/broker/clock")
async def get_clock():
    data = await asyncio.to_thread(broker.get_market_clock)
    if data.get("is_open"):
        try:
            quotes = await asyncio.to_thread(market.get_batch_quotes, ["SPY"])
            if quotes:
                data["spy"] = {"price": quotes[0].get("price"), "change_pct": quotes[0].get("change_pct")}
        except Exception:
            pass
    return data


class OrderBody(BaseModel):
    symbol:      str
    qty:         float
    side:        str
    order_type:  str = "market"
    limit_price: float | None = None


@router.post("/api/finance/broker/order")
async def place_order(body: OrderBody):
    if body.order_type == "limit" and body.limit_price is not None:
        result = await asyncio.to_thread(
            broker.place_limit_order, body.symbol, body.qty, body.side, body.limit_price)
    else:
        result = await asyncio.to_thread(
            broker.place_market_order, body.symbol, body.qty, body.side)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.delete("/api/finance/broker/order/{order_id}")
async def cancel_order(order_id: str):
    result = await asyncio.to_thread(broker.cancel_order, order_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


# ── Crawl ─────────────────────────────────────────────────────────────────────

@router.get("/api/finance/crawl/{source}")
def get_crawl(source: str):
    data = portfolio.get_crawl_results(source)
    return data if data else {"items": [], "source": source}


@router.get("/api/finance/crawl")
def get_all_crawl():
    return portfolio.get_crawl_results()


@router.post("/api/finance/crawl/run")
async def trigger_crawl(background_tasks: BackgroundTasks):
    watchlist = portfolio.get_watchlist()
    async def _run():
        await crawler.run_full_crawl(watchlist)
    background_tasks.add_task(_run)
    return {"status": "crawl started", "symbols": watchlist}
