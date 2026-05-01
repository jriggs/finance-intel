"""
Playwright-based browser crawler.

Targets (no login, no API keys required):
  - Reddit  (r/stocks, r/investing, r/wallstreetbets) — public JSON API, throttled
  - MarketWatch — market news headlines
  - Finviz — news headlines + screener buzz
  - Seeking Alpha — free headline feed
  - Polymarket — prediction market prices for US economic events
  - Google Finance news for specific symbols

All functions are async. Each returns a list of item dicts.
run_full_crawl runs sources one at a time (sequential) with delays between
each to avoid hammering sites and spawning too many browser processes at once.
Failures are caught and logged so one broken source never kills the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

import db as portfolio
from http_retry import retry_async, WEB_RETRY

logger = logging.getLogger("crawler")


# ── Playwright context factory ────────────────────────────────────────────────

async def _get_browser():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    return pw, browser


async def _fetch_page(url: str, wait_selector: str | None = None, timeout: int = 15_000) -> str:
    """Return page HTML; returns '' on error."""
    pw, browser = await _get_browser()
    try:
        ctx  = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if wait_selector:
            with contextlib.suppress(Exception):
                await page.wait_for_selector(wait_selector, timeout=8000)
        return await page.content()
    except Exception as e:
        logger.warning(f"fetch_page {url}: {e}")
        return ""
    finally:
        await browser.close()
        await pw.stop()


# ── Reddit ────────────────────────────────────────────────────────────────────

async def crawl_reddit(subreddits: list[str] | None = None) -> list[dict]:
    """
    Fetch hot posts from finance subreddits via Reddit's public JSON API.
    No API key required. Requests are made one subreddit at a time with a
    delay between each to stay well within Reddit's anonymous rate limits.
    """
    subs  = subreddits or ["stocks", "investing", "wallstreetbets", "StockMarket"]
    items: list[dict] = []
    for sub in subs:
        try:
            async with __import__("httpx").AsyncClient(
                headers={"User-Agent": "FinanceApp/1.0 (research bot)"},
                timeout=15,
                follow_redirects=True,
            ) as client:
                url = f"https://www.reddit.com/r/{sub}/hot.json?limit=10"
                r   = await client.get(url)
            if r.status_code != 200:
                logger.warning(f"reddit {sub}: HTTP {r.status_code}")
                await asyncio.sleep(3)
                continue
            posts = r.json().get("data", {}).get("children", [])
            for p in posts:
                d = p.get("data", {})
                if d.get("stickied") or d.get("is_video"):
                    continue
                items.append({
                    "source":     f"r/{sub}",
                    "title":      d.get("title", ""),
                    "url":        "https://reddit.com" + d.get("permalink", ""),
                    "score":      d.get("score", 0),
                    "comments":   d.get("num_comments", 0),
                    "flair":      d.get("link_flair_text", ""),
                    "fetched_at": datetime.now(UTC).isoformat(),
                })
        except Exception as e:
            logger.warning(f"reddit {sub}: {e}")
        await asyncio.sleep(2)  # throttle between subreddits
    portfolio.save_crawl_results("reddit", items)
    return items


# ── MarketWatch ───────────────────────────────────────────────────────────────

async def crawl_marketwatch() -> list[dict]:
    """Fetch MarketWatch headlines via RSS (no browser required)."""
    import xml.etree.ElementTree as ET

    import httpx

    RSS_FEEDS = [
        "https://www.marketwatch.com/rss/topstories",
        "https://www.marketwatch.com/rss/marketpulse",
    ]
    items: list[dict] = []
    seen: set[str] = set()
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; financebot/1.0)"},
            timeout=15,
            follow_redirects=True,
        ) as client:
            for url in RSS_FEEDS:
                try:
                    r = await retry_async(lambda: client.get(url), WEB_RETRY)
                    if r.status_code != 200:
                        logger.warning(f"marketwatch rss {url}: HTTP {r.status_code}")
                        continue
                    root = ET.fromstring(r.content)
                    for item in root.findall(".//item"):
                        title = (item.findtext("title") or "").strip()
                        link  = (item.findtext("link")  or "").strip()
                        pub   = (item.findtext("pubDate") or "").strip()
                        if title and link and title not in seen:
                            seen.add(title)
                            items.append({
                                "source":     "MarketWatch",
                                "title":      title,
                                "url":        link,
                                "published":  pub,
                                "fetched_at": datetime.now(UTC).isoformat(),
                            })
                except ET.ParseError as e:
                    logger.warning(f"marketwatch rss parse {url}: {e}")
                except Exception as e:
                    logger.warning(f"marketwatch rss {url}: {e}")
    except Exception as e:
        logger.warning(f"marketwatch: {e}")
    portfolio.save_crawl_results("marketwatch", items)
    return items


# ── Finviz news ───────────────────────────────────────────────────────────────

async def crawl_finviz_news(symbol: str | None = None) -> list[dict]:
    """Scrape news from Finviz — general or ticker-specific."""
    items: list[dict] = []
    url = f"https://finviz.com/quote.ashx?t={symbol.upper()}" if symbol else "https://finviz.com/news.ashx"
    try:
        # Finviz redesigned: table no longer has id="news-table"; use class selector
        html = await _fetch_page(url, "table.styled-table-new, table.fullview-news-outer")
        if not html:
            return items
        from bs4 import BeautifulSoup
        soup  = BeautifulSoup(html, "html.parser")
        # Try new design first, then legacy ticker-page table
        table = (
            soup.find("table", class_="styled-table-new")
            or soup.find("table", id="news-table")
            or soup.find("table", class_="fullview-news-outer")
        )
        if not table:
            return items
        for row in table.find_all("tr")[:25]:
            link_tag = row.find("a", class_="nn-tab-link") or row.find("a")
            if not link_tag:
                continue
            title = link_tag.get_text(strip=True)
            if not title:
                continue
            date_td = row.find("td", class_="news_date-cell") or row.find("td")
            items.append({
                "source":     f"Finviz{'/' + symbol.upper() if symbol else ''}",
                "title":      title,
                "url":        link_tag.get("href", ""),
                "date_str":   date_td.get_text(strip=True) if date_td else "",
                "fetched_at": datetime.now(UTC).isoformat(),
            })
    except Exception as e:
        logger.warning(f"finviz: {e}")
    key = f"finviz_{symbol.upper()}" if symbol else "finviz"
    portfolio.save_crawl_results(key, items)
    return items


# ── Polymarket ────────────────────────────────────────────────────────────────

async def crawl_polymarket() -> list[dict]:
    """
    Fetch active US economic / political prediction markets from Polymarket's API.
    Returns markets with probabilities — useful for macro event risk.
    """
    items: list[dict] = []
    try:
        import httpx
        # Polymarket public gamma-api — filter to macro/financial topics
        # Fetch a broader set then filter by relevant keywords
        url = "https://gamma-api.polymarket.com/markets?closed=false&limit=100&order=volume&ascending=false"
        _MACRO_KEYWORDS = {
            "fed", "federal reserve", "rate", "interest", "inflation", "cpi", "pce",
            "gdp", "recession", "unemployment", "jobs", "nonfarm", "payroll",
            "treasury", "yield", "debt", "deficit", "tariff", "trade", "sanctions",
            "oil", "energy", "bitcoin", "crypto", "s&p", "nasdaq", "dow",
            "earnings", "ipo", "merger", "acquisition", "bankruptcy",
            "election", "congress", "senate", "white house", "president",
            "china", "eu", "europe", "dollar", "euro", "yen", "currency",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await retry_async(lambda: client.get(url), WEB_RETRY)
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            all_markets = r.json()
            # Keep only macro/financial markets
            markets = [
                m for m in all_markets
                if any(kw in m.get("question", "").lower() for kw in _MACRO_KEYWORDS)
            ][:30]
            if not markets:
                markets = all_markets[:30]  # fallback: use top by volume
            for m in markets:
                outcomes = m.get("outcomes", [])
                prices   = m.get("outcomePrices", [])
                # API sometimes returns prices as a JSON string instead of a list
                if isinstance(prices, str):
                    try:
                        import json as _json
                        prices = _json.loads(prices)
                    except Exception:
                        prices = []
                outcome_str = ""
                if outcomes and prices:
                    parts = []
                    for o, p in zip(outcomes, prices):
                        with contextlib.suppress(ValueError, TypeError):
                            parts.append(f"{o}: {round(float(p)*100, 1)}%")
                    outcome_str = " | ".join(parts)
                items.append({
                    "source":       "Polymarket",
                    "question":     m.get("question", ""),
                    "outcomes":     outcome_str,
                    "volume":       m.get("volumeNum", 0),
                    "end_date":     m.get("endDate", ""),
                    "url":          "https://polymarket.com/event/" + m.get("slug", ""),
                    "fetched_at":   datetime.now(UTC).isoformat(),
                })
    except Exception as e:
        logger.warning(f"polymarket: {e}")
        # Fallback: browser scrape
        try:
            html = await _fetch_page("https://polymarket.com/markets?tag=finance", "div.market-card")
            if html:
                from bs4 import BeautifulSoup
                soup  = BeautifulSoup(html, "html.parser")
                cards = soup.select("div.market-card, div[class*='market']")
                for c in cards[:20]:
                    title = c.get_text(strip=True)[:200]
                    if title:
                        items.append({
                            "source": "Polymarket",
                            "question": title,
                            "url":    "https://polymarket.com",
                            "fetched_at": datetime.now(UTC).isoformat(),
                        })
        except Exception:
            pass
    portfolio.save_crawl_results("polymarket", items)
    return items


# ── Google Finance news for a symbol ─────────────────────────────────────────

async def crawl_google_finance_news(symbol: str) -> list[dict]:
    """Scrape news from Google Finance for a ticker."""
    items: list[dict] = []
    url = f"https://www.google.com/finance/quote/{symbol.upper()}:NASDAQ"
    try:
        html = await _fetch_page(url, None, timeout=15_000)
        if not html:
            url = f"https://www.google.com/finance/quote/{symbol.upper()}:NYSE"
            html = await _fetch_page(url, None, timeout=15_000)
        if not html:
            return items
        from bs4 import BeautifulSoup
        soup     = BeautifulSoup(html, "html.parser")
        articles = soup.select("div[class*='news'] a, div.yY3Lee a")
        seen     = set()
        for a in articles[:20]:
            title = a.get_text(strip=True)
            href  = a.get("href", "")
            if not title or title in seen or len(title) < 10:
                continue
            seen.add(title)
            if href and not href.startswith("http"):
                href = "https://www.google.com" + href
            items.append({
                "source":     f"Google Finance/{symbol.upper()}",
                "title":      title,
                "url":        href,
                "fetched_at": datetime.now(UTC).isoformat(),
            })
    except Exception as e:
        logger.warning(f"google_finance {symbol}: {e}")
    portfolio.save_crawl_results(f"google_finance_{symbol.upper()}", items)
    return items


# ── Zacks news (RSS) ──────────────────────────────────────────────────────────

async def crawl_zacks(watchlist: list[str] | None = None) -> list[dict]:
    """
    Scrape Zacks news and commentary via Playwright.
    Hits the main news page plus up to 3 watchlist symbol pages.
    """
    from bs4 import BeautifulSoup

    items: list[dict] = []
    seen: set[str] = set()

    urls = [("https://www.zacks.com/articles/index.php", "Zacks")]
    if watchlist:
        for sym in watchlist[:3]:
            urls.append((f"https://www.zacks.com/stock/quote/{sym.upper()}?q={sym.upper()}", f"Zacks/{sym.upper()}"))

    for page_url, source_label in urls:
        try:
            html = await _fetch_page(page_url, "table#news_table, article.news-item, div.news_wrap", timeout=20_000)
            if not html:
                logger.warning(f"zacks: no html from {page_url}")
                continue
            soup = BeautifulSoup(html, "html.parser")

            # Main news page: table#news_table rows
            for row in soup.select("table#news_table tr, div.news_wrap div.news-item")[:30]:
                a = row.find("a", href=True)
                if not a:
                    continue
                title = a.get_text(strip=True)
                if not title or title in seen or len(title) < 10:
                    continue
                seen.add(title)
                href = a["href"]
                if not href.startswith("http"):
                    href = "https://www.zacks.com" + href
                date_el = row.find("time") or row.find(class_=lambda c: c and "date" in c.lower())
                items.append({
                    "source":     source_label,
                    "title":      title,
                    "url":        href,
                    "published":  date_el.get_text(strip=True) if date_el else "",
                    "fetched_at": datetime.now(UTC).isoformat(),
                })

            # Fallback: any article links on the page
            if not items:
                for a in soup.select("a[href*='/stock/news/'], a[href*='/commentary/']")[:25]:
                    title = a.get_text(strip=True)
                    if not title or title in seen or len(title) < 15:
                        continue
                    seen.add(title)
                    href = a["href"]
                    if not href.startswith("http"):
                        href = "https://www.zacks.com" + href
                    items.append({
                        "source":     source_label,
                        "title":      title,
                        "url":        href,
                        "published":  "",
                        "fetched_at": datetime.now(UTC).isoformat(),
                    })

            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"zacks {page_url}: {e}")

    portfolio.save_crawl_results("zacks", items)
    return items


async def crawl_yahoo_finance(watchlist: list[str] | None = None) -> list[dict]:
    """
    Fetch Yahoo Finance news via RSS — top stories plus per-symbol feeds.
    """
    import xml.etree.ElementTree as ET

    import httpx

    feeds = ["https://finance.yahoo.com/rss/topfinancenews"]
    if watchlist:
        for sym in watchlist[:5]:
            feeds.append(
                f"https://feeds.finance.yahoo.com/rss/2.0/headline"
                f"?s={sym.upper()}&region=US&lang=en-US"
            )

    items: list[dict] = []
    seen: set[str] = set()
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; financebot/1.0)"},
            timeout=15,
            follow_redirects=True,
        ) as client:
            for url in feeds:
                try:
                    r = await retry_async(lambda: client.get(url), WEB_RETRY)
                    if r.status_code != 200:
                        logger.warning(f"yahoo rss {url}: HTTP {r.status_code}")
                        continue
                    root = ET.fromstring(r.content)
                    for item in root.findall(".//item"):
                        title = (item.findtext("title") or "").strip()
                        link  = (item.findtext("link")  or "").strip()
                        pub   = (item.findtext("pubDate") or "").strip()
                        desc  = (item.findtext("description") or "").strip()
                        if title and link and title not in seen:
                            seen.add(title)
                            items.append({
                                "source":     "Yahoo Finance",
                                "title":      title,
                                "url":        link,
                                "published":  pub,
                                "summary":    desc[:200] if desc else "",
                                "fetched_at": datetime.now(UTC).isoformat(),
                            })
                    await asyncio.sleep(1)
                except ET.ParseError as e:
                    logger.warning(f"yahoo rss parse {url}: {e}")
                except Exception as e:
                    logger.warning(f"yahoo rss {url}: {e}")
    except Exception as e:
        logger.warning(f"yahoo rss: {e}")

    portfolio.save_crawl_results("yahoo", items)
    return items


# ── Master crawl run ──────────────────────────────────────────────────────────

async def run_full_crawl(watchlist: list[str]) -> dict:
    """
    Run all crawlers sequentially, one at a time, with a pause between each.
    This prevents spawning multiple browser processes simultaneously and avoids
    rate-limiting by target sites.
    watchlist: symbols to crawl per-symbol sources for (first 3 used).
    Returns summary of items fetched per source.
    """
    logger.info(f"Starting sequential crawl for {len(watchlist)} symbols…")
    summary: dict = {}

    # Global sources — one at a time
    global_sources: list[tuple[str, Any]] = [
        ("reddit",          crawl_reddit),
        ("marketwatch",     crawl_marketwatch),
        ("yahoo",           lambda: crawl_yahoo_finance(watchlist)),
        ("finviz_general",  crawl_finviz_news),
        ("polymarket",      crawl_polymarket),
        ("zacks",           lambda: crawl_zacks(watchlist)),
    ]

    for name, factory in global_sources:
        try:
            result = await factory()
            summary[name] = {"count": len(result)}
            logger.info(f"  {name}: {len(result)} items")
        except Exception as e:
            summary[name] = {"error": str(e), "count": 0}
            logger.warning(f"  {name}: {e}")
        await asyncio.sleep(3)   # pause between sources

    # Per-symbol sources — cap at 3 symbols to keep runtime reasonable
    for sym in watchlist[:3]:
        sym_sources = [
            (f"finviz_{sym}",         lambda s=sym: crawl_finviz_news(s)),
            (f"google_finance_{sym}", lambda s=sym: crawl_google_finance_news(s)),
        ]
        for name, factory in sym_sources:
            try:
                result = await factory()
                summary[name] = {"count": len(result)}
                logger.info(f"  {name}: {len(result)} items")
            except Exception as e:
                summary[name] = {"error": str(e), "count": 0}
                logger.warning(f"  {name}: {e}")
            await asyncio.sleep(3)

    summary["completed_at"] = datetime.now(UTC).isoformat()
    logger.info(f"Crawl complete: {summary}")
    return summary
