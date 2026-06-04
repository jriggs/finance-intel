"""
Finance prompt builders.

Dependency-injected helpers that assemble the LLM prompts for extended
single-stock insights and the finance-aware Q&A context. The model (`llm`) and
RAG pipeline (`rag`) are passed in as arguments — no module/global state — so
these stay independent of the web/router layer and are unit-testable.
"""
from __future__ import annotations

import asyncio
import contextlib

import db as portfolio
import fundamentals as fd
import market
import signals

def _fmt_pct(v, digits: int = 1) -> str:
    """Format a decimal-fraction metric as a percentage string. Returns '—' if None/invalid."""
    try:
        f = float(v)
        return f"{f * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_ratio(v, digits: int = 2) -> str:
    """Format a ratio/multiple. Returns '—' if None/invalid."""
    try:
        return f"{float(v):.{digits}f}x"
    except (TypeError, ValueError):
        return "—"


async def _build_insight_prompt(sym: str, rag) -> str:
    """Gather all data for sym and return the fully-formatted LLM prompt string."""

    # Bust both caches so all fetches go live (no stale data in extended insights)
    await asyncio.to_thread(market.bust_symbol_cache, sym)
    await asyncio.to_thread(fd.bust_symbol_cache, sym)

    # ── Gather all available data in parallel ──────────────────────────────
    info, chart, news, snap, sig_scores = await asyncio.gather(
        asyncio.to_thread(market.get_stock_info, sym),
        asyncio.to_thread(market.get_chart_data, sym, "6mo"),
        asyncio.to_thread(market.get_news, sym, 15),
        asyncio.to_thread(fd.get_macro_snapshot),
        asyncio.to_thread(signals.score_stock, sym),
        return_exceptions=True,
    )

    # EDGAR financial history (5yr XBRL)
    try:
        fin_history = await asyncio.to_thread(fd.get_financial_history, sym)
    except Exception:
        fin_history = None

    # RAG: retrieve any ingested SEC filings / FRED context
    rag_context = ""
    if rag:
        with contextlib.suppress(Exception):
            rag_context = await rag.query_async(
                f"{sym} financial analysis SEC filing revenue earnings risk", n_results=4
            )

    # Crawled sentiment sources (Reddit, MarketWatch, Zacks, Finviz)
    crawl_parts = []
    for key, label in [
        ("reddit",                    "Reddit Trending"),
        ("marketwatch",               "MarketWatch"),
        (f"finviz_{sym}",             f"Finviz ({sym})"),
        (f"google_finance_{sym}",     f"Google Finance ({sym})"),
        (f"zacks_{sym}",              f"Zacks ({sym})"),
    ]:
        cached = portfolio.get_crawl_results(key)
        if cached and cached.get("items"):
            items = cached["items"][:5]
            crawl_parts.append(f"\n### {label} (web crawl, sample headlines only — treat as directional, not scored)")
            for item in items:
                title = item.get("title") or item.get("question") or ""
                crawl_parts.append(f"- {title[:140]}")

    # ── Build the system + user prompt ────────────────────────────────────
    sections = [
        f"Senior equity analyst. Write a tight Extended Insights report for {sym}. Rules:\n"
        "- UNITS: margins/ROE/ROA/growth are raw decimals (0.15=15%); D/E is a ratio; P/E/EV multiples are Xx. "
        "Flag implausible figures.\n"
        "- RECONCILE contradictions (overbought RSI + cheap P/E, etc.) — state which signal wins and why.\n"
        "- ONE ACTION: end with exactly 'Buy now', 'Buy on pullback to $X', or 'Wait'. No hedging.\n"
        "- RIGHT MULTIPLE: lead with the multiple that fits this business; always include ≥1 EV-based multiple.\n"
        "- MACRO→P&L: each macro point must name the specific line item affected, or omit it.\n"
        "- CATALYSTS: cover earnings, buybacks/dividends, M&A, spin-offs, guidance revisions.\n"
        "- CONSENSUS THEN EDGE: state analyst rating+target, then agree/differ and why.\n"
        "- COMPLETE CALL: target price (show math), invalidation condition, time horizon — all three required.\n"
        "- SIGNAL SCORES are algorithmic/rule-based; web-crawl headlines are directional only.\n"
        "- PAIR every positive metric with its most unfriendly comparable.\n"
        "- RANK RISKS by probability×impact; include estimated downside per risk.\n"
        "- STEELMAN: one sentence for the strongest bear argument before Bottom Line.\n"
        "Be direct. No filler. Each section 2-4 sentences or bullets — stop when the point is made."
    ]

    # Stock info
    if not isinstance(info, Exception) and info:
        sections.append(f"\n## {sym} — Snapshot")
        sections.append(
            f"Name: {info.get('name')}  |  Sector: {info.get('sector')}  |  Industry: {info.get('industry')}\n"
            f"Price: ${info.get('price')}  |  Change today: {info.get('change_pct')}%\n"
            f"52W High: ${info.get('52w_high')}  |  52W Low: ${info.get('52w_low')}\n"
            f"Market Cap: ${info.get('market_cap')}  |  Beta: {info.get('beta')}\n"
            f"Dividend Yield: {_fmt_pct(info.get('dividend_yield'))} (raw={info.get('dividend_yield')})  |  "
            f"Earnings Date: {info.get('earnings_date')}"
        )
        sections.append(
            f"\n## Valuation\n"
            f"NOTE: margins/ROE/ROA/growth below are raw yfinance decimals — multiply ×100 for percentage.\n"
            f"P/E (TTM): {info.get('pe_ratio')}x  |  Forward P/E: {info.get('forward_pe')}x  |  P/B: {info.get('pb_ratio')}x\n"
            f"EV/EBITDA: {info.get('ev_ebitda')}x  |  EV/Revenue: {info.get('ev_revenue')}x\n"
            f"Profit Margin: {info.get('profit_margin')} (={_fmt_pct(info.get('profit_margin'))})  |  "
            f"ROE: {info.get('roe')} (={_fmt_pct(info.get('roe'))})  |  "
            f"ROA: {info.get('roa')} (={_fmt_pct(info.get('roa'))})\n"
            f"Revenue Growth (YoY): {info.get('revenue_growth')} (={_fmt_pct(info.get('revenue_growth'))})  |  "
            f"Earnings Growth (YoY): {info.get('earnings_growth')} (={_fmt_pct(info.get('earnings_growth'))})\n"
            f"Free Cash Flow: ${info.get('free_cashflow')}  |  Debt/Equity: {_fmt_ratio(info.get('debt_equity'))} (raw={info.get('debt_equity')})\n"
            f"Current Ratio: {info.get('current_ratio')}x  |  "
            f"Insider Ownership: {info.get('insider_pct')}%  |  Institutional Ownership: {info.get('institution_pct')}%"
        )
        if info.get("analyst_target"):
            upside = ""
            try:
                price = float(info.get("price") or 0)
                target = float(info.get("analyst_target") or 0)
                if price and target:
                    upside = f"  ({(target/price - 1)*100:.1f}% upside/downside vs current)"
            except Exception:
                pass
            sections.append(
                f"\n## Analyst Consensus\n"
                f"Target Price: ${info.get('analyst_target')}{upside}\n"
                f"Recommendation Mean: {info.get('recommendation_mean')} (scale: 1=Strong Buy, 3=Hold, 5=Strong Sell)  |  "
                f"Number of Analysts: {info.get('analyst_count')}"
            )
        if info.get("description"):
            sections.append(f"\n## Business Description\n{str(info.get('description'))[:600]}")

        # Computed metrics from existing data
        computed = []
        try:
            fcf = float(info.get("free_cashflow") or 0)
            mcap = float(info.get("market_cap") or 0)
            if fcf and mcap:
                fcf_yield = fcf / mcap * 100
                computed.append(f"FCF Yield: {fcf_yield:.1f}% (FCF ${fcf:,.0f} / Mkt Cap ${mcap:,.0f})")
        except Exception:
            pass
        try:
            beta = float(info.get("beta") or 0)
            de = float(info.get("debt_equity") or 0)
            rf = 0.0
            if not isinstance(snap, Exception) and snap:
                t10 = snap.get("10Y_Treasury") or snap.get("10Y Treasury") or {}
                if isinstance(t10, dict):
                    rf = float(t10.get("value") or 0) / 100
            if beta and rf:
                ke = rf + beta * 0.055  # CAPM, 5.5% equity risk premium
                kd = rf + 0.02          # rough debt spread over risk-free
                tax = 0.21
                if de > 0:
                    e_w = 1 / (1 + de)
                    d_w = de / (1 + de)
                    wacc = ke * e_w + kd * (1 - tax) * d_w
                else:
                    wacc = ke
                computed.append(
                    f"Est. WACC: {wacc*100:.1f}% "
                    f"(Ke={ke*100:.1f}% via CAPM β={beta} rf={rf*100:.1f}%; "
                    f"Kd={kd*100:.1f}% after-tax; D/E={de:.2f})"
                )
        except Exception:
            pass
        if computed:
            sections.append("\n## Computed Metrics\n" + "\n".join(computed))

    # Price history & technicals
    if not isinstance(chart, Exception) and isinstance(chart, dict) and chart.get("candles"):
        candles = chart["candles"]
        latest = candles[-1] if candles else {}
        sections.append("\n## Technical Picture")
        sections.append(
            f"RSI(14): {latest.get('rsi', '—')} (overbought >70, oversold <30)  |  "
            f"MACD: {latest.get('macd', '—')} (Signal: {latest.get('macd_signal', '—')})\n"
            f"SMA20: ${latest.get('sma20', '—')}  |  SMA50: ${latest.get('sma50', '—')}  |  SMA200: ${latest.get('sma200', '—')}\n"
            f"Bollinger Upper: ${latest.get('bb_upper', '—')}  |  Lower: ${latest.get('bb_lower', '—')}\n"
            f"ATR(14): ${latest.get('atr14', '—')} (daily range measure; useful for stop/target sizing)"
        )
        try:
            first_close = float(candles[0]["close"]) if candles[0].get("close") else 0
            last_close  = float(latest["close"]) if latest.get("close") else 0
            if first_close:
                pct_6m = (last_close - first_close) / first_close * 100
                sections.append(f"6-Month Return: {pct_6m:.1f}%  |  Data points: {len(candles)} trading days")
        except Exception:
            pass

    # Signal scores
    if not isinstance(sig_scores, Exception) and sig_scores:
        s = sig_scores
        bd = s.get("breakdown", {})
        sections.append(
            f"\n## Signal Scores (algorithmic rule-based system, not a sentiment poll)\n"
            f"Overall: {s.get('score')}/100  Grade: {s.get('grade')}  Recommendation: {s.get('recommendation')}\n"
            f"Short-term: {s.get('short_term', {}).get('score')}/100  |  "
            f"Long-term: {s.get('long_term', {}).get('score')}/100  |  "
            f"Macro: {s.get('macro', {}).get('score')}/100  |  "
            f"Sentiment: {s.get('sentiment', {}).get('score')}/100"
        )
        val = bd.get("value", {})
        tech = bd.get("technical", {})
        ana = bd.get("analyst", {})
        if val:
            sections.append(
                f"Value sub-score: {val.get('score')}/{val.get('max')}  |  "
                f"Technical sub-score: {tech.get('score')}/{tech.get('max')}  |  "
                f"Analyst sub-score: {ana.get('score')}/{ana.get('max')}"
            )
        reasons = s.get("all_reasons", [])
        if reasons:
            sections.append("Key scoring factors: " + "; ".join(reasons[:10]))

    # FRED macro
    if not isinstance(snap, Exception) and snap:
        sections.append(
            "\n## Macroeconomic Environment (FRED)\n"
            "NOTE: For each macro factor in your report, you MUST name the specific {sym} P&L line it affects."
        )
        for _, d in snap.items():
            sections.append(f"{d['label']}: {d['value']} (as of {d['date']})")

    # Financial history
    if fin_history and not isinstance(fin_history, Exception):
        sections.append("\n## 5-Year Financial History (EDGAR XBRL)")
        by_period: dict[str, dict] = {}
        for metric, records in fin_history.items():
            for rec in records:
                period = rec.get("period", "?")
                if period not in by_period:
                    by_period[period] = {}
                by_period[period][metric] = rec.get("value")
        for period in sorted(by_period.keys(), reverse=True)[:5]:
            m = by_period[period]
            rev = m.get("Revenue") or "—"
            ni  = m.get("NetIncome") or "—"
            eps = m.get("EPS") or "—"
            sections.append(f"{period}: Revenue={rev}  NetIncome={ni}  EPS={eps}")

    # News
    if not isinstance(news, Exception) and news:
        sections.append(f"\n## Recent News & Catalysts ({len(news)} articles)")
        for n in news[:12]:
            pub = n.get("published", "")[:10] if n.get("published") else ""
            sections.append(f"- [{pub}] {n.get('title','')} ({n.get('publisher','')})")

    # Crawled sentiment
    if crawl_parts:
        sections.append("\n## Web-Crawled Sentiment Headlines (directional only — no scoring methodology)")
        sections.extend(crawl_parts)

    # RAG
    if rag_context:
        sections.append("\n## Knowledge Base (SEC Filings / FRED)")
        sections.append(rag_context[:2000])

    system_prompt = "\n".join(sections)

    user_prompt = (
        f"Write a concise Extended Insights report for {sym}. "
        "Keep each section to 2-4 bullets or sentences. Sections:\n"
        "1. Thesis (2 sentences — core variant perception)\n"
        "2. Valuation (lead multiple + ≥1 EV multiple; pair each positive with unfriendly comp)\n"
        "3. Technicals vs. Fundamentals (reconcile any contradiction; 52W range context)\n"
        "4. Macro Impact (P&L line per point only)\n"
        "5. Catalysts & Consensus (earnings date, capital return, analyst target — agree/differ)\n"
        "6. Risks (top 3, ranked by probability×impact, downside % each)\n"
        "7. Bottom Line: [Bear case in 1 sentence.] "
        "Action: Buy now / Buy on pullback to $X / Wait. "
        "Target: $X (Yx × $Z estimate). Invalidation: [condition]. Horizon: [timeframe].\n\n"
        "Numbers only from the data provided. No filler."
    )

    from types import SimpleNamespace

    import main as _main
    from prompts import build_prompt

    return build_prompt(
        template=getattr(_main, "active_template", "mistral"),
        system=system_prompt,
        messages=[SimpleNamespace(role="user", content=user_prompt)],
    )


def _insight_max_tokens(llm, prompt_text: str, desired: int = 2200) -> int:
    """Return max_tokens capped so prompt + output fits within n_ctx."""
    try:
        prompt_tokens = len(llm.tokenize(prompt_text))
        available = llm.n_ctx() - prompt_tokens - 64  # 64-token safety margin
        return max(256, min(desired, available))
    except Exception:
        return desired


async def generate_extended_insight_text(symbol: str, llm, rag) -> str:
    """Build prompt and collect full LLM response. Used by the scheduler."""
    prompt_text = await _build_insight_prompt(symbol.upper(), rag)
    full = ""
    async for token in llm.stream(prompt_text, max_tokens=_insight_max_tokens(llm, prompt_text)):
        full += token
    return full.strip()



async def _build_finance_context(symbol: str | None) -> str:
    parts = [
        "You are a financial analyst assistant. "
        "Match your answer length to the question: "
        "factual questions (CEO, price, ticker, date) → 1-2 sentences maximum. "
        "Analysis questions (buy/sell, outlook, risks) → structured but focused, no fluff. "
        "Never pad answers. Use the market data and news context provided below."
    ]

    if symbol:
        # Fetch stock info, news, and macro in parallel — skip score_stock (too slow)
        info, news, snap = await asyncio.gather(
            asyncio.to_thread(market.get_stock_info, symbol.upper()),
            asyncio.to_thread(market.get_news, symbol.upper(), 5),
            asyncio.to_thread(fd.get_macro_snapshot),
            return_exceptions=True,
        )

        # Handle stock info
        if not isinstance(info, Exception) and info:
            parts.append(f"\n## {symbol.upper()} — Current Data")
            parts.append(f"Price: ${info.get('price')}  |  P/E: {info.get('pe_ratio')}  |  Mkt Cap: ${info.get('market_cap')}")
            parts.append(f"52W High: ${info.get('52w_high')}  |  52W Low: ${info.get('52w_low')}")
            parts.append(f"Sector: {info.get('sector')}  |  Industry: {info.get('industry')}")

        # Use cached signal score (from volume analysis DB — no live fetch needed)
        sig = portfolio.get_volume_result(symbol.upper())
        if sig:
            parts.append(f"Signal Score: {sig.get('score')}/100  Grade: {sig.get('grade')}  Rec: {sig.get('recommendation')}")
            all_reasons = sig.get("all_reasons") or []
            if all_reasons:
                parts.append("Signal reasons: " + "; ".join(all_reasons[:5]))

        # Handle news
        if not isinstance(news, Exception) and news:
            parts.append("\n## Recent Headlines")
            for n in news[:4]:
                parts.append(f"- {n.get('title','')} ({n.get('publisher','')})")

        # Handle FRED macro snapshot
        if not isinstance(snap, Exception) and snap:
            parts.append("\n## US Macro Indicators (FRED)")
            for _, d in list(snap.items())[:8]:
                parts.append(f"{d['label']}: {d['value']} [{d['date']}]")

    # Attach crawled news sources
    reddit = portfolio.get_crawl_results("reddit")
    if reddit and reddit.get("items"):
        parts.append("\n## Reddit Trending")
        for item in reddit["items"][:4]:
            parts.append(f"- [{item.get('source','')}] {item.get('title','')[:120]}")

    marketwatch = portfolio.get_crawl_results("marketwatch")
    if marketwatch and marketwatch.get("items"):
        parts.append("\n## MarketWatch Headlines")
        for item in marketwatch["items"][:4]:
            parts.append(f"- {item.get('title','')[:120]}")

    yahoo = portfolio.get_crawl_results("yahoo")
    if yahoo and yahoo.get("items"):
        parts.append("\n## Yahoo Finance News")
        for item in yahoo["items"][:4]:
            parts.append(f"- {item.get('title','')[:120]}")

    zacks = portfolio.get_crawl_results("zacks")
    if zacks and zacks.get("items"):
        parts.append("\n## Zacks Headlines")
        for item in zacks["items"][:4]:
            parts.append(f"- {item.get('title','')[:120]}")

    finviz = portfolio.get_crawl_results("finviz")
    if finviz and finviz.get("items"):
        parts.append("\n## Finviz News")
        for item in finviz["items"][:4]:
            parts.append(f"- {item.get('title','')[:120]}")

    # Symbol-specific crawl results (Finviz, Google Finance, Zacks per-ticker)
    if symbol:
        for source_key in (
            f"finviz_{symbol.upper()}",
            f"google_finance_{symbol.upper()}",
            f"zacks_{symbol.upper()}",
        ):
            cached = portfolio.get_crawl_results(source_key)
            if cached and cached.get("items"):
                label = source_key.replace("_", " ").title()
                parts.append(f"\n## {label} News")
                for item in cached["items"][:3]:
                    parts.append(f"- {item.get('title','')[:120]}")

    polymarket = portfolio.get_crawl_results("polymarket")
    if polymarket and polymarket.get("items"):
        parts.append("\n## Macro Prediction Markets")
        for item in polymarket["items"][:3]:
            parts.append(f"- {item.get('question','')} → {item.get('outcomes','')}")

    return "\n".join(parts)


