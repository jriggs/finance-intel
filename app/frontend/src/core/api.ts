// ── API client ────────────────────────────────────────────────────────────────

export const API: string =
  (typeof (globalThis as any).__API_URL__ === "string" && (globalThis as any).__API_URL__) ||
  "http://localhost:8000";

export async function apiFetch<T = unknown>(
  path: string,
  opts: RequestInit = {},
): Promise<T> {
  const r = await fetch(API + path, opts);
  if (!r.ok) {
    const err = await r
      .json()
      .catch(() => ({ detail: r.statusText })) as { detail?: string };
    throw new Error(err.detail ?? r.statusText);
  }
  return r.json() as Promise<T>;
}

export function postJson(path: string, body: unknown): Promise<unknown> {
  return apiFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
