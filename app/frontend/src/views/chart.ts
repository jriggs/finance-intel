// ── Chart ──────────────────────────────────────────────────────────────────────

import { API } from "../core/api.js";
import { $, fmtPct, colorPct } from "../core/utils.js";

let _chartResizeObserver: ResizeObserver | null = null;
import { currentSymbol, setCurrentPeriod, currentPeriod } from "../core/state.js";
interface Candle {
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

const DEFAULTS: ChartSettings = {
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

function loadSettings(): ChartSettings {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<ChartSettings>) };
  } catch {
    /* ignore */
  }
  return { ...DEFAULTS };
}

function saveSettings(s: ChartSettings): void {
  localStorage.setItem(STORE_KEY, JSON.stringify(s));
}

const toTime = (s: string): number => Math.floor(new Date(s).getTime() / 1000);

// ── Module state ──────────────────────────────────────────────────────────────
let _candles: Candle[] = [];

// ── Public: period change ─────────────────────────────────────────────────────
function syncPeriodButtons(period: string): void {
  document.querySelectorAll(".period-btns button").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-period") === period);
  });
}

export async function loadChart(period: string): Promise<void> {
  setCurrentPeriod(period);
  syncPeriodButtons(period);

  const wrap = $("chart-wrap");
  wrap.innerHTML =
    '<div class="chart-placeholder"><div class="spinner"></div></div>';

  try {
    const r = await fetch(
      `${API}/api/finance/chart/${currentSymbol}?period=${period}`,
    );
    const d = (await r.json()) as { error?: string; candles?: Candle[]; prev_close?: number };
    if (d.error || !d.candles?.length) {
      wrap.innerHTML = '<div class="chart-placeholder">No chart data</div>';
      return;
    }
    _candles = d.candles;
    renderChart(wrap, _candles, loadSettings());

    // Update percent change to reflect selected time range.
    // For 1d, use prev_close as baseline (matches watchlist day-over-day %).
    // For other periods, use the first candle's open.
    if (_candles.length > 1) {
      const last = _candles[_candles.length - 1];
      const baseline = d.prev_close ?? (Number(_candles[0].open) || Number(_candles[0].close));
      const lastClose = Number(last.close);
      const chgEl = document.getElementById("stock-header-chg");
      if (chgEl && baseline > 0 && lastClose > 0) {
        const pctChange = ((lastClose - baseline) / baseline) * 100;
        chgEl.textContent = fmtPct(pctChange);
        chgEl.className = colorPct(pctChange);
      }
    }
  } catch (e) {
    wrap.innerHTML = `<div class="chart-placeholder">Chart error: ${(e as Error).message}</div>`;
  }
}

// ── Public: toggle a boolean option ──────────────────────────────────────────
export function toggleChartOption(
  key: keyof Omit<ChartSettings, "chartType">,
): void {
  const s = loadSettings();
  s[key] = !s[key];
  saveSettings(s);
  syncToolbarUI(s);
  if (_candles.length) renderChart($("chart-wrap"), _candles, s);
}

// ── Public: switch chart type ─────────────────────────────────────────────────
export function setChartType(type: ChartSettings["chartType"]): void {
  const s = loadSettings();
  s.chartType = type;
  saveSettings(s);
  syncToolbarUI(s);
  if (_candles.length) renderChart($("chart-wrap"), _candles, s);
}

// ── Public: initialise toolbar state on page load ────────────────────────────
export function initChartToolbar(): void {
  syncPeriodButtons(currentPeriod);
  syncToolbarUI(loadSettings());
}

// ── Sync button active states ─────────────────────────────────────────────────
function syncToolbarUI(s: ChartSettings): void {
  document.querySelectorAll<HTMLElement>("[data-chart-type]").forEach((b) => {
    b.classList.toggle("active", b.dataset.chartType === s.chartType);
  });
  (
    ["sma20", "sma50", "sma200", "bb", "volume", "rsi", "macd"] as const
  ).forEach((k) => {
    document.getElementById(`ct-${k}`)?.classList.toggle("active", s[k]);
  });
}

// ── Core render ───────────────────────────────────────────────────────────────
function renderChart(
  wrap: HTMLElement,
  candles: Candle[],
  s: ChartSettings,
): void {
  wrap.innerHTML = "";

  // Dynamic height: base 380 + 110 per sub-indicator
  const SUB_H = 110;
  const BASE_H = 380;
  const totalH = BASE_H + (s.rsi ? SUB_H : 0) + (s.macd ? SUB_H : 0);
  wrap.style.height = `${totalH}px`;

  // Compute scale margins so panes stack without overlap
  const pB = (totalH - BASE_H) / totalH; // price bottom
  const volT = (BASE_H * 0.83) / totalH; // volume top (inside price)
  const rsiT = BASE_H / totalH;
  const rsiB = s.macd ? SUB_H / totalH : 0;
  const macdT = (BASE_H + (s.rsi ? SUB_H : 0)) / totalH;

  const chart = LightweightCharts.createChart(wrap, {
    width: wrap.clientWidth,
    height: totalH,
    layout: { background: { color: "#161b22" }, textColor: "#8b949e" },
    grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: {
      borderColor: "#30363d",
      scaleMargins: { top: 0.02, bottom: pB },
    },
    timeScale: { borderColor: "#30363d", timeVisible: true },
    handleScroll: { mouseWheel: false, pressedMouseMove: true },
    handleScale: {
      mouseWheel: false,
      pinch: false,
      axisPressedMouseMove: true,
    },
  });

  // ── Price series ────────────────────────────────────────────────────────────
  const ohlc = candles.map((c) => ({
    time: toTime(c.date),
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
  }));

  if (s.chartType === "candle") {
    chart
      .addCandlestickSeries({
        upColor: "#3fb950",
        downColor: "#f85149",
        borderUpColor: "#3fb950",
        borderDownColor: "#f85149",
        wickUpColor: "#3fb950",
        wickDownColor: "#f85149",
      })
      .setData(ohlc);
  } else {
    chart
      .addLineSeries({ color: "#58a6ff", lineWidth: 2 })
      .setData(candles.map((c) => ({ time: toTime(c.date), value: c.close })));
  }

  // ── Volume ──────────────────────────────────────────────────────────────────
  if (s.volume) {
    const vol = chart.addHistogramSeries({
      color: "#58a6ff22",
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart
      .priceScale("vol")
      .applyOptions({ scaleMargins: { top: volT, bottom: pB } });
    vol.setData(
      candles.map((c) => ({
        time: toTime(c.date),
        value: c.volume ?? 0,
        color: c.close >= c.open ? "#3fb95033" : "#f8514933",
      })),
    );
  }

  // ── SMA overlays ────────────────────────────────────────────────────────────
  if (s.sma20) {
    const l = chart.addLineSeries({
      color: "#d29922",
      lineWidth: 1,
      title: "SMA20",
    });
    l.setData(
      candles
        .filter((c) => c.sma20)
        .map((c) => ({ time: toTime(c.date), value: c.sma20 as number })),
    );
  }
  if (s.sma50) {
    const l = chart.addLineSeries({
      color: "#58a6ff",
      lineWidth: 1,
      title: "SMA50",
    });
    l.setData(
      candles
        .filter((c) => c.sma50)
        .map((c) => ({ time: toTime(c.date), value: c.sma50 as number })),
    );
  }
  if (s.sma200) {
    const l = chart.addLineSeries({
      color: "#bc8cff",
      lineWidth: 1,
      title: "SMA200",
    });
    l.setData(
      candles
        .filter((c) => c.sma200)
        .map((c) => ({ time: toTime(c.date), value: c.sma200 as number })),
    );
  }

  // ── Bollinger Bands ─────────────────────────────────────────────────────────
  if (s.bb) {
    chart
      .addLineSeries({
        color: "#f0883e66",
        lineWidth: 1,
        lineStyle: 2,
        title: "BB+",
      })
      .setData(
        candles
          .filter((c) => c.bb_upper)
          .map((c) => ({ time: toTime(c.date), value: c.bb_upper as number })),
      );
    chart
      .addLineSeries({
        color: "#f0883e66",
        lineWidth: 1,
        lineStyle: 2,
        title: "BB-",
      })
      .setData(
        candles
          .filter((c) => c.bb_lower)
          .map((c) => ({ time: toTime(c.date), value: c.bb_lower as number })),
      );
  }

  // ── RSI ─────────────────────────────────────────────────────────────────────
  if (s.rsi) {
    const rsiData = candles.filter((c) => c.rsi !== null && c.rsi !== undefined && c.rsi !== 0);
    if (rsiData.length) {
      chart.priceScale("rsi").applyOptions({
        scaleMargins: { top: rsiT + 0.02, bottom: rsiB + 0.02 },
      });
      chart
        .addLineSeries({
          color: "#f0883e",
          lineWidth: 1,
          title: "RSI",
          priceScaleId: "rsi",
        })
        .setData(rsiData.map((c) => ({ time: toTime(c.date), value: c.rsi as number })));
      // 70 / 30 reference lines
      const t0 = toTime(rsiData[0].date);
      const t1 = toTime(rsiData[rsiData.length - 1].date);
      chart
        .addLineSeries({
          color: "#f8514940",
          lineWidth: 1,
          priceScaleId: "rsi",
        })
        .setData([
          { time: t0, value: 70 },
          { time: t1, value: 70 },
        ]);
      chart
        .addLineSeries({
          color: "#3fb95040",
          lineWidth: 1,
          priceScaleId: "rsi",
        })
        .setData([
          { time: t0, value: 30 },
          { time: t1, value: 30 },
        ]);
    }
  }

  // ── MACD ─────────────────────────────────────────────────────────────────────
  if (s.macd) {
    const macdData = candles.filter((c) => c.macd !== null && c.macd !== undefined);
    if (macdData.length) {
      chart
        .priceScale("macd")
        .applyOptions({ scaleMargins: { top: macdT + 0.02, bottom: 0.02 } });
      chart
        .addLineSeries({
          color: "#58a6ff",
          lineWidth: 1,
          title: "MACD",
          priceScaleId: "macd",
        })
        .setData(
          macdData.map((c) => ({ time: toTime(c.date), value: c.macd as number })),
        );
      chart
        .addLineSeries({
          color: "#f0883e",
          lineWidth: 1,
          title: "Signal",
          priceScaleId: "macd",
        })
        .setData(
          macdData
            .filter((c) => c.macd_signal !== null && c.macd_signal !== undefined)
            .map((c) => ({ time: toTime(c.date), value: c.macd_signal as number })),
        );
      chart.addHistogramSeries({ priceScaleId: "macd" }).setData(
        macdData
          .filter((c) => c.macd_hist !== null && c.macd_hist !== undefined)
          .map((c) => ({
            time: toTime(c.date),
            value: c.macd_hist as number,
            color: (c.macd_hist as number) >= 0 ? "#3fb95066" : "#f8514966",
          })),
      );
    }
  }

  chart.timeScale().fitContent();
  _chartResizeObserver?.disconnect();
  _chartResizeObserver = new ResizeObserver(() =>
    chart.applyOptions({ width: wrap.clientWidth }),
  );
  _chartResizeObserver.observe(wrap);
}
