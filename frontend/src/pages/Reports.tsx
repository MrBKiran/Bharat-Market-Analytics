import { useMemo, useState, type ReactNode } from "react";
import { useBacktests, useInstrument, useReportCatalog } from "../api/hooks";
import type { Interval } from "../api/types";
import { INTERVALS } from "../api/types";
import { availableIntervals } from "../components/ControlsBar";
import { ExportMenu } from "../components/ExportMenu";
import { SymbolSelect } from "../components/SymbolSelect";
import { Banner, Card, ErrorBox, Loading } from "../components/ui";
import { formatDateTime, titleCase } from "../lib/format";
import { useControls } from "../state/controls";

const COMMON_INDICATORS = ["rsi_14", "macd", "macd_signal", "ema_20", "ema_50", "sma_200", "atr_14", "bb_upper", "bb_lower", "stoch_k"];

function DateRange({ start, end, onChange }: { start: string; end: string; onChange: (s: string, e: string) => void }) {
  return (
    <>
      <label className="field">From<input className="input" type="date" value={start} max={end || undefined} onChange={(e) => onChange(e.target.value, end)} /></label>
      <label className="field">To<input className="input" type="date" value={end} min={start || undefined} onChange={(e) => onChange(start, e.target.value)} /></label>
    </>
  );
}

export function ReportsPage() {
  const catalog = useReportCatalog();
  const { controls, set } = useControls();
  const detail = useInstrument(controls.symbol, controls.exchange);
  const available = availableIntervals(detail.data);

  const [range, setRange] = useState({ start: "", end: "" });
  const [interval, setInterval] = useState<Interval>("1d");
  const [indicators, setIndicators] = useState<string[]>([]);
  const [compare, setCompare] = useState<string[]>(["RELIANCE", "TCS", "NIFTY_50"]);
  const [cmpRange, setCmpRange] = useState({ start: "", end: "" });
  const [perfSymbols, setPerfSymbols] = useState("");
  const [runId, setRunId] = useState("");
  const [technology, setTechnology] = useState("");
  const runs = useBacktests({ page: 1, page_size: 50 });

  const info = useMemo(() => Object.fromEntries((catalog.data?.reports ?? []).map((r) => [r.id, r])), [catalog.data]);
  const day = (s: string) => (s ? `${s}T00:00:00Z` : undefined);

  const card = (id: string, body: ReactNode, menu: ReactNode) => (
    <Card key={id} title={info[id]?.title ?? titleCase(id)} subtitle={info[id]?.description}>
      <div className="stack">
        {body}
        {menu}
      </div>
    </Card>
  );

  if (catalog.isLoading) return <Loading text="Loading the report list…" height={240} />;
  if (catalog.isError) return <ErrorBox error={catalog.error} onRetry={() => catalog.refetch()} title="Could not load the report list" />;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Reports &amp; exports</h1>
          <p>Download data and analysis as CSV (plain data), Excel (formatted, with charts) or PDF (a readable report).</p>
        </div>
      </div>
      <Banner kind="info" title="What the files contain">
        CSV and Excel carry every row (up to {catalog.data!.limits.max_rows_per_table.toLocaleString("en-IN")} per table for CSV, {catalog.data!.limits.max_rows_excel.toLocaleString("en-IN")} for Excel); a PDF shows the newest rows and says so. Rupee amounts use Indian grouping in Excel and
        PDF (₹12,34,567.00; the PDF font writes “Rs”) and are plain numbers in CSV. Prices are split-adjusted, not dividend-adjusted. Every report states its assumptions and warnings.
      </Banner>

      <div className="grid cols-2">
        {card("prices",
          <div className="controls">
            <SymbolSelect value={controls.symbol} exchange={controls.exchange} onSelect={(i) => set({ symbol: i.symbol, exchange: i.exchange })} />
            <label className="field">Timeframe
              <select className="select" value={interval} onChange={(e) => setInterval(e.target.value as Interval)}>
                {INTERVALS.map((i) => <option key={i.value} value={i.value} disabled={available ? !available.has(i.value) : false}>{i.label}</option>)}
              </select>
            </label>
            <DateRange start={range.start} end={range.end} onChange={(start, end) => setRange({ start, end })} />
          </div>,
          <ExportMenu report="prices" label="Prices" params={{ symbol: controls.symbol, exchange: controls.exchange, interval, start: day(range.start), end: day(range.end) }}
            disabled={!controls.symbol} disabledReason="Choose a symbol" />)}

        {card("indicators",
          <div className="stack">
            <div className="controls">
              <SymbolSelect value={controls.symbol} exchange={controls.exchange} onSelect={(i) => set({ symbol: i.symbol, exchange: i.exchange })} />
              <DateRange start={range.start} end={range.end} onChange={(start, end) => setRange({ start, end })} />
            </div>
            <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
              <legend className="muted" style={{ fontWeight: 600, fontSize: "0.8rem" }}>Indicators (none ticked means all of them)</legend>
              <div className="toggles">
                {COMMON_INDICATORS.map((n) => (
                  <label key={n}>
                    <input type="checkbox" checked={indicators.includes(n)} onChange={(e) => setIndicators((c) => (e.target.checked ? [...c, n] : c.filter((x) => x !== n)))} />
                    <span>{n.toUpperCase().replace(/_/g, " ")}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          </div>,
          <ExportMenu report="indicators" label="Indicators" params={{ symbol: controls.symbol, exchange: controls.exchange, interval, names: indicators.join(",") || undefined, start: day(range.start), end: day(range.end) }}
            disabled={!controls.symbol} disabledReason="Choose a symbol" />)}

        {card("performance",
          <label className="field" style={{ maxWidth: 420 }}>Limit to symbols (optional, comma-separated)
            <input className="input" value={perfSymbols} placeholder="e.g. RELIANCE,TCS" onChange={(e) => setPerfSymbols(e.target.value)} />
          </label>,
          <ExportMenu report="performance" label="Performance" params={{ symbols: perfSymbols.replace(/\s+/g, "") || undefined }} />)}

        {card("comparison",
          <div className="stack">
            <div className="controls">
              <SymbolSelect value="" clearOnSelect label={`Add a symbol (${compare.length}/10)`} onSelect={(i) => setCompare((c) => (c.includes(i.symbol) || c.length >= 10 ? c : [...c, i.symbol]))} />
              <DateRange start={cmpRange.start} end={cmpRange.end} onChange={(start, end) => setCmpRange({ start, end })} />
            </div>
            <div className="row" aria-live="polite">
              {compare.map((s) => (
                <span key={s} className="badge">{s} <button className="btn btn-ghost btn-sm" style={{ padding: "0 0.25rem" }} aria-label={`Remove ${s}`} onClick={() => setCompare((c) => c.filter((x) => x !== s))}>×</button></span>
              ))}
            </div>
          </div>,
          <ExportMenu report="comparison" label="Comparison" tables={info.comparison?.tables} disabled={compare.length < 2} disabledReason="Choose at least two symbols"
            params={{ symbols: compare.join(","), start: day(cmpRange.start), end: day(cmpRange.end) }} />)}

        {card("sectors", <p className="muted" style={{ margin: 0 }}>Sector returns over 1 day to year to date, and the stocks in each sector. No options.</p>,
          <ExportMenu report="sectors" label="Sectors" tables={info.sectors?.tables} params={{}} />)}

        {card("backtest",
          <div className="stack">
            {runs.isLoading ? <Loading text="Loading saved runs…" /> : runs.isError ? <ErrorBox error={runs.error} /> : (
              <label className="field" style={{ maxWidth: 520 }}>Saved backtest
                <select className="select" value={runId} onChange={(e) => setRunId(e.target.value)}>
                  <option value="">Choose a saved run…</option>
                  {runs.data!.items.map((r) => (
                    <option key={r.run_id} value={r.run_id}>{titleCase(r.strategy)} on {r.symbol} ({r.interval}), saved {formatDateTime(r.created_at)}</option>
                  ))}
                </select>
              </label>
            )}
            <p className="muted" style={{ margin: 0 }}><small>Only saved runs can be exported. Tick “Save this result” on the Backtesting page.</small></p>
          </div>,
          <ExportMenu report="backtest" label="Backtest" tables={info.backtest?.tables} disabled={!runId} disabledReason="Choose a saved backtest" params={{ run_id: runId || undefined }} />)}

        {card("strategies", <p className="muted" style={{ margin: 0 }}>Median results of every strategy across the stored comparison run, next to buy and hold.</p>,
          <ExportMenu report="strategies" label="Strategies" params={{}} />)}

        {card("benchmarks",
          <label className="field" style={{ maxWidth: 300 }}>Technology (optional)
            <select className="select" value={technology} onChange={(e) => setTechnology(e.target.value)}>
              <option value="">All</option><option value="pandas">pandas</option><option value="pyspark">PySpark</option><option value="mongodb_aggregation">MongoDB aggregation</option>
            </select>
          </label>,
          <ExportMenu report="benchmarks" label="Database report" tables={info.benchmarks?.tables} params={{ technology: technology || undefined }} />)}
      </div>
    </>
  );
}
