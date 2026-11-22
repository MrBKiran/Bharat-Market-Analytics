import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { ApiKeyDialog } from "../components/ApiKeyDialog";
import { Change, DataTable, ErrorBox, Pagination, ProgressBar } from "../components/ui";
import { Heatmap, SignedBars } from "../components/viz";
import { getApiKey } from "../api/client";

describe("ErrorBox", () => {
  it("shows the server's message and the request id to quote", () => {
    render(<ErrorBox error={new ApiError(404, "not_found", "No instrument called XYZ on NSE.", null, "req-abcdef12")} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("No instrument called XYZ on NSE.");
    expect(alert).toHaveTextContent("req-abcdef12");
  });

  it.each([
    [new ApiError(0, "network_error", "Cannot reach the server."), /uvicorn/],
    [new ApiError(401, "unauthorized", "A valid X-API-Key header is required."), /API key/],
    [new ApiError(403, "write_disabled", "disabled"), /production mode/],
    [new ApiError(429, "rate_limited", "Too many requests."), /wait a few seconds/],
    [new ApiError(503, "database_unavailable", "down"), /MongoDB/],
  ])("tells the user what to do next (%#)", (error, hint) => {
    render(<ErrorBox error={error} />);
    expect(screen.getByRole("alert")).toHaveTextContent(hint);
  });

  it("offers a retry button when it can retry", async () => {
    const retry = vi.fn();
    render(<ErrorBox error={new Error("boom")} onRetry={retry} title="Could not load prices" />);
    expect(screen.getByText("Could not load prices")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(retry).toHaveBeenCalledOnce();
  });
});

describe("Pagination", () => {
  it("disables the ends and reports the total in Indian grouping", async () => {
    const onPage = vi.fn();
    const { rerender } = render(<Pagination page={1} pages={3} total={123456} onPage={onPage} noun="trades" />);
    expect(screen.getByText("1,23,456 trades")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /previous/i })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(onPage).toHaveBeenCalledWith(2);
    rerender(<Pagination page={3} pages={3} total={123456} onPage={onPage} />);
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();
  });

  it("renders nothing when there is nothing to page", () => {
    const { container } = render(<Pagination page={1} pages={0} total={0} onPage={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("DataTable", () => {
  const rows = [{ s: "TCS", v: 2 }, { s: "INFY", v: 3 }, { s: "SBIN", v: 1 }];
  const columns = [
    { key: "s", header: "Symbol", render: (r: (typeof rows)[number]) => r.s, sortValue: (r: (typeof rows)[number]) => r.s },
    { key: "v", header: "Value", align: "right" as const, render: (r: (typeof rows)[number]) => r.v, sortValue: (r: (typeof rows)[number]) => r.v },
  ];
  const order = () => within(screen.getByRole("table")).getAllByRole("row").slice(1).map((r) => r.textContent);

  it("sorts by a column on click, and reverses on the second click", async () => {
    render(<DataTable rows={rows} columns={columns} rowKey={(r) => r.s} />);
    expect(order()).toEqual(["TCS2", "INFY3", "SBIN1"]);
    await userEvent.click(screen.getByRole("columnheader", { name: /value/i }));
    expect(order()).toEqual(["INFY3", "TCS2", "SBIN1"]);          // descending first
    await userEvent.click(screen.getByRole("columnheader", { name: /value/i }));
    expect(order()).toEqual(["SBIN1", "TCS2", "INFY3"]);
    expect(screen.getByRole("columnheader", { name: /value/i })).toHaveAttribute("aria-sort", "ascending");
  });

  it("says so when there are no rows", () => {
    render(<DataTable rows={[]} columns={columns} rowKey={(r: any) => r.s} empty="No trades." />);
    expect(screen.getByText("No trades.")).toBeInTheDocument();
  });
});

describe("Change", () => {
  it("shows direction with an arrow and a sign, not colour alone", () => {
    const { container } = render(<><Change value={2.345} /><Change value={-1.5} /><Change value={null} /></>);
    expect(container).toHaveTextContent("+2.35%");
    expect(container).toHaveTextContent("-1.50%");
    expect(container.querySelector(".up")).toHaveTextContent("▲");
    expect(container.querySelector(".down")).toHaveTextContent("▼");
    expect(container.querySelector(".flat")).toHaveTextContent("–");
  });
});

describe("ProgressBar and charts without a canvas", () => {
  it("exposes progress to assistive technology and clamps it", () => {
    render(<ProgressBar value={140} label="Upload" />);
    expect(screen.getByRole("progressbar", { name: "Upload" })).toHaveAttribute("aria-valuenow", "100");
  });

  it("prints every correlation value and marks missing ones", () => {
    render(<Heatmap labels={["A", "B"]} matrix={{ A: { A: 1, B: 0.5 }, B: { A: 0.5, B: null } }} />);
    expect(screen.getAllByRole("cell").map((c) => c.textContent)).toEqual(["1.00", "0.50", "0.50", "–"]);
  });

  it("labels signed bars with their sign", () => {
    render(<SignedBars rows={[{ label: "Banks", value: 18.56 }, { label: "IT", value: -3.17 }]} />);
    expect(screen.getByText("+18.6%")).toBeInTheDocument();
    expect(screen.getByText("-3.2%")).toBeInTheDocument();
  });
});

describe("ApiKeyDialog", () => {
  it("keeps the key for this tab only and can clear it", async () => {
    const onClose = vi.fn();
    render(<ApiKeyDialog open onClose={onClose} />);
    await userEvent.type(screen.getByLabelText("Key"), "my-key");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(getApiKey()).toBe("my-key");
    expect(localStorage.length).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(getApiKey()).toBe("");
    expect(onClose).toHaveBeenCalled();
  });
});
