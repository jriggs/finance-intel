// ── Settings & activities view ────────────────────────────────────────────────

import { apiFetch, API } from "../core/api.js";
import { $ } from "../core/utils.js";
import { loadVolumeResults } from "./volume.js";
import { loadCacheConfig, loadScreenerConfig, loadVolumeAnalysisConfig } from "./settings/config_panels.js";

// ── Generic job config ────────────────────────────────────────────────────────

interface JobConfig {
  enabled: boolean;
  interval_hours?: number;
  interval_minutes?: number;
  interval_offset_minutes?: number;
  skip_hours?: number;
  max_per_minute?: number;
  stocks_per_minute?: number;
}

export async function loadJobConfig(jobId: string): Promise<void> {
  try {
    const cfg = await apiFetch<JobConfig>(`/api/finance/jobs/${jobId}/config`);
    const enabledEl = document.getElementById(
      `job-${jobId}-enabled`,
    ) as HTMLInputElement | null;
    const intervalEl = document.getElementById(
      `job-${jobId}-interval`,
    ) as HTMLInputElement | null;
    if (enabledEl) enabledEl.checked = cfg.enabled !== false;
    if (intervalEl)
      intervalEl.value = String(
        cfg.interval_hours ?? cfg.interval_minutes ?? "",
      );
  } catch (e) {
    console.warn(`Job config load failed (${jobId}):`, (e as Error).message);
  }
}

export async function saveJobConfig(jobId: string): Promise<void> {
  const statusEl = document.getElementById(`job-${jobId}-status`);
  const enabledEl = document.getElementById(
    `job-${jobId}-enabled`,
  ) as HTMLInputElement | null;
  const intervalEl = document.getElementById(
    `job-${jobId}-interval`,
  ) as HTMLInputElement | null;
  try {
    const body: Record<string, unknown> = {
      enabled: enabledEl ? enabledEl.checked : true,
    };
    if (intervalEl) body.interval = parseFloat(intervalEl.value);
    await apiFetch(`/api/finance/jobs/${jobId}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (statusEl) {
      statusEl.textContent = "Saved ✓";
      setTimeout(() => {
        if (statusEl) statusEl.textContent = "";
      }, 2000);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `Error: ${(e as Error).message}`;
  }
}

export async function runJobNow(jobId: string): Promise<void> {
  const statusEl = document.getElementById(`job-${jobId}-status`);
  const runBtn = document.getElementById(
    `job-${jobId}-run-btn`,
  ) as HTMLButtonElement | null;
  if (runBtn) {
    runBtn.disabled = true;
    runBtn.textContent = "Starting…";
  }
  try {
    await apiFetch(`/api/finance/jobs/${jobId}/run`, { method: "POST" });
    if (statusEl) {
      statusEl.textContent = "Started ✓";
      setTimeout(() => {
        if (statusEl) statusEl.textContent = "";
      }, 2000);
    }
  } catch (e) {
    const msg = (e as Error).message;
    if (statusEl) {
      statusEl.textContent = msg.includes("409")
        ? "Already running"
        : `Error: ${msg}`;
      setTimeout(() => {
        if (statusEl) statusEl.textContent = "";
      }, 3000);
    }
  } finally {
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.textContent = "Run Now";
    }
  }
}

// ── Real-time jobs list via SSE ───────────────────────────────────────────────

let jobsSSE: EventSource | null = null;
const openJobDropdowns: Record<string, boolean> = {};
let _lastJobs: Array<{
  id: string;
  name: string;
  next_run?: string;
  running?: boolean;
  last_run?: string;
  enabled?: boolean;
}> = [];

export function stopJobsAutoRefresh(): void {
  if (jobsSSE) {
    jobsSSE.close();
    jobsSSE = null;
  }
  if (_yfStatusTimer) {
    clearInterval(_yfStatusTimer);
    _yfStatusTimer = null;
  }
}

function _fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return "—";
  }
}

function _updateJobBadge(jobId: string, running: boolean): void {
  const badge = document.getElementById(`job-badge-${jobId}`);
  if (!badge) return;
  if (running) {
    badge.className = "job-badge job-badge-running";
    badge.textContent = "RUNNING";
  } else {
    badge.className = "job-badge job-badge-idle";
    badge.textContent = "IDLE";
  }
}

function _updateJobMeta(
  jobId: string,
  nextRun: string | null | undefined,
  lastRun: string | null | undefined,
  running: boolean,
): void {
  const nextEl = document.getElementById(`job-next-${jobId}`);
  const lastEl = document.getElementById(`job-last-${jobId}`);
  const stopBtn = document.getElementById(
    `job-stop-btn-${jobId}`,
  ) as HTMLButtonElement | null;
  if (nextEl) nextEl.textContent = nextRun ? _fmtTime(nextRun) : "—";
  if (lastEl) lastEl.textContent = lastRun ? _fmtTime(lastRun) : "—";
  if (stopBtn) stopBtn.style.display = running ? "inline-block" : "none";
}

function _handleSSEMessage(jobs: typeof _lastJobs): void {
  const wasEmpty = _lastJobs.length === 0;
  _lastJobs = jobs;

  if (wasEmpty) {
    // First message — render the full list
    _renderJobsListFromData(jobs);
    return;
  }

  // Incremental update — just patch badges and meta without re-rendering dropdowns
  jobs.forEach((j) => {
    _updateJobBadge(j.id, Boolean(j.running));
    _updateJobMeta(j.id, j.next_run, j.last_run, Boolean(j.running));
    const enabledEl = document.getElementById(`job-enabled-${j.id}`);
    if (enabledEl) {
      const enabled = j.enabled !== false;
      enabledEl.className = `job-badge ${enabled ? "job-badge-enabled" : "job-badge-disabled"}`;
      enabledEl.textContent = enabled ? "ON" : "OFF";
    }
  });
}

export function renderJobsList(): void {
  if (_lastJobs.length > 0) {
    _renderJobsListFromData(_lastJobs);
  } else {
    const el = document.getElementById("jobs-list");
    if (!el) return;
    el.innerHTML = '<div class="text-muted">Connecting…</div>';
  }
}

function _renderJobsListFromData(jobs: typeof _lastJobs): void {
  const el = document.getElementById("jobs-list");
  if (!el) return;

  if (jobs.length === 0) {
    el.innerHTML = '<div class="text-muted">No scheduled jobs</div>';
    return;
  }

  el.innerHTML = jobs.map((j) => _renderJobRow(j)).join("");

  jobs.forEach((j) => {
    const row = document.getElementById(`job-row-${j.id}`);
    if (row) row.onclick = () => _toggleJobDropdown(j.id);
    if (openJobDropdowns[j.id]) void _loadJobConfigDropdown(j.id);
  });
}

function _renderJobRow(j: {
  id: string;
  name: string;
  next_run?: string;
  running?: boolean;
  last_run?: string;
  enabled?: boolean;
}): string {
  const running = Boolean(j.running);
  const enabled = j.enabled !== false;
  const expanded = Boolean(openJobDropdowns[j.id]);
  return `
    <div id="job-row-${j.id}" class="job-row">
      <div class="job-row-header">
        <span class="text-blue job-row-name">${j.name}</span>
        <span id="job-enabled-${j.id}" class="job-badge ${enabled ? "job-badge-enabled" : "job-badge-disabled"}">${enabled ? "ON" : "OFF"}</span>
        <span id="job-badge-${j.id}" class="job-badge ${running ? "job-badge-running" : "job-badge-idle"}">${running ? "RUNNING" : "IDLE"}</span>
        <span class="job-row-meta">
          next <span id="job-next-${j.id}">${_fmtTime(j.next_run)}</span>
        </span>
        <span class="job-row-meta">
          last <span id="job-last-${j.id}">${_fmtTime(j.last_run)}</span>
        </span>
        <button id="job-stop-btn-${j.id}" class="btn btn-sm btn-stop" style="display:${running ? "inline-block" : "none"}"
          onclick="event.stopPropagation(); stopJobById('${j.id}')">Stop</button>
        <span class="job-row-chevron">${expanded ? "▾" : "▸"}</span>
      </div>
      <div id="job-dropdown-${j.id}" style="display:${expanded ? "block" : "none"};margin:8px 0 8px 0"></div>
    </div>
  `;
}

function _toggleJobDropdown(jobId: string): void {
  openJobDropdowns[jobId] = !openJobDropdowns[jobId];
  _renderJobsListFromData(_lastJobs);
}

async function _loadJobConfigDropdown(jobId: string): Promise<void> {
  const el = document.getElementById(`job-dropdown-${jobId}`);
  if (!el) return;
  el.innerHTML = '<div class="spinner"></div>';
  try {
    const cfg = await apiFetch<JobConfig>(`/api/finance/jobs/${jobId}/config`);

    const isMinutes =
      cfg.interval_minutes !== undefined && cfg.interval_hours === undefined;
    const intervalVal = isMinutes
      ? (cfg.interval_minutes ?? 1)
      : (cfg.interval_hours ?? 1);
    const intervalStep = isMinutes ? "1" : "0.25";
    const intervalMin = isMinutes ? "1" : "0.25";
    const intervalUnit = isMinutes ? "min" : "hr";
    const offsetVal = cfg.interval_offset_minutes ?? 0;

    const rateFieldName =
      cfg.max_per_minute !== undefined
        ? "max_per_minute"
        : cfg.stocks_per_minute !== undefined
          ? "stocks_per_minute"
          : null;
    const rateValue = cfg.max_per_minute ?? cfg.stocks_per_minute;
    const rateMax = rateFieldName === "stocks_per_minute" ? 500 : 60;
    const skipDefault =
      cfg.skip_hours ?? (rateFieldName === "stocks_per_minute" ? 24 : 12);

    const extraFields =
      rateFieldName !== null
        ? `
      <div class="job-cfg-row">
        <label>Max/min:</label>
        <input id="job-${jobId}-max-per-minute" data-rate-field="${rateFieldName}" type="number"
          value="${rateValue}" min="1" max="${rateMax}" class="job-cfg-input" />
        <span class="muted-xs">per minute</span>
      </div>
      <div class="job-cfg-row">
        <label>Skip if fresh within:</label>
        <input id="job-${jobId}-skip-hours" type="number" value="${skipDefault}" min="0" max="168" class="job-cfg-input" />
        <span class="muted-xs">hr</span>
      </div>`
        : "";

    el.innerHTML = `
      <div class="job-cfg-panel">
        <label class="job-cfg-check">
          <input type="checkbox" id="job-${jobId}-enabled" ${cfg.enabled ? "checked" : ""}/> Enable
        </label>
        <div class="job-cfg-row">
          <label>Run every:</label>
          <input id="job-${jobId}-interval" type="number" value="${intervalVal}"
            min="${intervalMin}" step="${intervalStep}" class="job-cfg-input" />
          <span class="muted-xs">${intervalUnit}</span>
        </div>
        ${
          !isMinutes
            ? `
        <div class="job-cfg-row">
          <label>Offset:</label>
          <input id="job-${jobId}-offset" type="number" value="${offsetVal}" min="0" max="55" step="5" class="job-cfg-input" />
          <span class="muted-xs">min past interval start</span>
        </div>`
            : ""
        }
        ${extraFields}
        <div class="job-cfg-row">
          <button class="btn btn-sm" id="job-${jobId}-save-btn" disabled>Save</button>
          <button class="btn btn-sm" id="job-${jobId}-run-btn">Run Now</button>
          <span id="job-${jobId}-status" class="muted-xs"></span>
        </div>
      </div>
    `;

    el.onclick = (e: Event) => e.stopPropagation();

    const enabledEl = document.getElementById(
      `job-${jobId}-enabled`,
    ) as HTMLInputElement | null;
    const intervalEl = document.getElementById(
      `job-${jobId}-interval`,
    ) as HTMLInputElement | null;
    const offsetEl = document.getElementById(
      `job-${jobId}-offset`,
    ) as HTMLInputElement | null;
    const saveBtn = document.getElementById(
      `job-${jobId}-save-btn`,
    ) as HTMLButtonElement | null;
    const runBtn = document.getElementById(
      `job-${jobId}-run-btn`,
    ) as HTMLButtonElement | null;

    const markDirty = () => {
      if (saveBtn) saveBtn.disabled = false;
    };

    [enabledEl, intervalEl, offsetEl].forEach((inp) => {
      if (!inp) return;
      inp.oninput = markDirty;
      inp.onchange = markDirty;
      inp.onclick = (e: Event) => e.stopPropagation();
    });

    const maxPerMinEl = document.getElementById(
      `job-${jobId}-max-per-minute`,
    ) as HTMLInputElement | null;
    const skipHoursEl = document.getElementById(
      `job-${jobId}-skip-hours`,
    ) as HTMLInputElement | null;
    [maxPerMinEl, skipHoursEl].forEach((inp) => {
      if (!inp) return;
      inp.oninput = markDirty;
      inp.onclick = (e: Event) => e.stopPropagation();
    });

    if (saveBtn) {
      saveBtn.onclick = (e: Event) => {
        e.stopPropagation();
        void _saveJobConfigDropdown(jobId);
      };
    }
    if (runBtn) {
      runBtn.onclick = (e: Event) => {
        e.stopPropagation();
        void _runJobNowDropdown(jobId);
      };
    }
  } catch (e) {
    el.innerHTML = `<div class="text-red job-cfg-panel">Error: ${(e as Error).message}</div>`;
  }
}

async function _saveJobConfigDropdown(jobId: string): Promise<void> {
  const statusEl = document.getElementById(`job-${jobId}-status`);
  const enabledEl = document.getElementById(
    `job-${jobId}-enabled`,
  ) as HTMLInputElement | null;
  const intervalEl = document.getElementById(
    `job-${jobId}-interval`,
  ) as HTMLInputElement | null;
  const offsetEl = document.getElementById(
    `job-${jobId}-offset`,
  ) as HTMLInputElement | null;
  const saveBtn = document.getElementById(
    `job-${jobId}-save-btn`,
  ) as HTMLButtonElement | null;
  const maxPerMinEl = document.getElementById(
    `job-${jobId}-max-per-minute`,
  ) as HTMLInputElement | null;
  const skipHoursEl = document.getElementById(
    `job-${jobId}-skip-hours`,
  ) as HTMLInputElement | null;

  if (saveBtn) saveBtn.disabled = true;
  try {
    const body: Record<string, unknown> = {
      enabled: enabledEl ? enabledEl.checked : true,
    };
    if (intervalEl) body.interval = parseFloat(intervalEl.value);
    if (offsetEl) body.interval_offset_minutes = parseInt(offsetEl.value, 10) || 0;
    if (maxPerMinEl) {
      const rateField = maxPerMinEl.dataset.rateField ?? "max_per_minute";
      body[rateField] = parseInt(maxPerMinEl.value, 10);
    }
    if (skipHoursEl) body.skip_hours = parseInt(skipHoursEl.value, 10);

    await apiFetch(`/api/finance/jobs/${jobId}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (statusEl) {
      statusEl.textContent = "Saved ✓";
      setTimeout(() => {
        if (statusEl) statusEl.textContent = "";
      }, 2000);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `Error: ${(e as Error).message}`;
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function _runJobNowDropdown(jobId: string): Promise<void> {
  const statusEl = document.getElementById(`job-${jobId}-status`);
  const runBtn = document.getElementById(
    `job-${jobId}-run-btn`,
  ) as HTMLButtonElement | null;
  if (runBtn) {
    runBtn.disabled = true;
    runBtn.textContent = "Starting…";
  }
  try {
    await apiFetch(`/api/finance/jobs/${jobId}/run`, { method: "POST" });
    if (statusEl) {
      statusEl.textContent = "Started ✓";
      setTimeout(() => {
        if (statusEl) statusEl.textContent = "";
      }, 2000);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `Error: ${(e as Error).message}`;
  } finally {
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.textContent = "Run Now";
    }
  }
}

export async function stopJobById(jobId: string): Promise<void> {
  const btn = document.getElementById(
    `job-stop-btn-${jobId}`,
  ) as HTMLButtonElement | null;
  const origText = btn?.textContent ?? "Stop";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Stopping…";
  }
  try {
    await apiFetch(`/api/finance/jobs/${jobId}/stop`, { method: "POST" });
  } catch (e) {
    const msg = (e as Error).message;
    const isNotRunning =
      msg.toLowerCase().includes("not running") ||
      msg.toLowerCase().includes("conflict");
    if (!isNotRunning) console.error(`Stop ${jobId} failed:`, msg);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = origText;
    }
  }
}

// ── Settings loader ───────────────────────────────────────────────────────────

async function _refreshYfStatus(): Promise<void> {
  try {
    const s = await apiFetch<{
      rate_limited: boolean;
      cooldown_seconds_remaining: number;
    }>("/api/finance/yfinance-status");
    const banner = document.getElementById("yfinance-rate-limit-banner");
    const msg = document.getElementById("yfinance-rate-limit-msg");
    if (!banner) return;
    if (s.rate_limited) {
      const mins = Math.ceil(s.cooldown_seconds_remaining / 60);
      if (msg)
        msg.textContent = `yfinance rate-limited — all jobs paused. Resumes in ~${mins} min.`;
      banner.style.display = "flex";
    } else {
      banner.style.display = "none";
    }
  } catch {
    /* non-critical */
  }
}

let _yfStatusTimer: ReturnType<typeof setInterval> | null = null;

export async function loadSettings(): Promise<void> {
  const health = await apiFetch<{
    status?: string;
    broker_ready?: boolean;
    paper_trading?: boolean;
  }>("/api/finance/health").catch(
    () =>
      ({}) as {
        status?: string;
        broker_ready?: boolean;
        paper_trading?: boolean;
      },
  );

  $("api-status").innerHTML = `
    <div>Status: <span class="text-green">${health.status ?? "—"}</span></div>
    <div>Broker: <span class="${health.broker_ready ? "text-green" : "text-muted"}">${health.broker_ready ? "Configured" : "Not configured"}</span></div>
    <div>Mode: <span class="${health.paper_trading ? "text-yellow" : "text-red"}">${health.paper_trading ? "Paper" : "LIVE"}</span></div>
  `;

  void _refreshYfStatus();
  if (_yfStatusTimer) clearInterval(_yfStatusTimer);
  _yfStatusTimer = setInterval(() => void _refreshYfStatus(), 30_000);

  // Initial fetch to show jobs immediately
  const el = document.getElementById("jobs-list");
  if (el) el.innerHTML = '<div class="text-muted">Loading…</div>';
  try {
    const jobs = await apiFetch<typeof _lastJobs>("/api/finance/jobs");
    _handleSSEMessage(jobs);
  } catch {
    if (el) el.innerHTML = '<div class="text-red">Failed to load jobs</div>';
  }

  stopJobsAutoRefresh();

  // SSE for real-time updates
  jobsSSE = new EventSource(`${API}/api/finance/jobs/stream`);
  jobsSSE.onmessage = (e: MessageEvent) => {
    try {
      const jobs = JSON.parse(e.data as string) as typeof _lastJobs;
      _handleSSEMessage(jobs);
    } catch {
      // ignore parse errors
    }
  };
  jobsSSE.onerror = () => {
    // SSE reconnects automatically; no action needed
  };

  void loadJobConfig("crawl");
  void loadJobConfig("refresh_picks");
  void loadJobConfig("score_watchlist");
  void loadJobConfig("ingest");
  void loadJobConfig("refresh_symbols");
  void loadJobConfig("recommendations");
  void loadVolumeAnalysisConfig();
  void loadScreenerConfig();
  void loadCacheConfig();
  void loadVolumeResults();
}

// ── Config panels (volume / screener / cache) ──────────────────
// Extracted to ./settings/config_panels.ts; re-exported so this view's public surface is unchanged.
export * from "./settings/config_panels.js";
