import { useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { useBacktestRun, useBacktests, useInstrument, useLeaderboard, useRunBacktest, useStrategies, type RunRequest } from "../api/hooks";
import type { BacktestDoc, Trade } from "../api/types";
import { ControlsBar } from "../components/ControlsBar";
import { ExportMenu } from "../components/ExportMenu";
import { LineChart, type LineData } from "../components/LineChart";
import { Badge, Banner, Card, Change, DataTable, Empty, ErrorBox, Loading, Pagination, Stat } from "../components/ui";
import { daysBetween, isIntraday } from "../lib/dates";
import { compactINR, formatDate, formatDateTime, formatINR, formatNumber, formatPct, formatRatio, titleCase } from "../lib/format";
import { resolveRange, useControls } from "../state/controls";

const COST_OPTIONS = [
  { value: "", label: "Automatic (delivery for daily, intraday for intraday)" },
  { value: "delivery", label: "Delivery" },
  { value: "intraday", label: "Intraday" },
  { value: "stress", label: "Stress (every charge doubled)" },
  { value: "none", label: "No costs (unrealistic)" },
];
const EXIT_REASON: Record<string, string> = {
  signal: "Exit signal", stop: "Stop-loss", gap_stop: "Gapped through stop", target: "Target hit", end_of_data: "End of period",
  forced_eod: "End of day", time_stop: "Time stop",
};

// ------------------------------------------------------------------------------------------------ results
function Trades({ trades, intraday }: { trades: Trade[]; intraday: boolean }) {
  const [page, setPage] = useState(1);
  const size = 20;
  const pages = Math.max(1, Math.ceil(trades.length / size));
  const slice = trades.slice((page - 1) * size, page * size);
  const fmt = intraday ? formatDateTime : formatDate;
  return (
    <>
      <DataTable<Trade>
        rows={slice}
        rowKey={(t) => `${t.entry_ts}`}
        columns={[
          { key: "en", header: "Entry", render: (t) => fmt(t.entry_ts) },
          { key: "ex", header: "Exit", render: (t) => fmt(t.exit_ts) },
          { key: "ep", header: "Buy", align: "right", render: (t) => formatINR(t.entry_price) },
          { key: "xp", header: "Sell", align: "right", render: (t) => formatINR(t.exit_price) },
          { key: "q", header: "Qty", align: "right", render: (t) => formatNumber(t.qty) },
          { key: "p", header: "Net P&L", align: "right", render: (t) => <span className={t.net_pnl >= 0 ? "up" : "down"}>{formatINR(t.net_pnl, 0)}</span> },
          { key: "r", header: "Return", align: "right", render: (t) => <Change value={t.return_pct} /> },
          { key: "m", header: "R", align: "right", render: (t) => formatRatio(t.r_multiple) },
          { key: "d", header: "Days", align: "right", render: (t) => t.holding_days },
          { key: "w", header: "Exit reason", render: (t) => EXIT_REASON[t.exit_reason] ?? titleCase(t.exit_reason) },
        ]}
      />
      <Pagination page={page} pages={pages} total={trades.length} onPage={setPage} noun="trades" />
    </>
  );
}

function ResultView({ doc, title }: { doc: BacktestDoc; title?: string }) {
  const m = doc.metrics;
  const b = doc.benchmark;
  const intraday = isIntraday(doc.interval);
  const [tab, setTab] = useState<"summary" | "trades">("summary");
  const series: LineData[] = useMemo(
    () => [
      { id: "strategy", label: "Strategy", color: 0, points: doc.equity_curve.map((p) => ({ ts: p.ts, value: p.equity })) },
      { id: "bh", label: "Buy and hold", color: 1, points: doc.equity_curve.filter((p) => p.benchmark_equity !== undefined).map((p) => ({ ts: p.ts, value: p.benchmark_equity as number })) },
    ],
    [doc],
  );
  const p = doc.periods;

  return (
    <div className="stack">
      <div className="spread">
        <div>
          <h2>{title ?? titleCase(doc.strategy)} on {doc.symbol} <span className="muted" style={{ fontWeight: 400 }}>({doc.interval})</span></h2>
          <p className="muted" style={{ margin: 0 }}>{formatDate(doc.start)} to {formatDate(doc.end)} · parameters {Object.entries(doc.params).map(([k, v]) => `${k} ${v}`).join(", ")}{doc.target_r ? `, target ${doc.target_r}R` : ""}</p>
        </div>
        <div className="row">{doc.stored ? <Badge kind="ok">Saved</Badge> : <Badge>Not saved</Badge>}<Badge>{doc.costs.model.name} costs</Badge></div>
      </div>

      {doc.stored || doc.created_at ? (
        <ExportMenu report="backtest" label="Download this result" tables={["trades", "equity_curve", "periods"]} params={{ run_id: doc.run_id }} />
      ) : (
        <p className="muted" style={{ margin: 0 }}><small>To download this result as CSV, Excel or PDF, run it again with “Save this result” ticked.</small></p>
      )}

      {doc.warnings.length > 0 && (
        <Banner kind="warn" title="Read before trusting these numbers">
          <ul style={{ margin: "0.3rem 0 0", paddingLeft: "1.1rem" }}>{doc.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
        </Banner>
      )}

      <div className="tabs" role="tablist" aria-label="Result sections">
        <button role="tab" aria-selected={tab === "summary"} onClick={() => setTab("summary")}>Summary</button>
        <button role="tab" aria-selected={tab === "trades"} onClick={() => setTab("trades")}>Trades ({doc.trades_total})</button>
      </div>

      {tab === "trades" ? (
        doc.trades?.length ? <Trades trades={doc.trades} intraday={intraday} /> : <Empty title="This run made no trades" />
      ) : (
        <>
          <div className="grid auto">
            <Card><Stat label="Net profit (after costs)" value={<span className={m.net_pnl >= 0 ? "up" : "down"}>{compactINR(m.net_pnl)}</span>} sub={`on ${compactINR(m.initial_capital)} starting capital`} /></Card>
            <Card><Stat label="Total return" value={<Change value={m.total_return_pct} />} sub={`CAGR ${formatPct(m.cagr_pct, 1)}`} /></Card>
            <Card><Stat label="Buy and hold, same dates" value={<Change value={b.total_return_pct} />} sub={doc.excess_return_pct === null ? undefined : `strategy ${doc.excess_return_pct >= 0 ? "ahead by" : "behind by"} ${formatPct(Math.abs(doc.excess_return_pct), 1)} points`} /></Card>
            <Card><Stat label="Maximum drawdown" value={formatPct(m.max_drawdown_pct, 1)} sub={`buy and hold ${formatPct(b.max_drawdown_pct, 1)}`} /></Card>
            <Card><Stat label="Win rate" value={formatPct(m.win_rate_pct, 1)} sub={`${m.winners ?? 0} ${m.winners === 1 ? "winner" : "winners"}, ${m.losers ?? 0} ${m.losers === 1 ? "loser" : "losers"}`} /></Card>
            <Card><Stat label="Profit factor" value={m.profit_factor === null || m.profit_factor === undefined ? "–" : formatRatio(m.profit_factor)} sub={m.profit_factor == null && m.total_trades ? "no losing trade" : "gross profit ÷ gross loss"} /></Card>
            <Card><Stat label="Total trades" value={formatNumber(m.total_trades)} sub={`${formatNumber(m.exposure_pct, 0)}% of time invested`} /></Card>
            <Card><Stat label="Average risk-to-reward" value={formatRatio(m.avg_risk_reward)} sub={`average R multiple ${formatRatio(m.avg_r)}`} /></Card>
            <Card><Stat label="Sharpe ratio (approx.)" value={formatRatio(m.sharpe)} sub={`buy and hold ${formatRatio(b.sharpe)}`} /></Card>
            <Card><Stat label="Costs paid" value={compactINR(m.total_costs)} sub={m.costs_pct_of_gross_profit != null ? `${formatPct(m.costs_pct_of_gross_profit, 1)} of gross profit` : undefined} /></Card>
          </div>

          <Card title="Equity curve" subtitle="Value of the account over time, against simply buying and holding">
            <LineChart series={series} format={(v) => compactINR(v)} ariaLabel={`Equity curve of ${doc.strategy} against buy and hold`} height={360} />
          </Card>

          {p && (
            <Card title="In-sample vs out-of-sample" subtitle={`The same rules on the first 70% of the dates, then on the last 30% (from ${formatDate(p.split_date)}). A strategy that only works in-sample is probably fitted to noise.`}>
              <DataTable
                rows={[{ name: "In-sample", r: p.in_sample }, { name: "Out-of-sample", r: p.out_of_sample }]}
                rowKey={(x) => x.name}
                columns={[
                  { key: "n", header: "Period", render: (x) => <strong>{x.name}</strong> },
                  { key: "t", header: "Trades", align: "right", render: (x) => x.r.total_trades },
                  { key: "r", header: "Strategy return", align: "right", render: (x) => <Change value={x.r.total_return_pct} /> },
                  { key: "b", header: "Buy and hold", align: "right", render: (x) => <Change value={x.r.buy_hold_return_pct} /> },
                  { key: "w", header: "Win rate", align: "right", render: (x) => formatPct(x.r.win_rate_pct, 1) },
                  { key: "f", header: "Profit factor", align: "right", render: (x) => formatRatio(x.r.profit_factor) },
                ]}
              />
            </Card>
          )}

          <div className="grid cols-2">
            <Card title="Assumptions used">
              <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
                <li>Signals act at the next bar's open; stops are checked before targets; gaps fill at the open.</li>
                <li>Slippage {doc.costs.slippage_bps} bps per fill; costs preset “{doc.costs.model.name}” (brokerage, STT, exchange, SEBI, stamp duty, GST, DP).</li>
                <li>Cost rates are assumptions, not your broker's real schedule.</li>
                <li>Long-only, one position at a time, whole shares, {compactINR(doc.costs.initial_capital)} capital.</li>
              </ul>
            </Card>
            {doc.tax_estimate && (
              <Card title="Rough tax estimate">
                <Stat label="Estimated capital-gains tax" value={compactINR(doc.tax_estimate.total_tax)} sub={`net profit after tax ${compactINR(doc.tax_estimate.net_pnl_after_tax)}`} />
                <p className="muted" style={{ marginBottom: 0 }}><small>{doc.tax_estimate.assumptions}</small></p>
              </Card>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------- page
export function BacktestPage() {
  const { controls } = useControls();
  const strategies = useStrategies();
  const detail = useInstrument(controls.symbol, controls.exchange);
  const run = useRunBacktest();
  const [strategy, setStrategy] = useState("ema_crossover");
  const [params, setParams] = useState<Record<string, string>>({});
  const [cost, setCost] = useState("");
  const [slippage, setSlippage] = useState("5");
  const [capital, setCapital] = useState("1000000");
  const [position, setPosition] = useState("100");
  const [risk, setRisk] = useState("");
  const [save, setSave] = useState(false);
  const [shown, setShown] = useState<BacktestDoc | null>(null);
  const [openId, setOpenId] = useState<string | undefined>();
  const [runsPage, setRunsPage] = useState(1);

  const info = strategies.data?.strategies.find((s) => s.name === strategy);
  useEffect(() => {
    if (info) setParams(Object.fromEntries(Object.entries(info.defaults).map(([k, v]) => [k, v === null ? "" : String(v)])));
  }, [info]);

  const stored = useBacktestRun(openId);
  useEffect(() => {
    if (stored.data) setShown(stored.data);
  }, [stored.data]);

  const intraday = isIntraday(controls.interval);
  const lastBar = intraday ? detail.data?.coverage.intraday.find((c) => c.interval === controls.interval)?.last_ts : detail.data?.coverage.daily?.last;
  const range = resolveRange(controls, lastBar);
  // Intraday runs are limited to about a year; clamp the start so the server accepts it.
  let start = range.start;
  let clamped = false;
  if (intraday && lastBar) {
    const limit = new Date(new Date(lastBar).getTime() - 365 * 86_400_000).toISOString().slice(0, 10);
    if (!start || daysBetween(start, lastBar) > 365) {
      start = limit;
      clamped = true;
    }
  }

  const submit = (e: FormEvent) => {
    e.preventDefault();
    // Blank means "use the default", except a blank target means "no target".
    const sent: Record<string, number | null> = {};
    for (const [key, text] of Object.entries(params)) {
      if (text.trim() !== "") sent[key] = Number(text);
      else if (key === "target_r") sent[key] = null;
    }
    const body: RunRequest = {
      symbol: controls.symbol, exchange: controls.exchange, interval: controls.interval, strategy,
      params: sent,
      start, end: range.end, initial_capital: Number(capital), slippage_bps: Number(slippage), position_pct: Number(position),
      risk_pct: risk.trim() === "" ? null : Number(risk), cost_preset: cost || undefined, store: save,
    };
    setOpenId(undefined);
    run.mutate(body, { onSuccess: (doc) => setShown(doc) });
  };

  const board = useLeaderboard();
  const runs = useBacktests({ page: runsPage, page_size: 8 });
  const boardMissing = board.error instanceof ApiError && board.error.status === 404;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Strategy backtesting</h1>
          <p>Test a rule-based strategy on past prices, with Indian transaction costs and slippage, and compare it with simply buying and holding.</p>
        </div>
      </div>

      <Banner kind="warn" title="A backtest is not a forecast">
        Results look better than real life: the stock list is today's index members (survivorship bias), costs are assumptions, and parameters tuned on past data often fail on new data.
        Always read the buy-and-hold comparison and the out-of-sample result.
      </Banner>

      <Card title="Set up a test">
        <form className="stack" onSubmit={submit}>
          <ControlsBar detail={detail.data} />
          {intraday && <Banner kind="info">Intraday tests close every position at the end of the day and are limited to about a year{clamped ? ` (starting ${formatDate(start)})` : ""}. Intraday data exists only for the Nifty indices.</Banner>}
          <div className="controls">
            <label className="field" style={{ minWidth: 260 }}>Strategy
              <select className="select" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
                {(strategies.data?.strategies ?? []).map((s) => <option key={s.name} value={s.name}>{s.title}</option>)}
              </select>
            </label>
            {info && Object.keys(info.defaults).map((key) => (
              <label key={key} className="field" style={{ width: 130 }}>
                {titleCase(key)}
                <input className="input" type="number" step="any" min="0" value={params[key] ?? ""} placeholder={key === "target_r" ? "none" : String(info.defaults[key])} onChange={(e) => setParams((p) => ({ ...p, [key]: e.target.value }))} />
              </label>
            ))}
          </div>
          {info && <p className="muted" style={{ margin: 0 }}>{info.description}{info.needs_volume ? " Needs traded volume, so it never trades on an index." : ""}</p>}
          <div className="controls">
            <label className="field" style={{ minWidth: 220 }}>Transaction costs
              <select className="select" value={cost} onChange={(e) => setCost(e.target.value)}>{COST_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select>
            </label>
            <label className="field" style={{ width: 130 }}>Slippage (bps)<input className="input" type="number" min="0" max="500" step="any" value={slippage} onChange={(e) => setSlippage(e.target.value)} /></label>
            <label className="field" style={{ width: 160 }}>Capital (₹)<input className="input" type="number" min="10000" step="10000" value={capital} onChange={(e) => setCapital(e.target.value)} /></label>
            <label className="field" style={{ width: 130 }}>Position (% of cash)<input className="input" type="number" min="1" max="100" step="any" value={position} onChange={(e) => setPosition(e.target.value)} /></label>
            <label className="field" style={{ width: 140 }}>Risk per trade (%)<input className="input" type="number" min="0" max="100" step="any" value={risk} placeholder="off" onChange={(e) => setRisk(e.target.value)} /></label>
            <label className="toggles"><label><input type="checkbox" checked={save} onChange={(e) => setSave(e.target.checked)} /><span>Save this result</span></label></label>
            <button className="btn btn-primary" disabled={run.isPending || !detail.data}>{run.isPending ? "Running…" : "Run backtest"}</button>
          </div>
          {save && <p className="muted" style={{ margin: 0 }}><small>Saving changes data, so it needs the API key if one is configured (top bar).</small></p>}
        </form>
      </Card>

      {run.isPending && <Loading text="Running the backtest… this usually takes a second or two." height={160} />}
      {run.isError && <ErrorBox error={run.error} title="The backtest could not run" />}
      {stored.isLoading && openId && <Loading text="Loading the saved run…" />}
      {shown && !run.isPending && <ResultView doc={shown} title={strategies.data?.strategies.find((s) => s.name === shown.strategy)?.title} />}

      <Card title="Strategy comparison across stocks" subtitle="Medians over the stored comparison run (every strategy on every tradable stock)">
        {board.isLoading ? <Loading text="Loading comparison…" /> : boardMissing ? (
          <Empty title="No comparison has been stored yet"><p>Run <span className="mono">scripts\backtest.py compare</span> once to create it.</p></Empty>
        ) : board.isError ? <ErrorBox error={board.error} onRetry={() => board.refetch()} /> : (
          <>
            <Banner kind="warn">{board.data!.warning}</Banner>
            <div style={{ height: "0.6rem" }} />
            <DataTable
              rows={board.data!.strategies}
              rowKey={(r) => r.strategy}
              columns={[
                { key: "s", header: "Strategy", render: (r) => <strong>{titleCase(r.strategy)}</strong>, sortValue: (r) => r.strategy },
                { key: "n", header: "Stocks", align: "right", render: (r) => r.stocks },
                { key: "t", header: "Trades", align: "right", render: (r) => formatNumber(r.median_total_trades) , sortValue: (r) => r.median_total_trades },
                { key: "w", header: "Win rate", align: "right", render: (r) => formatPct(r.median_win_rate_pct, 1), sortValue: (r) => r.median_win_rate_pct },
                { key: "p", header: "Profit factor", align: "right", render: (r) => formatRatio(r.median_profit_factor), sortValue: (r) => r.median_profit_factor },
                { key: "r", header: "Return", align: "right", render: (r) => <Change value={r.median_total_return_pct} digits={0} />, sortValue: (r) => r.median_total_return_pct },
                { key: "h", header: "Buy and hold", align: "right", render: (r) => <Change value={r.median_buy_hold_return_pct} digits={0} />, sortValue: (r) => r.median_buy_hold_return_pct },
                { key: "d", header: "Max drawdown", align: "right", render: (r) => formatPct(r.median_max_drawdown_pct, 1), sortValue: (r) => r.median_max_drawdown_pct },
                { key: "x", header: "Sharpe", align: "right", render: (r) => formatRatio(r.median_sharpe), sortValue: (r) => r.median_sharpe },
                { key: "o", header: "Out-of-sample", align: "right", render: (r) => <Change value={r.median_oos_return_pct} digits={0} />, sortValue: (r) => r.median_oos_return_pct },
                { key: "b", header: "Beat buy-and-hold", align: "right", render: (r) => `${r.beat_buy_and_hold} of ${r.stocks}`, sortValue: (r) => r.beat_buy_and_hold },
              ]}
            />
            <p className="muted" style={{ marginBottom: 0 }}><small>{board.data!.runs} runs. Buy-and-hold median Sharpe: {formatRatio(board.data!.strategies[0]?.median_buy_hold_sharpe)}.</small></p>
          </>
        )}
      </Card>

      <Card title="Saved runs" subtitle="Click Open to view one">
        {runs.isLoading ? <Loading text="Loading saved runs…" /> : runs.isError ? <ErrorBox error={runs.error} onRetry={() => runs.refetch()} /> : (
          <>
            <DataTable
              rows={runs.data!.items}
              rowKey={(r) => r.run_id}
              empty="No saved runs yet. Tick “Save this result” when running a test."
              columns={[
                { key: "when", header: "Saved", render: (r) => formatDateTime(r.created_at) },
                { key: "s", header: "Strategy", render: (r) => titleCase(r.strategy) },
                { key: "y", header: "Symbol", render: (r) => r.symbol },
                { key: "t", header: "Trades", align: "right", render: (r) => r.metrics.total_trades },
                { key: "r", header: "Return", align: "right", render: (r) => <Change value={r.metrics.total_return_pct} digits={0} /> },
                { key: "b", header: "Buy and hold", align: "right", render: (r) => <Change value={r.benchmark?.total_return_pct} digits={0} /> },
                { key: "o", header: "", render: (r) => <button className="btn btn-sm" onClick={() => { setOpenId(r.run_id); window.scrollTo({ top: 0, behavior: "smooth" }); }}>Open</button> },
              ]}
            />
            <Pagination page={runs.data!.page} pages={runs.data!.pages} total={runs.data!.total} onPage={setRunsPage} noun="saved runs" />
          </>
        )}
      </Card>
    </>
  );
}
