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
  trades: [], trades_total: 5, stored: false,
};
