import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { downloadReport, filenameFrom } from "../api/client";
import { ExportMenu } from "../components/ExportMenu";
import { BACKTEST_DOC, BACKTESTS_LIST, BENCHMARKS, HEALTH, INSTRUMENTS_LIST, JOBS, OVERVIEW, RELIANCE_DETAIL, REPORTS_CATALOG, STATS, STRATEGIES, jsonError, mockApi, renderApp, renderWithProviders } from "./utils";

const base = { "/api/health": () => HEALTH, "/api/stats": () => STATS, "/api/reports": () => REPORTS_CATALOG };

function fileResponse(name = "BharatMarket_prices_TCS_1d_2026-01-01_to_2026-02-01.pdf", rows = 21, body = "%PDF-fake") {
  return new Response(body, { status: 200, headers: { "content-type": "application/pdf", "content-disposition": `attachment; filename="${name}"`, "x-report-rows": String(rows) } });
}

let clicked: { download: string; href: string }[] = [];
beforeEach(() => {
  clicked = [];
  // jsdom cannot save files: record what the page asked the browser to download.
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { clicked.push({ download: this.download, href: this.href }); });
  (URL as any).createObjectURL = vi.fn(() => "blob:fake-url");
  (URL as any).revokeObjectURL = vi.fn();
});
afterEach(() => vi.unstubAllGlobals());

describe("downloadReport", () => {
  it("asks the server for the file and saves it under the server's file name", async () => {
    const { calls } = mockApi({ "/api/reports/prices": () => fileResponse() });
    const result = await downloadReport("prices", "pdf", { symbol: "TCS", start: undefined, end: "", interval: "1d" });
    const call = calls[0];
    expect(call.path).toBe("/api/reports/prices");
    expect(Object.fromEntries(call.url.searchParams)).toEqual({ symbol: "TCS", interval: "1d", format: "pdf" });      // blank values are left out
    expect(result).toEqual({ filename: "BharatMarket_prices_TCS_1d_2026-01-01_to_2026-02-01.pdf", rows: 21, bytes: 9 });
    expect(clicked).toEqual([{ download: "BharatMarket_prices_TCS_1d_2026-01-01_to_2026-02-01.pdf", href: "blob:fake-url" }]);
  });

  it("sends the CSV table choice only when there is one", async () => {
    const { calls } = mockApi({ "/api/reports/comparison": () => fileResponse("x.zip") });
    await downloadReport("comparison", "csv", { symbols: "A,B" }, "all");
    expect(calls[0].url.searchParams.get("table")).toBe("all");
    await downloadReport("comparison", "xlsx", { symbols: "A,B" });
    expect(calls[1].url.searchParams.has("table")).toBe(false);
  });

  it("turns a server refusal into an ApiError with its message and request id, and saves nothing", async () => {
    mockApi({ "/api/reports/prices": () => jsonError(404, "no_data", "No 1m prices for TCS in that range.", "rid-77777777") });
    const err: any = await downloadReport("prices", "csv", { symbol: "TCS" }).catch((e) => e);
    expect(err).toMatchObject({ status: 404, code: "no_data", message: "No 1m prices for TCS in that range.", requestId: "rid-77777777" });
    expect(clicked).toEqual([]);
  });

  it("explains an unreachable server", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const err: any = await downloadReport("prices", "csv").catch((e) => e);
    expect(err).toMatchObject({ status: 0, code: "network_error" });
  });

  it("never trusts a file name from the server beyond safe characters", () => {
    const named = (header: string) => filenameFrom(new Response("", { headers: { "content-disposition": header } }), "fallback.csv");
    expect(named('attachment; filename="evil name?.exe"')).toBe("evil_name_.exe");
    expect(named('attachment; filename="a/b\\c.csv"')).toBe("a_b_c.csv");
    expect(named("inline")).toBe("fallback.csv");
    expect(filenameFrom(new Response("", { headers: { "x-report-filename": "r.pdf" } }), "f")).toBe("r.pdf");
  });
});

describe("ExportMenu", () => {
  it("offers CSV, Excel and PDF, shows progress, and reports what was saved", async () => {
    let finish: (r: Response) => void = () => {};
    mockApi({ "/api/reports/prices": () => new Promise<Response>((resolve) => { finish = resolve; }) });
    renderWithProviders(<ExportMenu report="prices" label="Prices" params={{ symbol: "TCS" }} />);
    const group = screen.getByRole("group", { name: "Prices" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent?.trim())).toEqual(["CSV", "Excel", "PDF"]);
    await userEvent.click(screen.getByRole("button", { name: "Prices as PDF" }));
    expect(await screen.findByText("Preparing the PDF file…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prices as CSV" })).toBeDisabled();                 // no second request while one is running
    finish(fileResponse("BharatMarket_prices_TCS.pdf", 1234567, "x".repeat(3000)));
    expect(await screen.findByText(/Saved BharatMarket_prices_TCS\.pdf \(3 KB, 12,34,567 rows\)\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prices as CSV" })).toBeEnabled();
  });

  it("asks for the right format", async () => {
    const { calls } = mockApi({ "/api/reports/sectors": () => fileResponse("s.xlsx") });
    renderWithProviders(<ExportMenu report="sectors" label="Sectors" params={{}} />);
    await userEvent.click(screen.getByRole("button", { name: "Sectors as Excel" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url.searchParams.get("format")).toBe("xlsx");
  });

  it("shows the server's explanation when a report cannot be built", async () => {
    mockApi({ "/api/reports/backtest": () => jsonError(404, "not_found", "No backtest run bt_x.", "rid-88888888") });
    renderWithProviders(<ExportMenu report="backtest" label="Backtest" params={{ run_id: "bt_x" }} />);
    await userEvent.click(screen.getByRole("button", { name: "Backtest as PDF" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The report could not be built");
    expect(alert).toHaveTextContent("No backtest run bt_x.");
    expect(alert).toHaveTextContent("rid-88888888");
    expect(clicked).toEqual([]);
  });

  it("is disabled, with the reason, when the report has nothing to build from", async () => {
    mockApi({});
    renderWithProviders(<ExportMenu report="backtest" label="Backtest" params={{}} disabled disabledReason="Choose a saved backtest" />);
    for (const b of screen.getAllByRole("button")) {
      expect(b).toBeDisabled();
      expect(b).toHaveAttribute("title", "Choose a saved backtest");
    }
  });

  it("lets a CSV choose one table of a multi-table report, or all of them", async () => {
    const { calls } = mockApi({ "/api/reports/comparison": () => fileResponse("c.csv") });
    renderWithProviders(<ExportMenu report="comparison" label="Comparison" tables={["statistics", "correlation", "rebased_series"]} params={{ symbols: "A,B" }} />);
    const select = screen.getByLabelText("CSV table");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["statistics (default)", "correlation", "rebased_series", "All tables (ZIP)"]);
    await userEvent.selectOptions(select, "all");
    await userEvent.click(screen.getByRole("button", { name: "Comparison as CSV" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url.searchParams.get("table")).toBe("all");
    await userEvent.click(screen.getByRole("button", { name: "Comparison as PDF" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].url.searchParams.has("table")).toBe(false);                                   // the table choice is for CSV only
  });
});

describe("Reports page", () => {
  const reportsApi = (extra: Record<string, () => unknown> = {}) =>
    mockApi({
      ...base, "/api/instruments/RELIANCE": () => RELIANCE_DETAIL, "/api/instruments": () => INSTRUMENTS_LIST, "/api/backtests": () => BACKTESTS_LIST, "/api/reports/prices": () => fileResponse(), ...extra,
    });

  it("is in the sidebar and lists every report with its description", async () => {
    reportsApi();
    renderApp("/reports");
    expect(await screen.findByRole("heading", { name: /Reports & exports/ })).toBeInTheDocument();
    const nav = screen.getByRole("complementary", { name: /main navigation/i, hidden: true });
    expect(within(nav).getByRole("link", { name: "Reports", hidden: true })).toHaveAttribute("aria-current", "page");
    for (const r of REPORTS_CATALOG.reports) {
      expect(await screen.findByRole("heading", { name: r.title })).toBeInTheDocument();
      expect(screen.getByText(r.description)).toBeInTheDocument();
    }
    expect(screen.getByText(/up to 2,50,000 per table for CSV, 1,00,000 for Excel/)).toBeInTheDocument();
  });

  it("downloads historical prices for the chosen symbol and dates", async () => {
    const { calls } = reportsApi();
    renderApp("/reports", { symbol: "RELIANCE", exchange: "NSE" });
    await screen.findByRole("heading", { name: "Historical prices" });
    const card = screen.getByRole("heading", { name: "Historical prices" }).closest("section")!;
    await userEvent.type(within(card).getByLabelText("From"), "2026-01-01");
    await userEvent.type(within(card).getByLabelText("To"), "2026-02-01");
    await userEvent.click(within(card).getByRole("button", { name: "Prices as PDF" }));
    await screen.findByText(/Saved BharatMarket_prices_TCS/);
    const sent = calls.find((c) => c.path === "/api/reports/prices")!.url.searchParams;
    expect(Object.fromEntries(sent)).toMatchObject({ symbol: "RELIANCE", exchange: "NSE", interval: "1d", start: "2026-01-01T00:00:00Z", end: "2026-02-01T00:00:00Z", format: "pdf" });
  });

  it("exports only a saved backtest, chosen from the list", async () => {
    const { calls } = reportsApi({ "/api/reports/backtest": () => fileResponse("bt.csv", 5) });
    renderApp("/reports");
    const card = (await screen.findByRole("heading", { name: "Backtest result" })).closest("section")!;
    const csv = within(card).getByRole("button", { name: "Backtest as CSV" });
    expect(csv).toBeDisabled();
    const select = await within(card).findByLabelText("Saved backtest");
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBeGreaterThan(1));
    await userEvent.selectOptions(select, "bt_1");
    expect(csv).toBeEnabled();
    await userEvent.click(csv);
    await waitFor(() => expect(calls.some((c) => c.path === "/api/reports/backtest")).toBe(true));
    expect(calls.find((c) => c.path === "/api/reports/backtest")!.url.searchParams.get("run_id")).toBe("bt_1");
  });

  it("needs two symbols to compare", async () => {
    reportsApi();
    renderApp("/reports");
    const card = (await screen.findByRole("heading", { name: "Stock comparison" })).closest("section")!;
    expect(within(card).getByRole("button", { name: "Comparison as PDF" })).toBeEnabled();
    await userEvent.click(within(card).getByRole("button", { name: "Remove TCS" }));
    await userEvent.click(within(card).getByRole("button", { name: "Remove NIFTY_50" }));
    expect(within(card).getByRole("button", { name: "Comparison as PDF" })).toBeDisabled();
  });

  it("shows an error if the report list cannot be loaded", async () => {
    mockApi({ ...base, "/api/reports": () => jsonError(500, "internal_error", "Something went wrong.", "rid-99999999") });
    renderApp("/reports");
    const alert = await screen.findByRole("alert", {}, { timeout: 8000 });
    expect(alert).toHaveTextContent("Could not load the report list");
  });
});

describe("export buttons on the other pages", () => {
  it("the Backtesting result offers a download once saved, and explains how otherwise", async () => {
    const api = (doc: Record<string, unknown>) => mockApi({
      ...base, "/api/instruments/RELIANCE": () => RELIANCE_DETAIL, "/api/instruments": () => INSTRUMENTS_LIST, "/api/backtests/strategies": () => STRATEGIES,
      "/api/backtests/leaderboard": () => jsonError(404, "no_runs", "none"), "/api/backtests": () => BACKTESTS_LIST, "POST /api/backtests/run": () => doc,
    });
    api({ ...BACKTEST_DOC, stored: false });
    const first = renderApp("/backtest", { symbol: "RELIANCE" });
    await userEvent.click(await screen.findByRole("button", { name: "Run backtest" }));
    expect(await screen.findByText(/run it again with “Save this result” ticked/)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Download this result" })).not.toBeInTheDocument();
    first.unmount();
    api({ ...BACKTEST_DOC, stored: true });
    renderApp("/backtest", { symbol: "RELIANCE" });
    await userEvent.click(await screen.findByRole("button", { name: "Run backtest" }));
    expect(await screen.findByRole("group", { name: "Download this result" })).toBeInTheDocument();
  });

  it("the Overview, Sectors and Data pages have their downloads", async () => {
    mockApi({ ...base, "/api/market/overview": () => OVERVIEW, "/api/sectors": () => ({ sectors: [], unassigned: [] }), "/api/jobs": () => JOBS, "/api/benchmarks": () => BENCHMARKS });
    const overview = renderApp("/");
    expect(await screen.findByRole("group", { name: "Download the performance analysis" })).toBeInTheDocument();
    overview.unmount();
    const sectors = renderApp("/sectors");
    expect(await screen.findByRole("group", { name: "Download sector analytics" })).toBeInTheDocument();
    sectors.unmount();
    renderApp("/data");
    const card = (await screen.findByRole("heading", { name: "Performance benchmarks" })).closest("section")!;
    expect(within(card).getByRole("group", { name: "Download" })).toBeInTheDocument();
  });
});
