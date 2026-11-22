import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    /* ignore */
  }
});

// jsdom lacks a few browser features the app uses.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null, addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as any).ResizeObserver ??= ResizeObserverStub;
if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); this.dispatchEvent(new Event("close")); };
}
window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;

// The charting library draws on a <canvas>, which jsdom does not implement. Tests check what the page shows around the
// chart, so the chart itself is replaced by an inert stub.
vi.mock("lightweight-charts", () => {
  const series = () => ({ setData: vi.fn(), applyOptions: vi.fn(), createPriceLine: vi.fn(), priceScale: () => ({ applyOptions: vi.fn() }) });
  return {
    createChart: () => ({
      addSeries: vi.fn(series), removeSeries: vi.fn(), applyOptions: vi.fn(), remove: vi.fn(), subscribeCrosshairMove: vi.fn(),
      priceScale: () => ({ applyOptions: vi.fn() }), timeScale: () => ({ fitContent: vi.fn() }), panes: () => [{ setHeight: vi.fn() }],
    }),
    CandlestickSeries: "candlestick", HistogramSeries: "histogram", LineSeries: "line",
    ColorType: { Solid: "solid" }, CrosshairMode: { Normal: 0 }, LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
  };
});
