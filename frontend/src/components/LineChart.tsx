// A multi-line chart for equity curves and rebased comparisons (same TradingView library as the price chart).
import { ColorType, CrosshairMode, LineSeries, createChart, type IChartApi, type ISeriesApi, type Time } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { formatDate } from "../lib/format";
import { chartColors, useTheme } from "../theme";

export interface LineData {
  id: string;
  label: string;
  /** index into the theme palette (0-5) */
  color: number;
  points: { ts: string; value: number }[];
}

export function LineChart({ series, height = 340, format = (v: number) => v.toFixed(2), ariaLabel }: { series: LineData[]; height?: number; format?: (v: number) => string; ariaLabel: string }) {
  const { theme } = useTheme();
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRefs = useRef<Record<string, ISeriesApi<"Line">>>({});
  const formatRef = useRef(format);
  formatRef.current = format;
  const [hover, setHover] = useState<{ label: string; values: Record<string, number> } | null>(null);

  useEffect(() => {
    if (!hostRef.current) return;
    const c = chartColors();
    const chart = createChart(hostRef.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: c.border },
      timeScale: { borderColor: c.border },
      localization: { priceFormatter: (p: number) => formatRef.current(p) },
    });
    chartRef.current = chart;
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || typeof param.time === "object") return setHover(null);
      const values: Record<string, number> = {};
      for (const [id, s] of Object.entries(seriesRefs.current)) {
        const d = param.seriesData.get(s) as { value?: number } | undefined;
        if (d?.value !== undefined) values[id] = d.value;
      }
      setHover({ label: typeof param.time === "string" ? formatDate(`${param.time}T00:00:00Z`) : "", values });
    });
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRefs.current = {};
    };
  }, []);

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
    const wanted = new Set(series.map((s) => s.id));
    for (const id of Object.keys(seriesRefs.current)) {
      if (!wanted.has(id)) {
        chart.removeSeries(seriesRefs.current[id]);
        delete seriesRefs.current[id];
      }
    }
    for (const s of series) {
      const options = { color: c.series[s.color % c.series.length], lineWidth: 2 as const, priceLineVisible: false, lastValueVisible: true, title: "" };
      const line = seriesRefs.current[s.id] ?? chart.addSeries(LineSeries, options);
      seriesRefs.current[s.id] = line;
      line.applyOptions(options);
      line.setData(s.points.map((p) => ({ time: p.ts.slice(0, 10) as Time, value: p.value })));
    }
    chart.timeScale().fitContent();
  }, [series, theme]);

  const palette = chartColors().series;
  return (
    <div>
      <div className="chart-box" role="img" aria-label={ariaLabel}>
        {hover && (
          <div className="chart-legend">
            <span>{hover.label}</span>
            {series.map((s) => (hover.values[s.id] !== undefined ? <span key={s.id}>{s.label} <b className="num">{format(hover.values[s.id])}</b></span> : null))}
          </div>
        )}
        <div ref={hostRef} style={{ height }} />
      </div>
      <div className="legend-row" aria-hidden="true">
        {series.map((s) => (
          <span key={s.id} style={{ ["--swatch" as any]: palette[s.color % palette.length] }}>{s.label}</span>
        ))}
      </div>
    </div>
  );
}
