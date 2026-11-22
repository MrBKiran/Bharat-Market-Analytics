import { useState } from "react";
import { useBenchmarkScaling } from "../api/hooks";
import type { BenchmarkJob, BenchmarkPoint, BenchmarkView } from "../api/types";
import { ExportMenu } from "../components/ExportMenu";
import { ScalingChart, shortNumber, type ChartSeries } from "../components/ScalingChart";
import { Badge, Banner, Card, DataTable, Empty, ErrorBox, Loading, Stat } from "../components/ui";
import { formatDateTime, formatNumber, titleCase } from "../lib/format";

type Mode = "synthetic" | "real";

const EMPTY_VIEW: BenchmarkView = { sizes: [], operations: [] };
const TECH: Record<string, { label: string; color: string }> = {
  pandas: { label: "pandas", color: "var(--c1)" },
  pyspark: { label: "PySpark", color: "var(--c3)" },
  mongodb_aggregation: { label: "MongoDB", color: "var(--c2)" },
};
const INGEST_COLOR: Record<string, string> = { ingest_parquet: "var(--c4)", ingest_mongodb_timeseries: "var(--c2)" };
const OPERATIONS: Record<string, string> = {
  ingest_parquet: "Write to Parquet files",
  ingest_mongodb_timeseries: "Insert into MongoDB (time-series)",
  query_symbol_window: "One symbol, five days (indexed)",
  query_scan_filter: "Filter scanning every bar",
  agg_daily_range_stats: "Daily range per symbol",
  agg_resample_1m_to_5m: "1-minute to 5-minute bars",
  intraday_resample_1m_to_5m: "1-minute to 5-minute bars",
  intraday_daily_range_stats: "Daily range per symbol",
  intraday_realised_volatility_5m: "Realised volatility (5-minute)",
  daily_returns_volatility: "Daily returns and volatility",
  return_correlation: "Return correlation",
  market_trend_200dma: "Market trend (200-day)",
};
const opLabel = (op: string) => OPERATIONS[op] ?? titleCase(op);
const techLabel = (t: string) => TECH[t]?.label ?? titleCase(t);

/** 0.004 -> "4 ms", 1.5 -> "1.5 s": one formatter for axis ticks and tooltips. */
export function formatSeconds(v: number): string {
  if (v < 1) return `${Math.round(v * 1000)} ms`;
  if (v < 10) return `${+v.toFixed(2)} s`;
  if (v < 100) return `${+v.toFixed(1)} s`;
  return `${Math.round(v)} s`;
}
const formatMb = (v: number) => (v >= 1024 ? `${+(v / 1024).toFixed(1)} GB` : `${+v.toFixed(v < 10 ? 1 : 0)} MB`);
const formatRate = (v: number) => `${shortNumber(v)}/s`;

function techSeries(job: BenchmarkJob | undefined, pick: (p: BenchmarkPoint) => number | null | undefined): ChartSeries[] {
  return (job?.series ?? []).map((s) => ({
    name: techLabel(s.technology),
    color: TECH[s.technology]?.color ?? "var(--c4)",
    points: s.points.flatMap((p) => { const y = pick(p); return y == null ? [] : [{ x: p.records, y }]; }),
  })).filter((s) => s.points.length > 0);
}

function ingestionSeries(jobs: BenchmarkJob[], pick: (p: BenchmarkPoint) => number | null | undefined): ChartSeries[] {
  return jobs.map((job) => ({
    name: opLabel(job.operation),
    color: INGEST_COLOR[job.operation] ?? "var(--c5)",
    points: (job.series[0]?.points ?? []).flatMap((p) => { const y = pick(p); return y == null ? [] : [{ x: p.records, y }]; }),
  })).filter((s) => s.points.length > 0);
}

function JobSelect({ label, jobs, value, onChange }: { label: string; jobs: BenchmarkJob[]; value: string; onChange: (op: string) => void }) {
  if (jobs.length < 2) return null;
  return (
    <label className="field" style={{ maxWidth: 320 }}>{label}
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        {jobs.map((j) => <option key={j.operation} value={j.operation}>{opLabel(j.operation)}</option>)}
      </select>
    </label>
  );
}

function NothingHere({ mode, what }: { mode: Mode; what: string }) {
  return mode === "synthetic"
    ? <Empty title={`No ${what} measured yet`}>Run <span className="mono">scripts\performance.py run</span> to measure it.</Empty>
    : <Empty title={`No ${what} measured on real data`}>The real-data benchmark times the jobs on the data already in the database. Switch to the synthetic data to see ingestion and storage.</Empty>;
}

export function PerformancePage() {
  const q = useBenchmarkScaling();
  const [mode, setMode] = useState<Mode>("synthetic");
  const [aggOp, setAggOp] = useState("");
  const [queryOp, setQueryOp] = useState("");
  const [metric, setMetric] = useState<"seconds" | "rate">("seconds");
  const [size, setSize] = useState<number | null>(null);

  if (q.isLoading) return <Loading text="Loading the benchmark results…" height={240} />;
  if (q.isError) return <ErrorBox error={q.error} onRetry={() => q.refetch()} title="Could not load the benchmark results" />;
  const data = q.data!;
  const view = data[mode] ?? EMPTY_VIEW;
  const ingestion = view.operations.filter((j) => j.kind === "ingestion");
  const queries = view.operations.filter((j) => j.kind === "query");
  const aggregations = view.operations.filter((j) => j.kind === "aggregation");
  const aggJob = aggregations.find((j) => j.operation === aggOp) ?? aggregations[0];
  const queryJob = queries.find((j) => j.operation === queryOp) ?? queries[0];
  const shownSize = size !== null && view.sizes.includes(size) ? size : view.sizes[view.sizes.length - 1];
  const runs = mode === "synthetic" ? data.synthetic.runs : [];
  const notMeasured = runs.flatMap((r) => r.not_measured.map((n) => ({ ...n, records: r.records })));
  const machine = runs.length ? runs[runs.length - 1].environment : null;
  const points = view.operations.reduce((n, j) => n + j.series.reduce((m, s) => m + s.points.length, 0), 0);

  const rows = view.operations.flatMap((job) => job.series.flatMap((s) => s.points.filter((p) => p.records === shownSize).map((p) => ({ job, technology: s.technology, p }))));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Performance</h1>
          <p>How ingestion, storage, queries and aggregations scale from 100 thousand to 10 million records, on pandas, MongoDB and Spark.</p>
        </div>
        <div className="seg" role="group" aria-label="Which data">
          <button aria-pressed={mode === "synthetic"} onClick={() => setMode("synthetic")}>Synthetic data</button>
          <button aria-pressed={mode === "real"} onClick={() => setMode("real")}>Real market data</button>
        </div>
      </div>

      {mode === "synthetic" ? (
        <Banner kind="warn" title="Synthetic benchmark data">
          These tests use generated 1-minute bars (made-up symbols, random prices, a separate database) so that 5 and 10 million records can be tested. They show how the technologies scale, not
          how the market behaved. Nothing here is mixed into the real market data.
        </Banner>
      ) : (
        <Banner kind="info" title="Real market data">
          These are the timings on the actual NSE and BSE bars in the database (at most 5.4 million rows, measured in Phase 8). The 5 and 10 million sizes exist only as synthetic data.
        </Banner>
      )}

      <div className="grid cols-4">
        <Card><Stat label="Data sizes" value={view.sizes.length ? view.sizes.map(shortNumber).join(", ") : "–"} sub={<Badge kind={mode === "synthetic" ? "warn" : "ok"}>{mode === "synthetic" ? "Synthetic" : "Real"}</Badge>} /></Card>
        <Card><Stat label="Timings" value={formatNumber(points)} sub="job, technology and size" /></Card>
        <Card><Stat label="Not measured" value={mode === "synthetic" ? formatNumber(notMeasured.length) : "–"} sub={mode === "synthetic" ? "failed or skipped (listed below)" : "see the synthetic data"} /></Card>
        <Card><Stat label="Machine" value={machine ? `${machine.cpu_cores} cores` : "–"} sub={machine ? `${machine.ram_gb} GB memory` : "one machine, one session"} /></Card>
      </div>

      {mode === "synthetic" && notMeasured.length > 0 && (
        <Banner kind="warn" title="Some jobs could not be measured">
          <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
            {notMeasured.map((n) => (
              <li key={`${n.records}:${n.operation}:${n.technology}`}>
                {shortNumber(n.records)} records, {opLabel(n.operation)} on {techLabel(n.technology)}: {n.status}. {n.error}
              </li>
            ))}
          </ul>
        </Banner>
      )}

      <div className="grid cols-2">
        <Card title="Data ingestion speed" subtitle="Rows written per second. Higher is better.">
          {ingestion.length ? (
            <ScalingChart title="Rows written per second by data size" series={ingestionSeries(ingestion, (p) => p.records_per_second)} xLabel="Records" yLabel="Rows per second"
              formatY={formatRate} logY footnote="Both axes are logarithmic. The MongoDB figure includes building the documents but not the importer's validation and de-duplication." />
          ) : <NothingHere mode={mode} what="ingestion" />}
        </Card>
        <Card title="Storage used" subtitle="What the data occupies on disk. Lower is better.">
          {ingestion.some((j) => j.series[0]?.points.some((p) => p.storage_mb != null)) ? (
            <ScalingChart title="Storage in megabytes by data size" series={ingestionSeries(ingestion, (p) => p.storage_mb)} xLabel="Records" yLabel="Megabytes" formatY={formatMb} logY
              footnote="MongoDB: compressed data plus its index. Parquet: snappy-compressed files. Both axes are logarithmic." />
          ) : <NothingHere mode={mode} what="storage" />}
        </Card>

        <Card title="Aggregation" subtitle="Grouping and combining every bar."
          actions={<div className="seg" role="group" aria-label="Metric">
            <button aria-pressed={metric === "seconds"} onClick={() => setMetric("seconds")}>Time</button>
            <button aria-pressed={metric === "rate"} onClick={() => setMetric("rate")}>Rows per second</button>
          </div>}>
          {aggJob ? (
            <div className="stack">
              <JobSelect label="Job" jobs={aggregations} value={aggJob.operation} onChange={setAggOp} />
              {metric === "seconds"
                ? <ScalingChart title={`${opLabel(aggJob.operation)}: time by data size`} series={techSeries(aggJob, (p) => p.seconds)} xLabel="Records" yLabel="Time" formatY={formatSeconds} logY
                    footnote="Lower is better. Both axes are logarithmic: a straight line means time grows in proportion to the data." />
                : <ScalingChart title={`${opLabel(aggJob.operation)}: rows per second by data size`} series={techSeries(aggJob, (p) => p.records_per_second)} xLabel="Records" yLabel="Rows per second" formatY={formatRate}
                    footnote="Higher is better. A line that falls as the data grows means the technology slows down at scale." />}
            </div>
          ) : <NothingHere mode={mode} what="aggregations" />}
        </Card>
        <Card title="Query time" subtitle="Finding rows. Lower is better.">
          {queryJob ? (
            <div className="stack">
              <JobSelect label="Query" jobs={queries} value={queryJob.operation} onChange={setQueryOp} />
              <ScalingChart title={`${opLabel(queryJob.operation)}: time by data size`} series={techSeries(queryJob, (p) => p.seconds)} xLabel="Records" yLabel="Time" formatY={formatSeconds} logY
                footnote={queryJob.operation === "query_symbol_window" ? "MongoDB uses its index; pandas and Spark read one symbol's Parquet file. Both axes are logarithmic." : "Both axes are logarithmic."} />
            </div>
          ) : <NothingHere mode={mode} what="queries" />}
        </Card>
      </div>

      <Card title="Time, CPU and memory" subtitle="Every job at one data size. Memory is the peak of the processes that engine used (MongoDB includes the database server), so compare it with care."
        actions={<>
          {view.sizes.length > 1 && (
            <div className="seg" role="group" aria-label="Data size">
              {view.sizes.map((s) => <button key={s} aria-pressed={s === shownSize} onClick={() => setSize(s)}>{shortNumber(s)}</button>)}
            </div>
          )}
          <ExportMenu report="benchmarks" label="Benchmarks" tables={["benchmarks", "fastest", "collections"]} params={{}} />
        </>}>
        <DataTable
          rows={rows}
          rowKey={(r) => `${r.job.operation}:${r.technology}`}
          empty="No timings at this size."
          columns={[
            { key: "job", header: "Job", render: (r) => opLabel(r.job.operation), sortValue: (r) => opLabel(r.job.operation) },
            { key: "tech", header: "Technology", render: (r) => (r.job.kind === "ingestion" ? "–" : techLabel(r.technology)), sortValue: (r) => r.technology },
            { key: "s", header: "Time", align: "right", render: (r) => formatSeconds(r.p.seconds), sortValue: (r) => r.p.seconds },
            { key: "rps", header: "Rows / second", align: "right", render: (r) => (r.p.records_per_second ? formatNumber(r.p.records_per_second) : "–"), sortValue: (r) => r.p.records_per_second },
            { key: "cpu", header: "CPU cores", align: "right", render: (r) => (r.p.cpu_percent == null ? "–" : (r.p.cpu_percent / 100).toFixed(1)), sortValue: (r) => r.p.cpu_percent },
            { key: "mem", header: "Peak memory", align: "right", render: (r) => (r.p.memory_mb == null ? "–" : formatMb(r.p.memory_mb)), sortValue: (r) => r.p.memory_mb },
            { key: "ok", header: "Answers", render: (r) => <Badge kind={r.p.answers_match ? "ok" : "bad"}>{r.p.answers_match ? "Same" : "Different"}</Badge> },
            { key: "when", header: "Measured", render: (r) => formatDateTime(r.p.measured_at), sortValue: (r) => r.p.measured_at },
          ]}
        />
      </Card>
      <p className="muted"><small>{data.notes.join(" ")}</small></p>
    </>
  );
}
