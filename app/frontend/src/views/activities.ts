// ── Scheduled jobs & activity logs ────────────────────────────────────────────

import { apiFetch } from '../core/api.js';
import { $, escapeHtml } from '../core/utils.js';

interface Job { id: string; name: string; next_run?: string; running?: boolean }
interface LogEntry { ts: string; level: string; logger: string; message: string }

export async function loadActivities(): Promise<void> {
  await loadActivityLogs();
}

export async function loadActivityJobs(): Promise<void> {
  try {
    const d   = await apiFetch<{ jobs: Job[] }>('/api/finance/scheduler/jobs');
    const el  = $('act-jobs');
    const now = Date.now();
    el.innerHTML = (d.jobs || []).map(j => {
      const next = j.next_run ? new Date(j.next_run) : null;
      const diff = next ? Math.round((next.getTime() - now) / 60000) : null;
      const when = diff === null ? '—' : diff <= 0 ? 'soon' : `in ${diff}m`;
      const runBadge = j.running
        ? '<span class="run-badge">RUNNING</span>'
        : '';
      return `<div class="act-job-row">
        <span class="text-blue">${j.name}${runBadge}</span>
        <span class="text-muted">next: ${when} &nbsp; <span class="act-job-next">${next ? next.toLocaleTimeString() : '—'}</span></span>
      </div>`;
    }).join('') || '<div class="text-muted">No scheduled jobs</div>';
  } catch (e) {
    $('act-jobs').innerHTML = `<div class="text-red">Error: ${escapeHtml((e as Error).message)}</div>`;
  }
}

export async function loadActivityLogs(): Promise<void> {
  const level = (($('act-log-level') || {}) as HTMLSelectElement).value || 'WARNING';
  try {
    const d  = await apiFetch<{ entries: LogEntry[] }>(`/api/finance/logs?level=${level}&limit=200`);
    const el = $('act-logs');
    const entries = d.entries || [];
    if (!entries.length) { el.innerHTML = '<div class="text-muted">No log entries</div>'; return; }
    el.innerHTML = [...entries].reverse().map(e => `
      <div class="log-entry">
        <span class="log-ts">${e.ts}</span>
        <span class="log-level ${e.level}">${e.level}</span>
        <span class="log-logger">${e.logger}</span>
        <span> ${e.message}</span>
      </div>`
    ).join('');
    el.scrollTop = 0;
  } catch (e) {
    $('act-logs').innerHTML = `<div class="text-red">Error: ${escapeHtml((e as Error).message)}</div>`;
  }
}
