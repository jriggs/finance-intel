// ── AI chat & insight ─────────────────────────────────────────────────────────

import { marked } from "marked";
import { API } from "../core/api.js";
import { $, escapeHtml } from "../core/utils.js";
import { currentSymbol, stockCache, collapsedCards } from "../core/state.js";
import { parseRoute, navigate } from "../core/router.js";

export function appendMessage(
  role: "user" | "assistant",
  content: string,
): HTMLElement {
  const el = $("chat-messages");
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = role === "user" ? "👤" : "🤖";
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  if (role === "user") {
    bubble.textContent = content;
  } else {
    bubble.innerHTML = content;
  }
  div.appendChild(avatar);
  div.appendChild(bubble);
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  return div.querySelector(".msg-bubble") as HTMLElement;
}

async function streamLlm(
  prompt: string,
  symbol: string | null,
  model: string | null,
  bubble: HTMLElement,
  signal?: AbortSignal,
): Promise<string> {
  const resp = await fetch(`${API  }/api/finance/llm/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, symbol, model }),
    signal,
  });

  if (!resp.ok) {
    const err = await resp
      .json()
      .catch(() => ({ detail: resp.statusText })) as { detail?: string };
    bubble.textContent = `⚠ ${  err.detail ?? resp.statusText}`;
    return "";
  }

  if (!resp.body) {
    bubble.textContent = "⚠ No response body";
    return "";
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let full = "";
  bubble.textContent = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    for (const line of decoder.decode(value).split("\n")) {
      if (!line.startsWith("data: ")) continue;
      try {
        const obj = JSON.parse(line.slice(6)) as {
          content?: string;
          error?: string;
        };
        if (obj.content) {
          full += obj.content;
          bubble.textContent = full;
        }
        if (obj.error) {
          bubble.textContent = `⚠ ${  obj.error}`;
        }
      } catch {
        /* partial JSON */
      }
    }
  }

  ($("chat-messages")).scrollTop = 9999;
  return full;
}

export async function sendChat(): Promise<void> {
  const input = $("chat-input") as HTMLInputElement;
  const msg = input.value.trim();
  if (!msg) return;
  input.value = "";
  ($("chat-send-btn") as HTMLButtonElement).disabled = true;

  appendMessage("user", msg);
  const bubble = appendMessage("assistant", '<span class="spinner"></span>');
  const model = ($("model-select") as HTMLSelectElement).value || null;

  try {
    await streamLlm(msg, currentSymbol || null, model, bubble);
  } catch (e) {
    bubble.textContent = `⚠ ${  (e as Error).message}`;
  } finally {
    ($("chat-send-btn") as HTMLButtonElement).disabled = false;
  }
}

type InsightData = {
  insight_text?: string | null;
  generated_at?: string | null;
  generating?: boolean;
};

/** Show a completed insight result in the UI. */
function applyInsightResult(sym: string, data: InsightData): void {
  const out = $("ai-insight-output");
  const btn = $("ai-insight-btn") as HTMLButtonElement;
  const tsEl = $("ei-generated-at");
  out.innerHTML = marked.parse(escapeHtml(data.insight_text ?? "")) as string;
  if (data.generated_at)
    tsEl.textContent =
      `Generated ${new Date(data.generated_at).toLocaleString()}`;
  btn.textContent = "Rerun";
  btn.disabled = false;
  const entry = stockCache.get(sym) ?? {
    info: null,
    sig: null,
    news: null,
    insightText: null,
  };
  stockCache.set(sym, { ...entry, insightText: data.insight_text ?? null });
}

/** Poll until the backend finishes generating for sym (or times out). */
function pollInsight(sym: string, since: number): void {
  const out = $("ai-insight-output");
  const btn = $("ai-insight-btn") as HTMLButtonElement;
  const tsEl = $("ei-generated-at");
  let attempts = 0;
  const maxAttempts = 120;
  const tick = async () => {
    attempts++;
    try {
      const r = await fetch(`${API  }/api/finance/extended-insights/${sym}`);
      if (r.ok) {
        const data = (await r.json()) as InsightData;
        if (data.insight_text) {
          const genAt = data.generated_at
            ? new Date(data.generated_at).getTime()
            : 0;
          if (genAt >= since) {
            applyInsightResult(sym, data);
            return;
          }
        }
        // still generating — keep polling
        if (!data.generating && attempts > 5) {
          // backend finished but no text (error); give up
          out.textContent = "⚠ Generation failed. Try Rerun.";
          btn.textContent = "Rerun";
          btn.disabled = false;
          return;
        }
      }
    } catch {
      /* silent */
    }
    if (attempts < maxAttempts) {
      setTimeout(tick, 2000);
    } else {
      out.textContent = "⚠ Timed out waiting for result. Try Rerun.";
      btn.textContent = "Rerun";
      btn.disabled = false;
      tsEl.textContent = "";
    }
  };
  setTimeout(tick, 3000);
}

/** Load cached extended insight from backend and display it. */
export async function loadExtendedInsight(sym: string): Promise<void> {
  const out = $("ai-insight-output");
  const btn = $("ai-insight-btn") as HTMLButtonElement;
  const tsEl = $("ei-generated-at");

  out.textContent = "";
  tsEl.textContent = "";
  btn.textContent = "Run";
  btn.disabled = false;

  try {
    const result = await fetch(`${API  }/api/finance/extended-insights/${sym}`);
    if (!result.ok) return;
    const data = (await result.json()) as InsightData;
    if (data.insight_text) {
      applyInsightResult(sym, data);
    } else if (data.generating) {
      btn.disabled = true;
      btn.textContent = "Generating…";
      out.textContent =
        "Generating extended insights — this may take a minute…";
      pollInsight(sym, Date.now() - 10 * 60 * 1000); // accept any result from last 10min
    } else {
      out.textContent = "No insight yet. Click Run to generate.";
    }
  } catch {
    out.textContent = "No insight yet. Click Run to generate.";
  }
}

/** Trigger on-demand extended insight generation for current symbol, streaming tokens. */
export async function runExtendedInsight(): Promise<void> {
  const sym = currentSymbol;
  if (!sym) return;

  const btn = $("ai-insight-btn") as HTMLButtonElement;
  const out = $("ai-insight-output");
  const tsEl = $("ei-generated-at");

  btn.disabled = true;
  btn.textContent = "Generating…";
  out.textContent = "";
  tsEl.textContent = "Generating…";

  try {
    const resp = await fetch(`${API}/api/finance/extended-insights/${sym}/stream`);
    if (!resp.ok) {
      if (resp.status === 409) {
        out.textContent = "Generating extended insights — this may take a minute…";
        pollInsight(sym, Date.now() - 10 * 60 * 1000);
        return;
      }
      const err = await resp.json().catch(() => ({ detail: resp.statusText })) as { detail?: string };
      out.textContent = `⚠ ${err.detail ?? resp.statusText}`;
      btn.disabled = false;
      btn.textContent = "Run";
      return;
    }

    const reader = resp.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let fullText = "";

    outer: while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE messages are separated by \n\n
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";

      for (const part of parts) {
        for (const line of part.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const token = line.slice(6).replace(/\\n/g, "\n").replace(/\\\\/g, "\\");
          if (token === "[DONE]") {
            const entry = stockCache.get(sym) ?? { info: null, sig: null, news: null, insightText: null };
            stockCache.set(sym, { ...entry, insightText: fullText });
            tsEl.textContent = `Generated ${new Date().toLocaleString()}`;
            btn.textContent = "Rerun";
            btn.disabled = false;
            break outer;
          }
          if (token.startsWith("[ERROR]")) {
            out.textContent = `⚠ ${token.slice(7).trim()}`;
            btn.textContent = "Rerun";
            btn.disabled = false;
            tsEl.textContent = "";
            break outer;
          }
          fullText += token;
          out.innerHTML = marked.parse(escapeHtml(fullText)) as string;
        }
      }
    }
  } catch (e) {
    out.textContent = `⚠ ${(e as Error).message}`;
    btn.disabled = false;
    btn.textContent = "Run";
  }
}

export function toggleCard(id: string): void {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle("collapsed");
  if (el.classList.contains("collapsed")) collapsedCards.add(id);
  else collapsedCards.delete(id);
}

export function quickPrompt(text: string): void {
  ($("chat-input") as HTMLInputElement).value = text;
  const { view } = parseRoute();
  if (view !== "stock") navigate("stock", { symbol: currentSymbol || "AAPL" });
  $("chat-input").focus();
}
