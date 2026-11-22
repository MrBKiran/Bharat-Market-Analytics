import { Link } from "react-router-dom";
import { ApiError } from "../api/client";
import { useOverview, useSectors } from "../api/hooks";
import { ExportMenu } from "../components/ExportMenu";
import { Banner, Card, Change, DataTable, Empty, ErrorBox, Loading } from "../components/ui";
import { SignedBars } from "../components/viz";
import { formatDate } from "../lib/format";

const PERIODS = [["1d", "1D"], ["1w", "1W"], ["1m", "1M"], ["3m", "3M"], ["6m", "6M"], ["1y", "1Y"], ["ytd", "YTD"]] as const;

export function SectorsPage() {
  const overview = useOverview();
  const members = useSectors();

  if (overview.isLoading || members.isLoading) return <Loading text="Loading sectors…" height={300} />;
  if (overview.error instanceof ApiError && overview.error.status === 404) {
    return <Card><Empty title="Sector analytics have not been computed yet"><p>Compute them from the <Link to="/data">Data &amp; imports</Link> page.</p></Empty></Card>;
  }
  if (overview.isError) return <ErrorBox error={overview.error} onRetry={() => overview.refetch()} title="Could not load sector performance" />;
  const o = overview.data!;
  const rows = Object.entries(o.sectors.sectors);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Sectors</h1>
          <p>Performance of each sector as of {formatDate(o.as_of)}</p>
        </div>
      </div>
      <Banner kind="warn" title="Read the sector numbers with care">
        {(o.sectors.note ?? "Sectors are equal-weighted averages of the listed stocks").replace(/\.?\s*$/, ".")} Sector labels were assigned by this project, not taken from NSE; verify one before relying on a single stock.
      </Banner>
      <ExportMenu report="sectors" label="Download sector analytics" tables={["sectors", "members"]} params={{}} />
      <div className="grid cols-2">
        <Card title="1-month ranking">
          <SignedBars rows={[...rows].sort((a, b) => ((b[1]["1m"] as number) ?? -999) - ((a[1]["1m"] as number) ?? -999)).map(([name, s]) => ({ label: name, value: s["1m"] as number, note: `${s.stocks} stock(s)` }))} />
        </Card>
        <Card title="Year to date ranking">
          <SignedBars rows={[...rows].sort((a, b) => ((b[1].ytd as number) ?? -999) - ((a[1].ytd as number) ?? -999)).map(([name, s]) => ({ label: name, value: s.ytd as number, note: `${s.stocks} stock(s)` }))} />
        </Card>
      </div>
      <Card title="All periods">
        <DataTable
          rows={rows}
          rowKey={([n]) => n}
          columns={[
            { key: "n", header: "Sector", render: ([n]) => <strong>{n}</strong>, sortValue: ([n]) => n },
            { key: "k", header: "Stocks", align: "right", render: ([, s]) => s.stocks, sortValue: ([, s]) => s.stocks },
            ...PERIODS.map(([key, label]) => ({
              key, header: label, align: "right" as const,
              render: ([, s]: [string, (typeof rows)[number][1]]) => <Change value={s[key] as number} />,
              sortValue: ([, s]: [string, (typeof rows)[number][1]]) => s[key] as number,
            })),
          ]}
        />
      </Card>
      <Card title="Stocks in each sector" subtitle="Click a symbol to open its chart">
        {members.isError ? <ErrorBox error={members.error} /> : (
          <div className="stack">
            {members.data!.sectors.map((s) => (
              <div key={s.sector}>
                <h3>{s.sector} <span className="muted" style={{ fontWeight: 400 }}>({s.count})</span></h3>
                <div className="row" style={{ marginTop: "0.3rem" }}>
                  {s.symbols.map((sym) => <Link key={sym} className="badge" to={`/stock?symbol=${encodeURIComponent(sym)}`}>{sym}</Link>)}
                </div>
              </div>
            ))}
            {members.data!.unassigned.length > 0 && <p className="muted">No sector assigned: {members.data!.unassigned.join(", ")}</p>}
          </div>
        )}
      </Card>
    </>
  );
}
