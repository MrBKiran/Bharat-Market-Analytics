// The market controls shared by the Stock, Compare and Backtest pages: symbol, exchange, timeframe and date range.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Exchange, Interval } from "../api/types";
import { presetStart, type RangePreset } from "../lib/dates";

export interface Controls {
  symbol: string;
  exchange: Exchange;
  interval: Interval;
  preset: RangePreset;
  start?: string;   // used when preset is CUSTOM
  end?: string;
}

const STORAGE = "bm-controls";
export const DEFAULT_CONTROLS: Controls = { symbol: "RELIANCE", exchange: "NSE", interval: "1d", preset: "1Y" };

function load(): Controls {
  try {
    const raw = localStorage.getItem(STORAGE);
    if (raw) return { ...DEFAULT_CONTROLS, ...JSON.parse(raw) };
  } catch {
    /* ignore */
  }
  return DEFAULT_CONTROLS;
}

interface ControlsContextValue {
  controls: Controls;
  set: (patch: Partial<Controls>) => void;
}
const ControlsContext = createContext<ControlsContextValue>({ controls: DEFAULT_CONTROLS, set: () => {} });

export function ControlsProvider({ children, initial }: { children: ReactNode; initial?: Partial<Controls> }) {
  const [controls, setControls] = useState<Controls>(() => ({ ...load(), ...initial }));
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE, JSON.stringify(controls));
    } catch {
      /* ignore */
    }
  }, [controls]);
  const set = useCallback((patch: Partial<Controls>) => setControls((c) => ({ ...c, ...patch })), []);
  const value = useMemo(() => ({ controls, set }), [controls, set]);
  return <ControlsContext.Provider value={value}>{children}</ControlsContext.Provider>;
}

export const useControls = () => useContext(ControlsContext);

/** The concrete start and end dates the controls describe, measured back from the last available bar. */
export function resolveRange(c: Controls, lastBarIso: string | undefined): { start?: string; end?: string } {
  if (c.preset === "CUSTOM") return { start: c.start || undefined, end: c.end || undefined };
  if (!lastBarIso) return {};
  return { start: presetStart(c.preset, lastBarIso) };
}
