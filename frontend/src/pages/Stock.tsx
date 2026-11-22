import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useBars, useIndicators, useInstrument, useLatest } from "../api/hooks";
import type { Bar } from "../api/types";
import { ControlsBar } from "../components/ControlsBar";
import { ExportMenu } from "../components/ExportMenu";
import { DEFAULT_OVERLAYS, PriceChart, type Overlays } from "../components/PriceChart";
import { Badge, Banner, Card, Change, DataTable, Empty, ErrorBox, Loading, Stat } from "../components/ui";
import { daysBetween, isIntraday } from "../lib/dates";
import { compactNumber, formatDate, formatDateTime, formatINR, formatNumber, titleCase } from "../lib/format";
import { resolveRange, useControls } from "../state/controls";

const OVERLAY_LABELS: { key: keyof Overlays; label: string }[] = [
  { key: "volume", label: "Volume" },
  { key: "ema20", label: "EMA 20" },
  { key: "ema50", label: "EMA 50" },
  { key: "sma200", label: "SMA 200" },
  { key: "bb", label: "Bollinger" },
  { key: "vwap", label: "VWAP" },
  { key: "rsi", label: "RSI 14" },
];

export function StockPage() {
  const { controls, set } = useControls();
  const [params] = useSearchParams();
  const [overlays, setOverlays] = useState<Overlays>(DEFAULT_OVERLAYS);

  // A link such as /stock?symbol=TCS selects that stock.
  const linked = params.get("symbol");
  useEffect(() => {
    if (linked && linked.toUpperCase() !== controls.symbol) set({ symbol: linked.toUpperCase() });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linked]);

  const detail = useInstrument(controls.symbol, controls.exchange);
  const info = detail.data;
  const intraday = isIntraday(controls.interval);
  const lastBar = intraday ? info?.coverage.intraday.find((c) => c.interval === controls.interval)?.last_ts : info?.coverage.daily?.last;
  const range = resolveRange(controls, lastBar);
  const ready = !!info;

  const barsQ = useBars({ symbol: ready ? controls.symbol : undefined, exchange: controls.exchange, interval: controls.interval, ...range });
  const needsIndicators = overlays.ema20 || overlays.ema50 || overlays.sma200 || overlays.bb || overlays.vwap || overlays.rsi;
  const indQ = useIndicators({ symbol: ready ? controls.symbol : undefined, exchange: controls.exchange, interval: controls.interval, ...range }, needsIndicators);
  const latest = useLatest(ready && !intraday ? controls.symbol : undefined, controls.exchange);

  const bars = barsQ.data?.bars ?? [];
  const hasVolume = bars.some((b) => (b.volume ?? 0) > 0);
  const summary = useMemo(() => {
    if (!bars.length) return null;
    const first = bars[0], last = bars[bars.length - 1];
    const vols = bars.map((b) => b.volume ?? 0).filter((v) => v > 0);
    return {
      high: Math.max(...bars.map((b) => b.high)),
      low: Math.min(...bars.map((b) => b.low)),
      change: ((last.close - first.open) / first.open) * 100,
      avgVolume: vols.length ? vols.reduce((a, b) => a + b, 0) / vols.length : null,
      flags: bars.filter((b) => b.quality_flags?.length).length,
      first, last,
    };
  }, [bars]);

  const stale = info?.coverage.daily && !intraday ? daysBetween(info.coverage.daily.last, new Date().toISOString()) > 60 : false;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{controls.symbol}{info ? <span className="muted" style={{ fontWeight: 400, fontSize: "1rem" }}> · {info.name}</span> : null}</h1>
          <div className="row" style={{ marginTop: "0.3rem" }}>
            {info && <Badge>{info.exchange}</Badge>}
            {info && <Badge>{titleCase(info.instrument_type)}</Badge>}
            {info?.sector && <Badge>{info.sector}</Badge>}
          </div>
        </div>
        {latest.data && (
          <div className="right">
            <div className="num" style={{ fontSize: "1.8rem", fontWeight: 700 }}>{formatINR(latest.data.close)}</div>
            <Change value={latest.data.change_pct} /> <span className="muted num">({formatINR(latest.data.change)}) · {formatDate(latest.data.as_of)}</span>
          </div>
        )}
      </div>

      <Card><ControlsBar detail={info} /></Card>

      {detail.isError ? (
        <ErrorBox error={detail.error} onRetry={() => detail.refetch()} title={`Could not load ${controls.symbol} on ${controls.exchange}`} />
      ) : (
        <>
          {stale && info?.coverage.daily && (
            <Banner kind="info" title="This stock's price history ends earlier">
              Data for {controls.symbol} runs to {formatDate(info.coverage.daily.last)}. Date ranges are measured back from that day. Import newer prices on the Data page to extend it.
            </Banner>
          )}
          <Card
            title="Price chart"
            subtitle="Scroll to zoom, drag to pan, hover for exact values"
            actions={
              <div className="toggles" role="group" aria-label="Chart overlays">
                {OVERLAY_LABELS.map(({ key, label }) => {
                  const disabled = (key === "vwap" || key === "volume") && !!barsQ.data && !hasVolume;
                  return (
                    <label key={key} title={disabled ? "Indices have no traded volume" : undefined} style={disabled ? { opacity: 0.5 } : undefined}>
                      <input type="checkbox" checked={overlays[key]} disabled={disabled} onChange={(e) => setOverlays((o) => ({ ...o, [key]: e.target.checked }))} />
                      <span>{label}</span>
                    </label>
                  );
                })}
              </div>
            }
          >
            {detail.isLoading || barsQ.isLoading ? (
              <Loading text={`Loading ${controls.symbol} prices…`} height={420} />
            ) : barsQ.isError ? (
              <ErrorBox error={barsQ.error} onRetry={() => barsQ.refetch()} title="Could not load prices" />
            ) : !bars.length ? (
              <Empty title={`No ${controls.interval} prices for ${controls.symbol} in this range`}>
                <p>Try a wider date range or the daily timeframe. Intraday data exists only for the Nifty indices.</p>
              </Empty>
            ) : (
              <>
                {barsQ.data?.truncated && <Banner kind="warn">Showing the latest {formatNumber(bars.length)} bars of a longer history. Narrow the date range to see the rest.</Banner>}
                {indQ.isError && <Banner kind="warn" title="Indicators unavailable">{(indQ.error as Error).message}</Banner>}
                <PriceChart bars={bars} interval={controls.interval} indicators={indQ.data?.rows} overlays={overlays} />
                <p className="muted" style={{ marginBottom: 0 }}>
                  <small>
                    {formatNumber(bars.length)} bars, {intraday ? formatDateTime(bars[0].ts) : formatDate(bars[0].ts)} to {intraday ? formatDateTime(bars[bars.length - 1].ts) : formatDate(bars[bars.length - 1].ts)}.
                    Prices are split-adjusted; dividends are not included.
                    {indQ.isFetching && " Updating indicators…"}
                  </small>
                </p>
                <div className="row" style={{ justifyContent: "space-between", marginTop: "0.5rem" }}>
                  <ExportMenu report="prices" label="Prices" params={{ symbol: controls.symbol, exchange: controls.exchange, interval: controls.interval, start: range.start, end: range.end }} />
                  <ExportMenu report="indicators" label="Indicators" params={{ symbol: controls.symbol, exchange: controls.exchange, interval: controls.interval, start: range.start, end: range.end }} />
                </div>
              </>
            )}
          </Card>

          {summary && (
            <div className="grid cols-4">
              <Card><Stat label="Period return" value={<Change value={summary.change} />} sub={`${formatINR(summary.first.open)} → ${formatINR(summary.last.close)}`} /></Card>
              <Card><Stat label="Period high" value={formatINR(summary.high)} /></Card>
              <Card><Stat label="Period low" value={formatINR(summary.low)} /></Card>
              <Card><Stat label="Average volume" value={summary.avgVolume ? compactNumber(summary.avgVolume) : "–"} sub={summary.flags ? `${summary.flags} bars flagged for review` : "no data-quality flags"} /></Card>
            </div>
          )}

          {info && (
            <div className="grid cols-2">
              <Card title="Instrument">
                <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "auto 1fr", gap: "0.3rem 1rem" }}>
                  <dt className="muted">Name</dt><dd style={{ margin: 0 }}>{info.name}</dd>
                  <dt className="muted">Type</dt><dd style={{ margin: 0 }}>{titleCase(info.instrument_type)}</dd>
                  <dt className="muted">Sector</dt><dd style={{ margin: 0 }}>{info.sector ?? "–"}</dd>
                  <dt className="muted">ISIN</dt><dd style={{ margin: 0 }} className="mono">{info.isin ?? "–"}</dd>
                  <dt className="muted">Yahoo ticker</dt><dd style={{ margin: 0 }} className="mono">{info.yfinance_symbol ?? "–"}</dd>
                </dl>
              </Card>
              <Card title="Data coverage">
                {info.coverage.daily ? (
                  <p style={{ marginTop: 0 }}>Daily: <strong>{formatNumber(info.coverage.daily.bars)}</strong> bars, {formatDate(info.coverage.daily.first)} to {formatDate(info.coverage.daily.last)}</p>
                ) : <p className="muted" style={{ marginTop: 0 }}>No daily data.</p>}
                {info.coverage.intraday.length > 0 ? (
                  <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
                    {info.coverage.intraday.map((c) => (
                      <li key={c.interval}>{c.interval}: {compactNumber(c.row_count)} bars, {formatDate(c.first_ts)} to {formatDate(c.last_ts)}</li>
                    ))}
                  </ul>
                ) : <p className="muted" style={{ margin: 0 }}>No intraday data for this instrument.</p>}
              </Card>
            </div>
          )}

          {bars.length > 0 && (
            <Card title="Recent bars" subtitle="The newest 100 bars of the range, as numbers">
              <DataTable<Bar>
                rows={[...bars].reverse().slice(0, 100)}
                rowKey={(b) => b.ts}
                columns={[
                  { key: "ts", header: "Date", render: (b) => (intraday ? formatDateTime(b.ts) : formatDate(b.ts)), sortValue: (b) => b.ts },
                  { key: "o", header: "Open", align: "right", render: (b) => formatNumber(b.open, 2), sortValue: (b) => b.open },
                  { key: "h", header: "High", align: "right", render: (b) => formatNumber(b.high, 2), sortValue: (b) => b.high },
                  { key: "l", header: "Low", align: "right", render: (b) => formatNumber(b.low, 2), sortValue: (b) => b.low },
                  { key: "c", header: "Close", align: "right", render: (b) => formatNumber(b.close, 2), sortValue: (b) => b.close },
                  { key: "v", header: "Volume", align: "right", render: (b) => (b.volume ? compactNumber(b.volume) : "–"), sortValue: (b) => b.volume },
                  { key: "f", header: "Flags", render: (b) => (b.quality_flags?.length ? <Badge kind="warn">{b.quality_flags.join(", ")}</Badge> : "") },
                ]}
              />
            </Card>
          )}
        </>
      )}
    </>
  );
}
