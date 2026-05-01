// ── Watchlist sidebar ─────────────────────────────────────────────────────────

import { apiFetch } from "../core/api.js";
import { $, fmtPct } from "../core/utils.js";
import { navigateToStock } from "../core/router.js";
import type { Quote } from "../core/types.js";

export async function loadWatchlistSidebar(): Promise<void> {
  try {
    const [wlRes, quotesRes] = await Promise.allSettled([
      apiFetch<{ symbols: string[] }>("/api/finance/watchlist"),
      apiFetch<{ quotes: Quote[] }>("/api/finance/quotes"),
    ]);
    if (wlRes.status !== "fulfilled") return;

    const wl = wlRes.value;
    const quotes =
      quotesRes.status === "fulfilled" ? quotesRes.value : { quotes: [] };
    const qMap: Record<string, Quote> = {};
    (quotes.quotes || []).forEach((q) => {
      qMap[q.symbol] = q;
    });

    const el = $("watchlist-sidebar");
    el.innerHTML = wl.symbols
      .map((sym) => {
        const q = qMap[sym] || ({} as Quote);
        const chg = q.change_pct;
        return `<div class="wl-item" onclick="navigateToStock('${sym}')">
        <span class="sym">${sym}</span>
        <span class="chg ${chg === null || chg === undefined ? "" : chg >= 0 ? "pos" : "neg"}">${fmtPct(chg)}</span>
        <span onclick="event.stopPropagation();removeFromWatchlist('${sym}')" class="wl-remove-btn" title="Remove">×</span>
      </div>`;
      })
      .join("");
  } catch {
    /* sidebar is non-critical */
  }
}

export async function addToWatchlistFromSearch(): Promise<void> {
  const sym = ($("search-input") as HTMLInputElement).value
    .trim()
    .toUpperCase();
  if (!sym) return;
  await apiFetch("/api/finance/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol: sym }),
  });
  void loadWatchlistSidebar();
}

export async function addToWatchlist(sym: string): Promise<void> {
  if (!sym) return;
  await apiFetch("/api/finance/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol: sym }),
  });
  void loadWatchlistSidebar();
}

export async function removeFromWatchlist(sym: string): Promise<void> {
  await apiFetch(`/api/finance/watchlist/${sym}`, { method: "DELETE" });
  void loadWatchlistSidebar();
}

// Suppress unused warning — addToWatchlist is available for external use
export { navigateToStock };
