import { useState } from "react";
import { downloadReport, type DownloadedReport, type Query } from "../api/client";
import { formatNumber } from "../lib/format";
import { ErrorBox, Spinner } from "./ui";

type Format = "csv" | "xlsx" | "pdf";
const FORMATS: { id: Format; label: string; hint: string }[] = [
  { id: "csv", label: "CSV", hint: "plain data for any tool" },
  { id: "xlsx", label: "Excel", hint: "formatted workbook with charts" },
  { id: "pdf", label: "PDF", hint: "a readable report" },
];

const size = (bytes: number) => (bytes >= 1_048_576 ? `${(bytes / 1_048_576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);

interface Props {
  /** report id from GET /api/reports, e.g. "prices" */
  report: string;
  /** the report's parameters; blank values are left out */
  params: Query;
  label?: string;
  disabled?: boolean;
  disabledReason?: string;
  /** a report with several tables lets the CSV choose one (or all of them in a ZIP) */
  tables?: string[];
}

/** Three buttons that build a report on the server and save it: CSV, Excel and PDF, with progress and the server's error message. */
export function ExportMenu({ report, params, label = "Download", disabled = false, disabledReason, tables }: Props) {
  const [busy, setBusy] = useState<Format | null>(null);
  const [done, setDone] = useState<DownloadedReport | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [table, setTable] = useState("");

  const run = async (format: Format) => {
    setBusy(format);
    setError(null);
    setDone(null);
    try {
      setDone(await downloadReport(report, format, params, format === "csv" && table ? table : undefined));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="export" role="group" aria-label={label}>
      <div className="row" style={{ gap: "0.5rem" }}>
        <span className="muted" style={{ fontWeight: 600 }}>{label}</span>
        <div className="seg">
          {FORMATS.map((f) => (
            <button
              key={f.id}
              type="button"
              disabled={disabled || busy !== null}
              title={disabled ? disabledReason : f.hint}
              aria-label={`${label} as ${f.label}`}
              onClick={() => run(f.id)}
            >
              {busy === f.id ? <Spinner /> : null} {f.label}
            </button>
          ))}
        </div>
        {tables && tables.length > 1 && (
          <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: "0.4rem" }}>
            CSV table
            <select className="select" value={table} onChange={(e) => setTable(e.target.value)} disabled={busy !== null}>
              <option value="">{tables[0]} (default)</option>
              {tables.slice(1).map((t) => <option key={t} value={t}>{t}</option>)}
              <option value="all">All tables (ZIP)</option>
            </select>
          </label>
        )}
      </div>
      <div role="status" aria-live="polite" className="muted" style={{ fontSize: "0.82rem", minHeight: "1.2em" }}>
        {busy && `Preparing the ${FORMATS.find((f) => f.id === busy)!.label} file…`}
        {!busy && done && `Saved ${done.filename} (${size(done.bytes)}${done.rows !== null ? `, ${formatNumber(done.rows)} rows` : ""}).`}
      </div>
      {error !== null && <ErrorBox error={error} title="The report could not be built" />}
    </div>
  );
}
