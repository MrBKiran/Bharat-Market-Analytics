// Two light-weight visualisations drawn with plain HTML: a correlation heat-map and signed horizontal bars.
import { formatPct } from "../lib/format";

/** Correlation matrix. Strong positive is green, strong negative is red; the number is always printed. */
export function Heatmap({ labels, matrix }: { labels: string[]; matrix: Record<string, Record<string, number | null>> }) {
  return (
    <div className="heat" style={{ gridTemplateColumns: `minmax(70px, auto) repeat(${labels.length}, minmax(52px, 1fr))` }} role="table" aria-label="Correlation of daily returns">
      <div className="hd" role="columnheader" />
      {labels.map((l) => <div key={`h-${l}`} className="hd" role="columnheader">{l}</div>)}
      {labels.map((row) => (
        <Row key={row} row={row} labels={labels} matrix={matrix} />
      ))}
    </div>
  );
}

function Row({ row, labels, matrix }: { row: string; labels: string[]; matrix: Record<string, Record<string, number | null>> }) {
  return (
    <>
      <div className="hd" role="rowheader" style={{ textAlign: "left" }}>{row}</div>
      {labels.map((col) => {
        const v = matrix[row]?.[col];
        const strength = v === null || v === undefined ? 0 : Math.min(Math.abs(v), 1) * 85;
        const bg = v === null || v === undefined ? "var(--surface-2)" : `color-mix(in srgb, var(${v >= 0 ? "--up" : "--down"}) ${strength}%, var(--surface-2))`;
        return <div key={col} role="cell" className="num" style={{ background: bg }}>{v === null || v === undefined ? "–" : v.toFixed(2)}</div>;
      })}
    </>
  );
}

/** Signed bars around a centre line: used for sector returns. */
export function SignedBars({ rows, digits = 1 }: { rows: { label: string; value: number | null | undefined; note?: string }[]; digits?: number }) {
  const max = Math.max(1, ...rows.map((r) => Math.abs(r.value ?? 0)));
  return (
    <div role="list">
      {rows.map((r) => {
        const v = r.value ?? 0;
        const width = (Math.abs(v) / max) * 50;
        return (
          <div className="hbar" role="listitem" key={r.label} title={r.note}>
            <span>{r.label}</span>
            <span className="track" aria-hidden="true">
              <i style={{ width: `${width}%`, left: v >= 0 ? "50%" : `${50 - width}%`, background: v >= 0 ? "var(--up)" : "var(--down)" }} />
            </span>
            <span className={`num right ${v > 0 ? "up" : v < 0 ? "down" : "flat"}`}>{formatPct(r.value, digits, true)}</span>
          </div>
        );
      })}
    </div>
  );
}
