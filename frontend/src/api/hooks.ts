// Data hooks. React Query gives every one of them loading, error, caching and retry behaviour for free; the pages only
// decide what to show in each state.
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "./client";
import type {
  BacktestDoc, BacktestSummary, BarSeries, Bar, CompareResponse, Exchange, Health, IndicatorSeries, IndicatorRow, Instrument,
  InstrumentDetail, Interval, Job, Leaderboard, LatestPrice, Overview, Paged, PriceHit, PricesResponse, ReportCatalog, StrategiesResponse, Stats,
  UploadResult,
} from "./types";

/** Server-side limits mean a long history needs several requests: follow `next_cursor` until the range is covered. */
export async function fetchPages<TResp extends { items: any[]; next_cursor: string | null }>(
  fetchPage: (cursor: string | undefined) => Promise<TResp>,
  maxItems: number,
  maxRequests = 12,
): Promise<{ items: any[]; truncated: boolean }> {
  const out: any[] = [];
  let cursor: string | undefined;
  let truncated = false;
  for (let i = 0; i < maxRequests; i++) {
    const page = await fetchPage(cursor);
    out.push(...page.items);
    if (!page.next_cursor) break;
    cursor = page.next_cursor;
    if (out.length >= maxItems || i === maxRequests - 1) {
      truncated = true;
      break;
    }
  }
  return { items: out.reverse(), truncated };   // pages arrive newest first; charts want oldest first
}

const MAX_BARS: Record<string, number> = { "1d": 20000 };
const maxBarsFor = (interval: string) => MAX_BARS[interval] ?? 15000;

// ----------------------------------------------------------------------------------------------- system
export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: () => api.get<Health>("/api/health"), refetchInterval: 30_000, retry: false });

export const useStats = () => useQuery({ queryKey: ["stats"], queryFn: () => api.get<Stats>("/api/stats"), staleTime: 60_000 });

// ------------------------------------------------------------------------------------------ instruments
export interface InstrumentFilters {
  q?: string;
  instrument_type?: string;
  sector?: string;
  exchange?: Exchange | "";
  active?: boolean;
  page?: number;
  page_size?: number;
}
export const useInstruments = (filters: InstrumentFilters, enabled = true) =>
  useQuery({
    queryKey: ["instruments", filters],
    queryFn: ({ signal }) => api.get<Paged<Instrument>>("/api/instruments", filters as any, signal),
    placeholderData: keepPreviousData,
    enabled,
  });

export const useInstrument = (symbol: string | undefined, exchange: Exchange) =>
  useQuery({
    queryKey: ["instrument", symbol, exchange],
    queryFn: ({ signal }) => api.get<InstrumentDetail>(`/api/instruments/${symbol}`, { exchange }, signal),
    enabled: !!symbol,
    staleTime: 5 * 60_000,
  });

export const useSectors = () =>
  useQuery({ queryKey: ["sectors"], queryFn: () => api.get<{ sectors: { sector: string; count: number; symbols: string[] }[]; unassigned: string[] }>("/api/sectors"), staleTime: 5 * 60_000 });

// ----------------------------------------------------------------------------------------------- prices
export interface BarsParams {
  symbol?: string;
  exchange: Exchange;
  interval: Interval;
  start?: string;
  end?: string;
}
export const useBars = (p: BarsParams) =>
  useQuery({
    queryKey: ["bars", p],
    enabled: !!p.symbol,
    staleTime: 60_000,
    queryFn: async ({ signal }): Promise<BarSeries> => {
      const { items, truncated } = await fetchPages(
        (cursor) => api.get<PricesResponse>(`/api/prices/${p.symbol}`, { interval: p.interval, exchange: p.exchange, start: p.start, end: p.end, limit: 5000, order: "desc", cursor }, signal),
        maxBarsFor(p.interval),
      );
      return { bars: items as Bar[], truncated };
    },
  });

export const useLatest = (symbol: string | undefined, exchange: Exchange) =>
  useQuery({
    queryKey: ["latest", symbol, exchange],
    enabled: !!symbol,
    queryFn: ({ signal }) => api.get<LatestPrice>(`/api/prices/${symbol}/latest`, { exchange }, signal),
    staleTime: 60_000,
    retry: (count, err) => !(err instanceof ApiError && err.status === 404) && count < 2,
  });

export const INDICATOR_NAMES = ["ema_20", "ema_50", "sma_200", "bb_upper", "bb_lower", "rsi_14", "vwap_20"] as const;

export const useIndicators = (p: BarsParams & { names?: readonly string[] }, enabled = true) =>
  useQuery({
    queryKey: ["indicators", p],
    enabled: !!p.symbol && enabled,
    staleTime: 60_000,
    queryFn: async ({ signal }): Promise<IndicatorSeries> => {
      const { items } = await fetchPages(
        (cursor) => api.get<{ items: IndicatorRow[]; next_cursor: string | null }>(`/api/indicators/${p.symbol}`, { interval: p.interval, exchange: p.exchange, start: p.start, end: p.end, names: (p.names ?? INDICATOR_NAMES).join(","), limit: 5000, order: "desc", cursor }, signal),
        maxBarsFor(p.interval),
      );
      return { rows: items as IndicatorRow[] };
    },
  });

export interface SearchParams {
  symbols?: string;
  start?: string;
  end?: string;
  min_close?: number;
  max_close?: number;
  min_volume?: number;
  flag?: string;
  sort_by?: string;
  order?: string;
  page?: number;
  page_size?: number;
}
export const useSearchPrices = (p: SearchParams, enabled = true) =>
  useQuery({
    queryKey: ["search", p],
    enabled,
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) => api.get<Paged<PriceHit>>("/api/search/prices", p as any, signal),
  });

// ------------------------------------------------------------------------------------------- analytics
export const useOverview = () =>
  useQuery({
    queryKey: ["overview"],
    queryFn: ({ signal }) => api.get<Overview>("/api/market/overview", undefined, signal),
    staleTime: 5 * 60_000,
    retry: (count, err) => !(err instanceof ApiError && err.status === 404) && count < 2,
  });

export const useCompare = (symbols: string[], start?: string, end?: string, exchange: Exchange = "NSE") =>
  useQuery({
    queryKey: ["compare", symbols, start, end, exchange],
    enabled: symbols.length >= 2,
    queryFn: ({ signal }) => api.get<CompareResponse>("/api/compare", { symbols: symbols.join(","), start, end, exchange }, signal),
    staleTime: 60_000,
    retry: false,
  });

// ------------------------------------------------------------------------------------------ backtesting
export const useStrategies = () =>
  useQuery({ queryKey: ["strategies"], queryFn: () => api.get<StrategiesResponse>("/api/backtests/strategies"), staleTime: Infinity });

export const useLeaderboard = () =>
  useQuery({
    queryKey: ["leaderboard"],
    queryFn: () => api.get<Leaderboard>("/api/backtests/leaderboard"),
    retry: (count, err) => !(err instanceof ApiError && err.status === 404) && count < 2,
  });

export const useBacktests = (params: { symbol?: string; strategy?: string; page?: number; page_size?: number }) =>
  useQuery({
    queryKey: ["backtests", params],
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) => api.get<Paged<BacktestSummary>>("/api/backtests", { ...params, sort_by: "created_at" } as any, signal),
  });

export const useBacktestRun = (runId: string | undefined) =>
  useQuery({
    queryKey: ["backtest", runId],
    enabled: !!runId,
    queryFn: ({ signal }) => api.get<BacktestDoc>(`/api/backtests/${runId}`, { include_trades: true }, signal),
  });

export interface RunRequest {
  symbol: string;
  strategy: string;
  params: Record<string, number | null>;
  interval: Interval;
  exchange: Exchange;
  start?: string;
  end?: string;
  initial_capital: number;
  slippage_bps: number;
  position_pct: number;
  risk_pct?: number | null;
  cost_preset?: string;
  store: boolean;
}
export const useRunBacktest = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RunRequest) => api.post<BacktestDoc>("/api/backtests/run", body),
    onSuccess: (doc) => {
      if (doc.stored) {
        qc.invalidateQueries({ queryKey: ["backtests"] });
        qc.invalidateQueries({ queryKey: ["leaderboard"] });
      }
    },
  });
};

// --------------------------------------------------------------------------------- import and processing
/** Jobs list that refreshes quickly while anything is queued or running, slowly otherwise. */
export const useJobs = (params: { job_type?: string; page?: number; page_size?: number } = {}) =>
  useQuery({
    queryKey: ["jobs", params],
    queryFn: ({ signal }) => api.get<Paged<Job>>("/api/jobs", { page_size: 15, ...params } as any, signal),
    refetchInterval: (q) => (q.state.data?.items.some((j) => j.status === "running" || j.status === "queued") ? 1500 : 15_000),
  });

export const useStartProcessing = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (step: string) => api.post<{ job_id: string; status: string }>(`/api/processing/${step}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
};

export const useReportCatalog = () =>
  useQuery({ queryKey: ["reports"], queryFn: () => api.get<ReportCatalog>("/api/reports"), staleTime: Infinity });

export const useInvalidateAfterImport = () => {
  const qc = useQueryClient();
  return () => {
    for (const key of ["jobs", "stats", "instruments", "instrument", "bars"]) qc.invalidateQueries({ queryKey: [key] });
  };
};

export type { UploadResult };
