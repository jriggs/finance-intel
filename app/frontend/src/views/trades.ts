// ── Daily Automated Trades view ───────────────────────────────────────────────

import { apiFetch } from '../core/api.js';
import { $, fmtDollar, fmtPct, fmt, colorPct } from '../core/utils.js';
import { navigateToStock } from '../core/router.js';

// ── Constants ─────────────────────────────────────────────────────────────────

const STATUS = {
  RECOMMENDED:  'recommended',
  PURCHASED:    'purchased',
  SELL_FLAGGED: 'sell_flagged',
  SOLD:         'sold',
  CONFIRMED:    'confirmed',
} as const;

const SOURCE = {
  VOLUME:   'volume',
  SCREENER: 'screener',
  OVERALL:  'overall',
} as const;

const API = {
  TODAY:   '/api/finance/daily-trades/today',
  HISTORY: '/api/finance/daily-trades/history',
  STATS:   '/api/finance/daily-trades/stats',
  CONFIRM: '/api/finance/daily-trades/today/confirm',
  SELL:    (id: string) => `/api/finance/daily-trades/position/${id}/sell`,
} as const;

// ── Types ─────────────────────────────────────────────────────────────────────

interface TradePosition {
  id: string;
  trade_date: string;
  symbol: string;
  signal_score: number | null;
  screener_score: number | null;
  volume_score: number | null;
  aggregate_score: number;
  allocation_usd: number;
  rank: number;
  status: string;
  sell_reason?: string;
  suggested_sell_reason?: string;
  entry_price: number | null;
  current_price: number | null;
  exit_price: number | null;
  pnl_pct: number | null;
  pnl_usd: number | null;
  sentiment_score: number | null;
  source: string | null;
}

interface TodayResponse {
  date: string;
  session: { status: string } | null;
  picks: TradePosition[];
  sell_review: TradePosition[];
  budget: number;
  data_warnings?: string[];
  is_market_open?: boolean;
}

interface HistorySession {
  id: string;
  trade_date: string;
  status: string;
  created_at: string;
  positions: TradePosition[];
}

interface StatsResponse {
  open_count: number | null;
  closed_count: number | null;
  flagged_count: number | null;
  total_invested: number | null;
  open_pnl_usd: number | null;
  avg_closed_pnl_pct: number | null;
}

// ── Module state ──────────────────────────────────────────────────────────────

let _todayData: TodayResponse | null = null;

// ── Formatting helpers ────────────────────────────────────────────────────────

function fmtScore(v: number | null): string {
  return v !== null ? fmt(v) : '—';
}

function fmtOptionalDollar(v: number | null): string {
  return v !== null ? fmtDollar(v) : '—';
}

function fmtOptionalPct(v: number | null): string {
  return v !== null ? fmtPct(v) : '—';
}

function statusBadge(status: string): string {
  const cls: Record<string, string> = {
    [STATUS.RECOMMENDED]:  'tag',
    [STATUS.PURCHASED]:    'tag open',
    [STATUS.SELL_FLAGGED]: 'tag sell',
    [STATUS.SOLD]:         'tag closed',
  };
  return `<span class="${cls[status] ?? 'tag'}">${status.replace('_', ' ').toUpperCase()}</span>`;
}

function sourceBadge(source: string | null): string {
  const cls: Record<string, string> = {
    [SOURCE.VOLUME]:   'tag open',
    [SOURCE.SCREENER]: 'tag',
    [SOURCE.OVERALL]:  'tag closed',
  };
  const label = source ?? SOURCE.OVERALL;
  return `<span class="${cls[label] ?? 'tag'}">${label.toUpperCase()}</span>`;
}

// ── KPI bar ───────────────────────────────────────────────────────────────────

function renderKpis(stats: StatsResponse): void {
  const openPnl  = stats.open_pnl_usd ?? 0;
  const pnlClass = openPnl >= 0 ? 'text-green' : 'text-red';

  const items: [string, string][] = [
    ['Open Positions',    String(stats.open_count    ?? '—')],
    ['Flagged Sell',      String(stats.flagged_count ?? '—')],
    ['Closed',            String(stats.closed_count  ?? '—')],
    ['Total Invested',    stats.total_invested    !== null ? fmtDollar(stats.total_invested)    : '—'],
    ['Open P&L',          stats.open_pnl_usd      !== null ? `<span class="${pnlClass}">${fmtDollar(openPnl)}</span>` : '—'],
    ['Avg Closed Return', stats.avg_closed_pnl_pct !== null ? fmtPct(stats.avg_closed_pnl_pct) : '—'],
  ];

  $('trades-kpis').innerHTML = items
    .map(([l, v]) => `<div class="kpi"><div class="kpi-label">${l}</div><div class="kpi-value kpi-value-lg">${v}</div></div>`)
    .join('');
}

// ── Data-freshness warnings ───────────────────────────────────────────────────

function renderDataWarnings(warnings: string[]): void {
  const el = $('trades-data-warnings');
  if (!el) return;
  if (!warnings.length) {
    el.style.display = 'none';
    el.innerHTML     = '';
    return;
  }
  el.style.display = '';
  el.innerHTML = warnings
    .map(w => `<div class="alert alert-warn">⚠ ${w}</div>`)
    .join('');
}

// ── Sell review panel ─────────────────────────────────────────────────────────

function renderSellReview(positions: TradePosition[]): void {
  const subtitle = $('trades-sell-subtitle');
  const empty    = $('trades-sell-empty');
  const table    = $('trades-sell-table') as HTMLTableElement;
  const tbody    = $('trades-sell-tbody');

  if (!positions.length) {
    subtitle.textContent = '';
    empty.style.display  = '';
    table.style.display  = 'none';
    return;
  }

  subtitle.textContent = ` · ${positions.length} flagged`;
  empty.style.display  = 'none';
  table.style.display  = '';

  tbody.innerHTML = positions.map(p => `
    <tr>
      <td><strong class="text-blue row-click" onclick="navigateToStock('${p.symbol}')">${p.symbol}</strong></td>
      <td class="text-sm text-muted">${p.trade_date}</td>
      <td class="mono">${fmtOptionalDollar(p.entry_price)}</td>
      <td class="mono">${fmtOptionalDollar(p.current_price)}</td>
      <td class="${colorPct(p.pnl_pct ?? 0)} mono">${fmtOptionalPct(p.pnl_pct)}</td>
      <td class="${colorPct(p.pnl_usd ?? 0)} mono">${fmtOptionalDollar(p.pnl_usd)}</td>
      <td class="text-sm">${p.suggested_sell_reason ?? p.sell_reason ?? '—'}</td>
      <td>
        <button class="btn btn-sm btn-ghost"
          onclick="closeDailyPosition('${p.id}', '${p.symbol}', ${p.current_price ?? 0})">
          Sell
        </button>
      </td>
    </tr>
  `).join('');
}

// ── Today's picks panel ───────────────────────────────────────────────────────

function renderPicks(
  picks: TradePosition[],
  session: TodayResponse['session'],
  budget: number,
): void {
  const loading    = $('trades-picks-loading');
  const table      = $('trades-picks-table') as HTMLTableElement;
  const tbody      = $('trades-picks-tbody');
  const subtitle   = $('trades-picks-subtitle');
  const confirmBtn = $('trades-confirm-btn') as HTMLButtonElement;

  const isConfirmed = session?.status === STATUS.CONFIRMED;
  subtitle.textContent = isConfirmed
    ? ' · confirmed'
    : ` · $${budget.toFixed(2)} budget`;

  if (isConfirmed) {
    confirmBtn.style.display = 'none';
  } else {
    confirmBtn.style.display = '';
  }

  if (!picks.length) {
    loading.textContent    = 'No data — run signal/screener/volume analysis first.';
    loading.style.display  = '';
    table.style.display    = 'none';
    return;
  }

  loading.style.display = 'none';
  table.style.display   = '';

  tbody.innerHTML = picks.map(p => `
    <tr>
      <td class="text-muted">${p.rank}</td>
      <td><strong class="text-blue row-click" onclick="navigateToStock('${p.symbol}')">${p.symbol}</strong></td>
      <td>${sourceBadge(p.source)}</td>
      <td class="mono" title="Signal ${fmtScore(p.signal_score)} · Screener ${fmtScore(p.screener_score)} · Volume ${fmtScore(p.volume_score)}"><strong>${fmtScore(p.aggregate_score)}</strong></td>
      <td>
        ${statusBadge(p.status)}
        ${p.status === STATUS.PURCHASED && p.entry_price !== null ? `
          <div class="text-sm">
            ${fmtDollar(p.entry_price)} → ${fmtOptionalDollar(p.current_price)}
            <span class="${colorPct(p.pnl_pct ?? 0)}">
              ${p.pnl_pct !== null ? fmtPct(p.pnl_pct) : ''}
              ${p.pnl_usd !== null ? `(${fmtDollar(p.pnl_usd)})` : ''}
            </span>
          </div>
        ` : ''}
      </td>
    </tr>
  `).join('');
}

// ── History panel ─────────────────────────────────────────────────────────────

function renderHistory(sessions: HistorySession[]): void {
  const el      = $('trades-history');
  const loading = $('trades-history-loading');

  if (!sessions.length) {
    loading.textContent = 'No history yet.';
    return;
  }
  loading.style.display = 'none';

  el.innerHTML = sessions.map(s => {
    const positions   = s.positions || [];
    const isConfirmed = s.status === STATUS.CONFIRMED;
    const totalPnlUsd = positions.reduce((sum, p) => sum + (p.pnl_usd ?? 0), 0);
    const pnlClass    = totalPnlUsd >= 0 ? 'text-green' : 'text-red';

    const rows = positions.map(p => `
      <tr>
        <td>${p.rank}</td>
        <td><span class="text-blue row-click" onclick="navigateToStock('${p.symbol}')">${p.symbol}</span></td>
        <td class="mono">${fmtScore(p.aggregate_score)}</td>
        <td>${statusBadge(p.status)}</td>
        <td class="mono">${fmtOptionalDollar(p.entry_price)}</td>
        <td class="mono">${p.current_price !== null ? fmtDollar(p.current_price) : fmtOptionalDollar(p.exit_price)}</td>
        <td class="${colorPct(p.pnl_pct ?? 0)} mono">${fmtOptionalPct(p.pnl_pct)}</td>
        <td class="${colorPct(p.pnl_usd ?? 0)} mono">${fmtOptionalDollar(p.pnl_usd)}</td>
        <td class="text-sm text-muted">${p.sell_reason ?? ''}</td>
      </tr>
    `).join('');

    return `
      <div style="margin-bottom:16px">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
          <strong>${s.trade_date}</strong>
          <span class="tag ${isConfirmed ? 'open' : ''}">${isConfirmed ? 'CONFIRMED' : 'RECOMMENDED'}</span>
          ${isConfirmed ? `<span class="${pnlClass} text-sm">P&L: ${fmtDollar(totalPnlUsd)}</span>` : ''}
        </div>
        <table>
          <thead><tr>
            <th>#</th><th>Symbol</th><th>Score</th>
            <th>Status</th><th>Entry</th><th>Current</th><th>P&L %</th><th>P&L $</th><th>Reason</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  }).join('<hr style="margin:16px 0;opacity:.2">');
}

// ── Public load function ──────────────────────────────────────────────────────

export async function loadTrades(force = false): Promise<void> {
  $('trades-picks-loading').textContent    = 'Computing picks…';
  $('trades-picks-loading').style.display  = '';
  $('trades-picks-table').style.display    = 'none';
  $('trades-history-loading').textContent  = 'Loading…';
  $('trades-history').innerHTML            = '';

  try {
    const url = force ? `${API.TODAY}?force=true` : API.TODAY;
    const [todayResp, histResp, statsResp] = await Promise.all([
      apiFetch<TodayResponse>(url),
      apiFetch<{ sessions: HistorySession[]; stats: StatsResponse }>(API.HISTORY),
      apiFetch<StatsResponse>(API.STATS),
    ]);

    _todayData = todayResp;

    renderKpis(statsResp);
    renderDataWarnings(todayResp.data_warnings ?? []);
    renderSellReview(todayResp.sell_review ?? []);
    renderPicks(todayResp.picks ?? [], todayResp.session, todayResp.budget);
    renderHistory(histResp.sessions ?? []);
  } catch (e) {
    $('trades-picks-loading').textContent = `Error: ${(e as Error).message}`;
  }
}

// ── Confirm action ────────────────────────────────────────────────────────────

export async function confirmTrades(): Promise<void> {
  if (!_todayData) return;

  const picks = _todayData.picks.filter(p => p.status === STATUS.RECOMMENDED);
  if (!picks.length) {
    alert('No recommended picks to confirm.');
    return;
  }

  const prices: Record<string, number> = {};
  for (const p of picks) {
    const defaultPrice = p.current_price !== null ? p.current_price.toFixed(2) : '';
    const input = prompt(
      `Entry price for ${p.symbol}?`,
      defaultPrice,
    );
    if (!input) return;
    const price = parseFloat(input);
    if (isNaN(price) || price <= 0) { alert(`Invalid price for ${p.symbol}`); return; }
    prices[p.symbol] = price;
  }

  try {
    await apiFetch(API.CONFIRM, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ prices }),
    });
    await loadTrades();
  } catch (e) {
    alert(`Confirm failed: ${(e as Error).message}`);
  }
}

// ── Sell a position ───────────────────────────────────────────────────────────

export async function closeDailyPosition(id: string, sym: string, currentPrice: number): Promise<void> {
  const input = prompt(`Exit price for ${sym}?`, currentPrice.toFixed(2));
  if (!input) return;
  const exitPrice = parseFloat(input);
  if (isNaN(exitPrice) || exitPrice <= 0) { alert('Invalid price'); return; }

  try {
    await apiFetch(API.SELL(id), {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ exit_price: exitPrice }),
    });
    await loadTrades();
  } catch (e) {
    alert(`Sell failed: ${(e as Error).message}`);
  }
}

export { navigateToStock };
