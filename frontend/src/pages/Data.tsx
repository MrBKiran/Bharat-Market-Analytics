import { useQuery } from "@tanstack/react-query";
import { useRef, useState, type DragEvent, type FormEvent } from "react";
import { ApiError, api, uploadFile } from "../api/client";
import { useInvalidateAfterImport, useJobs, useStartProcessing, useStats } from "../api/hooks";
import type { Job, Paged, UploadResult } from "../api/types";
import { Badge, Banner, Card, DataTable, ErrorBox, Loading, ProgressBar, Spinner } from "../components/ui";
import { compactNumber, formatDateTime, formatNumber, formatRatio, titleCase } from "../lib/format";

const STEPS = [
  { step: "adjust", title: "Rebuild adjusted prices", text: "Recreates the clean, split-adjusted daily prices from the raw data." },
  { step: "indicators", title: "Recompute daily indicators", text: "RSI, MACD, moving averages, Bollinger bands and more for every instrument." },
  { step: "analytics", title: "Recompute market analytics", text: "Trend, breadth, sector, volatility and correlation analyses behind the Overview page." },
  { step: "audit", title: "Run a data-quality audit", text: "Checks for duplicates, gaps and outliers and stores a report." },
];

function jobBadge(status: Job["status"]) {
  return <Badge kind={status === "completed" ? "ok" : status === "failed" ? "bad" : status === "running" || status === "queued" ? "warn" : undefined}>{titleCase(status)}</Badge>;
}

const countsText = (c: Job["counts"]) => (c ? Object.entries(c).filter(([, v]) => typeof v === "number").slice(0, 4).map(([k, v]) => `${titleCase(k)}: ${formatNumber(v as number)}`).join(" · ") : "");

// ------------------------------------------------------------------------------------------------- upload
type Phase = "idle" | "uploading" | "importing" | "done" | "error";

/** What to tell the user after an upload. A dry run reports what WOULD happen, so it must never say "stored". */
export function uploadSummary(r: UploadResult): { title: string; detail: string } {
  const c = r.counts;
  if (r.status === "already_imported") return { title: "This exact file was imported before", detail: "Nothing was changed." };
  const took = r.duration_seconds !== undefined ? ` · ${r.duration_seconds}s` : "";
  if (r.status === "dry_run") {
    return {
      title: "Dry run finished: nothing was stored",
      detail: c ? `${formatNumber(c.read)} rows read · ${formatNumber(c.inserted)} would be stored · ${formatNumber(c.duplicates)} duplicates would be skipped · ${formatNumber(c.rejected)} would be rejected${took}` : "The file looks importable.",
    };
  }
  return {
    title: "Import finished",
    detail: c ? `${formatNumber(c.read)} rows read · ${formatNumber(c.inserted)} stored · ${formatNumber(c.duplicates)} duplicates skipped · ${formatNumber(c.rejected)} rejected${took}` : `Status: ${r.status}`,
  };
}

function UploadCard() {
  const invalidate = useInvalidateAfterImport();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [drag, setDrag] = useState(false);
  const [form, setForm] = useState({ symbol: "", exchange: "NSE", interval: "1d", instrument_type: "", auto_register: false, dry_run: true });
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState<unknown>(null);

  const pick = (f: File | undefined | null) => {
    setResult(null);
    setError(null);
    setPhase("idle");
    if (!f) return setFile(null);
    if (!/\.(csv|xlsx)$/i.test(f.name)) {
      setFile(null);
      setPhase("error");
      return setError(new Error("Only .csv and .xlsx files can be imported."));
    }
    setFile(f);
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDrag(false);
    pick(e.dataTransfer.files?.[0]);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    body.append("exchange", form.exchange);
    body.append("interval", form.interval);
    if (form.symbol.trim()) body.append("symbol", form.symbol.trim().toUpperCase());
    if (form.instrument_type) body.append("instrument_type", form.instrument_type);
    body.append("auto_register", String(form.auto_register));
    body.append("dry_run", String(form.dry_run));
    setPhase("uploading");
    setProgress(0);
    setResult(null);
    setError(null);
    try {
      const res = await uploadFile<UploadResult>("/api/imports/upload", body, (f) => {
        setProgress(f);
        if (f >= 1) setPhase("importing");
      });
      setResult(res);
      setPhase("done");
      invalidate();
    } catch (err) {
      setError(err);
      setPhase("error");
    }
  };

  const busy = phase === "uploading" || phase === "importing";
  return (
    <Card title="Import a price file" subtitle="A CSV or Excel file of daily or intraday bars. Duplicates are skipped and bad rows are rejected and counted, never silently dropped.">
      <form className="stack" onSubmit={submit}>
        <div
          className={`upload-zone ${drag ? "drag" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
        >
          <p style={{ marginTop: 0 }}>{file ? <><strong>{file.name}</strong> · {(file.size / 1024 / 1024).toFixed(2)} MB</> : "Drag a .csv or .xlsx file here, or"}</p>
          <button type="button" className="btn" onClick={() => inputRef.current?.click()}>Choose a file</button>
          <input ref={inputRef} type="file" accept=".csv,.xlsx" hidden onChange={(e) => pick(e.target.files?.[0])} aria-label="Price file" />
        </div>
        <div className="controls">
          <label className="field">Symbol<input className="input" value={form.symbol} placeholder="from the file name" onChange={(e) => setForm({ ...form, symbol: e.target.value })} /></label>
          <label className="field">Exchange
            <select className="select" value={form.exchange} onChange={(e) => setForm({ ...form, exchange: e.target.value })}><option>NSE</option><option>BSE</option></select>
          </label>
          <label className="field">Timeframe
            <select className="select" value={form.interval} onChange={(e) => setForm({ ...form, interval: e.target.value })}>
              {["1d", "1h", "30m", "15m", "5m", "1m"].map((i) => <option key={i}>{i}</option>)}
            </select>
          </label>
          <label className="field">Type
            <select className="select" value={form.instrument_type} onChange={(e) => setForm({ ...form, instrument_type: e.target.value })}>
              <option value="">Detect</option><option value="equity">Equity</option><option value="index">Index</option><option value="etf">ETF</option>
            </select>
          </label>
        </div>
        <div className="row">
          <label className="toggles"><label><input type="checkbox" checked={form.dry_run} onChange={(e) => setForm({ ...form, dry_run: e.target.checked })} /><span>Dry run (check only, store nothing)</span></label></label>
          <label className="toggles"><label><input type="checkbox" checked={form.auto_register} onChange={(e) => setForm({ ...form, auto_register: e.target.checked })} /><span>Create the instrument if it is new</span></label></label>
          <button className="btn btn-primary" disabled={!file || busy}>{busy ? "Working…" : form.dry_run ? "Check file" : "Import file"}</button>
        </div>
      </form>

      {phase === "uploading" && (
        <div style={{ marginTop: "1rem" }} role="status">
          <div className="spread"><span>Uploading…</span><span className="num">{Math.round(progress * 100)}%</span></div>
          <ProgressBar value={progress * 100} label="Upload progress" />
        </div>
      )}
      {phase === "importing" && (
        <div style={{ marginTop: "1rem" }} role="status" className="row"><Spinner /><span>Uploaded. The server is validating and storing the rows…</span></div>
      )}
      {phase === "error" && <div style={{ marginTop: "1rem" }}><ErrorBox error={error} title="The file was not imported" /></div>}
      {phase === "done" && result && (
        <div style={{ marginTop: "1rem" }}>
          <Banner kind="info" title={uploadSummary(result).title}>{uploadSummary(result).detail}</Banner>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------- processing
function ProcessingCard() {
  const start = useStartProcessing();
  const jobs = useJobs();
  const [pending, setPending] = useState<string | null>(null);
  const running = (jobs.data?.items ?? []).filter((j) => j.status === "running" || j.status === "queued");
  return (
    <Card title="Processing steps" subtitle="Long jobs run in the background; progress appears below. Each step runs one at a time.">
      <div className="grid cols-2">
        {STEPS.map((s) => (
          <div key={s.step} className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", flexWrap: "nowrap" }}>
            <div><strong>{s.title}</strong><div className="muted"><small>{s.text}</small></div></div>
            <button className="btn btn-sm" disabled={start.isPending && pending === s.step} onClick={() => { setPending(s.step); start.mutate(s.step); }}>
              {start.isPending && pending === s.step ? "Starting…" : "Run"}
            </button>
          </div>
        ))}
      </div>
      {start.isError && <div style={{ marginTop: "0.8rem" }}><ErrorBox error={start.error} title={`Could not start ${pending ?? "the step"}`} /></div>}
      {running.length > 0 && <p className="muted" style={{ marginBottom: 0 }} role="status"><Spinner /> {running.length} job(s) running…</p>}
    </Card>
  );
}

function JobsCard() {
  const jobs = useJobs();
  return (
    <Card title="Recent jobs" subtitle="Imports and processing steps, newest first (refreshes while a job runs)">
      {jobs.isLoading ? <Loading text="Loading jobs…" /> : jobs.isError ? <ErrorBox error={jobs.error} onRetry={() => jobs.refetch()} /> : (
        <DataTable<Job>
          rows={jobs.data!.items}
          rowKey={(j) => j.job_id}
          empty="No jobs yet."
          columns={[
            { key: "t", header: "Type", render: (j) => titleCase(String(j.params?.step ?? j.job_type)) },
            { key: "s", header: "Status", render: (j) => jobBadge(j.status) },
            { key: "p", header: "Progress", render: (j) => <div style={{ minWidth: 120 }}><ProgressBar value={j.progress} label={`${j.job_id} progress`} /></div> },
            { key: "w", header: "Started", render: (j) => formatDateTime(j.created_at) },
            { key: "c", header: "Result", render: (j) => (j.error ? <span className="down">{j.error}</span> : <span className="muted">{countsText(j.counts)}</span>) },
          ]}
        />
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------------------- reference data
function BenchmarksCard() {
  const q = useQuery({ queryKey: ["benchmarks"], queryFn: () => api.get<Paged<{ run_id: string; technology: string; operation: string; record_count: number; duration_seconds: number; records_per_second?: number; is_synthetic: boolean }>>("/api/benchmarks", { page_size: 60 }) });
  return (
    <Card title="Performance benchmarks" subtitle="Stored timings of the same jobs on pandas, MongoDB and Spark (real market data). More in Phase 14.">
      {q.isLoading ? <Loading text="Loading benchmarks…" /> : q.isError ? <ErrorBox error={q.error} onRetry={() => q.refetch()} /> : (
        <DataTable
          rows={q.data!.items}
          rowKey={(r) => `${r.run_id}:${r.technology}:${r.operation}:${r.record_count}`}
          empty="No benchmark has been run yet."
          columns={[
            { key: "o", header: "Operation", render: (r) => r.operation, sortValue: (r) => r.operation },
            { key: "t", header: "Technology", render: (r) => titleCase(r.technology), sortValue: (r) => r.technology },
            { key: "n", header: "Records", align: "right", render: (r) => compactNumber(r.record_count), sortValue: (r) => r.record_count },
            { key: "d", header: "Seconds", align: "right", render: (r) => formatRatio(r.duration_seconds, 2), sortValue: (r) => r.duration_seconds },
            { key: "r", header: "Records / second", align: "right", render: (r) => (r.records_per_second ? compactNumber(r.records_per_second) : "–"), sortValue: (r) => r.records_per_second },
            { key: "s", header: "Data", render: (r) => <Badge kind={r.is_synthetic ? "warn" : "ok"}>{r.is_synthetic ? "Synthetic" : "Real"}</Badge> },
          ]}
        />
      )}
    </Card>
  );
}

function CollectionsCard() {
  const stats = useStats();
  return (
    <Card title="Collections" subtitle="Document counts in MongoDB">
      {stats.isLoading ? <Loading text="Loading…" /> : stats.isError ? <ErrorBox error={stats.error} onRetry={() => stats.refetch()} /> : (
        <DataTable
          rows={Object.entries(stats.data!.collections)}
          rowKey={([n]) => n}
          columns={[
            { key: "n", header: "Collection", render: ([n]) => <span className="mono">{n}</span>, sortValue: ([n]) => n },
            { key: "c", header: "Documents", align: "right", render: ([, c]) => formatNumber(c), sortValue: ([, c]) => c },
          ]}
        />
      )}
    </Card>
  );
}

export function DataPage() {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Data &amp; imports</h1>
          <p>Add price files, rebuild the processed data and watch jobs run. Actions that change data need the API key when one is configured.</p>
        </div>
      </div>
      <UploadCard />
      <ProcessingCard />
      <JobsCard />
      <div className="grid cols-2">
        <CollectionsCard />
        <BenchmarksCard />
      </div>
    </>
  );
}

export { ApiError };
