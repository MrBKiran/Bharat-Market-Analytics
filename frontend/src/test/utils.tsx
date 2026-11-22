import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import App from "../App";
import { ControlsProvider, type Controls } from "../state/controls";
import { ThemeProvider } from "../theme";

export type Handler = (url: URL, init?: RequestInit) => unknown | Response | Promise<unknown | Response>;

/**
 * Replace fetch with a tiny fake server. `routes` maps "METHOD /path" (or just "/path" for GET) to a handler returning the
 * JSON body, or a Response for full control. Unmatched requests fail loudly so a test never silently talks to nothing.
 */
export function mockApi(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; url: URL; body: unknown }[] = [];
  const fake = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : init?.body;
    calls.push({ method, path: url.pathname, url, body });
    const handler = routes[`${method} ${url.pathname}`] ?? (method === "GET" ? routes[url.pathname] : undefined);
    if (!handler) return new Response(JSON.stringify({ error: { code: "not_found", message: `unmocked ${method} ${url.pathname}`, request_id: "test" } }), { status: 404 });
    const out = await handler(url, init);
    if (out instanceof Response) return out;
    return new Response(JSON.stringify(out), { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fake);
  return { calls, fake };
}

export const jsonError = (status: number, code: string, message: string, requestId = "req-12345678") =>
  new Response(JSON.stringify({ error: { code, message, details: null, request_id: requestId } }), { status, headers: { "x-request-id": requestId } });

export function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 }, mutations: { retry: false } } });
}

/** Render a component inside every provider the real app uses. */
export function renderWithProviders(ui: ReactElement, opts: { route?: string; controls?: Partial<Controls> } = {}) {
  return render(
    <QueryClientProvider client={newClient()}>
      <ThemeProvider>
        <ControlsProvider initial={opts.controls}>
          <MemoryRouter initialEntries={[opts.route ?? "/"]}>{ui}</MemoryRouter>
        </ControlsProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

export const renderApp = (route = "/", controls?: Partial<Controls>) => renderWithProviders(<App />, { route, controls });

// ---------------------------------------------------------------------------------------------- fixtures
export const HEALTH = { status: "ok", database: { connected: true, server_version: "8.2.6" } };

export const RELIANCE_DETAIL = {
  symbol: "RELIANCE", exchange: "NSE", name: "Reliance Industries Ltd", instrument_type: "equity", sector: "Oil, Gas & Consumable Fuels", active: true,
  isin: "INE002A01018", yfinance_symbol: "RELIANCE.NS",
  coverage: { daily: { bars: 3, first: "2026-11-18T00:00:00Z", last: "2026-11-20T00:00:00Z" }, intraday: [] },
};

export const BARS_DESC = [
  { ts: "2026-11-20T00:00:00Z", open: 1369, high: 1383, low: 1356.1, close: 1377.5, volume: 13167730 },
  { ts: "2026-11-19T00:00:00Z", open: 1361.9, high: 1382.5, low: 1349.7, close: 1375.6, volume: 14486599 },
  { ts: "2026-11-18T00:00:00Z", open: 1370, high: 1376.5, low: 1354, close: 1363, volume: 18228940 },
];

export const OVERVIEW = {
  snapshot: "latest", as_of: "2026-11-20", computed_at: "2026-11-22T10:00:00Z",
  universe: { symbols: 16, stocks: 10, indices: 6, stale_excluded: ["ADANIPORTS", "ASIANPAINT"] },
  trend: { bullish: 8, neutral: 2, bearish: 0 },
  breadth: { advancers: 8, decliners: 2, unchanged: 0, advance_decline_ratio: 4, pct_above_sma50: 100, pct_above_sma200: 80, new_52w_highs: 2, new_52w_lows: 0 },
  top_movers: {
    "1d": { gainers: [{ symbol: "SBIN", name: "State Bank of India", return_pct: 2.5 }], losers: [{ symbol: "TCS", name: "Tata Consultancy Services Ltd", return_pct: -1.2 }] },
    "1w": { gainers: [], losers: [] },
    "1m": { gainers: [{ symbol: "SBIN", name: "State Bank of India", return_pct: 19.657 }], losers: [{ symbol: "TCS", name: "Tata Consultancy Services Ltd", return_pct: -3.442 }] },
  },
  sectors: {
    sectors: { "Financial Services": { stocks: 4, members: ["SBIN"], "1m": 18.56, "1d": 2.6, ytd: 10 }, "Information Technology": { stocks: 2, members: ["TCS"], "1m": -3.17, "1d": 0.99, ytd: -28.9 } },
    ranking_by_1m: ["Financial Services", "Information Technology"], note: "equal-weighted averages of the stocks listed",
  },
  indices: {
    indices: { NIFTY_50: { name: "NIFTY 50", close: 25800.6, last_date: "2026-11-20", performance_pct: { "1d": 1.122, "1m": 12.7, "1y": -1.5 }, max_drawdown_pct: -59.9, current_drawdown_pct: -2.01, annualised_vol_pct_last_1y: 14.4 } },
  },
};

export const STATS = {
  database: "BharatMarketDB", collections: { instruments: 56, adjusted_prices: 295086, intraday_prices: 5392391, indicators: 1491940, backtest_results: 343 },
  size_mb: { data: 800, storage: 500 }, daily_prices: { first: "1990-07-03T00:00:00Z", last: "2026-11-20T00:00:00Z", symbols: 55 },
};

export const STRATEGIES = {
  strategies: [
    { name: "ema_crossover", title: "EMA crossover", description: "Buy when the fast EMA crosses above the slow EMA.", defaults: { fast: 20, slow: 50, atr_stop_mult: 2, target_r: null }, needs_volume: false },
    { name: "rsi_reversal", title: "RSI overbought / oversold", description: "Mean reversion.", defaults: { period: 14 }, needs_volume: false },
  ],
  cost_presets: { delivery: {} }, notes: [],
};

export const BACKTEST_DOC = {
  run_id: "bt_1", strategy: "ema_crossover", symbol: "RELIANCE", exchange: "NSE", interval: "1d", start: "2025-11-20T00:00:00Z", end: "2026-11-20T00:00:00Z",
  params: { fast: 20, slow: 50 }, target_r: null, costs: { model: { name: "delivery" }, slippage_bps: 5, initial_capital: 1_000_000, execution: "" },
  metrics: { initial_capital: 1_000_000, net_pnl: 123456, total_return_pct: 12.35, cagr_pct: 12.3, max_drawdown_pct: -8.2, win_rate_pct: 40, winners: 2, losers: 3, profit_factor: 1.8, total_trades: 5, avg_risk_reward: 2.4, avg_r: 0.5, sharpe: 0.9, exposure_pct: 40, total_costs: 5000 },
  benchmark: { total_return_pct: 20.1, max_drawdown_pct: -15, sharpe: 1.1 }, excess_return_pct: -7.75,
  periods: { split_date: "2026-04-01T00:00:00Z", in_sample: { total_trades: 3, total_return_pct: 9, win_rate_pct: 50, profit_factor: 2, buy_hold_return_pct: 11 }, out_of_sample: { total_trades: 2, total_return_pct: 3, win_rate_pct: 0, profit_factor: 0.5, buy_hold_return_pct: 8 } },
  warnings: ["survivorship bias: the stock list is today's index members"],
  equity_curve: [{ ts: "2025-11-20T00:00:00Z", equity: 1_000_000, drawdown_pct: 0, benchmark_equity: 1_000_000 }, { ts: "2026-11-20T00:00:00Z", equity: 1_123_456, drawdown_pct: -1, benchmark_equity: 1_201_000 }],
  trades: [{ entry_ts: "2026-01-05T00:00:00Z", exit_ts: "2026-02-10T00:00:00Z", entry_price: 1300, exit_price: 1390, qty: 700, net_pnl: 61000, return_pct: 6.7, holding_days: 36, r_multiple: 1.8, exit_reason: "signal" }],
  trades_total: 5, stored: false,
};

// The other responses the dashboard reads. Each is checked against contract.json (contract.test.ts), so they cannot drift from
// what the real API returns (tests/test_contract.py checks the same file against the real API).
export const INSTRUMENTS_LIST = {
  items: [{ symbol: "TCS", exchange: "NSE", name: "Tata Consultancy Services Ltd", instrument_type: "equity", sector: "Information Technology", active: true }],
  page: 1, page_size: 25, total: 56, pages: 3,
};
export const PRICES = { symbol: "RELIANCE", interval: "1d", basis: "adjusted", order: "desc", count: 3, items: BARS_DESC, next_cursor: null };
export const LATEST = { symbol: "RELIANCE", as_of: "2026-11-20T00:00:00Z", close: 1377.5, previous_close: 1375.6, change: 1.9, change_pct: 0.138, bar: BARS_DESC[0] };
export const INDICATORS = { items: [{ ts: "2026-11-20T00:00:00Z", rsi_14: 55.2, ema_20: 1360.1 }], next_cursor: null };
export const SEARCH = { items: [{ symbol: "TCS", exchange: "NSE", ts: "2026-11-20T00:00:00Z", open: 2325, high: 2414.8, low: 2317, close: 2376.2, volume: 2960416 }], total: 1, page: 1, page_size: 25, pages: 1 };
export const SECTORS = { sectors: [{ sector: "Financial Services", count: 1, symbols: ["SBIN"] }], unassigned: [] };
export const COMPARE = {
  symbols: ["RELIANCE", "TCS", "NIFTY_50"], period: { start: "2023-11-20T00:00:00Z", end: "2026-11-20T00:00:00Z", bars: 741, years: 3 },
  stats: Object.fromEntries(["RELIANCE", "TCS", "NIFTY_50"].map((s, i) => [s, { first_close: 100, last_close: 120, total_return_pct: 20 - i * 10, cagr_pct: 6, volatility_pct: 18.5, max_drawdown_pct: -22, best_day_pct: 5, worst_day_pct: -4 }])),
  correlation: { RELIANCE: { RELIANCE: 1, TCS: 0.31, NIFTY_50: 0.74 }, TCS: { RELIANCE: 0.31, TCS: 1, NIFTY_50: 0.62 }, NIFTY_50: { RELIANCE: 0.74, TCS: 0.62, NIFTY_50: 1 } },
  rebased_series: [{ ts: "2023-11-20T00:00:00Z", RELIANCE: 100, TCS: 100, NIFTY_50: 100 }], note: "only dates on which every symbol has a close are used",
};
export const BACKTESTS_LIST = {
  items: [{ run_id: "bt_1", strategy: "ema_crossover", symbol: "RELIANCE", interval: "1d", start: "2025-11-20T00:00:00Z", end: "2026-11-20T00:00:00Z", created_at: "2026-11-22T10:00:00Z",
    metrics: { total_trades: 5, total_return_pct: 12.35 }, benchmark: { total_return_pct: 20.1 } }],
  total: 1, page: 1, page_size: 8, pages: 1,
};
export const LEADERBOARD = {
  batch: "compare", runs: 98, warning: "survivorship bias: the stock list is today's index members",
  strategies: [{ strategy: "ema_crossover", stocks: 49, beat_buy_and_hold: 9, median_total_trades: 45, median_win_rate_pct: 32, median_profit_factor: 1.62, median_total_return_pct: 309, median_max_drawdown_pct: -54.8,
    median_sharpe: 0.38, median_buy_hold_return_pct: 1497, median_buy_hold_sharpe: 0.6, median_oos_return_pct: 37.2 }],
};
export const JOBS = {
  items: [{ job_id: "analytics_analytics_1", job_type: "analytics", status: "running", progress: 40, created_at: "2026-11-22T10:00:00Z", params: { step: "analytics" } },
          { job_id: "clean_adjust_1", job_type: "clean", status: "failed", progress: 5, created_at: "2026-11-22T09:00:00Z", error: "disk full", params: { step: "adjust" } }],
  page: 1, page_size: 15, total: 2, pages: 1,
};
export const BENCHMARKS = { items: [{ run_id: "r1", technology: "pandas", operation: "resample", record_count: 1000, duration_seconds: 0.5, records_per_second: 2000, is_synthetic: false }], page: 1, page_size: 60, total: 1, pages: 1 };
export const UPLOAD_RESULT = { status: "dry_run", file: "TCS_NSE.csv", counts: { read: 6, inserted: 0, duplicates: 0, rejected: 0 }, duration_seconds: 0.1 };
export const PROCESSING_START = { job_id: "analytics_analytics_2", status: "queued", step: "analytics", status_url: "/api/jobs/analytics_analytics_2" };
export const ERROR_BODY = { error: { code: "not_found", message: "No instrument called NOPE on NSE.", details: null, request_id: "abc12345" } };
