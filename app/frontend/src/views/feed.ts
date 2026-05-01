// ── News & social feed ────────────────────────────────────────────────────────

import { apiFetch } from "../core/api.js";
import { $ } from "../core/utils.js";
import type { FeedItem } from "../core/types.js";

const FEED_SOURCES = [
  { key: "reddit", label: "Reddit" },
  { key: "marketwatch", label: "MarketWatch" },
  { key: "yahoo", label: "Yahoo Finance" },
  { key: "zacks", label: "Zacks" },
  { key: "finviz", label: "Finviz" },
  { key: "polymarket", label: "Polymarket" },
];

let activeFeedSource = "reddit";

export function loadFeed(): void {
  const tabs = $("feed-tabs");
  tabs.innerHTML = FEED_SOURCES.map(
    (s) => `
    <div class="feed-tab ${s.key === activeFeedSource ? "active" : ""}" onclick="switchFeed('${s.key}')">${s.label}</div>
  `,
  ).join("");
  void renderFeedSource(activeFeedSource);
}

export function switchFeed(key: string): void {
  activeFeedSource = key;
  document.querySelectorAll(".feed-tab").forEach((t) => {
    const label = FEED_SOURCES.find((s) => s.key === key)?.label ?? "";
    t.classList.toggle("active", t.textContent === label);
  });
  void renderFeedSource(key);
}

export async function renderFeedSource(key: string): Promise<void> {
  const el = $("feed-items");
  el.innerHTML = '<div class="spinner"></div>';
  const d = await apiFetch<{ items?: FeedItem[] }>(
    `/api/finance/crawl/${key}`,
  ).catch(() => ({}));
  const items = (d as { items?: FeedItem[] }).items ?? [];
  if (!items.length) {
    el.innerHTML = '<div class="empty">No data — run a crawl</div>';
    return;
  }
  el.innerHTML = items
    .map((item) => {
      const title = item.title ?? item.question ?? item.text ?? "";
      const url = item.url ?? "";
      const meta = [item.source, item.date_str, item.published, item.outcomes]
        .filter(Boolean)
        .slice(0, 3);
      const score = item.score ? `↑${item.score}` : "";
      const cmts = item.comments ? `💬${item.comments}` : "";
      return `<div class="feed-item">
      <div class="feed-item-src">${item.source ?? key}</div>
      <div class="feed-item-title">${url ? `<a href="${url}" target="_blank" rel="noopener">${title}</a>` : title}</div>
      <div class="feed-item-meta">
        ${meta.map((m) => `<span>${m}</span>`).join("")}
        ${score ? `<span>${score}</span>` : ""}
        ${cmts ? `<span>${cmts}</span>` : ""}
      </div>
    </div>`;
    })
    .join("");
}

export async function triggerCrawl(): Promise<void> {
  $("crawl-status").textContent = "Starting crawl…";
  await apiFetch("/api/finance/crawl/run", { method: "POST" }).catch(() => {});
  $("crawl-status").textContent = "Crawl running in background…";
  setTimeout(() => {
    $("crawl-status").textContent = "";
    loadFeed();
  }, 15000);
}
