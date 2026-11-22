// Shapes of the API's JSON (see docs/API.md). Only the fields the dashboard uses are listed.

export type Exchange = "NSE" | "BSE";
export type Interval = "1d" | "1m" | "5m" | "15m" | "30m" | "1h";
export const INTERVALS: { value: Interval; label: string }[] = [
  { value: "1d", label: "Daily" },
  { value: "1h", label: "1 hour" },
  { value: "15m", label: "15 min" },
  { value: "5m", label: "5 min" },
  { value: "1m", label: "1 min" },
];

export interface Paged<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
}

export interface Instrument {
  symbol: string;
  exchange: Exchange;
  name: string;
  instrument_type: "equity" | "index" | "etf" | "future" | "option";
  sector?: string;
  industry?: string;
  active: boolean;
  yfinance_symbol?: string;
  isin?: string;
}

export interface IntradayCoverage {
  interval: Interval;
  source: string;
  first_ts: string;
  last_ts: string;
  row_count: number;
}

export interface InstrumentDetail extends Instrument {
  coverage: { daily: { bars: number; first: string; last: string } | null; intraday: IntradayCoverage[] };
}

export interface Bar {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  source?: string;
  quality_flags?: string[];
}

export interface PricesResponse {
  symbol: string;
  exchange: Exchange;
  interval: Interval;
  basis: string;
  order: "asc" | "desc";
  count: number;
  items: Bar[];
  next_cursor: string | null;
}

export interface BarSeries {
  bars: Bar[];
  truncated: boolean;
}

export interface LatestPrice {
  symbol: string;
  as_of: string;
  close: number;
  previous_close: number | null;
  change: number | null;
  change_pct: number | null;
  bar: Bar;
}

export type IndicatorRow = { ts: string } & Record<string, number | string | undefined>;
export interface IndicatorSeries {
  rows: IndicatorRow[];
}

export interface MoverRow {
  symbol: string;
  name: string;
  return_pct: number;
}
export interface MoverPeriod {
  gainers: MoverRow[];
  losers: MoverRow[];
}

export interface SectorRow {
  stocks: number;
  members: string[];
  best_1m?: string;
  worst_1m?: string;
  [period: string]: number | string | string[] | undefined;
}

export interface IndexInfo {
  name: string;
  close: number;
  last_date: string;
  performance_pct: Record<string, number | string>;
  max_drawdown_pct: number;
  current_drawdown_pct: number;
  annualised_vol_pct_last_1y: number;
}

export interface Overview {
  snapshot: string;
  as_of: string;
  computed_at: string;
  universe: { symbols: number; stocks: number; indices: number; stale_excluded: string[] };
  trend: { bullish: number; neutral: number; bearish: number };
  breadth: {
    advancers: number;
    decliners: number;
    unchanged: number;
    advance_decline_ratio: number | null;
    pct_above_sma50: number | null;
    pct_above_sma200: number | null;
    new_52w_highs: number;
    new_52w_lows: number;
  };
  top_movers: Record<"1d" | "1w" | "1m", MoverPeriod> & { universe?: unknown };
  sectors: { sectors: Record<string, SectorRow>; ranking_by_1m: string[]; note?: string };
  indices: { indices: Record<string, IndexInfo>; nifty_vs_bank?: { window_days: number; correlation: number; beta_of_bank_on_nifty: number } };
}

export interface Stats {
  database: string;
  collections: Record<string, number>;
  size_mb: { data: number; storage: number };
  daily_prices: { first: string; last: string; symbols: number } | null;
}

export interface CompareStat {
  first_close: number;
  last_close: number;
  total_return_pct: number;
  cagr_pct: number | null;
  volatility_pct: number;
  max_drawdown_pct: number;
  best_day_pct: number;
  worst_day_pct: number;
}
export interface CompareResponse {
  symbols: string[];
  period: { start: string; end: string; bars: number; years: number };
  stats: Record<string, CompareStat>;
  correlation: Record<string, Record<string, number | null>>;
  rebased_series: ({ ts: string } & Record<string, number | string>)[];
  note: string;
}

export interface PriceHit extends Bar {
  symbol: string;
  exchange: Exchange;
}

export interface StrategyInfo {
  name: string;
  title: string;
  description: string;
  defaults: Record<string, number | null>;
  needs_volume: boolean;
}
export interface StrategiesResponse {
  strategies: StrategyInfo[];
  cost_presets: Record<string, Record<string, unknown>>;
  notes: string[];
}

export type Metrics = Record<string, any>;
export interface Trade {
  entry_ts: string;
  exit_ts: string;
  entry_price: number;
  exit_price: number;
  qty: number;
  net_pnl: number;
  costs: number;
  return_pct: number;
  holding_days: number;
  r_multiple: number | null;
  exit_reason: string;
}
export interface CurvePoint {
  ts: string;
  equity: number;
  drawdown_pct: number;
  benchmark_equity?: number;
}
export interface PeriodResult {
  total_trades: number;
  total_return_pct: number | null;
  win_rate_pct: number | null;
  profit_factor: number | null;
  buy_hold_return_pct: number | null;
}
export interface BacktestDoc {
  run_id: string;
  strategy: string;
  symbol: string;
  exchange: Exchange;
  interval: Interval;
  start: string;
  end: string;
  params: Record<string, number | null>;
  target_r?: number | null;
  costs: { model: { name: string }; slippage_bps: number; initial_capital: number; execution: string };
  metrics: Metrics;
  benchmark: Metrics;
  excess_return_pct: number | null;
  periods?: { split_date: string; in_sample: PeriodResult; out_of_sample: PeriodResult };
  warnings: string[];
  equity_curve: CurvePoint[];
  trades?: Trade[];
  trades_total: number;
  tax_estimate?: { total_tax: number; net_pnl_after_tax: number; assumptions: string };
  stored?: boolean;
  created_at?: string;
  batch?: string;
}
export interface BacktestSummary {
  run_id: string;
  strategy: string;
  symbol: string;
  interval: Interval;
  start: string;
  end: string;
  metrics: Metrics;
  benchmark?: { total_return_pct?: number };
  excess_return_pct?: number | null;
  created_at: string;
}
export interface LeaderboardRow {
  strategy: string;
  stocks: number;
  beat_buy_and_hold: number;
  median_total_trades: number | null;
  median_win_rate_pct: number | null;
  median_profit_factor: number | null;
  median_total_return_pct: number | null;
  median_max_drawdown_pct: number | null;
  median_sharpe: number | null;
  median_buy_hold_return_pct: number | null;
  median_buy_hold_sharpe: number | null;
  median_oos_return_pct: number | null;
}
export interface Leaderboard {
  batch: string;
  runs: number;
  strategies: LeaderboardRow[];
  warning: string;
}

export interface Job {
  job_id: string;
  job_type: string;
  status: "queued" | "running" | "paused" | "completed" | "failed" | "cancelled";
  progress: number;
  created_at: string;
  finished_at?: string;
  error?: string;
  counts?: Record<string, number | Record<string, number>>;
  params?: Record<string, unknown>;
}

export interface UploadResult {
  status: string;
  file: string;
  counts?: { read: number; inserted: number; duplicates: number; rejected: number };
  duration_seconds?: number;
}

export interface ReportParam {
  name: string;
  required?: boolean;
  default?: string;
  description?: string;
}
export interface ReportInfo {
  id: string;
  title: string;
  description: string;
  params: ReportParam[];
  tables: string[];
  formats: ("csv" | "xlsx" | "pdf")[];
}
export interface ReportCatalog {
  reports: ReportInfo[];
  formats: string[];
  limits: { max_rows_per_table: number; max_rows_excel: number };
  notes: string[];
}

export interface Health {
  status: string;
  database: { connected: boolean; server_version?: string };
}
