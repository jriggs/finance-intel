"""
Market data routes — stock info, chart, news, quotes, peers.
Mounted into the main finance router via include_router().
No LLM or RAG dependency.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import market
import db as portfolio

router = APIRouter()


@router.get("/api/finance/stock/{symbol}")
async def get_stock(symbol: str):
    try:
        return await asyncio.to_thread(market.get_stock_info, symbol)
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.get("/api/finance/chart/{symbol}")
async def get_chart(symbol: str, period: str = "6mo"):
    try:
        return await asyncio.to_thread(market.get_chart_data, symbol, period)
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.get("/api/finance/news/{symbol}")
async def get_news(symbol: str, limit: int = 15):
    try:
        items = await asyncio.to_thread(market.get_news, symbol, limit)
        return {"symbol": symbol.upper(), "news": items}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.get("/api/finance/quotes")
async def get_quotes():
    watchlist = portfolio.get_watchlist()
    return {"quotes": await asyncio.to_thread(market.get_batch_quotes, watchlist)}


@router.get("/api/finance/peers/{symbol}")
async def get_peers(symbol: str):
    peers  = await asyncio.to_thread(market.get_sector_peers, symbol)
    quotes = await asyncio.to_thread(market.get_batch_quotes, peers)
    return {"symbol": symbol.upper(), "peers": quotes}


@router.get("/api/finance/yfinance-status")
async def get_yfinance_status():
    return market.get_yf_status()


class ClearBlocklistBody(BaseModel):
    symbols: list[str] | None = None


@router.delete("/api/finance/market/not-found-blocklist")
async def clear_not_found_blocklist(body: ClearBlocklistBody | None = None):
    remaining = await asyncio.to_thread(
        market.clear_not_found_blocklist,
        body.symbols if body else None,
    )
    return {"cleared": True, "remaining_count": len(remaining)}
