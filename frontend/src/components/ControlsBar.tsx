import { useEffect } from "react";
import type { Exchange, InstrumentDetail, Interval } from "../api/types";
import { INTERVALS } from "../api/types";
import { RANGE_PRESETS, type RangePreset } from "../lib/dates";
import { useControls } from "../state/controls";
import { SymbolSelect } from "./SymbolSelect";

export const DEFAULT_SYMBOL: Record<Exchange, string> = { NSE: "RELIANCE", BSE: "SENSEX" };

/** Which timeframes the chosen instrument actually has data for. Unknown (still loading) allows everything. */
export function availableIntervals(detail: InstrumentDetail | undefined): Set<Interval> | null {
  if (!detail) return null;
  const set = new Set<Interval>();
  if (detail.coverage.daily) set.add("1d");
  for (const c of detail.coverage.intraday) set.add(c.interval);
  return set;
}

interface Props {
  detail?: InstrumentDetail;
  showInterval?: boolean;
  showRange?: boolean;
}

/** The symbol, exchange, timeframe and date-range selectors, shared by the Stock and Backtest pages. */
export function ControlsBar({ detail, showInterval = true, showRange = true }: Props) {
  const { controls, set } = useControls();
  const available = availableIntervals(detail);

  // If the instrument has no data for the chosen timeframe (for example 5-minute bars of a stock), fall back to daily.
  useEffect(() => {
    if (available && !available.has(controls.interval) && available.has("1d")) set({ interval: "1d" });
  }, [available, controls.interval, set]);

  return (
    <div className="controls" role="group" aria-label="Market controls">
      <div className="field-group">
        <span id="exchange-label">Exchange</span>
        <div className="seg" role="group" aria-labelledby="exchange-label">
          {(["NSE", "BSE"] as Exchange[]).map((ex) => (
            <button
              key={ex}
              aria-pressed={controls.exchange === ex}
              onClick={() => controls.exchange !== ex && set({ exchange: ex, symbol: DEFAULT_SYMBOL[ex], interval: "1d" })}
            >
              {ex}
            </button>
          ))}
        </div>
      </div>

      <SymbolSelect value={controls.symbol} exchange={controls.exchange} onSelect={(i) => set({ symbol: i.symbol, exchange: i.exchange })} />

      {showInterval && (
        <div className="field-group">
          <span id="interval-label">Timeframe</span>
          <div className="seg" role="group" aria-labelledby="interval-label">
            {INTERVALS.map((it) => {
              const missing = available ? !available.has(it.value) : false;
              return (
                <button
                  key={it.value}
                  aria-pressed={controls.interval === it.value}
                  disabled={missing}
                  title={missing ? `No ${it.label.toLowerCase()} data for ${controls.symbol}` : undefined}
                  onClick={() => set({ interval: it.value })}
                >
                  {it.label}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {showRange && (
        <>
          <div className="field-group">
            <span id="range-label">Date range</span>
            <div className="seg" role="group" aria-labelledby="range-label">
              {RANGE_PRESETS.map((p) => (
                <button key={p} aria-pressed={controls.preset === p} onClick={() => set({ preset: p as RangePreset })}>
                  {p === "MAX" ? "All" : p}
                </button>
              ))}
              <button aria-pressed={controls.preset === "CUSTOM"} onClick={() => set({ preset: "CUSTOM" })}>Custom</button>
            </div>
          </div>
          {controls.preset === "CUSTOM" && (
            <>
              <label className="field">
                From
                <input className="input" type="date" value={controls.start ?? ""} max={controls.end || undefined} onChange={(e) => set({ start: e.target.value || undefined })} />
              </label>
              <label className="field">
                To
                <input className="input" type="date" value={controls.end ?? ""} min={controls.start || undefined} onChange={(e) => set({ end: e.target.value || undefined })} />
              </label>
            </>
          )}
        </>
      )}
    </div>
  );
}
