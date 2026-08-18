// ── Stock detail view ─────────────────────────────────────────────────────────

import { apiFetch } from "../core/api.js";
import { $, fmt, fmtDollar, fmtPct, fmtBig, colorPct, escapeHtml } from "../core/utils.js";
import {
  setCurrentSymbol,
  currentPeriod,
  stockCache,
  insightAbort,
  setInsightAbort,
} from "../core/state.js";
// insightAbort/setInsightAbort kept for abort-on-navigate
import { loadChart } from "./chart.js";
import { loadExtendedInsight } from "./chat.js";
import type { StockInfo, Signal, NewsItem } from "../core/types.js";

function applyStockData(
  sym: string,
  info: StockInfo | null,
  sig: Signal | null,
  news: { news: NewsItem[] } | null,
): void {
  $("stock-header-sym").textContent = sym;
  ($("link-yahoo") as HTMLAnchorElement).href =
    `https://finance.yahoo.com/quote/${sym}`;
  ($("link-zacks") as HTMLAnchorElement).href =
    `https://www.zacks.com/stock/quote/${sym}`;
  $("header-badge-group").style.display = "flex";
  if (info) {
    $("stock-header-name").textContent = info.name ?? "";
    const priceEl = $("stock-header-price");
    priceEl.textContent = fmtDollar(info.price);
    priceEl.className = `stock-header-price ${colorPct(info.change_pct)}`;
    const chgEl = $("stock-header-chg");
    chgEl.textContent = fmtPct(info.change_pct);
    chgEl.className = colorPct(info.change_pct);
    if (info.price)
      ($("pick-entry") as HTMLInputElement).value = String(info.price);
  }
  if (sig) {
    const badge = $("stock-rec-badge");
    badge.textContent = sig.recommendation;
    badge.className = `rec-badge rec-${sig.recommendation}`;
    badge.style.display = "";
    const rings = $("header-score-rings");
    const st = sig.short_term ?? { score: 0, grade: "D" };
    const lt = sig.long_term ?? { score: 0, grade: "D" };
    const mo = sig.macro ?? { score: 0, grade: "D" };
    const se = sig.sentiment ?? { score: 0, grade: "D" };
    rings.innerHTML = (
      [
        ["ST", st],
        ["LT", lt],
        ["MO", mo],
        ["SE", se],
      ] as [string, { score: number; grade: string }][]
    )
      .map(
        ([abbr, cat]) => `
        <div class="score-circle-sm ${cat.grade}">
          <span class="score-num">${cat.score}</span>
          <span class="score-grade">${abbr}</span>
        </div>`,
      )
      .join("");
    rings.style.display = "flex";
    renderSignalDetail($("signal-detail"), sig, info?.description);
  } else {
    $("signal-detail").innerHTML = '<div class="empty">No signal data</div>';
  }
  if (info) renderFundamentals($("fund-detail"), info, sig);
  else $("fund-detail").innerHTML = '<div class="empty">No data</div>';
  renderNews($("stock-news"), news?.news ?? []);
}

export async function loadStockDetail(sym: string): Promise<void> {
  $("main").scrollTop = 0;
  if (insightAbort) {
    insightAbort.abort();
    setInsightAbort(null);
  }

  $("ai-insight-output").textContent = "";
  $("ei-generated-at").textContent = "";
  ($("ai-insight-btn") as HTMLButtonElement).disabled = false;
  $("ai-insight-btn").textContent = "Run";
  $("chat-messages").innerHTML = "";

  $("stock-header-sym").textContent = sym;
  $("stock-header-name").textContent = "…";
  $("stock-header-price").textContent = "";
  $("stock-header-chg").textContent = "";
  $("header-badge-group").style.display = "none";
  $("header-score-rings").style.display = "none";
  ($("pick-entry") as HTMLInputElement).value = "";

  setCurrentSymbol(sym);

  const cached = stockCache.get(sym);
  if (cached) {
    applyStockData(sym, cached.info, cached.sig, cached.news);
    void loadChart(currentPeriod);
    void loadExtendedInsight(sym);
    // If a previous fetch partially failed, silently backfill the missing pieces
    if (!cached.info || !cached.sig || !cached.news) {
      const [info, sig, news] = await Promise.all([
        cached.info
          ? Promise.resolve(cached.info)
          : apiFetch<StockInfo>(`/api/finance/stock/${sym}`).catch(() => null),
        cached.sig
          ? Promise.resolve(cached.sig)
          : apiFetch<Signal>(`/api/finance/signal/${sym}`).catch(() => null),
        cached.news
          ? Promise.resolve(cached.news)
          : apiFetch<{ news: NewsItem[] }>(`/api/finance/news/${sym}`).catch(() => null),
      ]);
      if (info !== cached.info || sig !== cached.sig || news !== cached.news) {
        stockCache.set(sym, { ...cached, info, sig, news });
        applyStockData(sym, info, sig, news);
      }
    }
    return;
  }

  $("signal-detail").innerHTML = '<div class="spinner"></div>';
  $("fund-detail").innerHTML = '<div class="spinner"></div>';
  $("stock-news").innerHTML = '<div class="spinner"></div>';

  const [info, sig, news] = await Promise.all([
    apiFetch<StockInfo>(`/api/finance/stock/${sym}`).catch(() => null),
    apiFetch<Signal>(`/api/finance/signal/${sym}`).catch(() => null),
    apiFetch<{ news: NewsItem[] }>(`/api/finance/news/${sym}`).catch(
      () => null,
    ),
  ]);

  stockCache.set(sym, { info, sig, news, insightText: null });
  applyStockData(sym, info, sig, news);
  void loadChart(currentPeriod);
  void loadExtendedInsight(sym);
}

// Pre-entry gate thresholds — mirror daily_trades_router (_MAX_VOLATILITY /
// _MIN_DOLLAR_VOLUME) so the fundamentals grid flags names that would fail them.
const RULE_MAX_VOL = 0.55;
const RULE_MIN_DVOL = 5_000_000;

/** 60-day annualized volatility, red-flagged if it breaches the trade ceiling. */
function volatilityCell(v: number | null | undefined): string {
  if (v == null) return "—";
  const s = `${(v * 100).toFixed(0)}%`;
  return v > RULE_MAX_VOL ? `<span class="text-red">${s} ⚠</span>` : s;
}

/** Median 60-day dollar volume, red-flagged if below the trade liquidity floor. */
function liquidityCell(v: number | null | undefined): string {
  if (v == null) return "—";
  const s = `${fmtBig(v)}/day`;
  return v < RULE_MIN_DVOL ? `<span class="text-red">${s} ⚠</span>` : s;
}


export function renderSignalDetail(
  el: HTMLElement,
  sig: Signal,
  description?: string,
): void {
  const bd = sig.breakdown ?? {};

  const scoreBars = (
    [
      ["value", "Value", bd.value],
      ["technical", "Technical", bd.technical],
      ["analyst", "Analyst", bd.analyst],
    ] as [string, string, { score: number; max: number } | undefined][]
  )
    .filter(
      (row): row is [string, string, { score: number; max: number }] =>
        row[2] !== null && row[2] !== undefined,
    )
    .map(
      ([cls, label, b]) => `
      <div class="score-bar-row">
        <span class="score-bar-label">${label}</span>
        <div class="score-bar-track"><div class="score-bar-fill ${cls}" style="width:${Math.round((b.score / b.max) * 100)}%"></div></div>
        <span class="score-bar-val">${b.score}/${b.max}</span>
      </div>`,
    )
    .join("");

  el.innerHTML = `
    ${description ? `<div class="stock-description">${escapeHtml(description)}</div>` : ""}
    <div class="score-bars">${scoreBars}</div>
    <div class="reasons-list">
      <div class="reasons-header">Key factors:</div>
      ${(sig.all_reasons ?? []).map((r) => `<div class="reason-item">• ${r}</div>`).join("")}
    </div>
  `;
}

export function renderFundamentals(el: HTMLElement, info: StockInfo, sig?: Signal | null): void {
  const rows: [string, string][] = [
    ["Volatility (60d)", volatilityCell(sig?.volatility)],
    ["Liquidity ($/day)", liquidityCell(sig?.dollar_volume)],
    ["Market Cap", fmtBig(info.market_cap)],
    ["P/E (TTM)", info.pe_ratio !== null && info.pe_ratio !== undefined ? fmt(info.pe_ratio) : "—"],
    ["Forward P/E", info.forward_pe !== null && info.forward_pe !== undefined ? fmt(info.forward_pe) : "—"],
    ["P/B", info.pb_ratio !== null && info.pb_ratio !== undefined ? fmt(info.pb_ratio) : "—"],
    ["EV/EBITDA", info.ev_ebitda !== null && info.ev_ebitda !== undefined ? fmt(info.ev_ebitda) : "—"],
    ["Debt/Equity", info.debt_equity !== null && info.debt_equity !== undefined ? fmt(info.debt_equity) : "—"],
    [
      "Current Ratio",
      info.current_ratio !== null && info.current_ratio !== undefined ? fmt(info.current_ratio) : "—",
    ],
    ["ROE", info.roe !== null && info.roe !== undefined ? fmtPct(info.roe * 100) : "—"],
    ["ROA", info.roa !== null && info.roa !== undefined ? fmtPct(info.roa * 100) : "—"],
    [
      "Rev Growth",
      info.revenue_growth !== null && info.revenue_growth !== undefined ? fmtPct(info.revenue_growth * 100) : "—",
    ],
    [
      "Earn Growth",
      info.earnings_growth !== null && info.earnings_growth !== undefined ? fmtPct(info.earnings_growth * 100) : "—",
    ],
    [
      "Profit Margin",
      info.profit_margin !== null && info.profit_margin !== undefined ? fmtPct(info.profit_margin * 100) : "—",
    ],
    ["Free Cash Flow", fmtBig(info.free_cashflow)],
    ["52W High", fmtDollar(info["52w_high"])],
    ["52W Low", fmtDollar(info["52w_low"])],
    ["Analyst Target", fmtDollar(info.analyst_target)],
    ["Beta", info.beta !== null && info.beta !== undefined ? fmt(info.beta) : "—"],
    [
      "Div Yield",
      info.dividend_yield !== null && info.dividend_yield !== undefined ? fmtPct(info.dividend_yield * 100) : "—",
    ],
    [
      "Insider %",
      info.insider_pct !== null && info.insider_pct !== undefined ? fmtPct(info.insider_pct * 100) : "—",
    ],
    [
      "Inst %",
      info.institution_pct !== null && info.institution_pct !== undefined ? fmtPct(info.institution_pct * 100) : "—",
    ],
    ["Earnings Date", info.earnings_date ?? "—"],
    ["Sector", info.sector ?? "—"],
  ];
  el.innerHTML = `<div class="fund-grid">
    ${rows.map(([k, v]) => `<div class="fund-item"><div class="fund-key">${k}</div><div class="fund-val">${v}</div></div>`).join("")}
  </div>`;
}

export function renderNews(el: HTMLElement, items: NewsItem[]): void {
  if (!items || items.length === 0) {
    el.innerHTML = '<div class="empty">No news</div>';
    return;
  }
  el.innerHTML = items
    .map(
      (n) => `
    <div class="news-item">
      ${n.thumbnail ? `<img class="news-thumb" src="${n.thumbnail}" onerror="this.style.display='none'" />` : ""}
      <div class="news-body">
        <div class="news-title"><a href="${n.url}" target="_blank" rel="noopener">${n.title}</a></div>
        <div class="news-meta">${n.publisher ?? ""} ${n.published ? `· ${n.published.slice(0, 10)}` : ""}</div>
      </div>
    </div>
  `,
    )
    .join("");
}
