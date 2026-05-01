// ── Signals, screeners & recommendations ──────────────────────────────────────

import { apiFetch } from '../core/api.js';
import { $, fmt, spinner, empty, loadingBlock } from '../core/utils.js';
import { navigateToStock } from '../core/router.js';
import type { Signal } from '../core/types.js';

// ── Watchlist signals ─────────────────────────────────────────────────────────

export async function loadSignals(force = false): Promise<void> {
  $('signals-grid').innerHTML = spinner();
  const url = `/api/finance/signals/watchlist${  force ? '?force=true' : ''}`;
  const d = await apiFetch<{ scores: Signal[]; cached_at?: string }>(url).catch(() => ({ scores: [], cached_at: undefined }));
  if (d.cached_at) $('signals-cached').textContent = `Updated ${  d.cached_at.slice(0, 16).replace('T', ' ')}`;
  renderSignalCards($('signals-grid'), d.scores || []);
}

export function renderSignalCards(el: HTMLElement, scores: Signal[]): void {
  if (!scores.length) { el.innerHTML = empty('No results'); return; }
  el.innerHTML = scores.map(s => `
    <div class="sig-card" onclick="navigateToStock('${s.symbol}')">
      <div class="sig-card-top">
        <span class="rec-badge rec-${s.recommendation ?? 'HOLD'} rec-badge-sm">${s.recommendation ?? '—'}</span>
        <div class="sig-card-meta">
          <span class="sig-card-sym">${s.symbol}</span>
          <span class="sig-score-num">${s.score || 0}</span>
        </div>
      </div>
      <div class="sub-text">${s.name ?? ''}${s.sector ? ` · <span class="text-purple">${s.sector}</span>` : ''}</div>
      <div class="sig-card-grades">
        ${s.short_term ? `<span class="grade-badge grade-${s.short_term.grade || 'D'}">ST:${s.short_term.grade || '?'}</span>` : ''}
        ${s.long_term  ? `<span class="grade-badge grade-${s.long_term.grade  || 'D'}">LT:${s.long_term.grade  || '?'}</span>` : ''}
        ${s.macro      ? `<span class="grade-badge grade-${s.macro.grade      || 'D'}">MO:${s.macro.grade      || '?'}</span>` : ''}
        ${s.sentiment  ? `<span class="grade-badge grade-${s.sentiment.grade  || 'D'}">SE:${s.sentiment.grade  || '?'}</span>` : ''}
      </div>
      ${s.pe_ratio ? `<div class="sub-text">P/E ${fmt(s.pe_ratio)}</div>` : ''}
      <div class="sig-reasons">${(s.reasons ?? s.all_reasons ?? []).slice(0, 2).map(r => `• ${  r}`).join('<br>')}</div>
    </div>
  `).join('');
}

// ── Recommendations ───────────────────────────────────────────────────────────

export async function loadRecommendations(): Promise<void> {
  try {
    const d = await apiFetch<{ generated_at?: string; results?: Signal[] | { recommendations?: Signal[] } }>('/api/finance/recommendations');
    const generated = d.generated_at ? new Date(d.generated_at).toLocaleString() : 'N/A';
    $('rec-generated').textContent = `Generated: ${generated}`;

    const results: Signal[] = Array.isArray(d.results)
      ? d.results
      : (d.results as { recommendations?: Signal[] })?.recommendations ?? [];

    if (results.length > 0) {
      $('rec-grid').innerHTML = results.map(s => `
        <div class="sig-card" onclick="navigateToStock('${s.symbol}')">
          <div class="sig-card-header">
            <span class="sig-card-sym">${s.symbol}</span>
            <span class="sig-card-price">${s.score || 0}</span>
          </div>
          <div class="sig-card-rec"><span class="rec-badge rec-${s.recommendation}">${s.recommendation}</span></div>
          <div class="sig-reasons">${(s.reasons ?? s.all_reasons ?? []).slice(0, 2).map(r => `• ${  r}`).join('<br>')}</div>
        </div>
      `).join('');
    } else {
      const msg = d.generated_at
        ? `${empty(`No recommendations currently<br><span class="text-sm text-muted">Last generated: ${generated}</span>`)}`
        : empty('Recommendations will appear here after generation. Click Run Now to start.');
      $('rec-grid').innerHTML = msg;
    }
  } catch (e) {
    $('rec-grid').innerHTML = empty(`Error: ${(e as Error).message}`);
    $('rec-generated').textContent = '';
  }
}

export async function runRecommendationsNow(): Promise<void> {
  const btn = $('rec-run-btn') as HTMLButtonElement;
  btn.disabled = true;
  $('rec-grid').innerHTML = loadingBlock('Generating recommendations…');
  $('rec-generated').textContent = 'Generating…';
  try {
    await apiFetch('/api/finance/recommendations/run', { method: 'POST' });
    let updated = false;
    for (let i = 0; i < 30; i++) {
      await new Promise(r => setTimeout(r, 1000));
      $('rec-generated').textContent = `Generating… (${i}s)`;
      const d = await apiFetch<{ results?: unknown; generated_at?: string }>('/api/finance/recommendations');
      const hasResults = Array.isArray(d.results) ? d.results.length > 0 : false;
      if ((hasResults || i > 3) && d.generated_at) {
        await loadRecommendations();
        updated = true;
        break;
      }
    }
    if (!updated) await loadRecommendations();
  } catch (e) {
    $('rec-grid').innerHTML = `<div class="empty">Error: ${(e as Error).message}</div>`;
    $('rec-generated').textContent = 'Error';
  } finally {
    btn.disabled = false;
  }
}

// ── Screeners ─────────────────────────────────────────────────────────────────

interface Screener { name: string; label: string; desc: string }
interface ScreenerResult {
  results?: Signal[];
  generated_at?: string;
  cached?: boolean;
  shortlist_size?: number;
  universe_size?: number;
}

let screeners: Screener[] = [];
let screenerCache: Record<string, ScreenerResult> = {};
let currentScreenerName = '';

export async function initScreenerTabs(restoreActive?: string): Promise<void> {
  try {
    const d = await apiFetch<{ screeners: Screener[] }>('/api/finance/screeners');
    screeners = d.screeners || [];

    const cached = await apiFetch<{ results: Record<string, ScreenerResult> }>('/api/finance/screeners-cached').catch(() => ({ results: {} }));
    screenerCache = cached.results || {};

    const tabs = $('screener-tabs');
    tabs.innerHTML = screeners.map(s => {
      const c = screenerCache[s.name];
      const ts = c?.generated_at ? ` (${new Date(c.generated_at).toLocaleDateString()})` : '';
      const active = (restoreActive ?? currentScreenerName) === s.name ? ' active' : '';
      return `<div class="screener-tab${active}" data-name="${s.name}" data-desc="${s.desc}" onclick="selectScreener(this)">${s.label}${ts}</div>`;
    }).join('');
  } catch { /* non-critical */ }
}

function renderScreenerResults(d: ScreenerResult, name: string): void {
  const results = d.results ?? [];
  const generated = d.generated_at ? new Date(d.generated_at).toLocaleString() : 'N/A';
  const shortlistNote = d.shortlist_size !== null && d.shortlist_size !== undefined
    ? `shortlisted ${d.shortlist_size} of ${d.universe_size}`
    : `${d.universe_size ?? 0} stocks`;
  $('screener-desc').textContent = `${name}  ·  ${results.length} results  ·  ${shortlistNote}  ·  Generated: ${generated}`;

  if (results.length > 0) {
    $('screen-grid').innerHTML = results.map(s => `
      <div class="sig-card" onclick="navigateToStock('${s.symbol}')">
        <div class="sig-card-header">
          <span class="sig-card-sym">${s.symbol}</span>
          <span class="sig-card-price">${s.score || 0}</span>
        </div>
        <div class="sig-card-rec"><span class="rec-badge rec-${s.recommendation}">${s.recommendation}</span></div>
        <div class="sig-reasons">${(s.reasons ?? s.all_reasons ?? []).slice(0, 2).map(r => `• ${  r}`).join('<br>')}</div>
      </div>
    `).join('');
  } else {
    const msg = d.cached
      ? empty(`No matching stocks<br><span class="text-sm text-muted">Last run: ${generated}</span>`)
      : empty('Screener returned no results. Click Run to try again.');
    $('screen-grid').innerHTML = msg;
  }
}

export async function selectScreener(tab: HTMLElement): Promise<void> {
  document.querySelectorAll('.screener-tab').forEach(t => t.classList.remove('active'));
  tab.classList.add('active');
  const name = tab.dataset.name ?? '';
  currentScreenerName = name;
  const runBtn = document.getElementById('screener-run-btn');
  if (runBtn) runBtn.style.display = '';
  $('screen-grid').innerHTML = spinner();
  try {
    const d = await apiFetch<ScreenerResult>(`/api/finance/screen/${encodeURIComponent(name)}`);
    renderScreenerResults(d, tab.dataset.desc ?? name);
  } catch (e) {
    $('screen-grid').innerHTML = empty(`Error: ${(e as Error).message}`);
  }
}

export async function runCurrentScreener(): Promise<void> {
  if (currentScreenerName) await runScreenerNow(currentScreenerName);
}

export async function runScreenerNow(name: string): Promise<void> {
  const desc = $('screener-desc');
  $('screen-grid').innerHTML = loadingBlock('Running screener…');
  desc.textContent = `Running ${name}…`;

  // Capture pre-run timestamp so we can detect when a new result arrives
  const pre = await apiFetch<ScreenerResult>(`/api/finance/screen/${encodeURIComponent(name)}`).catch(() => ({} as ScreenerResult));
  const preTs = pre.generated_at ?? null;

  try {
    await apiFetch(`/api/finance/screen/${encodeURIComponent(name)}/run`, { method: 'POST' });
    let updated = false;
    for (let i = 0; i < 30; i++) {
      await new Promise(r => setTimeout(r, 1000));
      desc.textContent = `Running ${name}… (${i + 1}s)`;
      const d = await apiFetch<ScreenerResult>(`/api/finance/screen/${encodeURIComponent(name)}`);
      if (d.generated_at && d.generated_at !== preTs) {
        updated = true;
        await initScreenerTabs(name);
        const tab = document.querySelector<HTMLElement>(`.screener-tab[data-name="${name}"]`);
        if (tab) await selectScreener(tab);
        else renderScreenerResults(d, name);
        break;
      }
    }
    if (!updated) {
      await initScreenerTabs(name);
      const tab = document.querySelector<HTMLElement>(`.screener-tab[data-name="${name}"]`);
      const d = await apiFetch<ScreenerResult>(`/api/finance/screen/${encodeURIComponent(name)}`);
      if (tab) await selectScreener(tab);
      else renderScreenerResults(d, name);
      desc.textContent += ' (timed out — showing last result)';
    }
  } catch (e) {
    $('screen-grid').innerHTML = empty(`Error: ${(e as Error).message}`);
    desc.textContent = '';
  }
}

// suppress unused warning
export { navigateToStock };
