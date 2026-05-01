// ── Picks tracker ─────────────────────────────────────────────────────────────

import { apiFetch } from "../core/api.js";
import { $, fmtDollar, fmtPct, fmt, colorPct } from "../core/utils.js";
import { currentSymbol } from "../core/state.js";
import { navigateToStock } from "../core/router.js";
import type { Pick } from "../core/types.js";

interface PickStats {
  total?: number;
  open?: number;
  win_rate?: number;
  avg_return?: number;
  best_pick?: { symbol: string; return: number };
}

export async function loadPicks(): Promise<void> {
  const [picks, stats] = await Promise.all([
    apiFetch<{ picks: Pick[] }>("/api/finance/picks").catch(() => ({
      picks: [],
    })),
    apiFetch<PickStats>("/api/finance/picks/stats").catch(
      () => ({}) as PickStats,
    ),
  ]);

  const kpis: [string, string][] = [
    ["Total Picks", String(stats.total ?? "—")],
    ["Open", String(stats.open ?? "—")],
    ["Win Rate", stats.win_rate !== null && stats.win_rate !== undefined ? `${stats.win_rate  }%` : "—"],
    ["Avg Return", stats.avg_return !== null && stats.avg_return !== undefined ? fmtPct(stats.avg_return) : "—"],
    [
      "Best Pick",
      stats.best_pick
        ? `${stats.best_pick.symbol} +${fmt(stats.best_pick.return)}%`
        : "—",
    ],
  ];
  $("picks-kpis").innerHTML = kpis
    .map(
      ([l, v]) =>
        `<div class="kpi"><div class="kpi-label">${l}</div><div class="kpi-value kpi-value-lg">${v}</div></div>`,
    )
    .join("");

  const open = (picks.picks || []).filter((p) => p.status === "open");
  const closed = (picks.picks || []).filter((p) => p.status === "closed");

  $("picks-tbody").innerHTML =
    open
      .map(
        (p) => `<tr>
    <td><strong class="text-blue row-click" onclick="navigateToStock('${p.symbol}')">${p.symbol}</strong></td>
    <td><span class="tag ${p.direction}">${p.direction.toUpperCase()}</span></td>
    <td class="mono">${fmtDollar(p.entry_price)}</td>
    <td class="mono">${fmtDollar(p.current_price)}</td>
    <td class="mono">${fmtDollar(p.target_price)}</td>
    <td class="${colorPct(p.unrealized_pnl_pct)} mono">${fmtPct(p.unrealized_pnl_pct)}</td>
    <td>${p.signal_score ?? "—"}</td>
    <td><span class="tag open">OPEN</span></td>
    <td class="text-sm text-muted">${p.horizon_days}d</td>
    <td><button class="btn btn-sm btn-ghost" onclick="closePick('${p.id}','${p.symbol}',${p.current_price ?? 0})">Close</button></td>
  </tr>`,
      )
      .join("") || '<tr><td colspan="10" class="empty">No open picks</td></tr>';

  $("closed-picks-tbody").innerHTML =
    closed
      .map(
        (p) => `<tr>
    <td><strong>${p.symbol}</strong></td>
    <td><span class="tag ${p.direction}">${p.direction.toUpperCase()}</span></td>
    <td class="mono">${fmtDollar(p.entry_price)}</td>
    <td class="mono">${fmtDollar(p.exit_price)}</td>
    <td class="${colorPct(p.final_return)} mono">${fmtPct(p.final_return)}</td>
    <td>${p.outcome ?? "—"}</td>
    <td>${p.was_correct === null || p.was_correct === undefined ? "—" : p.was_correct ? "✅" : "❌"}</td>
    <td class="text-sm text-muted">${(p.exit_date ?? "").slice(0, 10)}</td>
  </tr>`,
      )
      .join("") ||
    '<tr><td colspan="8" class="empty">No closed picks</td></tr>';
}

export async function submitPick(): Promise<void> {
  if (!currentSymbol) return;
  const entry = parseFloat(($("pick-entry") as HTMLInputElement).value);
  const target =
    parseFloat(($("pick-target") as HTMLInputElement).value) || undefined;
  const stop =
    parseFloat(($("pick-stop") as HTMLInputElement).value) || undefined;
  const reason = ($("pick-reason") as HTMLInputElement).value;
  const horizon = parseInt(($("pick-horizon") as HTMLSelectElement).value, 10);
  if (!entry) {
    alert("Entry price required");
    return;
  }
  await apiFetch("/api/finance/picks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol: currentSymbol,
      entry_price: entry,
      direction: "buy",
      reasoning: reason,
      target_price: target,
      stop_loss: stop,
      horizon_days: horizon,
    }),
  });
  alert(`Pick added for ${currentSymbol}`);
}

export async function closePick(
  id: string,
  sym: string,
  price: number,
): Promise<void> {
  const exit = prompt(`Close pick for ${sym}. Exit price?`, String(price));
  if (!exit) return;
  const outcome = prompt(
    "Outcome? (hit_target / stopped_out / held / expired)",
    "held",
  );
  await apiFetch(`/api/finance/picks/${id}/close`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      exit_price: parseFloat(exit),
      outcome: outcome ?? "held",
    }),
  });
  void loadPicks();
}

export { navigateToStock };
