// ── Shared types ──────────────────────────────────────────────────────────────

/** Discriminated state for async data loads — prevents impossible UI states. */
export type ViewState = 'idle' | 'loading' | 'error' | 'success';

export interface Quote {
  symbol: string;
  name?: string;
  price: number;
  change_pct?: number;
  volume?: number;
}

export interface ScoreBreakdown {
  score: number;
  max: number;
}

export interface ScoreCategory {
  score: number;
  grade: string;
}

export interface Signal {
  symbol: string;
  score: number;
  recommendation: string;
  breakdown?: {
    value?: ScoreBreakdown;
    technical?: ScoreBreakdown;
    analyst?: ScoreBreakdown;
  };
  short_term?: ScoreCategory;
  long_term?: ScoreCategory;
  macro?: ScoreCategory;
  sentiment?: ScoreCategory;
  all_reasons?: string[];
  reasons?: string[];
  name?: string;
  sector?: string;
  pe_ratio?: number;
  volatility?: number | null;      // trailing-60d annualized (0.55 = 55%)
  dollar_volume?: number | null;   // median 60d dollar volume
  cached_at?: string;
}

export interface StockInfo {
  name?: string;
  price: number;
  change_pct?: number;
  description?: string;
  market_cap?: number;
  pe_ratio?: number;
  forward_pe?: number;
  pb_ratio?: number;
  ev_ebitda?: number;
  debt_equity?: number;
  current_ratio?: number;
  roe?: number;
  roa?: number;
  revenue_growth?: number;
  earnings_growth?: number;
  profit_margin?: number;
  free_cashflow?: number;
  '52w_high'?: number;
  '52w_low'?: number;
  analyst_target?: number;
  beta?: number;
  dividend_yield?: number;
  insider_pct?: number;
  institution_pct?: number;
  earnings_date?: string;
  sector?: string;
}

export interface NewsItem {
  title: string;
  url?: string;
  thumbnail?: string;
  publisher?: string;
  published?: string;
}

export interface Pick {
  id: string;
  symbol: string;
  direction: string;
  status: 'open' | 'closed';
  entry_price: number;
  current_price?: number;
  exit_price?: number;
  target_price?: number;
  stop_loss?: number;
  unrealized_pnl_pct?: number;
  final_return?: number;
  signal_score?: number;
  horizon_days: number;
  outcome?: string;
  was_correct?: boolean;
  exit_date?: string;
  reasoning?: string;
}

export interface Position {
  symbol: string;
  qty: number;
  avg_cost: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
}

export interface Order {
  id: string;
  symbol: string;
  side: string;
  qty: number;
  filled_qty?: number;
  type: string;
  limit_price?: number;
  filled_avg?: number;
  status: string;
  created_at?: string;
}

export interface VolumeResult {
  symbol: string;
  overall_score?: number;
  macro_score?: number;
  sentiment_score?: number;
  short_term_score?: number;
  long_term_score?: number;
  grade?: string;
  recommendation?: string;
  last_updated_at?: string;
  [key: string]: unknown;
}

export interface FeedItem {
  title?: string;
  question?: string;
  text?: string;
  url?: string;
  source?: string;
  date_str?: string;
  published?: string;
  outcomes?: string;
  score?: number;
  comments?: number;
}

export interface StockCacheEntry {
  info: StockInfo | null;
  sig: Signal | null;
  news: { news: NewsItem[] } | null;
  insightText: string | null;
}

// LightweightCharts is loaded from CDN as a global
declare global {
  const LightweightCharts: {
    createChart: (container: HTMLElement, options: Record<string, unknown>) => LWChart;
    CrosshairMode: { Normal: number };
  };
}

export interface LWChart {
  addCandlestickSeries: (opts: Record<string, unknown>) => LWSeries;
  addHistogramSeries: (opts: Record<string, unknown>) => LWSeries;
  addLineSeries: (opts: Record<string, unknown>) => LWSeries;
  priceScale: (id: string) => { applyOptions: (opts: Record<string, unknown>) => void };
  timeScale: () => { fitContent: () => void };
  applyOptions: (opts: Record<string, unknown>) => void;
}

export interface LWSeries {
  setData: (data: unknown[]) => void;
}
