import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ScalingChart, linearTicks, logTicks, shortNumber } from "../components/ScalingChart";
import { formatSeconds } from "../pages/Performance";
import { BENCHMARK_SCALING, HEALTH, REPORTS_CATALOG, STATS, jsonError, mockApi, renderApp } from "./utils";

const base = { "/api/health": () => HEALTH, "/api/stats": () => STATS, "/api/reports": () => REPORTS_CATALOG, "/api/benchmarks/scaling": () => BENCHMARK_SCALING };

describe("chart helpers", () => {
  it("writes data sizes the way the brief does", () => {
    expect([100_000, 1_000_000, 2_500_000, 10_000_000, 950].map(shortNumber)).toEqual(["100K", "1M", "2.5M", "10M", "950"]);
    expect([295_086, 839_439, 1_048_738, 5_392_391].map(shortNumber)).toEqual(["295K", "839K", "1.05M", "5.39M"]);      // real row counts: three significant figures
  });

  it("picks round axis ticks", () => {
    expect(logTicks(0.004, 12)).toEqual([0.001, 0.01, 0.1, 1, 10, 100]);
    expect(logTicks(5, 5)).toEqual([1, 10]);                                       // a single value still gets a span
    expect(linearTicks(1_700_000)).toEqual([0, 500_000, 1_000_000, 1_500_000, 2_000_000]);
    expect(linearTicks(0)).toEqual([0, 1]);
  });

  it("formats times in milliseconds below a second", () => {
    expect([0.004, 0.25, 1, 1.5, 12.34, 250.4].map(formatSeconds)).toEqual(["4 ms", "250 ms", "1 s", "1.5 s", "12.3 s", "250 s"]);
  });
});

describe("ScalingChart", () => {
  const series = [
    { name: "pandas", color: "var(--c1)", points: [{ x: 1_000_000, y: 0.65 }, { x: 100_000, y: 0.1 }] },
    { name: "PySpark", color: "var(--c3)", points: [{ x: 100_000, y: 0.88 }] },
  ];
  const draw = (extra = {}) => render(<ScalingChart title="Time by data size" series={series} xLabel="Records" yLabel="Time" formatY={formatSeconds} logY {...extra} />);

  it("draws a labelled chart with a legend, a point for every measurement and the numbers as a table", () => {
    draw();
    const chart = screen.getByRole("img", { name: /Time by data size/ });
    expect(chart.querySelectorAll("circle")).toHaveLength(3);
    expect(chart.querySelectorAll("path")).toHaveLength(2);
    expect(chart).toHaveAccessibleDescription(/pandas, PySpark\. 2 data sizes from 100K to 1M records/);
    expect(within(screen.getByRole("list", { name: "Legend" })).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["pandas", "PySpark"]);
    expect([...chart.querySelectorAll("title")].map((t) => t.textContent)).toContain("pandas, 1M records: 650 ms");
    const table = screen.getByRole("table", { hidden: true });
    expect(within(table).getAllByRole("row", { hidden: true }).map((r) => r.textContent)).toEqual(["RecordspandasPySpark", "100K100 ms880 ms", "1M650 ms–"]);
  });

  it("puts the larger size to the right on the log axis and the larger time higher up", () => {
    draw();
    const [small, large] = [...screen.getByRole("img").querySelectorAll("path")[0].getAttribute("d")!.matchAll(/[ML]([\d.]+) ([\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])]);
    expect(large[0]).toBeGreaterThan(small[0]);          // 1M is right of 100K
    expect(large[1]).toBeLessThan(small[1]);             // 0.65 s is above 0.1 s (SVG y grows downwards)
  });

  it("leaves out points a log axis cannot show, and renders nothing when none are left", () => {
    const { container } = render(<ScalingChart title="t" series={[{ name: "a", color: "red", points: [{ x: 1, y: 0 }, { x: 2, y: -1 }] }]} xLabel="x" yLabel="y" formatY={String} logY />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a footnote when given one", () => {
    draw({ footnote: "Lower is better." });
    expect(screen.getByText("Lower is better.")).toBeInTheDocument();
  });
});

describe("Performance page", () => {
  it("is in the sidebar, shows the synthetic data first with its warning, and charts every kind of measurement", async () => {
    mockApi(base);
    renderApp("/performance");
    expect(await screen.findByRole("heading", { name: "Performance" })).toBeInTheDocument();
    const nav = screen.getByRole("complementary", { name: /main navigation/i, hidden: true });
    expect(within(nav).getByRole("link", { name: "Performance", hidden: true })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("Synthetic benchmark data")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Synthetic data" })).toHaveAttribute("aria-pressed", "true");
    for (const title of ["Rows written per second by data size", "Storage in megabytes by data size", "Daily range per symbol: time by data size", "One symbol, five days (indexed): time by data size"]) {
      expect(screen.getByRole("img", { name: title })).toBeInTheDocument();
    }
    expect(screen.getByText("100K, 1M, 5M")).toBeInTheDocument();                  // the data sizes tested
    expect(screen.getByText("12 cores")).toBeInTheDocument();
  });

  it("lists the jobs that could not be measured, with the reason", async () => {
    mockApi(base);
    renderApp("/performance");
    const banner = (await screen.findByText("Some jobs could not be measured")).closest("div")!;
    expect(within(banner).getByText(/5M records, Daily range per symbol on pandas: skipped\. not enough free memory/)).toBeInTheDocument();
  });

  it("keeps real and synthetic data apart: the real tab shows only the real timings and says what is missing", async () => {
    mockApi(base);
    renderApp("/performance");
    await screen.findByRole("heading", { name: "Performance" });
    await userEvent.click(screen.getByRole("button", { name: "Real market data" }));
    expect(screen.getByText("Real market data", { selector: "strong, h2, h3, div, span" })).toBeInTheDocument();
    expect(screen.queryByText("Synthetic benchmark data")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: /1-minute to 5-minute bars: time by data size/ })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /Rows written per second/ })).not.toBeInTheDocument();
    expect(screen.getByText("No ingestion measured on real data")).toBeInTheDocument();
    expect(screen.getByText("1.05M, 2.1M")).toBeInTheDocument();                   // the real data sizes (Phase 8: 1.05 and 2.1 million rows)
  });

  it("switches the aggregation chart between time and rows per second, and between jobs", async () => {
    mockApi(base);
    renderApp("/performance");
    await screen.findByRole("img", { name: /Daily range per symbol: time by data size/ });
    await userEvent.click(screen.getByRole("button", { name: "Rows per second" }));
    expect(screen.getByRole("img", { name: "Daily range per symbol: rows per second by data size" })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /Daily range per symbol: time by data size/ })).not.toBeInTheDocument();
  });

  it("shows every job at the chosen size, flagging an engine that gave a different answer", async () => {
    mockApi(base);
    renderApp("/performance");
    const card = (await screen.findByRole("heading", { name: "Time, CPU and memory" })).closest("section")!;
    expect(within(card).getByRole("button", { name: "5M" })).toHaveAttribute("aria-pressed", "true");             // the largest size first
    await userEvent.click(within(card).getByRole("button", { name: "1M" }));
    const rows = within(card).getAllByRole("row").slice(1);
    const spark = rows.find((r) => /Daily range per symbol/.test(r.textContent ?? "") && /PySpark/.test(r.textContent ?? ""))!;
    expect(within(spark).getByText("Different")).toBeInTheDocument();
    expect(within(spark).getByText("1.15 s")).toBeInTheDocument();
    const mongoInsert = rows.find((r) => /Insert into MongoDB/.test(r.textContent ?? ""))!;
    expect(within(mongoInsert).getByText("78,000")).toBeInTheDocument();
  });

  it("explains how to produce the numbers when none exist yet", async () => {
    mockApi({ ...base, "/api/benchmarks/scaling": () => ({ synthetic: { sizes: [], operations: [], runs: [] }, real: { sizes: [], operations: [] }, notes: [] }) });
    renderApp("/performance");
    expect(await screen.findByText("No ingestion measured yet")).toBeInTheDocument();
    expect(screen.getAllByText(/scripts\\performance\.py run/).length).toBeGreaterThan(0);
  });

  it("shows the server's message when the results cannot be loaded", async () => {
    mockApi({ ...base, "/api/benchmarks/scaling": () => jsonError(500, "internal_error", "database exploded") });
    renderApp("/performance");
    await waitFor(() => expect(screen.getByText(/Could not load the benchmark results/)).toBeInTheDocument());
    expect(screen.getByText(/database exploded/)).toBeInTheDocument();
  });
});
