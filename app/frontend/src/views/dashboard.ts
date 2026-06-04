// ── Dashboard view ────────────────────────────────────────────────────────────

import { apiFetch } from "../core/api.js";
import { $, fmtDollar, fmtPct, colorPct, empty } from "../core/utils.js";
import { navigateToStock } from "../core/router.js";
import type { Quote, Signal, Position } from "../core/types.js";

interface Account {
  error?: string;
  portfolio_value?: number;
  cash?: number;
  pnl_today?: number;
  pnl_today_pct?: number;
}

interface PickStats {
  open?: number;
  win_rate?: number;
}

export async function loadDashboard(): Promise<void> {
  await Promise.all([
    loadAccountKpis(),
    loadDashSignals(),
    loadDashPositions(),
    loadDashQuotes(),
  ]);
}

export async function loadAccountKpis(): Promise<void> {
  try {
    const [acct, stats] = await Promise.all([
      apiFetch<Account>("/api/finance/broker/account"),
      apiFetch<PickStats>("/api/finance/picks/stats"),
    ]);
    $("kpi-portfolio").textContent = acct.error
      ? "—"
      : fmtDollar(acct.portfolio_value);
    $("kpi-cash").textContent = acct.error ? "—" : fmtDollar(acct.cash);
    const pnl = $("kpi-pnl");
    if (!acct.error) {
      pnl.textContent =
        `${fmtDollar(acct.pnl_today)} (${fmtPct(acct.pnl_today_pct)})`;
      pnl.className = `kpi-value ${colorPct(acct.pnl_today)}`;
    } else {
      pnl.textContent = "—";
    }
    $("kpi-open-picks").textContent = String(stats.open ?? "—");
    $("kpi-winrate").textContent =
      stats.win_rate !== null && stats.win_rate !== undefined ? `${stats.win_rate  }%` : "—";
  } catch {
    /* non-critical */
  }
}

export async function loadDashSignals(): Promise<void> {
  try {
    const d = await apiFetch<{ scores: Signal[] }>(
      "/api/finance/signals/watchlist",
    );
    const el = $("dash-signals");
    if (!d.scores || d.scores.length === 0) {
      el.innerHTML = empty('No scores yet');
      return;
    }
    el.innerHTML = d.scores
      .slice(0, 5)
      .map(
        (s) => `
      <div class="wl-item" onclick="navigateToStock('${s.symbol}')">
        <span class="sym">${s.symbol}</span>
        <span class="rec-badge rec-${s.recommendation} rec-badge-xs">${s.recommendation}</span>
        <span class="mono text-sm text-muted">${s.score}/100</span>
      </div>
    `,
      )
      .join("");
  } catch {
    /* non-critical */
  }
}

export async function loadDashPositions(): Promise<void> {
  try {
    const d = await apiFetch<{ positions: Position[] }>(
      "/api/finance/broker/positions",
    );
    const el = $("dash-positions");
    if (!d.positions || d.positions.length === 0) {
      el.innerHTML = empty('No open positions');
      return;
    }
    el.innerHTML = d.positions
      .slice(0, 6)
      .map(
        (p) => `
      <div class="wl-item" onclick="navigateToStock('${p.symbol}')">
        <span class="sym">${p.symbol}</span>
        <span class="${p.unrealized_pnl >= 0 ? "pos" : "neg"}">${fmtPct(p.unrealized_pnl_pct)}</span>
        <span class="text-sm text-muted">${fmtDollar(p.market_value)}</span>
      </div>
    `,
      )
      .join("");
  } catch {
    /* non-critical */
  }
}

export async function loadDashQuotes(): Promise<void> {
  try {
    const d = await apiFetch<{ quotes: Quote[] }>("/api/finance/quotes");
    const el = $("dash-quotes");
    el.innerHTML = `<table><thead><tr><th>Symbol</th><th>Price</th><th>Change</th><th>Volume</th></tr></thead><tbody>
      ${d.quotes
        .map(
          (q) => `<tr onclick="navigateToStock('${q.symbol}')" class="row-click">
        <td><strong>${q.symbol}</strong><div class="sub-text">${q.name ?? ""}</div></td>
        <td class="mono">${fmtDollar(q.price)}</td>
        <td class="${colorPct(q.change_pct)} mono">${fmtPct(q.change_pct)}</td>
        <td class="text-sm text-muted">${q.volume !== null && q.volume !== undefined ? Number(q.volume).toLocaleString() : "—"}</td>
      </tr>`,
        )
        .join("")}
    </tbody></table>`;
  } catch {
    /* non-critical */
  }
}

export { navigateToStock };
