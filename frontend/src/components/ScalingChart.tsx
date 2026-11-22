// A small SVG line chart for benchmark results: the data size on a logarithmic x axis (100 thousand to 10 million records),
// the measured value on y (logarithmic or linear). Drawn by hand because the price chart library wants a time axis.
// Accessible: the chart has a title and description, every point has a tooltip, and "Show the numbers" is a real table.
import { useId, useMemo } from "react";
import { DataTable } from "./ui";

export interface ChartPoint { x: number; y: number }
export interface ChartSeries { name: string; color: string; points: ChartPoint[] }

interface Props {
  title: string;
  series: ChartSeries[];
  xLabel: string;
  yLabel: string;
  formatY: (value: number) => string;
  logY?: boolean;
  footnote?: string;
}

const W = 640, H = 300, LEFT = 66, RIGHT = 20, TOP = 14, BOTTOM = 48;
const TEXT = { fill: "var(--chart-text)", fontSize: 11 } as const;

/** 100000 -> "100K", 1000000 -> "1M", 2500000 -> "2.5M": the way benchmark sizes are talked about (the brief says 100K, 1M, 5M, 10M). */
export function shortNumber(v: number): string {
  const trim = (n: number) => String(Number(n.toFixed(Math.abs(n) >= 100 ? 0 : Math.abs(n) >= 10 ? 1 : 2)));       // three significant figures
  if (Math.abs(v) >= 1e6) return `${trim(v / 1e6)}M`;
  if (Math.abs(v) >= 1e3) return `${trim(v / 1e3)}K`;
  return trim(v);
}

/** Powers of ten that cover [min, max]. */
export function logTicks(min: number, max: number): number[] {
  const lo = Math.floor(Math.log10(min));
  const hi = Math.max(Math.ceil(Math.log10(max)), lo + 1);
  return Array.from({ length: hi - lo + 1 }, (_, i) => 10 ** (lo + i));
}

/** About `count` evenly spaced round numbers from 0 up to at least `max`. */
export function linearTicks(max: number, count = 4): number[] {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const n = raw / pow;
  const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * pow;
  return Array.from({ length: Math.ceil(max / step - 1e-9) + 1 }, (_, i) => i * step);
}

export function ScalingChart({ title, series, xLabel, yLabel, formatY, logY = false, footnote }: Props) {
  const uid = useId();
  const drawn = useMemo(
    () => series
      .map((s) => ({ ...s, points: s.points.filter((p) => Number.isFinite(p.y) && (!logY || p.y > 0)).sort((a, b) => a.x - b.x) }))
      .filter((s) => s.points.length > 0),
    [series, logY],
  );
  if (drawn.length === 0) return null;

  const xs = [...new Set(drawn.flatMap((s) => s.points.map((p) => p.x)))].sort((a, b) => a - b);
  const ys = drawn.flatMap((s) => s.points.map((p) => p.y));
  const yTicks = logY ? logTicks(Math.min(...ys), Math.max(...ys)) : linearTicks(Math.max(...ys));
  const yLo = yTicks[0], yHi = yTicks[yTicks.length - 1];
  const plotW = W - LEFT - RIGHT, plotH = H - TOP - BOTTOM;
  const spanX = Math.log10(xs[xs.length - 1]) - Math.log10(xs[0]);
  const px = (x: number) => (xs.length === 1 ? LEFT + plotW / 2 : LEFT + ((Math.log10(x) - Math.log10(xs[0])) / spanX) * plotW);
  const py = (y: number) => {
    const t = logY ? (Math.log10(y) - Math.log10(yLo)) / (Math.log10(yHi) - Math.log10(yLo)) : (y - yLo) / (yHi - yLo);
    return TOP + plotH - t * plotH;
  };
  const description = `${drawn.map((s) => s.name).join(", ")}. ${xs.length} data size${xs.length === 1 ? "" : "s"} from ${shortNumber(xs[0])} to ${shortNumber(xs[xs.length - 1])} records; the numbers are in the table below the chart.`;

  return (
    <figure style={{ margin: 0 }}>
      <ul aria-label="Legend" style={{ display: "flex", gap: "1rem", flexWrap: "wrap", listStyle: "none", padding: 0, margin: "0 0 0.5rem", fontSize: "0.85rem" }}>
        {drawn.map((s) => (
          <li key={s.name}>
            <span aria-hidden="true" style={{ display: "inline-block", width: 16, height: 3, background: s.color, marginRight: 6, verticalAlign: "middle", borderRadius: 2 }} />
            {s.name}
          </li>
        ))}
      </ul>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${uid}-t`} aria-describedby={`${uid}-d`} style={{ width: "100%", height: "auto", display: "block" }}>
        <title id={`${uid}-t`}>{title}</title>
        <desc id={`${uid}-d`}>{description}</desc>
        {yTicks.map((t) => (
          <g key={t}>
            <line x1={LEFT} x2={W - RIGHT} y1={py(t)} y2={py(t)} style={{ stroke: "var(--chart-grid)" }} />
            <text x={LEFT - 8} y={py(t) + 4} textAnchor="end" style={TEXT}>{formatY(t)}</text>
          </g>
        ))}
        {xs.map((x) => <text key={x} x={px(x)} y={H - BOTTOM + 18} textAnchor="middle" style={TEXT}>{shortNumber(x)}</text>)}
        <text x={LEFT + plotW / 2} y={H - 6} textAnchor="middle" style={TEXT}>{xLabel}</text>
        <text transform={`translate(13 ${TOP + plotH / 2}) rotate(-90)`} textAnchor="middle" style={TEXT}>{yLabel}</text>
        {drawn.map((s) => (
          <g key={s.name}>
            <path d={s.points.map((p, i) => `${i ? "L" : "M"}${px(p.x).toFixed(1)} ${py(p.y).toFixed(1)}`).join(" ")} fill="none" style={{ stroke: s.color, strokeWidth: 2 }} />
            {s.points.map((p) => (
              <circle key={p.x} cx={px(p.x)} cy={py(p.y)} r={4} style={{ fill: s.color }}>
                <title>{`${s.name}, ${shortNumber(p.x)} records: ${formatY(p.y)}`}</title>
              </circle>
            ))}
          </g>
        ))}
      </svg>
      {footnote && <figcaption className="muted" style={{ fontSize: "0.8rem", marginTop: "0.25rem" }}>{footnote}</figcaption>}
      <details style={{ marginTop: "0.5rem" }}>
        <summary style={{ cursor: "pointer", fontSize: "0.85rem" }}>Show the numbers</summary>
        <DataTable
          rows={xs}
          rowKey={(x) => String(x)}
          columns={[
            { key: "x", header: xLabel, align: "right", render: (x) => shortNumber(x) },
            ...drawn.map((s) => ({
              key: s.name, header: s.name, align: "right" as const,
              render: (x: number) => { const p = s.points.find((q) => q.x === x); return p ? formatY(p.y) : "–"; },
            })),
          ]}
        />
      </details>
    </figure>
  );
}
