import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BACKTEST_DOC, BARS_DESC, HEALTH, OVERVIEW, RELIANCE_DETAIL, STATS, STRATEGIES, jsonError, mockApi, renderApp } from "./utils";

afterEach(() => vi.unstubAllGlobals());

const base = { "/api/health": () => HEALTH, "/api/stats": () => STATS };

describe("Overview page", () => {
  it("shows a loading state, then the market numbers", async () => {
    mockApi({ ...base, "/api/market/overview": () => OVERVIEW });
    renderApp("/");
    expect(screen.getByText("Loading market overview…")).toBeInTheDocument();
    expect((await screen.findAllByText("25,800.60")).length).toBeGreaterThan(0);   // Nifty 50 close (card and table), Indian grouping
    expect(screen.getAllByText("NIFTY 50").length).toBeGreaterThan(0);
    expect(screen.getByText(/Latest trading day: 20 Nov 2026/)).toBeInTheDocument();
    expect(screen.getByText("80%")).toBeInTheDocument();                      // above the 200-day average
    expect(screen.getByText(/2 more stocks are left out/)).toBeInTheDocument();
    expect(await screen.findByText("53.92 L")).toBeInTheDocument();         // 5,392,391 intraday bars, written in lakh
  });

  it("lists movers and opens a stock from one", async () => {
    mockApi({ ...base, "/api/market/overview": () => OVERVIEW });
    renderApp("/");
    const gainers = await screen.findByText("Gainers");
    expect(gainers).toBeInTheDocument();
    expect(screen.getByText("+19.66%")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Today/ }));
    expect(screen.getByText("+2.50%")).toBeInTheDocument();
  });

  it("explains how to compute analytics when none exist yet", async () => {
    mockApi({ ...base, "/api/market/overview": () => jsonError(404, "not_computed", "Market analytics have not been computed yet.") });
    renderApp("/");
    expect(await screen.findByText("Market analytics have not been computed yet")).toBeInTheDocument();
    expect(within(screen.getByRole("main")).getByRole("link", { name: /Data & imports/ })).toHaveAttribute("href", "/data");
  });

  it("shows the server's message and request id when the overview fails", async () => {
    mockApi({ ...base, "/api/market/overview": () => jsonError(500, "internal_error", "Something went wrong on the server.", "trace-9999aaaa") });
    renderApp("/");
    const alert = await screen.findByRole("alert", {}, { timeout: 8000 });          // the page retries a transient 500 twice before giving up
    expect(alert).toHaveTextContent("Could not load the market overview");
    expect(alert).toHaveTextContent("trace-9999aaaa");
  });

  it("reports an offline API in the top bar", async () => {
    mockApi({ "/api/health": () => jsonError(503, "database_unavailable", "down"), "/api/stats": () => STATS, "/api/market/overview": () => OVERVIEW });
    renderApp("/");
    expect(await screen.findByText(/API offline|Database down/)).toBeInTheDocument();
  });
});

describe("Navigation and theme", () => {
  it("has every page in the sidebar and marks the current one", async () => {
    mockApi({ ...base, "/api/market/overview": () => OVERVIEW });
    renderApp("/");
    const nav = screen.getByRole("complementary", { name: /main navigation/i, hidden: true });
    for (const label of ["Overview", "Stock chart", "Compare", "Sectors", "Search", "Backtesting", "Data & imports"]) {
      expect(within(nav).getByRole("link", { name: label, hidden: true })).toBeInTheDocument();
    }
    expect(within(nav).getByRole("link", { name: "Overview", hidden: true })).toHaveAttribute("aria-current", "page");
  });

  it("switches between light and dark and remembers the choice", async () => {
    mockApi({ ...base, "/api/market/overview": () => OVERVIEW });
    document.documentElement.setAttribute("data-theme", "light");
    renderApp("/");
    await userEvent.click(screen.getByRole("button", { name: /switch to dark theme/i }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(localStorage.getItem("bm-theme")).toBe("dark");
    await userEvent.click(screen.getByRole("button", { name: /switch to light theme/i }));
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
  });

  it("shows a friendly page for an unknown address", async () => {
    mockApi({ ...base });
    renderApp("/nonsense");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });
});

describe("Stock page", () => {
  const stockApi = (extra: Record<string, () => unknown> = {}) =>
    mockApi({
      ...base,
      "/api/instruments/RELIANCE": () => RELIANCE_DETAIL,
      "/api/prices/RELIANCE": () => ({ symbol: "RELIANCE", interval: "1d", items: BARS_DESC, next_cursor: null }),
      "/api/prices/RELIANCE/latest": () => ({ symbol: "RELIANCE", as_of: "2026-11-20T00:00:00Z", close: 1377.5, previous_close: 1375.6, change: 1.9, change_pct: 0.138, bar: BARS_DESC[0] }),
      "/api/indicators/RELIANCE": () => ({ items: [], next_cursor: null }),
      "/api/instruments": () => ({ items: [], page: 1, page_size: 12, total: 0, pages: 0 }),
      ...extra,
    });

  it("shows the price in rupees, the change, the summary and the bars", async () => {
    stockApi();
    renderApp("/stock?symbol=RELIANCE", { symbol: "RELIANCE" });
    expect(await screen.findByText("₹1,377.50")).toBeInTheDocument();
    expect(screen.getAllByText(/Reliance Industries Ltd/).length).toBeGreaterThan(0);
    expect(await screen.findByText("Period high")).toBeInTheDocument();
    expect(screen.getByText("₹1,383.00")).toBeInTheDocument();                   // the highest high of the three bars
    expect(screen.getByText("₹1,349.70")).toBeInTheDocument();                   // the lowest low
    expect(screen.getByText("3 bars, 18 Nov 2026 to 20 Nov 2026. Prices are split-adjusted; dividends are not included.", { exact: false })).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(4);                   // header + 3 bars
  });

  it("asks for the chart's data over the date range measured from the last bar", async () => {
    const { calls } = stockApi();
    renderApp("/stock?symbol=RELIANCE", { symbol: "RELIANCE", preset: "1Y" });
    await screen.findByText("Period high");
    const prices = calls.find((c) => c.path === "/api/prices/RELIANCE")!;
    expect(prices.url.searchParams.get("start")).toBe("2025-11-20");
    expect(prices.url.searchParams.get("interval")).toBe("1d");
    expect(prices.url.searchParams.get("exchange")).toBe("NSE");
  });

  it("disables timeframes the stock has no data for", async () => {
    stockApi();
    renderApp("/stock?symbol=RELIANCE", { symbol: "RELIANCE" });
    await screen.findByText("Period high");
    expect(screen.getByRole("button", { name: "Daily" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "5 min" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "1 min" })).toBeDisabled();
  });

  it("offers the exchange, symbol, timeframe and date-range selectors", async () => {
    stockApi();
    renderApp("/stock?symbol=RELIANCE", { symbol: "RELIANCE" });
    await screen.findByText("Period high");
    const group = screen.getByRole("group", { name: /market controls/i });
    for (const name of ["NSE", "BSE", "1M", "3M", "6M", "1Y", "3Y", "5Y", "10Y", "All", "Custom"]) {
      expect(within(group).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(within(group).getByRole("combobox", { name: /symbol/i })).toBeInTheDocument();
    await userEvent.click(within(group).getByRole("button", { name: "Custom" }));
    expect(within(group).getByLabelText("From")).toBeInTheDocument();
    expect(within(group).getByLabelText("To")).toBeInTheDocument();
  });

  it("says plainly when the symbol is not listed on the chosen exchange", async () => {
    stockApi({ "/api/instruments/RELIANCE": () => jsonError(404, "not_found", "No instrument called RELIANCE on BSE.") });
    renderApp("/stock?symbol=RELIANCE", { symbol: "RELIANCE", exchange: "BSE" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load RELIANCE on BSE");
    expect(alert).toHaveTextContent("No instrument called RELIANCE on BSE.");
  });

  it("explains an empty range instead of drawing nothing", async () => {
    stockApi({ "/api/prices/RELIANCE": () => ({ items: [], next_cursor: null }) });
    renderApp("/stock?symbol=RELIANCE", { symbol: "RELIANCE" });
    expect(await screen.findByText(/No 1d prices for RELIANCE in this range/)).toBeInTheDocument();
  });

  it("warns when a stock's history ends long ago", async () => {
    stockApi({ "/api/instruments/RELIANCE": () => ({ ...RELIANCE_DETAIL, coverage: { ...RELIANCE_DETAIL.coverage, daily: { bars: 3, first: "2021-04-28T00:00:00Z", last: "2021-04-30T00:00:00Z" } } }) });
    renderApp("/stock?symbol=RELIANCE", { symbol: "RELIANCE" });
    expect(await screen.findByText("This stock's price history ends earlier")).toBeInTheDocument();
  });
});

describe("Backtest page", () => {
  const btApi = (extra: Record<string, () => unknown> = {}) =>
    mockApi({
      ...base,
      "/api/instruments/RELIANCE": () => RELIANCE_DETAIL,
      "/api/instruments": () => ({ items: [], page: 1, page_size: 12, total: 0, pages: 0 }),
      "/api/backtests/strategies": () => STRATEGIES,
      "/api/backtests/leaderboard": () => jsonError(404, "no_runs", "No stored runs"),
      "/api/backtests": () => ({ items: [], page: 1, page_size: 8, total: 0, pages: 0 }),
      "POST /api/backtests/run": () => BACKTEST_DOC,
      ...extra,
    });

  it("warns up front that a backtest is not a forecast", async () => {
    btApi();
    renderApp("/backtest", { symbol: "RELIANCE" });
    expect(await screen.findByText("A backtest is not a forecast")).toBeInTheDocument();
    expect(screen.getByText(/survivorship bias/i)).toBeInTheDocument();
  });

  it("fills the strategy's parameters from its defaults and runs the test with them", async () => {
    const { calls } = btApi();
    renderApp("/backtest", { symbol: "RELIANCE" });
    const run = await screen.findByRole("button", { name: "Run backtest" });
    await waitFor(() => expect(screen.getByLabelText("Fast")).toHaveValue(20));
    expect(screen.getByLabelText("Slow")).toHaveValue(50);
    await userEvent.clear(screen.getByLabelText("Fast"));
    await userEvent.type(screen.getByLabelText("Fast"), "12");
    await waitFor(() => expect(run).toBeEnabled());
    await userEvent.click(run);
    await screen.findByText(/Read before trusting these numbers/);
    const sent = calls.find((c) => c.method === "POST" && c.path === "/api/backtests/run")!.body as any;
    expect(sent).toMatchObject({ symbol: "RELIANCE", exchange: "NSE", interval: "1d", strategy: "ema_crossover", store: false, slippage_bps: 5, initial_capital: 1_000_000 });
    expect(sent.params).toMatchObject({ fast: 12, slow: 50, atr_stop_mult: 2, target_r: null });   // a blank target means no target
  });

  it("shows the warnings, the buy-and-hold comparison and the out-of-sample result", async () => {
    btApi();
    renderApp("/backtest", { symbol: "RELIANCE" });
    await userEvent.click(await screen.findByRole("button", { name: "Run backtest" }));
    expect(await screen.findByText(/survivorship bias: the stock list is today's index members/)).toBeInTheDocument();
    expect(screen.getByText("₹1.23 L")).toBeInTheDocument();                     // net profit, lakh wording
    expect(screen.getByText("Buy and hold, same dates")).toBeInTheDocument();
    expect(screen.getByText(/strategy behind by 7\.8% points/)).toBeInTheDocument();
    expect(screen.getAllByText("Win rate").length).toBeGreaterThan(0);
    expect(screen.getByText("2 winners, 3 losers")).toBeInTheDocument();
    expect(screen.getByText("In-sample vs out-of-sample")).toBeInTheDocument();
    expect(screen.getByText("Average risk-to-reward")).toBeInTheDocument();
    expect(screen.getByText("EMA crossover on RELIANCE")).toBeInTheDocument();
    expect(screen.getByText("Not saved")).toBeInTheDocument();
  });

  it("tells the user to enter the API key when saving is refused", async () => {
    btApi({ "POST /api/backtests/run": () => jsonError(401, "unauthorized", "A valid X-API-Key header is required for this endpoint.") });
    renderApp("/backtest", { symbol: "RELIANCE" });
    await userEvent.click(await screen.findByLabelText("Save this result"));
    await userEvent.click(screen.getByRole("button", { name: "Run backtest" }));
    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(alert).toHaveTextContent("The backtest could not run");
    expect(alert).toHaveTextContent(/API key/);
  });

  it("explains that no stored comparison exists yet", async () => {
    btApi();
    renderApp("/backtest", { symbol: "RELIANCE" });
    expect(await screen.findByText("No comparison has been stored yet")).toBeInTheDocument();
  });
});

describe("Data page", () => {
  const dataApi = (extra: Record<string, () => unknown> = {}) =>
    mockApi({
      ...base,
      "/api/jobs": () => ({ items: [{ job_id: "analytics_analytics_1", job_type: "analytics", status: "running", progress: 40, created_at: "2026-11-22T10:00:00Z", params: { step: "analytics" } },
                                    { job_id: "clean_adjust_1", job_type: "clean", status: "failed", progress: 5, created_at: "2026-11-22T09:00:00Z", error: "disk full", params: { step: "adjust" } }], page: 1, page_size: 15, total: 2, pages: 1 }),
      "/api/benchmarks": () => ({ items: [], page: 1, page_size: 60, total: 0, pages: 0 }),
      ...extra,
    });

  it("lists jobs with their status, progress and errors", async () => {
    dataApi();
    renderApp("/data");
    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("disk full")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "analytics_analytics_1 progress" })).toHaveAttribute("aria-valuenow", "40");
  });

  it("starts a processing step and shows a refusal clearly", async () => {
    const { calls } = dataApi({ "POST /api/processing/analytics": () => jsonError(409, "already_running", "The analytics step is already running.") });
    renderApp("/data");
    const row = (await screen.findByText("Recompute market analytics")).closest("div")!.parentElement!;
    await userEvent.click(within(row).getByRole("button", { name: "Run" }));
    expect(await screen.findByText("The analytics step is already running.")).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/processing/analytics")).toBe(true);
  });

  it("refuses a file type the server would refuse, before uploading", async () => {
    const { calls } = dataApi();
    renderApp("/data");
    const input = await screen.findByLabelText("Price file");
    await userEvent.upload(input, new File(["x"], "notes.txt", { type: "text/plain" }), { applyAccept: false });
    expect(await screen.findByText("Only .csv and .xlsx files can be imported.")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/api/imports/upload")).toBe(false);
    expect(screen.getByRole("button", { name: /check file|import file/i })).toBeDisabled();
  });

  it("enables the import only once a valid file is chosen", async () => {
    dataApi();
    renderApp("/data");
    const input = await screen.findByLabelText("Price file");
    await userEvent.upload(input, new File(["Date,Open\n"], "TCS_NSE.csv", { type: "text/csv" }));
    expect(await screen.findByText("TCS_NSE.csv")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check file" })).toBeEnabled();         // dry run is on by default: nothing is stored by accident
  });
});

describe("Compare, Sectors and Search pages", () => {
  it("compares symbols and prints statistics and correlations", async () => {
    mockApi({
      ...base,
      "/api/instruments/RELIANCE": () => RELIANCE_DETAIL,
      "/api/instruments/TCS": () => ({ ...RELIANCE_DETAIL, symbol: "TCS", name: "Tata Consultancy Services Ltd" }),
      "/api/instruments/NIFTY_50": () => ({ ...RELIANCE_DETAIL, symbol: "NIFTY_50", name: "NIFTY 50" }),
      "/api/instruments": () => ({ items: [], page: 1, page_size: 12, total: 0, pages: 0 }),
      "/api/compare": () => ({
        symbols: ["RELIANCE", "TCS", "NIFTY_50"], period: { start: "2023-11-20T00:00:00Z", end: "2026-11-20T00:00:00Z", bars: 741, years: 3 },
        stats: Object.fromEntries(["RELIANCE", "TCS", "NIFTY_50"].map((s, i) => [s, { first_close: 100, last_close: 120, total_return_pct: 20 - i * 10, cagr_pct: 6, volatility_pct: 18.5, max_drawdown_pct: -22, best_day_pct: 5, worst_day_pct: -4 }])),
        correlation: { RELIANCE: { RELIANCE: 1, TCS: 0.31, NIFTY_50: 0.74 }, TCS: { RELIANCE: 0.31, TCS: 1, NIFTY_50: 0.62 }, NIFTY_50: { RELIANCE: 0.74, TCS: 0.62, NIFTY_50: 1 } },
        rebased_series: [{ ts: "2023-11-20T00:00:00Z", RELIANCE: 100, TCS: 100, NIFTY_50: 100 }], note: "only dates on which every symbol has a close are used",
      }),
    });
    renderApp("/compare");
    expect(await screen.findByText(/741 trading days in common, 20 Nov 2023 to 20 Nov 2026/)).toBeInTheDocument();
    expect(screen.getByText(/Only dates on which every symbol has a close are used/)).toBeInTheDocument();   // sentence starts with a capital
    expect(screen.getByRole("table", { name: /correlation of daily returns/i })).toBeInTheDocument();
    expect(screen.getAllByText("0.74")).toHaveLength(2);                          // the matrix is symmetric: shown on both sides of the diagonal
  });

  it("asks for a second symbol when only one is selected", async () => {
    mockApi({ ...base, "/api/instruments": () => ({ items: [], page: 1, page_size: 12, total: 0, pages: 0 }), "/api/instruments/RELIANCE": () => RELIANCE_DETAIL, "/api/instruments/TCS": () => RELIANCE_DETAIL, "/api/instruments/NIFTY_50": () => RELIANCE_DETAIL,
      "/api/compare": () => jsonError(422, "x", "x") });
    renderApp("/compare");
    await userEvent.click(await screen.findByRole("button", { name: "Remove TCS" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove NIFTY_50" }));
    expect(await screen.findByText("Pick at least two symbols to compare")).toBeInTheDocument();
  });

  it("shows sector rankings and tells the reader the labels are the project's own", async () => {
    mockApi({ ...base, "/api/market/overview": () => OVERVIEW, "/api/sectors": () => ({ sectors: [{ sector: "Financial Services", count: 1, symbols: ["SBIN"] }], unassigned: [] }) });
    renderApp("/sectors");
    expect(await screen.findByText("Read the sector numbers with care")).toBeInTheDocument();
    expect(screen.getByText(/equal-weighted averages of the stocks listed\. Sector labels/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "SBIN" })).toHaveAttribute("href", "/stock?symbol=SBIN");
  });

  it("searches instruments and paginates", async () => {
    const { calls } = mockApi({
      ...base, "/api/sectors": () => ({ sectors: [], unassigned: [] }),
      "/api/instruments": (url) => ({ items: [{ symbol: "TCS", exchange: "NSE", name: "Tata Consultancy Services Ltd", instrument_type: "equity", sector: "Information Technology", active: true }], page: Number(url.searchParams.get("page") ?? 1), page_size: 25, total: 56, pages: 3 }),
    });
    renderApp("/search");
    expect(await screen.findByRole("link", { name: "TCS" })).toHaveAttribute("href", "/stock?symbol=TCS");
    expect(screen.getByText("56 instruments")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/instruments" && c.url.searchParams.get("page") === "2")).toBe(true));
  });
});
