// The dashboard's test fixtures must satisfy the same contract the real API is tested against (tests/test_contract.py).
import { describe, expect, it } from "vitest";
import contract from "./contract.json";
import {
  BACKTESTS_LIST, BACKTEST_DOC, BARS_DESC, BENCHMARKS, COMPARE, ERROR_BODY, HEALTH, INDICATORS, INSTRUMENTS_LIST, JOBS, LATEST, LEADERBOARD, OVERVIEW,
  PRICES, PROCESSING_START, RELIANCE_DETAIL, REPORTS_CATALOG, SEARCH, SECTORS, STATS, STRATEGIES, UPLOAD_RESULT,
} from "./utils";

const isType = (value: unknown, kind: string): boolean =>
  kind === "number" ? typeof value === "number" && Number.isFinite(value)
  : kind === "array" ? Array.isArray(value)
  : kind === "object" ? typeof value === "object" && value !== null && !Array.isArray(value)
  : typeof value === kind;

function* leaves(node: unknown, parts: string[], trail: string, errors: string[]): Generator<unknown> {
  if (parts.length === 0) { yield node; return; }
  const [part, ...rest] = parts;
  const isObject = typeof node === "object" && node !== null && !Array.isArray(node);
  if (part === "*") {
    if (!isObject) { errors.push(`${trail}: expected an object`); return; }
    for (const [key, value] of Object.entries(node as object)) yield* leaves(value, rest, `${trail}.${key}`, errors);
  } else if (part.endsWith("[]")) {
    const key = part.slice(0, -2);
    if (!isObject || !(key in (node as object))) { errors.push(`${trail}.${key}: missing`); return; }
    const list = (node as Record<string, unknown>)[key];
    if (!Array.isArray(list)) { errors.push(`${trail}.${key}: expected a list`); return; }
    for (let i = 0; i < list.length; i++) yield* leaves(list[i], rest, `${trail}.${key}[${i}]`, errors);
  } else {
    if (!isObject || !(part in (node as object))) { errors.push(`${trail}.${part}: missing`); return; }
    yield* leaves((node as Record<string, unknown>)[part], rest, `${trail}.${part}`, errors);
  }
}

export function check(data: unknown, fields: string[]): string[] {
  const errors: string[] = [];
  for (const spec of fields) {
    const colon = spec.indexOf(":");
    const path = colon < 0 ? spec : spec.slice(0, colon);
    const kind = colon < 0 ? "" : spec.slice(colon + 1);
    const before = errors.length;
    const found = [...leaves(data, path.split("."), "$", errors)];
    if (found.length === 0 && errors.length === before && (path.includes("[]") || path.includes("*"))) {
      errors.push(`${path}: matched nothing, so it proved nothing`);
    }
    if (kind) for (const value of found) if (value !== null && !isType(value, kind)) errors.push(`${path}: expected ${kind}, got ${typeof value} (${JSON.stringify(value)?.slice(0, 40)})`);
  }
  return errors;
}

const fixtures: Record<string, unknown> = {
  health: HEALTH, instruments_list: INSTRUMENTS_LIST, instrument_detail: RELIANCE_DETAIL, prices: PRICES, latest: LATEST, indicators: INDICATORS, search: SEARCH,
  sectors: SECTORS, overview: OVERVIEW, compare: COMPARE, stats: STATS, strategies: STRATEGIES, backtest_run: BACKTEST_DOC, backtests_list: BACKTESTS_LIST,
  leaderboard: LEADERBOARD, jobs: JOBS, benchmarks: BENCHMARKS, upload_result: UPLOAD_RESULT, processing_start: PROCESSING_START, reports_catalog: REPORTS_CATALOG, error: ERROR_BODY,
};
const entries = Object.entries(contract as Record<string, unknown>).filter(([name]) => !name.startsWith("_")) as [string, string[]][];

describe("API contract", () => {
  it("has a fixture for every endpoint in the contract, and nothing extra", () => {
    expect(Object.keys(fixtures).sort()).toEqual(entries.map(([name]) => name).sort());
  });

  it.each(entries)("the %s fixture has the shape the dashboard reads", (name, fields) => {
    expect(check(fixtures[name], fields)).toEqual([]);
  });

  it("uses the same bars in the price fixtures", () => {
    expect(PRICES.items).toBe(BARS_DESC);
  });
});

describe("the contract checker", () => {
  it("reports missing keys, wrong types and vacuous paths, and allows null for a present key", () => {
    const data = { a: { b: 1, c: null }, items: [{ x: 1 }, { x: "no" }], empty: [], by: { k1: { v: 1 }, k2: {} } };
    expect(check(data, ["a.b:number", "a.c", "a.c:string"])).toEqual([]);
    expect(check(data, ["a.zzz"])).toEqual(["$.a.zzz: missing"]);
    expect(check(data, ["a.b:string"])[0]).toContain("a.b: expected string");
    expect(check(data, ["items[].x:number"])).toHaveLength(1);
    expect(check(data, ["by.*.v"])).toEqual(["$.by.k2.v: missing"]);
    expect(check(data, ["empty[].y"])[0]).toContain("matched nothing");
    expect(check({ n: true }, ["n:number"])).toHaveLength(1);
  });

  it("catches a renamed field in a fixture (what drift looks like)", () => {
    const drifted = { ...STATS, collections: { ...STATS.collections, adjusted_prices: undefined } };
    expect(check(JSON.parse(JSON.stringify(drifted)), contract.stats)[0]).toContain("collections.adjusted_prices: missing");
  });
});
