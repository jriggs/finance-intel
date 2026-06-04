// ── Settings · config panels (volume analysis, screener, yfinance cache) ──────

import { apiFetch } from "../../core/api.js";
import { $ } from "../../core/utils.js";
import { loadVolumeResults } from "../volume.js";

// ── Volume analysis config ────────────────────────────────────────────────────

export async function loadVolumeAnalysisConfig(): Promise<void> {
  try {
    const config = await apiFetch<{
      skip_hours?: number;
      stocks_per_minute?: number;
      max_retries?: number;
      enabled?: boolean;
    }>("/api/finance/volume-analysis/config");
    ($("volume-skip") as HTMLInputElement).value = String(
      config.skip_hours ?? 24,
    );
    ($("volume-stocks-per-minute") as HTMLInputElement).value = String(
      config.stocks_per_minute ?? 20,
    );
    ($("volume-retries") as HTMLInputElement).value = String(
      config.max_retries ?? 3,
    );
    ($("volume-enabled") as HTMLInputElement).checked =
      config.enabled !== false;
  } catch (e) {
    console.warn("Volume config load failed:", (e as Error).message);
  }
}

export async function saveVolumeConfig(): Promise<void> {
  const status = $("volume-config-status");
  try {
    await apiFetch("/api/finance/volume-analysis/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        skip_hours:
          parseInt(($("volume-skip") as HTMLInputElement).value, 10) || 24,
        stocks_per_minute:
          parseInt(($("volume-stocks-per-minute") as HTMLInputElement).value, 10) ||
          20,
        max_retries:
          parseInt(($("volume-retries") as HTMLInputElement).value, 10) || 3,
        enabled: ($("volume-enabled") as HTMLInputElement).checked,
      }),
    });
    status.textContent = "Saved ✓";
    setTimeout(() => {
      status.textContent = "";
    }, 2000);
  } catch (e) {
    status.textContent = `Error: ${(e as Error).message}`;
  }
}

export async function runVolumeAnalysisNow(): Promise<void> {
  const btn = $("volume-run-btn") as HTMLButtonElement;
  const stopBtn = $("volume-stop-btn") as HTMLButtonElement;
  const status = $("volume-config-status");
  btn.disabled = true;
  btn.textContent = "Starting…";
  try {
    await apiFetch("/api/finance/volume-analysis/run", { method: "POST" });
    status.textContent = "Started ✓";
    stopBtn.style.display = "inline-block";
    setTimeout(() => {
      status.textContent = "";
    }, 2000);
    setTimeout(() => loadVolumeResults(), 1000);
  } catch (e) {
    const msg = (e as Error).message;
    if (msg.includes("409")) {
      status.textContent = "Already running";
      stopBtn.style.display = "inline-block";
    } else {
      status.textContent = `Error: ${msg}`;
      stopBtn.style.display = "none";
    }
    setTimeout(() => {
      status.textContent = "";
    }, 3000);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Now";
  }
}

export async function stopVolumeAnalysis(): Promise<void> {
  const btn = $("volume-run-btn") as HTMLButtonElement;
  const stopBtn = $("volume-stop-btn") as HTMLButtonElement;
  const status = $("volume-config-status");
  try {
    await apiFetch("/api/finance/volume-analysis/stop", { method: "POST" });
    status.textContent = "Stopping…";
  } catch (e) {
    status.textContent = (e as Error).message.includes("409")
      ? "Not running"
      : `Error: ${(e as Error).message}`;
  } finally {
    stopBtn.style.display = "none";
    btn.disabled = false;
    btn.textContent = "Run Now";
    setTimeout(() => {
      status.textContent = "";
    }, 3000);
  }
}

// ── Screener config ───────────────────────────────────────────────────────────

export async function loadScreenerConfig(): Promise<void> {
  try {
    const config = await apiFetch<{
      enabled?: boolean;
      interval_hours?: number;
      delay_secs?: number;
    }>("/api/finance/screeners/config");
    ($("screener-enabled") as HTMLInputElement).checked =
      config.enabled !== false;
    ($("screener-interval") as HTMLInputElement).value = String(
      config.interval_hours ?? 12,
    );
    ($("screener-delay") as HTMLInputElement).value = String(
      config.delay_secs ?? 5,
    );
  } catch (e) {
    console.warn("Screener config load failed:", (e as Error).message);
  }
}

export async function saveScreenerConfig(): Promise<void> {
  const status = $("screener-config-status");
  try {
    await apiFetch("/api/finance/screeners/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: ($("screener-enabled") as HTMLInputElement).checked,
        interval_hours:
          parseInt(($("screener-interval") as HTMLInputElement).value, 10) || 12,
        delay_secs:
          parseInt(($("screener-delay") as HTMLInputElement).value, 10) || 5,
      }),
    });
    status.textContent = "Saved ✓";
    setTimeout(() => {
      status.textContent = "";
    }, 2000);
  } catch (e) {
    status.textContent = `Error: ${(e as Error).message}`;
  }
}

// ── Cache config ──────────────────────────────────────────────────────────────

export async function loadCacheConfig(): Promise<void> {
  try {
    const config = await apiFetch<{ enabled?: boolean; ttl_minutes?: number }>(
      "/api/finance/cache/config",
    );
    ($("cache-enabled") as HTMLInputElement).checked = config.enabled !== false;
    ($("cache-ttl") as HTMLInputElement).value = String(
      config.ttl_minutes ?? 30,
    );
  } catch (e) {
    console.warn("Cache config load failed:", (e as Error).message);
  }
}

export async function saveCacheConfig(): Promise<void> {
  const status = $("cache-config-status");
  try {
    await apiFetch("/api/finance/cache/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: ($("cache-enabled") as HTMLInputElement).checked,
        ttl_minutes: parseInt(($("cache-ttl") as HTMLInputElement).value, 10) || 30,
      }),
    });
    status.textContent = "Saved ✓";
    setTimeout(() => {
      status.textContent = "";
    }, 2000);
  } catch (e) {
    status.textContent = `Error: ${(e as Error).message}`;
  }
}
