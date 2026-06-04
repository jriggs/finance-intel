// ── Chart · data types & persisted toolbar settings ──────────────────────────

export interface Candle {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  sma20?: number;
  sma50?: number;
  sma200?: number;
  bb_upper?: number;
  bb_lower?: number;
  rsi?: number;
  macd?: number;
  macd_signal?: number;
  macd_hist?: number;
}

export interface ChartSettings {
  chartType: "candle" | "line";
  sma20: boolean;
  sma50: boolean;
  sma200: boolean;
  bb: boolean;
  volume: boolean;
  rsi: boolean;
  macd: boolean;
}

export const DEFAULTS: ChartSettings = {
  chartType: "candle",
  sma20: true,
  sma50: true,
  sma200: true,
  bb: false,
  volume: true,
  rsi: false,
  macd: false,
};

const STORE_KEY = "chartSettings_v1";

export function loadSettings(): ChartSettings {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<ChartSettings>) };
  } catch {
    /* ignore */
  }
  return { ...DEFAULTS };
}

export function saveSettings(s: ChartSettings): void {
  localStorage.setItem(STORE_KEY, JSON.stringify(s));
}

export const toTime = (s: string): number => Math.floor(new Date(s).getTime() / 1000);
