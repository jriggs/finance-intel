// ── Entry point — init, routing, global bindings ──────────────────────────────

import { apiFetch } from "./core/api.js";
import { $ } from "./core/utils.js";
import { parseRoute, navigate, navigateToStock } from "./core/router.js";
import { setCurrentSymbol } from "./core/state.js";

import {
  loadWatchlistSidebar,
  addToWatchlistFromSearch,
  removeFromWatchlist,
} from "./views/watchlist.js";
import { loadStockDetail } from "./views/stock.js";
import {
  loadChart,
  toggleChartOption,
  setChartType,
  initChartToolbar,
  type ChartSettings,
} from "./views/chart.js";
import {
  loadSignals,
  loadRecommendations,
  initScreenerTabs,
  selectScreener,
  runCurrentScreener,
  runRecommendationsNow,
} from "./views/signals.js";
import { loadVolumeResults, sortVolumeBy, rescoreVolume } from "./views/volume.js";
import { loadDashboard } from "./views/dashboard.js";
import { loadPicks, submitPick, closePick } from "./views/picks.js";
import {
  loadBroker,
  loadOrders,
  placeOrder,
  cancelOrder,
  toggleLimitPrice,
} from "./views/broker.js";
import { loadFeed, switchFeed, triggerCrawl } from "./views/feed.js";
import {
  sendChat,
  runExtendedInsight,
  toggleCard,
  quickPrompt,
} from "./views/chat.js";
import {
  loadSettings,
  stopJobsAutoRefresh,
  stopJobById,
  saveCacheConfig,
} from "./views/settings.js";
import { loadActivities, loadActivityLogs } from "./views/activities.js";

// ── Model selector ────────────────────────────────────────────────────────────

let modelSwitchPoll: ReturnType<typeof setInterval> | null = null;

async function loadModels(): Promise<void> {
  try {
    const d = await apiFetch<{
      models?: Array<{
        path: string;
        name: string;
        size_gb: number;
        active?: boolean;
      }>;
      switching?: boolean;
    }>("/api/finance/llm/models");
    const sel = $("model-select") as HTMLSelectElement;
    const ind = $("model-switching-indicator");

    if (!d.models || d.models.length === 0) {
      sel.innerHTML = '<option value="">No models found</option>';
      return;
    }

    sel.innerHTML = d.models
      .map(
        (m) =>
          `<option value="${m.path}" ${m.active ? "selected" : ""}>${m.name} (${m.size_gb}GB)</option>`,
      )
      .join("");

    if (d.switching) {
      sel.disabled = true;
      ind.style.display = "inline";
      modelSwitchPoll ??= setInterval(loadModels, 1500);
    } else {
      sel.disabled = false;
      ind.style.display = "none";
      if (modelSwitchPoll) {
        clearInterval(modelSwitchPoll);
        modelSwitchPoll = null;
      }
    }
  } catch {
    /* non-critical */
  }
}

async function onModelSelect(): Promise<void> {
  const sel = $("model-select") as HTMLSelectElement;
  const ind = $("model-switching-indicator");
  const path = sel.value;
  if (!path) return;
  try {
    sel.disabled = true;
    ind.style.display = "inline";
    await apiFetch("/api/finance/llm/models/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    modelSwitchPoll ??= setInterval(loadModels, 1500);
  } catch (e) {
    ind.style.display = "none";
    sel.disabled = false;
    console.warn("Model switch failed:", (e as Error).message);
  }
}

// ── Market clock ──────────────────────────────────────────────────────────────

async function refreshClock(): Promise<void> {
  try {
    const d = await apiFetch<{
      is_open: boolean;
      spy?: { price: number | null; change_pct: number | null };
    }>("/api/finance/broker/clock");
    const badge = $("clock-badge");
    badge.textContent = d.is_open ? "Market Open" : "Market Closed";
    badge.className = d.is_open ? "open" : "closed";

    const ticker = document.getElementById("spy-ticker");
    if (ticker) {
      if (d.is_open && d.spy?.price != null) {
        const pct = d.spy.change_pct ?? 0;
        const sign = pct >= 0 ? "▲" : "▼";
        ticker.textContent = `SPY ${sign} $${d.spy.price.toFixed(2)}  ${pct >= 0 ? "+" : ""}${Math.abs(pct).toFixed(2)}%`;
        ticker.className = pct >= 0 ? "up" : "down";
        ticker.style.display = "inline-block";
      } else {
        ticker.style.display = "none";
      }
    }
  } catch {
    /* non-critical */
  }
}

// ── Symbol autocomplete ───────────────────────────────────────────────────────

async function loadSymbolList(retryMs = 0): Promise<void> {
  if (retryMs) await new Promise((r) => setTimeout(r, retryMs));
  try {
    const d = await apiFetch<{
      symbols: Array<{ symbol: string; name?: string }>;
      cached?: boolean;
    }>("/api/finance/symbols");
    const datalist = $("symbol-list");
    datalist.innerHTML = (d.symbols || [])
      .map((s) => {
        const label = s.name ? `${s.symbol} — ${s.name}` : s.symbol;
        return `<option value="${s.symbol}" label="${label}"></option>`;
      })
      .join("");
    if (!d.cached) setTimeout(() => loadSymbolList(), 5000);
  } catch (e) {
    console.warn("Symbol list load failed:", (e as Error).message);
  }
}

// ── Router ────────────────────────────────────────────────────────────────────

function router(): void {
  const { view, params } = parseRoute();

  document
    .querySelectorAll(".nav-item")
    .forEach((n) => n.classList.remove("active"));
  document.querySelector(`[data-view="${view}"]`)?.classList.add("active");

  document
    .querySelectorAll(".view")
    .forEach((v) => v.classList.remove("active"));
  const viewEl = document.getElementById(`view-${  view}`);
  if (!viewEl) return;
  viewEl.classList.add("active");

  // Stop jobs auto-refresh if leaving settings
  if (view !== "settings") stopJobsAutoRefresh();

  if (view === "dashboard") void loadDashboard();
  if (view === "stock") {
    const sym = (params.symbol || "").toUpperCase();
    if (sym) {
      setCurrentSymbol(sym);
      void loadStockDetail(sym);
      ($("search-input") as HTMLInputElement).value = sym;
    }
  }
  if (view === "signals") {
    void loadSignals();
    void loadRecommendations();
    void initScreenerTabs();
    void loadVolumeResults();
  }
  if (view === "picks") void loadPicks();
  if (view === "broker") void loadBroker();
  if (view === "feed") void loadFeed();
  if (view === "settings") {
    void loadSettings();
    void loadActivities();
  }
}

// ── Event listeners ───────────────────────────────────────────────────────────

function wireEvents(): void {
  document.querySelectorAll<HTMLElement>(".nav-item").forEach((el) => {
    el.addEventListener("click", () => navigate(el.dataset.view ?? ""));
  });

  window.addEventListener("popstate", router);

  $("search-input").addEventListener("keydown", (e: Event) => {
    if ((e as KeyboardEvent).key === "Enter") {
      const sym = ($("search-input") as HTMLInputElement).value
        .trim()
        .toUpperCase();
      if (sym) navigateToStock(sym);
    }
  });
  $("search-input").addEventListener("change", () => {
    const sym = ($("search-input") as HTMLInputElement).value
      .trim()
      .toUpperCase();
    if (sym) navigateToStock(sym);
  });

  $("model-select").addEventListener("change", () => void onModelSelect());

  $("chat-input").addEventListener("keydown", (e: Event) => {
    if (
      (e as KeyboardEvent).key === "Enter" &&
      !(e as KeyboardEvent).shiftKey
    ) {
      e.preventDefault();
      void sendChat();
    }
  });

  $("act-log-level").addEventListener("change", () => void loadActivityLogs());

  $("wl-add-btn").addEventListener("click", () => void addToWatchlistFromSearch());
  $("chat-send-btn").addEventListener("click", () => void sendChat());
  $("ai-insight-btn").addEventListener("click", () => void runExtendedInsight());
  $("submit-pick-btn").addEventListener("click", () => void submitPick());
  $("signals-refresh-btn").addEventListener("click", () => void loadSignals(true));
  $("rec-run-btn").addEventListener("click", () => void runRecommendationsNow());
  $("screener-run-btn").addEventListener("click", () => void runCurrentScreener());
  $("volume-refresh-btn").addEventListener("click", () => void loadVolumeResults());
  $("order-type").addEventListener("change", () => toggleLimitPrice());
  $("place-order-btn").addEventListener("click", () => void placeOrder());
  $("orders-refresh-btn").addEventListener("click", () => void loadOrders());
  $("crawl-run-btn").addEventListener("click", () => void triggerCrawl());
  $("settings-reload-btn").addEventListener("click", () => location.reload());
  $("save-cache-config-btn").addEventListener("click", () => void saveCacheConfig());

  document.querySelectorAll<HTMLElement>("[data-period]").forEach((b) => {
    b.addEventListener("click", () => void loadChart(b.dataset.period!));
  });
  document.querySelectorAll<HTMLElement>("[data-chart-type]").forEach((b) => {
    b.addEventListener("click", () =>
      setChartType(b.dataset.chartType as ChartSettings["chartType"]),
    );
  });
  document.querySelectorAll<HTMLElement>("[data-chart-opt]").forEach((b) => {
    b.addEventListener("click", () =>
      toggleChartOption(
        b.dataset.chartOpt as keyof Omit<ChartSettings, "chartType">,
      ),
    );
  });
  document.querySelectorAll<HTMLElement>("[data-toggle-card]").forEach((b) => {
    b.addEventListener("click", () => toggleCard(b.dataset.toggleCard!));
  });
  document.querySelectorAll<HTMLElement>("[data-prompt]").forEach((b) => {
    b.addEventListener("click", () => void quickPrompt(b.dataset.prompt!));
  });
  document.querySelectorAll<HTMLElement>("[data-sort-vol]").forEach((th) => {
    th.addEventListener("click", () => sortVolumeBy(th.dataset.sortVol!));
  });
}

// ── Global bindings (for dynamically-rendered inline onclick= handlers) ────────

function exposeGlobals(): void {
  Object.assign(window, {
    navigateToStock,
    removeFromWatchlist,
    selectScreener,
    closePick,
    cancelOrder,
    rescoreVolume,
    switchFeed,
    stopJobById,
  });
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

async function init(): Promise<void> {
  window.addEventListener("unhandledrejection", (e) => {
    console.error("Unhandled rejection:", e.reason);
  });
  exposeGlobals();
  wireEvents();
  initChartToolbar();
  await Promise.all([loadModels(), refreshClock(), loadWatchlistSidebar()]);
  void loadSymbolList();
  router();
  setInterval(refreshClock, 60_000);
  setInterval(loadWatchlistSidebar, 120_000);
}

void init();
