// ── Broker / portfolio view ───────────────────────────────────────────────────

import { apiFetch } from "../core/api.js";
import { $, fmtDollar, fmtPct, colorPct } from "../core/utils.js";
import type { Position, Order } from "../core/types.js";

interface Account {
  error?: string;
  portfolio_value?: number;
  cash?: number;
  buying_power?: number;
  pnl_today?: number;
  pnl_today_pct?: number;
}

export async function loadBroker(): Promise<void> {
  const [acct, pos] = await Promise.all([
    apiFetch<Account>("/api/finance/broker/account").catch(
      () => ({}) as Account,
    ),
    apiFetch<{ positions: Position[] }>("/api/finance/broker/positions").catch(
      () => ({ positions: [] }),
    ),
  ]);

  const kpis: [string, string][] = [
    ["Portfolio", fmtDollar(acct.portfolio_value)],
    ["Cash", fmtDollar(acct.cash)],
    ["Buying Power", fmtDollar(acct.buying_power)],
    [
      "Today P&L",
      acct.pnl_today !== null && acct.pnl_today !== undefined
        ? `${fmtDollar(acct.pnl_today)  } (${  fmtPct(acct.pnl_today_pct)  })`
        : "—",
    ],
  ];
  $("broker-kpis").innerHTML = kpis
    .map(
      ([l, v]) =>
        `<div class="kpi"><div class="kpi-label">${l}</div><div class="kpi-value kpi-value-lg">${v}</div></div>`,
    )
    .join("");

  const posEl = $("broker-positions");
  const positions = pos.positions || [];
  if (positions.length === 0) {
    posEl.innerHTML = '<div class="empty">No positions</div>';
  } else {
    posEl.innerHTML = `<div class="positions-grid">${positions
      .map(
        (p) => `
      <div class="pos-card">
        <div class="pos-card-sym">${p.symbol}</div>
        <div class="pos-card-row"><span>Qty</span><span>${p.qty}</span></div>
        <div class="pos-card-row"><span>Avg Cost</span><span class="mono">${fmtDollar(p.avg_cost)}</span></div>
        <div class="pos-card-row"><span>Market Val</span><span class="mono">${fmtDollar(p.market_value)}</span></div>
        <div class="pos-card-row"><span>Unrealized</span><span class="${colorPct(p.unrealized_pnl_pct)} mono">${fmtPct(p.unrealized_pnl_pct)}</span></div>
      </div>`,
      )
      .join("")}</div>`;
  }
  void loadOrders();
}

export async function loadOrders(): Promise<void> {
  const d = await apiFetch<{ orders: Order[] }>(
    "/api/finance/broker/orders",
  ).catch(() => ({ orders: [] }));
  $("orders-tbody").innerHTML =
    (d.orders || [])
      .map(
        (o) => `<tr>
    <td><strong>${o.symbol}</strong></td>
    <td><span class="tag ${o.side}">${o.side.toUpperCase()}</span></td>
    <td class="mono">${o.qty}</td>
    <td class="mono">${o.filled_qty ?? 0}</td>
    <td>${o.type}</td>
    <td class="mono">${o.limit_price ? fmtDollar(o.limit_price) : "—"}</td>
    <td class="mono">${o.filled_avg ? fmtDollar(o.filled_avg) : "—"}</td>
    <td><span class="tag ${o.status === "filled" ? "closed" : "open"}">${o.status}</span></td>
    <td class="text-sm text-muted">${(o.created_at ?? "").slice(0, 16).replace("T", " ")}</td>
    <td>${["open", "new", "accepted"].includes(o.status) ? `<button class="btn btn-sm btn-danger" onclick="cancelOrder('${o.id}')">Cancel</button>` : ""}</td>
  </tr>`,
      )
      .join("") || '<tr><td colspan="10" class="empty">No orders</td></tr>';
}

export function toggleLimitPrice(): void {
  ($("order-limit") as HTMLInputElement).style.display =
    ($("order-type") as HTMLSelectElement).value === "limit" ? "" : "none";
}

export async function placeOrder(): Promise<void> {
  const sym = ($("order-sym") as HTMLInputElement).value.trim().toUpperCase();
  const qty = parseFloat(($("order-qty") as HTMLInputElement).value);
  const side = ($("order-side") as HTMLSelectElement).value;
  const type = ($("order-type") as HTMLSelectElement).value;
  const lim =
    parseFloat(($("order-limit") as HTMLInputElement).value) || undefined;
  const result = $("order-result");

  if (!sym || !qty) {
    result.textContent = "Symbol and qty required";
    return;
  }
  try {
    const r = await apiFetch<{ id: string; status: string }>(
      "/api/finance/broker/order",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: sym,
          qty,
          side,
          order_type: type,
          limit_price: lim,
        }),
      },
    );
    result.innerHTML = `<span class="text-green">Order submitted: ${r.id} (${r.status})</span>`;
    setTimeout(loadOrders, 1500);
  } catch (e) {
    result.innerHTML = `<span class="text-red">Error: ${(e as Error).message}</span>`;
  }
}

export async function cancelOrder(id: string): Promise<void> {
  await apiFetch(`/api/finance/broker/order/${id}`, { method: "DELETE" }).catch(
    () => {},
  );
  void loadOrders();
}
