import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useInstruments, useSearchPrices, useSectors, type SearchParams } from "../api/hooks";
import type { Exchange, Instrument, PriceHit } from "../api/types";
import { SymbolSelect, useDebounced } from "../components/SymbolSelect";
import { Badge, Banner, Card, DataTable, ErrorBox, Loading, Pagination } from "../components/ui";
import { compactNumber, formatDate, formatINR, titleCase } from "../lib/format";

type Tab = "instruments" | "prices";

// ------------------------------------------------------------------------------------------------ instruments
function AddInstrument({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState({ symbol: "", name: "", instrument_type: "equity", exchange: "NSE", sector: "" });
  const add = useMutation({
    mutationFn: () => api.post<Instrument>("/api/instruments", { ...form, sector: form.sector || undefined }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["instruments"] });
      qc.invalidateQueries({ queryKey: ["sectors"] });
      setForm({ symbol: "", name: "", instrument_type: "equity", exchange: "NSE", sector: "" });
      onDone();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate();
  };
  return (
    <form className="stack" onSubmit={submit} aria-label="Add an instrument">
      <div className="controls">
        <label className="field">Symbol<input className="input" required maxLength={30} value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value.toUpperCase() })} placeholder="e.g. WIPRO" /></label>
        <label className="field">Name<input className="input" required maxLength={200} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Company or index name" /></label>
        <label className="field">Type
          <select className="select" value={form.instrument_type} onChange={(e) => setForm({ ...form, instrument_type: e.target.value })}>
            <option value="equity">Equity</option><option value="index">Index</option><option value="etf">ETF</option>
          </select>
        </label>
        <label className="field">Exchange
          <select className="select" value={form.exchange} onChange={(e) => setForm({ ...form, exchange: e.target.value })}><option>NSE</option><option>BSE</option></select>
        </label>
        <label className="field">Sector (optional)<input className="input" maxLength={100} value={form.sector} onChange={(e) => setForm({ ...form, sector: e.target.value })} /></label>
        <button className="btn btn-primary" disabled={add.isPending}>{add.isPending ? "Adding…" : "Add instrument"}</button>
      </div>
      {add.isError && <ErrorBox error={add.error} title="Could not add the instrument" />}
    </form>
  );
}

function Instruments() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [exchange, setExchange] = useState<Exchange | "">("");
  const [sector, setSector] = useState("");
  const [page, setPage] = useState(1);
  const [adding, setAdding] = useState(false);
  const search = useDebounced(q.trim(), 250);
  const sectors = useSectors();
  const list = useInstruments({ q: search || undefined, instrument_type: type || undefined, exchange, sector: sector || undefined, page, page_size: 25 });
  const toggle = useMutation({
    mutationFn: (i: Instrument) => api.patch<Instrument>(`/api/instruments/${i.symbol}?exchange=${i.exchange}`, { active: !i.active }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["instruments"] }),
  });
  const reset = (fn: () => void) => { fn(); setPage(1); };

  return (
    <div className="stack">
      <Card>
        <div className="controls">
          <label className="field" style={{ minWidth: 240 }}>Search<input className="input" value={q} onChange={(e) => reset(() => setQ(e.target.value))} placeholder="Symbol or company name" /></label>
          <label className="field">Type
            <select className="select" value={type} onChange={(e) => reset(() => setType(e.target.value))}>
              <option value="">All</option><option value="equity">Equity</option><option value="index">Index</option><option value="etf">ETF</option>
            </select>
          </label>
          <label className="field">Exchange
            <select className="select" value={exchange} onChange={(e) => reset(() => setExchange(e.target.value as Exchange | ""))}><option value="">All</option><option>NSE</option><option>BSE</option></select>
          </label>
          <label className="field">Sector
            <select className="select" value={sector} onChange={(e) => reset(() => setSector(e.target.value))}>
              <option value="">All</option>
              {sectors.data?.sectors.map((s) => <option key={s.sector} value={s.sector}>{s.sector}</option>)}
            </select>
          </label>
          <button className="btn" onClick={() => setAdding((a) => !a)} aria-expanded={adding}>{adding ? "Close" : "+ Add instrument"}</button>
        </div>
        {adding && <div style={{ marginTop: "1rem" }}><AddInstrument onDone={() => setAdding(false)} /></div>}
      </Card>
      {toggle.isError && <ErrorBox error={toggle.error} title="Could not change the instrument" />}
      <Card>
        {list.isLoading ? <Loading text="Loading instruments…" /> : list.isError ? <ErrorBox error={list.error} onRetry={() => list.refetch()} /> : (
          <>
            <DataTable<Instrument>
              rows={list.data!.items}
              rowKey={(i) => `${i.exchange}:${i.symbol}`}
              empty="No instrument matches these filters."
              columns={[
                { key: "s", header: "Symbol", render: (i) => <Link to={`/stock?symbol=${encodeURIComponent(i.symbol)}`}><strong>{i.symbol}</strong></Link>, sortValue: (i) => i.symbol },
                { key: "n", header: "Name", render: (i) => i.name, sortValue: (i) => i.name },
                { key: "e", header: "Exchange", render: (i) => i.exchange },
                { key: "t", header: "Type", render: (i) => titleCase(i.instrument_type) },
                { key: "c", header: "Sector", render: (i) => i.sector ?? "–", sortValue: (i) => i.sector },
                { key: "a", header: "Status", render: (i) => <Badge kind={i.active ? "ok" : "warn"}>{i.active ? "Active" : "Inactive"}</Badge> },
                { key: "x", header: "", render: (i) => <button className="btn btn-sm" disabled={toggle.isPending} onClick={() => toggle.mutate(i)}>{i.active ? "Deactivate" : "Activate"}</button> },
              ]}
            />
            <Pagination page={list.data!.page} pages={list.data!.pages} total={list.data!.total} onPage={setPage} noun="instruments" />
          </>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- price search
function PriceSearch() {
  const [symbols, setSymbols] = useState<string[]>([]);
  const [draft, setDraft] = useState({ start: "", end: "", min_close: "", max_close: "", min_volume: "", flag: "", sort_by: "date", order: "desc" });
  const [applied, setApplied] = useState<SearchParams | null>(null);
  const [page, setPage] = useState(1);
  const num = (s: string) => (s.trim() === "" ? undefined : Number(s));
  const run = (e: FormEvent) => {
    e.preventDefault();
    setPage(1);
    setApplied({
      symbols: symbols.join(",") || undefined, start: draft.start || undefined, end: draft.end || undefined,
      min_close: num(draft.min_close), max_close: num(draft.max_close), min_volume: num(draft.min_volume),
      flag: draft.flag || undefined, sort_by: draft.sort_by, order: draft.order,
    });
  };
  const result = useSearchPrices({ ...(applied ?? {}), page, page_size: 25 }, applied !== null);

  return (
    <div className="stack">
      <Card title="Find days that match" subtitle="Searches the split-adjusted daily prices of every instrument">
        <form className="stack" onSubmit={run}>
          <div className="controls">
            <SymbolSelect value="" clearOnSelect label="Limit to symbols (optional)" onSelect={(i) => setSymbols((s) => (s.includes(i.symbol) || s.length >= 50 ? s : [...s, i.symbol]))} />
            <div className="field-group"><span>Chosen</span><div className="row">{symbols.length ? symbols.map((s) => <span key={s} className="badge">{s} <button type="button" className="btn btn-ghost btn-sm" aria-label={`Remove ${s}`} style={{ padding: "0 0.25rem" }} onClick={() => setSymbols((x) => x.filter((y) => y !== s))}>×</button></span>) : <span className="muted">all instruments</span>}</div></div>
          </div>
          <div className="controls">
            <label className="field">From<input className="input" type="date" value={draft.start} onChange={(e) => setDraft({ ...draft, start: e.target.value })} /></label>
            <label className="field">To<input className="input" type="date" value={draft.end} onChange={(e) => setDraft({ ...draft, end: e.target.value })} /></label>
            <label className="field">Close at least (₹)<input className="input" type="number" min="0" step="any" value={draft.min_close} onChange={(e) => setDraft({ ...draft, min_close: e.target.value })} /></label>
            <label className="field">Close at most (₹)<input className="input" type="number" min="0" step="any" value={draft.max_close} onChange={(e) => setDraft({ ...draft, max_close: e.target.value })} /></label>
            <label className="field">Volume at least<input className="input" type="number" min="0" step="1" value={draft.min_volume} onChange={(e) => setDraft({ ...draft, min_volume: e.target.value })} /></label>
            <label className="field">Data flag
              <select className="select" value={draft.flag} onChange={(e) => setDraft({ ...draft, flag: e.target.value })}>
                <option value="">Any</option><option value="return_outlier">Return outlier</option><option value="gap_before">Gap before</option><option value="no_trade">No trade</option>
              </select>
            </label>
            <label className="field">Sort by
              <select className="select" value={draft.sort_by} onChange={(e) => setDraft({ ...draft, sort_by: e.target.value })}><option value="date">Date</option><option value="close">Close</option><option value="volume">Volume</option></select>
            </label>
            <label className="field">Order
              <select className="select" value={draft.order} onChange={(e) => setDraft({ ...draft, order: e.target.value })}><option value="desc">Highest / newest first</option><option value="asc">Lowest / oldest first</option></select>
            </label>
            <button className="btn btn-primary">Search</button>
          </div>
        </form>
      </Card>
      {applied === null ? (
        <Banner kind="info">Set any filters and press Search. With no filters it lists the newest bars of every instrument.</Banner>
      ) : result.isLoading ? <Loading text="Searching…" /> : result.isError ? <ErrorBox error={result.error} onRetry={() => result.refetch()} title="Search failed" /> : (
        <Card title={`${result.data!.total.toLocaleString("en-IN")} matching days`}>
          <DataTable<PriceHit>
            rows={result.data!.items}
            rowKey={(b) => `${b.symbol}:${b.ts}`}
            empty="No day matches these filters."
            columns={[
              { key: "s", header: "Symbol", render: (b) => <Link to={`/stock?symbol=${encodeURIComponent(b.symbol)}`}><strong>{b.symbol}</strong></Link> },
              { key: "d", header: "Date", render: (b) => formatDate(b.ts) },
              { key: "o", header: "Open", align: "right", render: (b) => formatINR(b.open) },
              { key: "h", header: "High", align: "right", render: (b) => formatINR(b.high) },
              { key: "l", header: "Low", align: "right", render: (b) => formatINR(b.low) },
              { key: "c", header: "Close", align: "right", render: (b) => formatINR(b.close) },
              { key: "v", header: "Volume", align: "right", render: (b) => (b.volume ? compactNumber(b.volume) : "–") },
              { key: "f", header: "Flags", render: (b) => (b.quality_flags?.length ? <Badge kind="warn">{b.quality_flags.join(", ")}</Badge> : "") },
            ]}
          />
          <Pagination page={result.data!.page} pages={result.data!.pages} total={result.data!.total} onPage={setPage} noun="days" />
        </Card>
      )}
    </div>
  );
}

export function SearchPage() {
  const [tab, setTab] = useState<Tab>("instruments");
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Search</h1>
          <p>Find instruments, or find the days when prices met a condition.</p>
        </div>
      </div>
      <div className="tabs" role="tablist" aria-label="Search type">
        <button role="tab" aria-selected={tab === "instruments"} onClick={() => setTab("instruments")}>Instruments</button>
        <button role="tab" aria-selected={tab === "prices"} onClick={() => setTab("prices")}>Historical prices</button>
      </div>
      {tab === "instruments" ? <Instruments /> : <PriceSearch />}
    </>
  );
}
