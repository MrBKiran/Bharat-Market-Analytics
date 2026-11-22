// Date-range presets and the time values the chart library needs.

export type RangePreset = "1M" | "3M" | "6M" | "1Y" | "3Y" | "5Y" | "10Y" | "MAX" | "CUSTOM";
export const RANGE_PRESETS: RangePreset[] = ["1M", "3M", "6M", "1Y", "3Y", "5Y", "10Y", "MAX"];

const MONTHS: Record<string, number> = { "1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60, "10Y": 120 };

/** YYYY-MM-DD in UTC. */
export const isoDate = (d: Date): string => d.toISOString().slice(0, 10);

/**
 * First date of a preset window ending at `endIso`. The window is measured back from the LAST AVAILABLE bar, not from
 * today, so "1Y" on a stock whose data stops in 2021 still shows its final year. MAX returns undefined (no lower limit).
 */
export function presetStart(preset: RangePreset, endIso: string): string | undefined {
  const months = MONTHS[preset];
  if (!months) return undefined;
  const end = new Date(endIso.slice(0, 10) + "T00:00:00Z");
  // Count back whole calendar months, clamping the day: one month before 31 March is 28 February, not 3 March.
  const month = end.getUTCMonth() - months;
  const lastDay = new Date(Date.UTC(end.getUTCFullYear(), month + 1, 0)).getUTCDate();
  return isoDate(new Date(Date.UTC(end.getUTCFullYear(), month, Math.min(end.getUTCDate(), lastDay))));
}

const IST_OFFSET_SECONDS = 5.5 * 3600;

/**
 * The value the chart library wants as a bar's time. Daily bars use their trading date (a "business day" string, so there
 * are no timezone surprises). Intraday bars use a Unix time shifted by +5:30, because the library prints times as UTC and
 * the Indian market is read in IST.
 */
export function chartTime(ts: string, intraday: boolean): string | number {
  if (!intraday) return ts.slice(0, 10);
  return Math.floor(Date.parse(ts) / 1000) + IST_OFFSET_SECONDS;
}

export const isIntraday = (interval: string): boolean => interval !== "1d";

export function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000);
}
