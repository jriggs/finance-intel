"""
External data integrations:
  - SEC EDGAR   — filings index, XBRL company facts, 8-K earnings releases
  - FRED        — macro economic series (GDP, CPI, Fed Funds, yield curve, etc.)
  - OpenFIGI    — ticker ↔ FIGI normalization / metadata enrichment
  - Macrotrends — historical ratio scrape (Playwright, best-effort)

All functions are synchronous; call via asyncio.to_thread() from async handlers.
Heavy responses are cached in memory for the process lifetime to respect rate limits.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from typing import Any

import httpx

from http_retry import RetryConfig, retry_sync, SEC_RETRY, DEFAULT_RETRY

logger = logging.getLogger("fundamentals")

FRED_API_KEY   = os.getenv("FRED_API_KEY", "")
OPENFIGI_KEY   = os.getenv("OPENFIGI_API_KEY", "")   # optional — raises rate limit

# SEC requires a descriptive User-Agent: https://www.sec.gov/developer
SEC_UA = "FinanceApp/1.0 contact@example.com"

# Simple in-process cache {key: (timestamp, value)}
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 3600  # 1 hour default


def _cached(key: str, fn, ttl: int = _CACHE_TTL, retry: RetryConfig | None = SEC_RETRY):
    now = time.time()
    if key in _CACHE:
        ts, val = _CACHE[key]
        if now - ts < ttl:
            return val
    val = retry_sync(fn, retry) if retry else fn()
    _CACHE[key] = (now, val)
    return val


def bust_symbol_cache(symbol: str) -> None:
    """Remove all cached entries for a symbol so the next fetch goes live."""
    sym = symbol.upper()
    for key in [k for k in list(_CACHE.keys()) if sym in k]:
        _CACHE.pop(key, None)


# ── SEC EDGAR ─────────────────────────────────────────────────────────────────

def get_all_tickers() -> list[dict]:
    """
    Fetch all SEC-registered tickers with company names from EDGAR company_tickers.json.
    Returns list of {"symbol": str, "name": str}. Cached 24 hours. ~10k entries.
    """
    def _fetch():
        url = "https://www.sec.gov/files/company_tickers.json"
        with httpx.Client(headers={"User-Agent": SEC_UA}, timeout=20) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.json()
        result = []
        for entry in data.values():
            ticker = entry.get("ticker", "").strip().upper()
            name   = entry.get("title", "").strip()
            if ticker and name:
                result.append({"symbol": ticker, "name": name})
        result.sort(key=lambda x: x["symbol"])
        return result
    return _cached("all_tickers", _fetch, ttl=86400)


def get_cik(symbol: str) -> str | None:
    """Resolve a ticker symbol to its SEC CIK (zero-padded to 10 digits)."""
    def _fetch():
        url = "https://www.sec.gov/files/company_tickers.json"
        with httpx.Client(headers={"User-Agent": SEC_UA}, timeout=15) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.json()
        sym = symbol.upper()
        for entry in data.values():
            if entry.get("ticker", "").upper() == sym:
                return str(entry["cik_str"]).zfill(10)
        return None
    return _cached(f"cik_{symbol.upper()}", _fetch, ttl=86400)


def get_edgar_filings(symbol: str, forms: list[str] | None = None, limit: int = 10) -> list[dict]:
    """
    Return recent filing metadata for a symbol from EDGAR submissions API.
    forms: e.g. ['10-K', '10-Q', '8-K'] — None returns all.
    """
    cik = get_cik(symbol)
    if not cik:
        logger.warning(f"CIK not found for {symbol}")
        return []

    def _fetch():
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        with httpx.Client(headers={"User-Agent": SEC_UA}, timeout=20) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.json()

    data = _cached(f"edgar_sub_{symbol.upper()}", _fetch, ttl=3600)
    recent = data.get("filings", {}).get("recent", {})

    accessions = recent.get("accessionNumber", [])
    form_types  = recent.get("form", [])
    dates       = recent.get("filingDate", [])
    descriptions = recent.get("primaryDocument", [])
    documents   = recent.get("primaryDocDescription", [])

    results = []
    for acc, form, dt, doc, desc in zip(accessions, form_types, dates, descriptions, documents):
        if forms and form not in forms:
            continue
        acc_clean = acc.replace("-", "")
        results.append({
            "symbol":      symbol.upper(),
            "cik":         cik,
            "form":        form,
            "date":        dt,
            "accession":   acc,
            "doc_url":     f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{doc}",
            "index_url":   f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}&dateb=&owner=include&count=10",
            "description": desc or form,
        })
        if len(results) >= limit:
            break
    return results


_MAX_FILING_BYTES = 5_000_000   # 5 MB hard cap — iXBRL files can be 10+ GiB
_SKIP_EXTENSIONS  = {".xml", ".xsd", ".xbrl", ".json"}


def get_edgar_filing_text(doc_url: str, max_chars: int = 80_000) -> str:
    """
    Fetch raw text of an EDGAR filing document.
    Guards against massive iXBRL / XML files:
      1. Skip known-huge extensions immediately.
      2. HEAD request to check Content-Length; skip if > 5 MB.
      3. Stream body; stop after _MAX_FILING_BYTES bytes.
    Strips HTML tags, returns plain text capped at max_chars.
    """
    import re
    from pathlib import PurePosixPath

    # Skip file types that are never human-readable prose
    suffix = PurePosixPath(doc_url.split("?")[0]).suffix.lower()
    if suffix in _SKIP_EXTENSIONS:
        logger.info(f"edgar skip (extension {suffix}): {doc_url}")
        return ""

    def _do_fetch() -> str:
        with httpx.Client(headers={"User-Agent": SEC_UA}, timeout=20) as c:
            # HEAD check — skip oversized files before downloading anything
            try:
                head = c.head(doc_url, follow_redirects=True)
                cl = int(head.headers.get("content-length", 0))
                if cl > _MAX_FILING_BYTES:
                    logger.warning(
                        f"edgar skip (Content-Length {cl/1e6:.1f} MB > limit): {doc_url}"
                    )
                    return ""
            except Exception:
                pass  # HEAD not supported by all SEC servers — fall through to streaming

            # Stream with byte cap
            raw_bytes = bytearray()
            with c.stream("GET", doc_url, follow_redirects=True, timeout=30) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=65_536):
                    raw_bytes.extend(chunk)
                    if len(raw_bytes) >= _MAX_FILING_BYTES:
                        logger.info(
                            f"edgar truncated at {_MAX_FILING_BYTES/1e6:.0f} MB: {doc_url}"
                        )
                        break

        content = raw_bytes.decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", content)
        text = re.sub(r"\s{3,}", "\n\n", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        return text.strip()[:max_chars]

    try:
        return retry_sync(_do_fetch, SEC_RETRY) or ""
    except Exception as e:
        logger.warning(f"edgar fetch {doc_url}: {e}")
        return ""


def get_edgar_company_facts(symbol: str) -> dict:
    """
    Fetch XBRL company facts from EDGAR — structured financial history.
    Returns dict of concept → unit → list of {end, val, form, accn}.
    Useful facts: us-gaap/Revenues, us-gaap/NetIncomeLoss, us-gaap/EarningsPerShareBasic,
                  us-gaap/Assets, us-gaap/LongTermDebt, us-gaap/OperatingCashFlowsFromOperations
    """
    cik = get_cik(symbol)
    if not cik:
        return {}

    def _fetch():
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        with httpx.Client(headers={"User-Agent": SEC_UA}, timeout=30) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.json()

    try:
        return _cached(f"edgar_facts_{symbol.upper()}", _fetch, ttl=3600)
    except Exception as e:
        logger.warning(f"edgar facts {symbol}: {e}")
        return {}


# Key GAAP concepts to extract for financial summary
_KEY_GAAP = {
    "Revenue":         ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "NetIncome":       ["NetIncomeLoss"],
    "EPS":             ["EarningsPerShareBasic", "EarningsPerShareDiluted"],
    "OperatingCF":     ["NetCashProvidedByUsedInOperatingActivities"],
    "TotalAssets":     ["Assets"],
    "TotalDebt":       ["LongTermDebt", "LongTermDebtNoncurrent"],
    "Equity":          ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "ResearchDev":     ["ResearchAndDevelopmentExpense"],
    "OperatingIncome": ["OperatingIncomeLoss"],
    "GrossProfit":     ["GrossProfit"],
}


def get_financial_history(symbol: str, years: int = 5) -> dict[str, list[dict]]:
    """
    Extract key annual financials from EDGAR XBRL company facts.
    Returns {metric_name: [{period, value, unit}]} sorted newest first.
    """
    facts = get_edgar_company_facts(symbol)
    gaap  = facts.get("facts", {}).get("us-gaap", {})
    cutoff = str(date.today().year - years)
    result: dict[str, list] = {}

    for metric, concepts in _KEY_GAAP.items():
        for concept in concepts:
            node = gaap.get(concept, {})
            units = node.get("units", {})
            # USD denominated annual filings
            entries = units.get("USD", []) or units.get("shares", []) or units.get("USD/shares", [])
            annual  = [
                e for e in entries
                if e.get("form") in ("10-K", "20-F")
                and e.get("end", "") >= cutoff
                and e.get("fp") == "FY"
            ]
            if annual:
                annual.sort(key=lambda x: x.get("end", ""), reverse=True)
                result[metric] = [
                    {"period": e["end"], "value": e["val"], "form": e["form"]}
                    for e in annual[:years]
                ]
                break  # found for this metric, skip other concept aliases

    return result


def format_financial_history_text(symbol: str) -> str:
    """Return a plain-text summary of historical financials for RAG ingestion."""
    hist = get_financial_history(symbol)
    if not hist:
        return ""
    lines = [f"# {symbol.upper()} — Historical Financials (EDGAR XBRL)\n"]
    for metric, rows in hist.items():
        vals = "  |  ".join(f"{r['period'][:4]}: {r['value']:,.0f}" for r in rows)
        lines.append(f"{metric}: {vals}")
    return "\n".join(lines)


# ── FRED (Federal Reserve Economic Data) ─────────────────────────────────────

FRED_SERIES = {
    "GDP":              ("GDP",       "US Real GDP (Billions)"),
    "CPI":              ("CPIAUCSL",  "Consumer Price Index"),
    "FedFunds":         ("FEDFUNDS",  "Federal Funds Effective Rate"),
    "Unemployment":     ("UNRATE",    "US Unemployment Rate"),
    "10Y_Treasury":     ("DGS10",     "10-Year Treasury Yield"),
    "2Y_Treasury":      ("DGS2",      "2-Year Treasury Yield"),
    "YieldCurve":       ("T10Y2Y",    "10Y-2Y Yield Spread"),
    "SP500":            ("SP500",     "S&P 500 Index"),
    "VIX":              ("VIXCLS",    "CBOE VIX Volatility Index"),
    "ConsumerSentiment":("UMCSENT",   "U Michigan Consumer Sentiment"),
    "RetailSales":      ("RSAFS",     "Advance Retail Sales"),
    "IndustrialProd":   ("INDPRO",    "Industrial Production Index"),
    "HousingStarts":    ("HOUST",     "Housing Starts"),
    "CorePCE":          ("PCEPILFE",  "Core PCE Price Index"),
    "M2":               ("M2SL",      "M2 Money Supply"),
}


def get_fred_series(series_id: str, limit: int = 24) -> list[dict]:
    """Fetch recent observations for a FRED series. Returns [{date, value}]."""
    if not FRED_API_KEY:
        logger.warning("FRED_API_KEY not set — macro data unavailable")
        return []

    def _fetch():
        url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={FRED_API_KEY}"
            f"&file_type=json&sort_order=desc&limit={limit}"
        )
        with httpx.Client(timeout=15) as c:
            r = c.get(url)
            r.raise_for_status()
            obs = r.json().get("observations", [])
        return [
            {"date": o["date"], "value": o["value"]}
            for o in obs
            if o["value"] not in (".", "")
        ]

    return _cached(f"fred_{series_id}_{limit}", _fetch, ttl=3600)


def get_macro_snapshot() -> dict[str, Any]:
    """
    Return latest value for all key FRED series.
    Returns {name: {label, value, date, series_id}}.
    """
    snapshot = {}
    for name, (sid, label) in FRED_SERIES.items():
        obs = get_fred_series(sid, limit=1)
        if obs:
            snapshot[name] = {
                "label":     label,
                "series_id": sid,
                "value":     obs[0]["value"],
                "date":      obs[0]["date"],
            }
    return snapshot


def format_macro_text() -> str:
    """Plain-text macro snapshot for RAG ingestion."""
    snap = get_macro_snapshot()
    if not snap:
        return ""
    lines = [f"# US Macro Snapshot — {date.today().isoformat()}\n"]
    for name, d in snap.items():
        lines.append(f"{d['label']} ({name}): {d['value']}  [{d['date']}]")
    return "\n".join(lines)


def get_fred_series_history(series_id: str, periods: int = 60) -> list[dict]:
    """Return longer history for a specific FRED series."""
    return get_fred_series(series_id, limit=periods)


# ── OpenFIGI ──────────────────────────────────────────────────────────────────

def figi_lookup(ticker: str, exchange_code: str = "US") -> dict | None:
    """
    Look up FIGI and instrument metadata for a ticker via OpenFIGI.
    Returns the best match or None.
    """
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY

    payload = [{"idType": "TICKER", "idValue": ticker.upper(), "exchCode": exchange_code}]

    def _fetch():
        with httpx.Client(timeout=10) as c:
            r = c.post("https://api.openfigi.com/v3/mapping", json=payload, headers=headers)
            r.raise_for_status()
            return r.json()

    try:
        results = retry_sync(_fetch, DEFAULT_RETRY)
        if results and results[0].get("data"):
            best = results[0]["data"][0]
            return {
                "ticker":      ticker.upper(),
                "figi":        best.get("figi"),
                "name":        best.get("name"),
                "type":        best.get("securityType"),
                "type2":       best.get("securityType2"),
                "market":      best.get("marketSector"),
                "exchange":    best.get("exchCode"),
                "currency":    best.get("currency"),
                "compositeFigi": best.get("compositeFigi"),
                "shareClassFigi": best.get("shareClassFigi"),
            }
    except Exception as e:
        logger.warning(f"OpenFIGI {ticker}: {e}")
    return None


def figi_batch(tickers: list[str]) -> dict[str, dict]:
    """
    Look up FIGIs for a list of tickers (max 100 per call).
    Returns {ticker: figi_data}.
    """
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY

    results = {}
    # OpenFIGI allows 100 per request
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i+100]
        payload = [{"idType": "TICKER", "idValue": t.upper(), "exchCode": "US"} for t in batch]
        try:
            def _fetch_batch():
                with httpx.Client(timeout=15) as c:
                    r = c.post("https://api.openfigi.com/v3/mapping", json=payload, headers=headers)
                    r.raise_for_status()
                    return r.json()
            data = retry_sync(_fetch_batch, DEFAULT_RETRY)
            for ticker, entry in zip(batch, data):
                if entry.get("data"):
                    best = entry["data"][0]
                    results[ticker.upper()] = {
                        "figi":   best.get("figi"),
                        "name":   best.get("name"),
                        "type":   best.get("securityType"),
                        "market": best.get("marketSector"),
                    }
        except Exception as e:
            logger.warning(f"OpenFIGI batch: {e}")
        time.sleep(0.2)  # stay within rate limits
    return results


# ── Macrotrends (Playwright scrape — best effort) ─────────────────────────────

# Symbols that timed out or returned no data — skip on future runs this session
_MACROTRENDS_SKIP: set[str] = set()


async def scrape_macrotrends(symbol: str, metric: str = "revenue") -> list[dict]:
    """
    Scrape annual historical data from Macrotrends.
    metric: revenue | net-income | eps | free-cash-flow | pe-ratio | price-book
    Returns [{date, value}] or [] on failure.
    Falls back to EDGAR XBRL if scrape fails.
    """
    METRIC_MAP = {
        "revenue":        "revenue",
        "net-income":     "net-income",
        "eps":            "eps-earnings-per-share-diluted",
        "free-cash-flow": "free-cash-flow",
        "pe-ratio":       "pe-ratio",
        "price-book":     "price-book",
        "debt":           "long-term-debt",
        "operating-cf":   "cash-flow-from-operating-activities",
    }
    sym = symbol.upper()
    if sym in _MACROTRENDS_SKIP:
        return []

    slug = METRIC_MAP.get(metric, metric)
    sym_lower = symbol.lower()
    url = f"https://www.macrotrends.net/stocks/charts/{sym}/{sym_lower}/{slug}"

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page    = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=10_000)
            except Exception:
                # Timeout or navigation error — mark symbol as not on Macrotrends
                _MACROTRENDS_SKIP.add(sym)
                logger.info(f"Macrotrends {sym}: not available (skipping future attempts)")
                await browser.close()
                return []

            content = await page.content()
            await browser.close()

        import re
        match = re.search(r'var originalData\s*=\s*(\[.*?\]);', content, re.DOTALL)
        if not match:
            _MACROTRENDS_SKIP.add(sym)
            return []
        rows = json.loads(match.group(1))
        results = []
        for row in rows:
            val = row.get("v") or row.get("value")
            dt  = row.get("date") or row.get("d")
            if dt and val:
                results.append({"date": str(dt)[:10], "value": val})
        return results
    except Exception as e:
        logger.warning(f"Macrotrends {sym} {metric}: {e}")
        return []


def get_liquid_us_stocks(limit: int = 3000) -> list[dict]:
    """
    Return top US stocks from NYSE + NASDAQ (combined, deduped).
    Uses EDGAR company_tickers_exchange.json which includes exchange info.
    NYSE/NASDAQ listing requirements filter out OTC/pink sheet stocks.
    Returns list of {symbol, name}, capped at limit. Cached 24 hours.
    """
    def _fetch():
        url = "https://www.sec.gov/files/company_tickers_exchange.json"
        with httpx.Client(headers={"User-Agent": SEC_UA}, timeout=20) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.json()

        # data["data"] is a list of [cik, name, ticker, exchange]
        fields = data.get("fields", [])
        rows   = data.get("data", [])

        ticker_idx   = fields.index("ticker")   if "ticker"   in fields else 2
        name_idx     = fields.index("name")     if "name"     in fields else 1
        exchange_idx = fields.index("exchange") if "exchange" in fields else 3

        valid_exchanges = {"NYSE", "Nasdaq", "NASDAQ"}
        seen = set()
        result = []

        for row in rows:
            exchange = str(row[exchange_idx]).strip()
            if exchange not in valid_exchanges:
                continue

            sym  = str(row[ticker_idx]).strip().upper()
            name = str(row[name_idx]).strip()

            if not sym or not name:
                continue
            if sym in seen:
                continue
            # Skip preferred shares (symbol contains hyphen like BRK-A, MS-PA)
            if '-' in sym:
                continue
            # Skip warrants/rights (W, R suffix common patterns)
            if len(sym) > 5:
                continue

            seen.add(sym)
            result.append({"symbol": sym, "name": name})

        return result

    all_stocks = _cached("liquid_us_stocks_exchange", _fetch, ttl=86400)
    result = all_stocks[:limit]
    logger.info(f"Returning {len(result)} NYSE+NASDAQ stocks (total available: {len(all_stocks)})")
    return result
