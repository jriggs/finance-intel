"""
Tradable-symbol universe: S&P 500 and NYSE constituent fetching with daily
caching and a static fallback list used until the background symbol job has
populated the DB.
"""
from __future__ import annotations

import logging
import time as _time

import httpx as _httpx
from http_retry import WEB_RETRY as _WEB_RETRY, retry_sync as _retry_sync

_universe_logger = logging.getLogger("signals")

_SP500_CACHE: tuple[float, list[str]] | None = None
_NYSE_CACHE: tuple[float, list[str]] | None = None
_UNIVERSE_TTL = 86400          # refresh once per day
_NYSE_FALLBACK_TTL = 900       # 15 min — retry real fetch sooner after fallback

_FALLBACK_UNIVERSE = [
    "AAPL","MSFT","GOOGL","META","AMZN","NVDA","AMD","INTC","CRM","ADBE","ORCL","CSCO","QCOM","TXN","AMAT",
    "JPM","BAC","GS","MS","WFC","V","MA","BRK-B","AXP","C","SCHW","BLK","USB","PNC","TFC",
    "JNJ","UNH","PFE","ABT","MRK","LLY","AMGN","GILD","BMY","CVS","HCA","CI","ELV","HUM","ISRG",
    "XOM","CVX","COP","SLB","OXY","PSX","VLO","MPC","HAL","BKR","EOG","DVN","FANG",
    "CAT","GE","HON","UPS","RTX","BA","LMT","DE","MMM","ETN","EMR","ITW","FDX","NOC","GD",
    "WMT","TGT","HD","COST","MCD","SBUX","NKE","LOW","TJX","BKNG","ABNB","MAR","YUM",
    "PG","KO","PEP","CL","GIS","MO","PM","KHC","STZ","EL","MDLZ",
    "NEE","DUK","SO","D","AEP","EXC","SRE","PCG","ED","FE","ES","PPL",
    "T","VZ","CMCSA","DIS","NFLX","CHTR","FOX","OMC","WBD",
    "AMT","PLD","CCI","EQIX","SPG","O","DLR","PSA","WELL","VTR",
    "LIN","APD","SHW","FCX","NEM","DD","NUE","ALB","MOS","CF",
    "TSLA","F","GM","TM","RIVN","LCID","STLA",
]

# Static name map for fallback universe — no network calls required
_FALLBACK_NAMES: dict[str, str] = {
    "AAPL":"Apple Inc.","MSFT":"Microsoft Corp.","GOOGL":"Alphabet Inc.","META":"Meta Platforms",
    "AMZN":"Amazon.com Inc.","NVDA":"NVIDIA Corp.","AMD":"Advanced Micro Devices","INTC":"Intel Corp.",
    "CRM":"Salesforce Inc.","ADBE":"Adobe Inc.","ORCL":"Oracle Corp.","CSCO":"Cisco Systems",
    "QCOM":"Qualcomm Inc.","TXN":"Texas Instruments","AMAT":"Applied Materials",
    "JPM":"JPMorgan Chase","BAC":"Bank of America","GS":"Goldman Sachs","MS":"Morgan Stanley",
    "WFC":"Wells Fargo","V":"Visa Inc.","MA":"Mastercard Inc.","BRK-B":"Berkshire Hathaway",
    "AXP":"American Express","C":"Citigroup Inc.","SCHW":"Charles Schwab","BLK":"BlackRock Inc.",
    "USB":"U.S. Bancorp","PNC":"PNC Financial","TFC":"Truist Financial",
    "JNJ":"Johnson & Johnson","UNH":"UnitedHealth Group","PFE":"Pfizer Inc.",
    "ABT":"Abbott Laboratories","MRK":"Merck & Co.","LLY":"Eli Lilly","AMGN":"Amgen Inc.",
    "GILD":"Gilead Sciences","BMY":"Bristol-Myers Squibb","CVS":"CVS Health",
    "HCA":"HCA Healthcare","CI":"Cigna Group","ELV":"Elevance Health","HUM":"Humana Inc.",
    "ISRG":"Intuitive Surgical",
    "XOM":"Exxon Mobil","CVX":"Chevron Corp.","COP":"ConocoPhillips","SLB":"SLB (Schlumberger)",
    "OXY":"Occidental Petroleum","PSX":"Phillips 66","VLO":"Valero Energy",
    "MPC":"Marathon Petroleum","HAL":"Halliburton Co.","BKR":"Baker Hughes",
    "EOG":"EOG Resources","DVN":"Devon Energy","FANG":"Diamondback Energy",
    "CAT":"Caterpillar Inc.","GE":"GE Aerospace","HON":"Honeywell International",
    "UPS":"United Parcel Service","RTX":"RTX Corp.","BA":"Boeing Co.","LMT":"Lockheed Martin",
    "DE":"Deere & Co.","MMM":"3M Co.","ETN":"Eaton Corp.","EMR":"Emerson Electric",
    "ITW":"Illinois Tool Works","FDX":"FedEx Corp.","NOC":"Northrop Grumman","GD":"General Dynamics",
    "WMT":"Walmart Inc.","TGT":"Target Corp.","HD":"Home Depot","COST":"Costco Wholesale",
    "MCD":"McDonald's Corp.","SBUX":"Starbucks Corp.","NKE":"Nike Inc.","LOW":"Lowe's Companies",
    "TJX":"TJX Companies","BKNG":"Booking Holdings","ABNB":"Airbnb Inc.","MAR":"Marriott International",
    "YUM":"Yum! Brands",
    "PG":"Procter & Gamble","KO":"Coca-Cola Co.","PEP":"PepsiCo Inc.","CL":"Colgate-Palmolive",
    "GIS":"General Mills","MO":"Altria Group","PM":"Philip Morris International",
    "KHC":"Kraft Heinz Co.","STZ":"Constellation Brands","EL":"Estée Lauder","MDLZ":"Mondelez International",
    "NEE":"NextEra Energy","DUK":"Duke Energy","SO":"Southern Co.","D":"Dominion Energy",
    "AEP":"American Electric Power","EXC":"Exelon Corp.","SRE":"Sempra","PCG":"PG&E Corp.",
    "ED":"Consolidated Edison","FE":"FirstEnergy Corp.","ES":"Eversource Energy","PPL":"PPL Corp.",
    "T":"AT&T Inc.","VZ":"Verizon Communications","CMCSA":"Comcast Corp.",
    "DIS":"Walt Disney Co.","NFLX":"Netflix Inc.","CHTR":"Charter Communications",
    "FOX":"Fox Corp.","OMC":"Omnicom Group","WBD":"Warner Bros. Discovery",
    "AMT":"American Tower Corp.","PLD":"Prologis Inc.","CCI":"Crown Castle Inc.",
    "EQIX":"Equinix Inc.","SPG":"Simon Property Group","O":"Realty Income Corp.",
    "DLR":"Digital Realty Trust","PSA":"Public Storage","WELL":"Welltower Inc.","VTR":"Ventas Inc.",
    "LIN":"Linde plc","APD":"Air Products & Chemicals","SHW":"Sherwin-Williams",
    "FCX":"Freeport-McMoRan","NEM":"Newmont Corp.","DD":"DuPont de Nemours",
    "NUE":"Nucor Corp.","ALB":"Albemarle Corp.","MOS":"Mosaic Co.","CF":"CF Industries",
    "TSLA":"Tesla Inc.","F":"Ford Motor Co.","GM":"General Motors","TM":"Toyota Motor",
    "RIVN":"Rivian Automotive","LCID":"Lucid Group","STLA":"Stellantis N.V.",
}


def _portfolio_symbol_cache() -> list[str]:
    """Return symbols already cached in the portfolio DB (from refresh_symbols job)."""
    try:
        import db as _portfolio
        rows = _portfolio.get_symbols()
        if rows:
            return [r["symbol"] for r in rows]
    except Exception:
        pass
    return []


def get_sp500_symbols() -> list[str]:
    """
    Fetch current S&P 500 constituents from Wikipedia.
    Falls back to portfolio DB cache, then hardcoded subset on failure.
    Cached for 24 hours.
    """
    global _SP500_CACHE
    now = _time.time()
    if _SP500_CACHE and now - _SP500_CACHE[0] < _UNIVERSE_TTL:
        return _SP500_CACHE[1]

    try:
        import pandas as pd
        html = _retry_sync(
            lambda: _httpx.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; finance-bot/1.0)"},
            ).text,
            _WEB_RETRY,
        )
        tables = pd.read_html(html, attrs={"id": "constituents"})
        syms = tables[0]["Symbol"].tolist()
        syms = [s.replace(".", "-") for s in syms if isinstance(s, str)]
        _universe_logger.info(f"S&P 500 universe loaded: {len(syms)} symbols")
        _SP500_CACHE = (now, syms)
        return syms
    except Exception as e:
        _universe_logger.warning(f"S&P 500 fetch failed ({e}), using fallback universe")

    cached = _portfolio_symbol_cache()
    if cached:
        _universe_logger.info(f"S&P 500 fallback: using {len(cached)} portfolio-cached symbols")
        _SP500_CACHE = (now, cached)
        return cached

    _SP500_CACHE = (now, _FALLBACK_UNIVERSE)
    return _FALLBACK_UNIVERSE


def get_nyse_symbols() -> list[str]:
    """
    Fetch all NYSE-listed common stocks from NASDAQ's symbol directory.
    Filters out ETFs, preferred shares, and illiquid names.
    Falls back to portfolio DB cache, then S&P 500 on failure. Cached 24 hours.
    """
    global _NYSE_CACHE
    now = _time.time()
    if _NYSE_CACHE and now - _NYSE_CACHE[0] < _UNIVERSE_TTL:
        return _NYSE_CACHE[1]

    try:
        r = _retry_sync(
            lambda: _httpx.get(
                "https://ftp.nasdaqtrader.com/dynamic/SymbolDirectory/otherlisted.txt",
                timeout=15, follow_redirects=True,
            ),
            _WEB_RETRY,
        )
        r.raise_for_status()
        syms = []
        for line in r.text.splitlines()[1:]:     # skip header
            parts = line.split("|")
            if len(parts) < 7:
                continue
            sym      = parts[0].strip()
            exchange = parts[2].strip()           # N=NYSE, A=AMEX, P=Arca
            etf      = parts[4].strip()           # Y if ETF
            test     = parts[6].strip()           # Y if test issue
            # Keep NYSE/ARCA common stocks with clean ticker symbols only
            if exchange in ("N", "P") and etf != "Y" and test != "Y" and sym and sym.isalpha() and len(sym) <= 5:
                syms.append(sym)
        _universe_logger.info(f"NYSE universe loaded: {len(syms)} symbols")
        _NYSE_CACHE = (now, syms)
        return syms
    except Exception as e:
        _universe_logger.warning(f"NYSE fetch failed ({e}), falling back to portfolio cache")

    cached = _portfolio_symbol_cache()
    if cached:
        _universe_logger.info(f"NYSE fallback: using {len(cached)} portfolio-cached symbols")
        # Short TTL so we retry the real NASDAQ fetch soon
        _NYSE_CACHE = (now - _UNIVERSE_TTL + _NYSE_FALLBACK_TTL, cached)
        return cached

    return get_sp500_symbols()
