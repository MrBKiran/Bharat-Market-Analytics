import { useQueries } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import { useCompare } from "../api/hooks";
import type { CompareStat, InstrumentDetail } from "../api/types";
import { LineChart, type LineData } from "../components/LineChart";
import { SymbolSelect } from "../components/SymbolSelect";
import { Banner, Card, Change, DataTable, Empty, ErrorBox, Loading } from "../components/ui";
import { Heatmap } from "../components/viz";
import { presetStart, type RangePreset } from "../lib/dates";
import { formatDate, formatINR, formatPct } from "../lib/format";

const MAX_SYMBOLS = 6;
const PRESETS: RangePreset[] = ["1Y", "3Y", "5Y", "10Y", "MAX"];

export function ComparePage() {
  const [symbols, setSymbols] = useState<string[]>(["RELIANCE", "TCS", "NIFTY_50"]);
  const [preset, setPreset] = useState<RangePreset>("3Y");
  const [custom, setCustom] = useState<{ start: string; end: string }>({ start: "", end: "" });

  // Date presets are measured back from the earliest "last bar" of the chosen symbols, so every one has data in the window.
  const details = useQueries({
    queries: symbols.map((s) => ({
      queryKey: ["instrument", s, "NSE"],
      queryFn: ({ signal }: { signal: AbortSignal }) => api.get<InstrumentDetail>(`/api/instruments/${s}`, { exchange: "NSE" }, signal),
      staleTime: 5 * 60_000,
    })),
  });
  const lastBars = details.map((d) => d.data?.coverage.daily?.last).filter(Boolean) as string[];
  const commonEnd = lastBars.length ? lastBars.reduce((a, b) => (a < b ? a : b)) : undefined;
  const range = useMemo(() => {
    if (preset === "CUSTOM") return { start: custom.start || undefined, end: custom.end || undefined };
    if (!commonEnd) return {};
    return { start: presetStart(preset, commonEnd), end: commonEnd.slice(0, 10) };
  }, [preset, custom, commonEnd]);

  const waiting = details.some((d) => d.isLoading);
  const compare = useCompare(symbols, range.start, range.end);
  const colorOf = (sym: string) => symbols.indexOf(sym);
  const series: LineData[] = useMemo(
    () => (compare.data ? compare.data.symbols.map((sym) => ({ id: sym, label: sym, color: colorOf(sym), points: compare.data!.rebased_series.map((p) => ({ ts: p.ts, value: p[sym] as number })) })) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [compare.data],
  );

  const add = (sym: string) => setSymbols((s) => (s.includes(sym) || s.length >= MAX_SYMBOLS ? s : [...s, sym]));
  const remove = (sym: string) => setSymbols((s) => s.filter((x) => x !== sym));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Compare</h1>
          <p>Stocks and indices over the same dates, rebased to 100 at the start.</p>
        </div>
      </div>

      <Card>
        <div className="controls">
          <SymbolSelect value="" clearOnSelect label={`Add a symbol (${symbols.length}/${MAX_SYMBOLS})`} onSelect={(i) => add(i.symbol)} />
          <div className="field-group">
            <span>Selected</span>
            <div className="row" aria-live="polite">
              {symbols.map((s) => (
                <span key={s} className="badge" style={{ borderColor: `var(--c${(colorOf(s) % 6) + 1})` }}>
                  {s} <button className="btn btn-ghost btn-sm" style={{ padding: "0 0.25rem" }} aria-label={`Remove ${s}`} onClick={() => remove(s)}>×</button>
                </span>
              ))}
            </div>
          </div>
          <div className="field-group">
            <span id="cmp-range">Date range</span>
            <div className="seg" role="group" aria-labelledby="cmp-range">
              {PRESETS.map((p) => <button key={p} aria-pressed={preset === p} onClick={() => setPreset(p)}>{p === "MAX" ? "All" : p}</button>)}
              <button aria-pressed={preset === "CUSTOM"} onClick={() => setPreset("CUSTOM")}>Custom</button>
            </div>
          </div>
          {preset === "CUSTOM" && (
            <>
              <label className="field">From<input className="input" type="date" value={custom.start} onChange={(e) => setCustom((c) => ({ ...c, start: e.target.value }))} /></label>
              <label className="field">To<input className="input" type="date" value={custom.end} onChange={(e) => setCustom((c) => ({ ...c, end: e.target.value }))} /></label>
            </>
          )}
        </div>
      </Card>

      {symbols.length < 2 ? (
        <Card><Empty title="Pick at least two symbols to compare" /></Card>
      ) : waiting || compare.isLoading ? (
        <Loading text="Comparing…" height={300} />
      ) : compare.isError ? (
        <ErrorBox error={compare.error} onRetry={() => compare.refetch()} title="Could not compare these symbols" />
      ) : compare.data ? (
        <>
          <Banner kind="info">
            {compare.data.period.bars.toLocaleString("en-IN")} trading days in common, {formatDate(compare.data.period.start)} to {formatDate(compare.data.period.end)} ({compare.data.period.years} years). {compare.data.note.charAt(0).toUpperCase() + compare.data.note.slice(1)}.
          </Banner>
          <Card title="Growth of ₹100" subtitle="Every series starts at 100 on the first common day">
            <LineChart series={series} format={(v) => formatINR(v, 1)} ariaLabel={`Rebased price comparison of ${symbols.join(", ")}`} height={380} />
          </Card>
          <Card title="Statistics">
            <DataTable<[string, CompareStat]>
              rows={Object.entries(compare.data.stats)}
              rowKey={([s]) => s}
              columns={[
                { key: "s", header: "Symbol", render: ([s]) => <strong style={{ color: `var(--c${(colorOf(s) % 6) + 1})` }}>{s}</strong>, sortValue: ([s]) => s },
                { key: "r", header: "Total return", align: "right", render: ([, x]) => <Change value={x.total_return_pct} />, sortValue: ([, x]) => x.total_return_pct },
                { key: "c", header: "CAGR", align: "right", render: ([, x]) => (x.cagr_pct === null ? "–" : <Change value={x.cagr_pct} />), sortValue: ([, x]) => x.cagr_pct },
                { key: "v", header: "Volatility (annual)", align: "right", render: ([, x]) => formatPct(x.volatility_pct, 1), sortValue: ([, x]) => x.volatility_pct },
                { key: "d", header: "Max drawdown", align: "right", render: ([, x]) => <Change value={x.max_drawdown_pct} />, sortValue: ([, x]) => x.max_drawdown_pct },
                { key: "b", header: "Best day", align: "right", render: ([, x]) => <Change value={x.best_day_pct} />, sortValue: ([, x]) => x.best_day_pct },
                { key: "w", header: "Worst day", align: "right", render: ([, x]) => <Change value={x.worst_day_pct} />, sortValue: ([, x]) => x.worst_day_pct },
              ]}
            />
          </Card>
          <Card title="Correlation of daily returns" subtitle="1.00 moves together, 0 unrelated, below 0 moves oppositely">
            <Heatmap labels={compare.data.symbols} matrix={compare.data.correlation} />
          </Card>
        </>
      ) : null}
    </>
  );
}
