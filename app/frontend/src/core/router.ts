// ── History API router ────────────────────────────────────────────────────────

export interface RouteState {
  view: string;
  params: Record<string, string>;
}

export function parseRoute(): RouteState {
  const view = window.location.pathname.replace(/^\//, '') || 'dashboard';
  const params: Record<string, string> = {};
  new URLSearchParams(window.location.search).forEach((val, key) => {
    params[key] = val;
  });
  return { view, params };
}

export function navigate(view: string, params: Record<string, string> = {}): void {
  let url = `/${  view}`;
  const qs = new URLSearchParams(params).toString();
  if (qs) url += `?${  qs}`;
  history.pushState(null, '', url);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

export function navigateToStock(sym: string): void {
  navigate('stock', { symbol: sym.toUpperCase() });
  window.scrollTo(0, 0);
}
