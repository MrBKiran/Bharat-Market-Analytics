// Interactive price chart (TradingView lightweight-charts): candlesticks, volume, moving averages, Bollinger bands, VWAP and an RSI pane.
// Zoom with the mouse wheel or pinch, drag to pan, hover for the crosshair and exact values.
import { CandlestickSeries, ColorType, CrosshairMode, HistogramSeries, LineSeries, LineStyle, createChart, type IChartApi, type ISeriesApi, type Time } from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Bar, IndicatorRow, Interval } from "../api/types";
import { chartTime, isIntraday } from "../lib/dates";
import { compactNumber, formatDate, formatDateTime, formatNumber, formatPct } from "../lib/format";
import { chartColors, useTheme } from "../theme";

export interface Overlays {
  volume: boolean;
  ema20: boolean;
  ema50: boolean;
  sma200: boolean;
  bb: boolean;
  vwap: boolean;
  rsi: boolean;
}
export const DEFAULT_OVERLAYS: Overlays = { volume: true, ema20: true, ema50: false, sma200: false, bb: false, vwap: false, rsi: false };

interface LineSpec {
  key: string;            // indicator name in the API
  on: boolean;
  color: number;          // index into the theme's series palette
  title: string;
  pane: number;
  dashed?: boolean;
}

const IST_SHIFT = 5.5 * 3600;

function labelFor(time: Time): string {
  if (typeof time === "number") return formatDateTime(new Date((time - IST_SHIFT) * 1000).toISOString());
  if (typeof time === "string") return formatDate(`${time}T00:00:00Z`);
  return "";
}

interface Hover {
  label: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

export function PriceChart({ bars, interval, indicators, overlays, height = 520 }: { bars: Bar[]; interval: Interval; indicators?: IndicatorRow[]; overlays: Overlays; height?: number }) {
  const { theme } = useTheme();
  const intraday = isIntraday(interval);
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lineRefs = useRef<Record<string, ISeriesApi<"Line">>>({});
  const [hover, setHover] = useState<Hover | null>(null);
  const barsByTime = useRef<Map<string | number, Bar>>(new Map());

  const specs: LineSpec[] = useMemo(
    () => [
      { key: "ema_20", on: overlays.ema20, color: 2, title: "EMA 20", pane: 0 },
      { key: "ema_50", on: overlays.ema50, color: 1, title: "EMA 50", pane: 0 },
      { key: "sma_200", on: overlays.sma200, color: 3, title: "SMA 200", pane: 0 },
      { key: "bb_upper", on: overlays.bb, color: 4, title: "BB upper", pane: 0, dashed: true },
      { key: "bb_lower", on: overlays.bb, color: 4, title: "BB lower", pane: 0, dashed: true },
      { key: "vwap_20", on: overlays.vwap, color: 5, title: "VWAP 20", pane: 0 },
      { key: "rsi_14", on: overlays.rsi, color: 3, title: "RSI 14", pane: 1 },
    ],
    [overlays],
  );

  // ---- create the chart once
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const c = chartColors();
    const chart = createChart(host, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: c.border },
      timeScale: { borderColor: c.border, timeVisible: false, secondsVisible: false },
      localization: { priceFormatter: (p: number) => formatNumber(p, 2) },
    });
    const candles = chart.addSeries(CandlestickSeries, { upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down, borderVisible: false });
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chartRef.current = chart;
    candleRef.current = candles;
    volumeRef.current = volume;
    chart.subscribeCrosshairMove((param) => {
      if (!param.time) return setHover(null);
      const key = typeof param.time === "object" ? null : param.time;
      const bar = key === null ? undefined : barsByTime.current.get(key);
      if (!bar) return setHover(null);
      setHover({ label: labelFor(param.time), open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume });
    });
    return () => {
      chart.remove();
      chartRef.current = candleRef.current = volumeRef.current = null;
      lineRefs.current = {};
    };
  }, []);

  // ---- theme
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const c = chartColors();
    chart.applyOptions({
      layout: { background: { type: ColorType.Solid, color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.border },
      timeScale: { borderColor: c.border },
    });
    candleRef.current?.applyOptions({ upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down });
  }, [theme]);

  // ---- bars (price and volume)
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !candleRef.current || !volumeRef.current) return;
    const c = chartColors();
    chart.applyOptions({ timeScale: { timeVisible: intraday } });
    const map = new Map<string | number, Bar>();
    const candles = bars.map((b) => {
      const time = chartTime(b.ts, intraday);
      map.set(time, b);
      return { time: time as Time, open: b.open, high: b.high, low: b.low, close: b.close };
    });
    barsByTime.current = map;
    candleRef.current.setData(candles);
    const hasVolume = bars.some((b) => (b.volume ?? 0) > 0);
    volumeRef.current.setData(
      overlays.volume && hasVolume
        ? bars.map((b) => ({ time: chartTime(b.ts, intraday) as Time, value: b.volume ?? 0, color: (b.close >= b.open ? c.up : c.down) + "66" }))
        : [],
    );
    chart.timeScale().fitContent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bars, intraday, overlays.volume, theme]);

  // ---- indicator lines
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const palette = chartColors().series;
    for (const spec of specs) {
      const existing = lineRefs.current[spec.key];
      const data = spec.on && indicators ? indicators.filter((r) => typeof r[spec.key] === "number").map((r) => ({ time: chartTime(r.ts, intraday) as Time, value: r[spec.key] as number })) : [];
      if (!spec.on || !data.length) {
        if (existing) {
          chart.removeSeries(existing);
          delete lineRefs.current[spec.key];
        }
        continue;
      }
      const options = { color: palette[spec.color], lineWidth: 1 as const, lineStyle: spec.dashed ? LineStyle.Dashed : LineStyle.Solid, title: spec.title, priceLineVisible: false, lastValueVisible: spec.pane === 1 };
      if (existing) existing.applyOptions(options);
      const series = existing ?? chart.addSeries(LineSeries, options, spec.pane);
      if (!existing) {
        lineRefs.current[spec.key] = series;
        if (spec.key === "rsi_14") {
          series.createPriceLine({ price: 70, color: palette[0], lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
          series.createPriceLine({ price: 30, color: palette[0], lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
        }
      }
      series.setData(data);
    }
    const panes = chart.panes();
    if (panes.length > 1) panes[1].setHeight(Math.round(height * 0.26));
  }, [specs, indicators, intraday, theme, height]);

  const last = bars[bars.length - 1];
  const shown = hover ?? (last ? { label: intraday ? formatDateTime(last.ts) : formatDate(last.ts), open: last.open, high: last.high, low: last.low, close: last.close, volume: last.volume } : null);
  const change = shown ? ((shown.close - shown.open) / shown.open) * 100 : null;

  return (
    <div className="chart-box" role="img" aria-label={`Price chart, ${bars.length} bars. Use the table view for exact values.`}>
      {shown && (
        <div className="chart-legend">
          <span>{shown.label}</span>
          <span>O <b className="num">{formatNumber(shown.open, 2)}</b></span>
          <span>H <b className="num">{formatNumber(shown.high, 2)}</b></span>
          <span>L <b className="num">{formatNumber(shown.low, 2)}</b></span>
          <span>C <b className="num">{formatNumber(shown.close, 2)}</b></span>
          <span className={change !== null && change >= 0 ? "up" : "down"}>{formatPct(change, 2, true)}</span>
          {!!shown.volume && <span>Vol <b className="num">{compactNumber(shown.volume)}</b></span>}
        </div>
      )}
      <div ref={hostRef} style={{ height }} />
    </div>
  );
}
