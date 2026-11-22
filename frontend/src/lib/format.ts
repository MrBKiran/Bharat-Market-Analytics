// Indian market formatting: rupee symbol, lakh/crore digit grouping (1,23,45,678), IST dates.

export const DASH = "–";
const IST = "Asia/Kolkata";

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** ₹1,23,456.78 */
export function formatINR(v: number | null | undefined, digits = 2): string {
  if (!finite(v)) return DASH;
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(v);
}

/** Large rupee amounts the way Indians read them: ₹1.23 Cr, ₹4.50 L, else ₹12,345. */
export function compactINR(v: number | null | undefined): string {
  if (!finite(v)) return DASH;
  const abs = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (abs >= 1e7) return `${sign}₹${(abs / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `${sign}₹${(abs / 1e5).toFixed(2)} L`;
  return formatINR(v, 0);
}

/** 1,23,456 (en-IN grouping) */
export function formatNumber(v: number | null | undefined, digits = 0): string {
  if (!finite(v)) return DASH;
  return new Intl.NumberFormat("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
}

/** Share volumes: 1.23 Cr, 4.50 L, 12,345. */
export function compactNumber(v: number | null | undefined): string {
  if (!finite(v)) return DASH;
  const abs = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(2)} L`;
  return formatNumber(v, 0);
}

export function formatPct(v: number | null | undefined, digits = 2, signed = false): string {
  if (!finite(v)) return DASH;
  const text = `${Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
  if (v < 0) return `-${text}`;
  return signed && v > 0 ? `+${text}` : text;
}

/** Plain decimal such as a ratio or a Sharpe value. */
export function formatRatio(v: number | null | undefined, digits = 2): string {
  return finite(v) ? v.toFixed(digits) : DASH;
}

export type Tone = "up" | "down" | "flat";
export const toneOf = (v: number | null | undefined): Tone => (!finite(v) || v === 0 ? "flat" : v > 0 ? "up" : "down");
export const arrowOf = (v: number | null | undefined): string => (toneOf(v) === "up" ? "▲" : toneOf(v) === "down" ? "▼" : "•");

/** "20 Nov 2026" (daily bars are dated by their trading day; the 00:00Z stamp is the same calendar day in IST). */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "2-digit", month: "short", year: "numeric" }).format(d);
}

/** "20 Nov 2026, 09:15 IST" */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  const text = new Intl.DateTimeFormat("en-IN", {
    timeZone: IST, day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(d);
  return `${text} IST`;
}

export function titleCase(text: string): string {
  return text.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
