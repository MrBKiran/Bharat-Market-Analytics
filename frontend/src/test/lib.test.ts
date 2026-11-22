import { describe, expect, it } from "vitest";
import { chartTime, daysBetween, isIntraday, presetStart } from "../lib/dates";
import { DASH, arrowOf, compactINR, compactNumber, formatDate, formatDateTime, formatINR, formatNumber, formatPct, formatRatio, titleCase, toneOf } from "../lib/format";

describe("Indian number formatting", () => {
  it("groups digits the Indian way with the rupee sign", () => {
    expect(formatINR(123456.78)).toBe("₹1,23,456.78");
    expect(formatINR(1377.5)).toBe("₹1,377.50");
    expect(formatINR(12345678.9, 0)).toBe("₹1,23,45,679");
    expect(formatINR(-2500)).toContain("2,500.00");
    expect(formatNumber(1234567)).toBe("12,34,567");
  });

  it("writes large amounts in lakh and crore", () => {
    expect(compactINR(12345678)).toBe("₹1.23 Cr");
    expect(compactINR(450000)).toBe("₹4.50 L");
    expect(compactINR(-25000000)).toBe("-₹2.50 Cr");
    expect(compactINR(12345)).toBe("₹12,345");
    expect(compactNumber(13167730)).toBe("1.32 Cr");
    expect(compactNumber(250000)).toBe("2.50 L");
    expect(compactNumber(9999)).toBe("9,999");
  });

  it("shows a dash for anything that is not a number, never NaN or undefined", () => {
    for (const bad of [null, undefined, NaN, Infinity]) {
      expect(formatINR(bad)).toBe(DASH);
      expect(compactINR(bad)).toBe(DASH);
      expect(formatNumber(bad)).toBe(DASH);
      expect(formatPct(bad)).toBe(DASH);
      expect(formatRatio(bad)).toBe(DASH);
    }
  });

  it("formats percentages with an explicit sign when asked", () => {
    expect(formatPct(2.5, 1, true)).toBe("+2.5%");
    expect(formatPct(-3.442)).toBe("-3.44%");
    expect(formatPct(0, 2, true)).toBe("0.00%");
    expect(formatPct(1234.5, 1)).toBe("1,234.5%");
    expect(formatRatio(1.8)).toBe("1.80");
  });

  it("derives direction from the sign, so colour is never the only signal", () => {
    expect(toneOf(1)).toBe("up");
    expect(toneOf(-1)).toBe("down");
    expect(toneOf(0)).toBe("flat");
    expect(toneOf(null)).toBe("flat");
    expect([arrowOf(2), arrowOf(-2), arrowOf(0)]).toEqual(["▲", "▼", "•"]);
  });

  it("prints dates in Indian Standard Time", () => {
    expect(formatDate("2026-11-20T00:00:00Z")).toBe("20 Nov 2026");
    expect(formatDateTime("2026-01-05T03:45:00Z")).toContain("09:15");      // 09:15 IST is 03:45 UTC
    expect(formatDateTime("2026-01-05T03:45:00Z")).toContain("IST");
    expect(formatDate(null)).toBe(DASH);
    expect(formatDate("not a date")).toBe(DASH);
    expect(titleCase("ema_crossover")).toBe("Ema Crossover");
  });
});

describe("date ranges and chart times", () => {
  it("measures presets back from the last available bar, not from today", () => {
    expect(presetStart("1Y", "2021-04-30T00:00:00Z")).toBe("2020-04-30");
    expect(presetStart("3M", "2026-11-20T00:00:00Z")).toBe("2026-08-20");
    expect(presetStart("10Y", "2026-11-20")).toBe("2016-11-20");
  });

  it("returns no lower limit for All and for custom ranges", () => {
    expect(presetStart("MAX", "2026-11-20")).toBeUndefined();
    expect(presetStart("CUSTOM", "2026-11-20")).toBeUndefined();
  });

  it("clamps the day at month ends instead of overflowing into the next month", () => {
    expect(presetStart("1M", "2026-03-31")).toBe("2026-02-28");
    expect(presetStart("1Y", "2024-02-29")).toBe("2023-02-28");
    expect(presetStart("3M", "2026-01-31")).toBe("2025-10-31");
    expect(presetStart("6M", "2026-02-15")).toBe("2025-08-15");
  });

  it("gives daily bars their trading date and intraday bars an IST-shifted time", () => {
    expect(chartTime("2026-11-20T00:00:00Z", false)).toBe("2026-11-20");
    const utc = Date.parse("2026-01-05T03:45:00Z") / 1000;
    expect(chartTime("2026-01-05T03:45:00Z", true)).toBe(utc + 19800);   // the chart prints it as 09:15
    expect(isIntraday("1d")).toBe(false);
    expect(isIntraday("5m")).toBe(true);
    expect(daysBetween("2026-01-01", "2026-01-31")).toBe(30);
  });
});
