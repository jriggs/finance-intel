// ── Global mutable state ──────────────────────────────────────────────────────

import type { StockCacheEntry, VolumeResult } from './types.js';

export let currentSymbol = '';
export let currentPeriod = '6mo';

export function setCurrentSymbol(sym: string): void { currentSymbol = sym; }
export function setCurrentPeriod(p: string): void { currentPeriod = p; }

/** In-flight AbortController for the insight stream. */
export let insightAbort: AbortController | null = null;
export function setInsightAbort(ctrl: AbortController | null): void { insightAbort = ctrl; }

/** Per-symbol cache: info + signal + news + insight text. */
export const stockCache = new Map<string, StockCacheEntry>();

/** Cards whose collapsed state should persist across renders. */
export const collapsedCards = new Set<string>();

/** Volume analysis results (mutable, sorted in place). */
export let volumeData: VolumeResult[] = [];
export let volumeSortBy = 'overall_score';
export let volumeSortDesc = true;

export function setVolumeData(d: VolumeResult[]): void { volumeData = d; }
export function setVolumeSort(col: string, desc: boolean): void {
  volumeSortBy = col;
  volumeSortDesc = desc;
}
