// Small building blocks used by every page: loading, error and empty states, cards, stats, tables and paging.
import { useMemo, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import { arrowOf, toneOf } from "../lib/format";

export function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

export function Loading({ text = "Loading…", height }: { text?: string; height?: number }) {
  return (
    <div className="loading-block" role="status" aria-live="polite" style={height ? { minHeight: height } : undefined}>
      <Spinner />
      <span>{text}</span>
    </div>
  );
}

/** Show what went wrong in plain words, with the request id to quote and a way to try again. */
export function ErrorBox({ error, onRetry, title = "Something went wrong" }: { error: unknown; onRetry?: () => void; title?: string }) {
  const err = error instanceof ApiError ? error : null;
  const message = err?.message ?? (error instanceof Error ? error.message : String(error));
  let hint = "";
  if (err?.code === "network_error") hint = "Start the API: .venv\\Scripts\\python -m uvicorn app.main:app --app-dir backend";
  else if (err?.status === 401) hint = "This action needs the API key. Click “API key” in the top bar and enter the value of API_KEY from your .env file.";
  else if (err?.status === 403) hint = "The server has write actions disabled (production mode without an API_KEY).";
  else if (err?.status === 429) hint = "Too many requests: wait a few seconds and try again.";
  else if (err?.status === 503) hint = "MongoDB is not reachable. Start the MongoDB service.";
  return (
    <div className="banner error" role="alert">
      <span aria-hidden="true">⚠</span>
      <div className="body">
        <strong>{title}</strong>
        {message}
        {hint && <div><small>{hint}</small></div>}
        {err?.requestId && <div><small>Request id: <span className="mono">{err.requestId}</span></small></div>}
      </div>
      {onRetry && <button className="btn btn-sm" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children}
    </div>
  );
}

export function Banner({ kind = "info", title, children }: { kind?: "info" | "warn" | "error"; title?: string; children?: ReactNode }) {
  return (
    <div className={`banner ${kind}`} role={kind === "error" ? "alert" : "note"}>
      <span aria-hidden="true">{kind === "info" ? "ℹ" : "⚠"}</span>
      <div className="body">
        {title && <strong>{title}</strong>}
        {children}
      </div>
    </div>
  );
}

export function Card({ title, subtitle, actions, children, className = "" }: { title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header>
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p>{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub, delta }: { label: string; value: ReactNode; sub?: ReactNode; delta?: number | null }) {
  const tone = toneOf(delta);
  return (
    <div className="stat">
      <span className="label">{label}</span>
      <span className="value">{value}</span>
      {sub !== undefined && (
        <span className={`sub ${delta !== undefined ? tone : "muted"}`}>
          {delta !== undefined && delta !== null && <span aria-hidden="true">{arrowOf(delta)} </span>}
          {sub}
        </span>
      )}
    </div>
  );
}

export function Badge({ kind, children }: { kind?: "ok" | "bad" | "warn"; children: ReactNode }) {
  return <span className={`badge ${kind ?? ""}`}>{children}</span>;
}

export function ProgressBar({ value, label }: { value: number; label?: string }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)} aria-label={label}>
      <div style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Pagination({ page, pages, total, onPage, noun = "rows" }: { page: number; pages: number; total: number; onPage: (p: number) => void; noun?: string }) {
  if (!total) return null;
  return (
    <nav className="pager" aria-label="Pagination">
      <span>{total.toLocaleString("en-IN")} {noun}</span>
      <span className="row">
        <button className="btn btn-sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>← Previous</button>
        <span>Page {page} of {Math.max(pages, 1)}</span>
        <button className="btn btn-sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next →</button>
      </span>
    </nav>
  );
}

export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T) => ReactNode;
  align?: "right";
  sortValue?: (row: T) => number | string | null | undefined;
}

/** A table that scrolls sideways on a phone, and sorts by any column that provides a sortValue. */
export function DataTable<T>({ columns, rows, rowKey, empty = "Nothing to show." }: { columns: Column<T>[]; rows: T[]; rowKey: (row: T) => string; empty?: string }) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sort?.key);
    if (!sort || !col?.sortValue) return rows;
    const get = col.sortValue;
    return [...rows].sort((a, b) => {
      const x = get(a), y = get(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
    });
  }, [rows, columns, sort]);
  if (!rows.length) return <Empty title={empty} />;
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            {columns.map((c) => {
              const sortable = !!c.sortValue;
              const active = sort?.key === c.key;
              return (
                <th
                  key={c.key}
                  className={`${c.align === "right" ? "r" : ""} ${sortable ? "sortable" : ""}`}
                  aria-sort={active ? (sort!.dir === 1 ? "ascending" : "descending") : undefined}
                  onClick={sortable ? () => setSort(active ? { key: c.key, dir: (sort!.dir * -1) as 1 | -1 } : { key: c.key, dir: -1 }) : undefined}
                >
                  {c.header}
                  {active && <span aria-hidden="true"> {sort!.dir === 1 ? "↑" : "↓"}</span>}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((c) => (
                <td key={c.key} className={c.align === "right" ? "r num" : ""}>{c.render(row)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Green or red, with an arrow so the direction never depends on colour alone. */
export function Change({ value, digits = 2, suffix = "%" }: { value: number | null | undefined; digits?: number; suffix?: string }) {
  if (value === null || value === undefined || !Number.isFinite(value)) return <span className="flat">–</span>;
  const text = `${value > 0 ? "+" : ""}${value.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${suffix}`;
  return (
    <span className={`num ${toneOf(value)}`}>
      <span aria-hidden="true">{arrowOf(value)} </span>
      {text}
    </span>
  );
}
