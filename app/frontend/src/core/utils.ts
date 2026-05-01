// ── DOM & formatting utilities ────────────────────────────────────────────────

export const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;

export const fmt = (v: number | null | undefined, decimals = 2): string =>
  v === null || v === undefined ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });

export const fmtDollar = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : `$${  fmt(v)}`;

export const fmtPct = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : `${(v >= 0 ? '+' : '-') + fmt(Math.abs(v))}%`;

export const fmtBig = (v: number | null | undefined): string => {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  if (Math.abs(n) >= 1e12) return `$${  fmt(n / 1e12, 2)  }T`;
  if (Math.abs(n) >= 1e9)  return `$${  fmt(n / 1e9,  2)  }B`;
  if (Math.abs(n) >= 1e6)  return `$${  fmt(n / 1e6,  2)  }M`;
  return `$${  fmt(n, 0)}`;
};

export const colorPct = (v: number | null | undefined): string =>
  v === null || v === undefined ? '' : v >= 0 ? 'text-green' : 'text-red';

export const spinner = (): string => '<div class="spinner"></div>';

export const empty = (msg: string): string => `<div class="empty">${msg}</div>`;

export const loadingBlock = (msg: string): string =>
  `<div class="loading-block"><div class="spinner"></div><div class="text-muted loading-block-msg">${msg}</div></div>`;

export const renderError = (msg: string): string =>
  `<div class="empty text-red">⚠ ${msg}</div>`;

export const escapeHtml = (s: string): string =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
   .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
