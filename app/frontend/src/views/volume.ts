// ── Volume analysis table ─────────────────────────────────────────────────────

import { apiFetch } from "../core/api.js";
import { $, fmt } from "../core/utils.js";
import { navigateToStock } from "../core/router.js";
import {
  volumeData,
  volumeSortBy,
  volumeSortDesc,
  setVolumeData,
  setVolumeSort,
} from "../core/state.js";
import type { VolumeResult } from "../core/types.js";

export async function loadVolumeResults(): Promise<void> {
  const body = $("volume-tbody");
  const count = $("volume-count");
  const status = $("volume-status");

  body.innerHTML =
    '<tr><td colspan="9" class="vol-spinner-td"><div class="spinner"></div></td></tr>';
  count.textContent = "Loading…";
  status.textContent = "";

  try {
    const [d, tasks] = await Promise.all([
      apiFetch<{
        results: VolumeResult[];
        count: number;
        total_scored: number;
      }>("/api/finance/volume-analysis/results"),
      apiFetch<{ total: number; pending: number; ever_scored: number }>(
        "/api/finance/volume-analysis/tasks",
      ),
    ]);
    setVolumeData(d.results || []);

    const total = tasks.total ?? 0;
    const pending = tasks.pending ?? 0;
    const everScored = tasks.ever_scored ?? 0;

    const showingTop = everScored > volumeData.length;
    const scoredLabel = showingTop
      ? `Top ${volumeData.length} of ${everScored} scored`
      : `${everScored} scored`;
    const processedLabel = total > 0 ? `${everScored}/${total} processed` : "";

    count.textContent = [scoredLabel, processedLabel]
      .filter(Boolean)
      .join(" · ");
    status.textContent =
      pending > 0 ? `${pending} pending` : total > 0 ? "up to date" : "";
    renderVolumeTable();
  } catch (e) {
    body.innerHTML = `<tr><td colspan="9" class="vol-error-td">${(e as Error).message}</td></tr>`;
    count.textContent = "Error loading results";
  }
}

export function sortVolumeBy(col: string): void {
  const newDesc = volumeSortBy === col ? !volumeSortDesc : true;
  setVolumeSort(col, newDesc);
  renderVolumeTable();
}

export function renderVolumeTable(): void {
  const key = volumeSortBy;
  const desc = volumeSortDesc;
  const isNum = volumeData.length > 0 && typeof volumeData[0][key] === "number";
  const tagged = volumeData.map((r) => {
    const val = r[key];
    return { r, v: (val ?? (isNum ? 0 : "")) as string | number };
  });
  tagged.sort(
    isNum
      ? (a, b) =>
          desc
            ? (b.v as number) - (a.v as number)
            : (a.v as number) - (b.v as number)
      : (a, b) => {
          const c = String(a.v).localeCompare(String(b.v));
          return desc ? -c : c;
        },
  );
  const sorted = tagged.map((t) => t.r);

  const body = $("volume-tbody");
  if (sorted.length === 0) {
    body.innerHTML =
      '<tr><td colspan="9" class="vol-td-empty">No results yet. Scoring in progress…</td></tr>';
    return;
  }

  const html = sorted
    .map(
      (r) =>
        `<tr class="vol-row"><td class="vol-td" onclick="navigateToStock('${r.symbol}')"><strong class="text-blue">${r.symbol}</strong></td><td class="vol-td vol-td-center" onclick="navigateToStock('${r.symbol}')">${fmt((r.overall_score as number) || 0)}</td><td class="vol-td vol-td-center" onclick="navigateToStock('${r.symbol}')">${fmt((r.macro_score as number) || 0)}</td><td class="vol-td vol-td-center" onclick="navigateToStock('${r.symbol}')">${fmt((r.sentiment_score as number) || 0)}</td><td class="vol-td vol-td-center" onclick="navigateToStock('${r.symbol}')">${fmt((r.short_term_score as number) || 0)}</td><td class="vol-td vol-td-center" onclick="navigateToStock('${r.symbol}')">${fmt((r.long_term_score as number) || 0)}</td><td class="vol-td vol-td-center vol-td-grade" onclick="navigateToStock('${r.symbol}')">${r.grade ?? "—"}</td><td class="vol-td vol-td-center" onclick="navigateToStock('${r.symbol}')">${r.recommendation ?? "—"}</td><td class="vol-td vol-td-date" onclick="navigateToStock('${r.symbol}')">${
          r.last_updated_at
            ? (() => {
                const d = new Date(r.last_updated_at);
                return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
              })()
            : "—"
        }</td><td class="vol-td vol-td-center"><button class="btn btn-sm" onclick="event.stopPropagation(); rescoreVolume('${r.symbol}')">Score</button></td></tr>`,
    )
    .join("");
  body.innerHTML = html;
}

export async function rescoreVolume(symbol: string): Promise<void> {
  try {
    await apiFetch(`/api/finance/volume-analysis/rescore/${symbol}`, {
      method: "POST",
    });
    setTimeout(() => loadVolumeResults(), 500);
  } catch (e) {
    console.error(`Rescore ${symbol} failed:`, (e as Error).message);
  }
}

// suppress unused warning
export { navigateToStock };
