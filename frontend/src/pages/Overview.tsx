import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { useOverview, useStats } from "../api/hooks";
import type { MoverRow, Overview as OverviewData } from "../api/types";
import { Banner, Card, Change, DataTable, Empty, ErrorBox, Loading, Stat } from "../components/ui";
import { SignedBars } from "../components/viz";
import { compactNumber, formatDate, formatNumber, formatPct, titleCase } from "../lib/format";
import { useControls } from "../state/controls";

type Period = "1d" | "1w" | "1m";
const PERIOD_LABEL: Record<Period, string> = { "1d": "Today", "1w": "1 week", "1m": "1 month" };
const SECTOR_PERIODS = ["1d", "1w", "1m", "3m", "6m", "1y", "ytd"] as const;

function Movers({ data }: { data: OverviewData["top_movers"] }) {
  const [period, setPeriod] = useState<Period>("1m");
  const navigate = useNavigate();
  const { set } = useControls();
  const open = (m: MoverRow) => {
    set({ symbol: m.symbol, exchange: "NSE", interval: "1d" });
    navigate("/stock");
  };
  const list = (rows: MoverRow[], empty: string) =>
    rows.length ? (
      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {rows.map((m) => (
          <li key={m.symbol} className="spread" style={{ padding: "0.3rem 0", borderBottom: "1px solid var(--border)" }}>
            <button className="btn btn-ghost btn-sm" onClick={() => open(m)} title={m.name} style={{ padding: 0 }}>
              <strong>{m.symbol}</strong> <span className="muted" style={{ fontWeight: 400 }}>{m.name}</span>
            </button>
            <Change value={m.return_pct} />
          </li>
        ))}
      </ul>
    ) : <span className="muted">{empty}</span>;
  return (
    <Card
      title="Top movers"
      subtitle="Stocks in the current universe"
      actions={
        <div className="seg" role="group" aria-label="Movers period">
          {(Object.keys(PERIOD_LABEL) as Period[]).map((p) => (
            <button key={p} aria-pressed={period === p} onClick={() => setPeriod(p)}>{PERIOD_LABEL[p]}</button>
          ))}
        </div>
      }
    >
      <div className="grid cols-2">
        <div><h3 className="up" style={{ marginBottom: "0.4rem" }}>Gainers</h3>{list(data[period].gainers, "No gainers")}</div>
        <div><h3 className="down" style={{ marginBottom: "0.4rem" }}>Losers</h3>{list(data[period].losers, "No losers")}</div>
      </div>
    </Card>
  );
}

function Sectors({ data }: { data: OverviewData["sectors"] }) {
  const [period, setPeriod] = useState<(typeof SECTOR_PERIODS)[number]>("1m");
  const rows = Object.entries(data.sectors)
    .map(([name, s]) => ({ label: name, value: typeof s[period] === "number" ? (s[period] as number) : null, note: `${s.stocks} stock(s): ${s.members.join(", ")}` }))
    .sort((a, b) => (b.value ?? -999) - (a.value ?? -999));
  return (
    <Card
      title="Sector performance"
      subtitle="Equal-weighted average of each sector's stocks"
      actions={
        <select className="select" aria-label="Sector period" value={period} onChange={(e) => setPeriod(e.target.value as typeof period)}>
          {SECTOR_PERIODS.map((p) => <option key={p} value={p}>{p === "ytd" ? "Year to date" : p.toUpperCase()}</option>)}
        </select>
      }
    >
      <SignedBars rows={rows} />
      {data.note && <p className="muted" style={{ marginBottom: 0 }}><small>{data.note}</small></p>}
    </Card>
  );
}

export function OverviewPage() {
  const overview = useOverview();
  const stats = useStats();

  const body = () => {
    if (overview.isLoading) return <Loading text="Loading market overview…" height={260} />;
    if (overview.error instanceof ApiError && overview.error.status === 404) {
      return (
        <Card>
          <Empty title="Market analytics have not been computed yet">
            <p>Compute them once from the <Link to="/data">Data &amp; imports</Link> page (or run <span className="mono">scripts\analyze_data.py all</span>).</p>
          </Empty>
        </Card>
      );
    }
    if (overview.isError) return <ErrorBox error={overview.error} onRetry={() => overview.refetch()} title="Could not load the market overview" />;
    const o = overview.data!;
    const b = o.breadth;
    const idx = Object.entries(o.indices.indices);
    return (
      <>
        <div className="grid cols-4">
          {idx.slice(0, 2).map(([key, i]) => (
            <Card key={key}>
              <Stat label={i.name} value={formatNumber(i.close, 2)} sub={`${formatPct(i.performance_pct["1d"] as number, 2, true)} today`} delta={i.performance_pct["1d"] as number} />
            </Card>
          ))}
          <Card><Stat label="Advancers / decliners" value={<><span className="up">{b.advancers}</span> / <span className="down">{b.decliners}</span></>} sub={`${b.unchanged} unchanged`} /></Card>
          <Card><Stat label="Above 200-day average" value={formatPct(b.pct_above_sma200, 0)} sub={`${formatPct(b.pct_above_sma50, 0)} above 50-day`} /></Card>
        </div>

        {o.universe.stale_excluded.length > 0 && (
          <Banner kind="info" title={`${o.universe.stocks} stocks have current data`}>
            {o.universe.stale_excluded.length} more stocks are left out of today's numbers because their history ends before {formatDate(o.as_of)}
            {" "}(the free Kaggle dataset stops in April 2021). They are still available for charting and backtesting.
          </Banner>
        )}

        <div className="grid cols-2">
          <Movers data={o.top_movers} />
          <Sectors data={o.sectors} />
        </div>

        <div className="grid cols-2">
          <Card title="Market breadth" subtitle={`As of ${formatDate(o.as_of)}`}>
            <div className="grid cols-2">
              <Stat label="Trend: bullish" value={o.trend.bullish} sub="rule-based" />
              <Stat label="Trend: bearish" value={o.trend.bearish} sub={`${o.trend.neutral} neutral`} />
              <Stat label="52-week highs" value={b.new_52w_highs} />
              <Stat label="52-week lows" value={b.new_52w_lows} />
              <Stat label="Advance / decline ratio" value={b.advance_decline_ratio === null ? "–" : b.advance_decline_ratio.toFixed(2)} />
              <Stat label="Stocks counted" value={o.universe.stocks} sub={`${o.universe.indices} indices tracked`} />
            </div>
          </Card>
          <Card title="Indices" subtitle="Performance and risk">
            <DataTable
              rows={idx}
              rowKey={([k]) => k}
              columns={[
                { key: "n", header: "Index", render: ([, i]) => <strong>{i.name}</strong>, sortValue: ([, i]) => i.name },
                { key: "c", header: "Close", align: "right", render: ([, i]) => formatNumber(i.close, 2), sortValue: ([, i]) => i.close },
                { key: "d", header: "1D", align: "right", render: ([, i]) => <Change value={i.performance_pct["1d"] as number} />, sortValue: ([, i]) => i.performance_pct["1d"] as number },
                { key: "m", header: "1M", align: "right", render: ([, i]) => <Change value={i.performance_pct["1m"] as number} />, sortValue: ([, i]) => i.performance_pct["1m"] as number },
                { key: "y", header: "1Y", align: "right", render: ([, i]) => <Change value={i.performance_pct["1y"] as number} />, sortValue: ([, i]) => i.performance_pct["1y"] as number },
                { key: "dd", header: "From peak", align: "right", render: ([, i]) => <Change value={i.current_drawdown_pct} />, sortValue: ([, i]) => i.current_drawdown_pct },
              ]}
            />
            {o.indices.nifty_vs_bank && (
              <p className="muted" style={{ marginBottom: 0 }}>
                <small>Nifty Bank vs Nifty 50 (last {o.indices.nifty_vs_bank.window_days} trading days): correlation {o.indices.nifty_vs_bank.correlation.toFixed(2)}, beta {o.indices.nifty_vs_bank.beta_of_bank_on_nifty.toFixed(2)}.</small>
              </p>
            )}
          </Card>
        </div>
      </>
    );
  };

  const o = overview.data;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Market overview</h1>
          <p>{o ? `Latest trading day: ${formatDate(o.as_of)} · analytics computed ${formatDate(o.computed_at)}` : "Indian equities and indices"}</p>
        </div>
      </div>
      {body()}
      <Card title="What is in the database" subtitle="Real market data only; synthetic benchmark data is labelled separately">
        {stats.isLoading ? <Loading text="Loading…" /> : stats.isError ? <ErrorBox error={stats.error} onRetry={() => stats.refetch()} /> : (
          <div className="grid auto">
            <Stat label="Instruments" value={formatNumber(stats.data!.collections.instruments)} />
            <Stat label="Daily price rows" value={compactNumber(stats.data!.collections.adjusted_prices)} sub={stats.data!.daily_prices ? `${formatDate(stats.data!.daily_prices.first)} to ${formatDate(stats.data!.daily_prices.last)}` : undefined} />
            <Stat label="Intraday bars" value={compactNumber(stats.data!.collections.intraday_prices)} />
            <Stat label="Indicator values" value={compactNumber(stats.data!.collections.indicators)} />
            <Stat label="Backtest runs" value={formatNumber(stats.data!.collections.backtest_results)} />
            <Stat label="Database size" value={`${formatNumber(stats.data!.size_mb.storage)} MB`} sub={`${titleCase(stats.data!.database)} on disk`} />
          </div>
        )}
      </Card>
    </>
  );
}
