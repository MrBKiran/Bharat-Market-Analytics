import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, buildUrl, getApiKey, setApiKey, uploadFile } from "../api/client";
import { fetchPages } from "../api/hooks";
import { jsonError, mockApi } from "./utils";

afterEach(() => vi.unstubAllGlobals());

describe("buildUrl", () => {
  it("drops empty values and encodes the rest", () => {
    expect(buildUrl("/api/prices/TCS", { interval: "1d", start: undefined, end: "", cursor: null, limit: 500, q: "M&M" })).toBe("/api/prices/TCS?interval=1d&limit=500&q=M%26M");
    expect(buildUrl("/api/health")).toBe("/api/health");
    expect(buildUrl("/x", { flag: false })).toBe("/x?flag=false");
  });
});

describe("request errors", () => {
  it("turns the API's error envelope into an ApiError with the request id", async () => {
    mockApi({ "/api/prices/NOPE": () => jsonError(404, "not_found", "No instrument called NOPE on NSE.", "abc-1234567") });
    const err: any = await api.get("/api/prices/NOPE").catch((e) => e as any);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 404, code: "not_found", message: "No instrument called NOPE on NSE.", requestId: "abc-1234567" });
  });

  it("copes with an error body that is not JSON", async () => {
    mockApi({ "/api/x": () => new Response("<html>Bad gateway</html>", { status: 502, statusText: "Bad Gateway", headers: { "x-request-id": "gw-1" } }) });
    const err: any = await api.get("/api/x").catch((e) => e as any);
    expect(err).toMatchObject({ status: 502, code: "http_error", requestId: "gw-1" });
    expect(err.message).toContain("502");
  });

  it("reports an unreachable server in words a person can act on", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const err: any = await api.get("/api/health").catch((e) => e as any);
    expect(err).toMatchObject({ status: 0, code: "network_error" });
    expect(err.message).toMatch(/Cannot reach the server/);
  });

  it("lets an aborted request stay an abort, not an error banner", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw Object.assign(new Error("aborted"), { name: "AbortError" }); }));
    const err: any = await api.get("/api/health").catch((e) => e as any);
    expect(err.name).toBe("AbortError");
  });

  it("flags authentication problems", async () => {
    mockApi({ "POST /api/processing/adjust": () => jsonError(401, "unauthorized", "A valid X-API-Key header is required.") });
    const err: any = await api.post("/api/processing/adjust").catch((e) => e as any);
    expect(err.isAuth).toBe(true);
  });
});

describe("the API key", () => {
  it("is sent only when set, and lives in sessionStorage", async () => {
    const { calls, fake } = mockApi({ "/api/stats": () => ({}) });
    await api.get("/api/stats");
    expect((fake.mock.calls[0][1]!.headers as Record<string, string>)["X-API-Key"]).toBeUndefined();
    setApiKey("  not-trimmed? ");
    expect(getApiKey()).toBe("  not-trimmed? ");
    await api.get("/api/stats");
    expect((fake.mock.calls[1][1]!.headers as Record<string, string>)["X-API-Key"]).toBe("  not-trimmed? ");
    expect(localStorage.getItem("bm-api-key")).toBeNull();                       // never persisted beyond the tab
    setApiKey("");
    expect(getApiKey()).toBe("");
    expect(calls).toHaveLength(2);
  });

  it("sends JSON bodies with the right content type", async () => {
    const { fake, calls } = mockApi({ "POST /api/backtests/run": () => ({ ok: true }) });
    await api.post("/api/backtests/run", { symbol: "TCS" });
    expect((fake.mock.calls[0][1]!.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(calls[0].body).toEqual({ symbol: "TCS" });
  });
});

describe("uploadFile", () => {
  class FakeXhr {
    static last: FakeXhr;
    upload: { onprogress?: (e: { lengthComputable: boolean; loaded: number; total: number }) => void } = {};
    headers: Record<string, string> = {};
    status = 200;
    responseText = "";
    onload: () => void = () => {};
    onerror: () => void = () => {};
    constructor() { FakeXhr.last = this; }
    open() {}
    setRequestHeader(k: string, v: string) { this.headers[k] = v; }
    getResponseHeader() { return "up-1"; }
    send() {}
  }

  it("reports progress and resolves with the server's JSON", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    setApiKey("k1");
    const seen: number[] = [];
    const promise = uploadFile<{ status: string }>("/api/imports/upload", new FormData(), (f) => seen.push(f));
    FakeXhr.last.upload.onprogress!({ lengthComputable: true, loaded: 50, total: 200 });
    FakeXhr.last.upload.onprogress!({ lengthComputable: true, loaded: 200, total: 200 });
    FakeXhr.last.responseText = JSON.stringify({ status: "completed" });
    FakeXhr.last.onload();
    await expect(promise).resolves.toEqual({ status: "completed" });
    expect(seen).toEqual([0.25, 1]);
    expect(FakeXhr.last.headers["X-API-Key"]).toBe("k1");
  });

  it("rejects with the server's error message", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const promise = uploadFile("/api/imports/upload", new FormData());
    FakeXhr.last.status = 422;
    FakeXhr.last.responseText = JSON.stringify({ error: { code: "not_a_csv", message: "The file is named .csv but contains binary data.", request_id: "u-1" } });
    FakeXhr.last.onload();
    await expect(promise).rejects.toMatchObject({ status: 422, code: "not_a_csv", requestId: "u-1" });
  });

  it("rejects when the network fails", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const promise = uploadFile("/api/imports/upload", new FormData());
    FakeXhr.last.onerror();
    await expect(promise).rejects.toMatchObject({ code: "network_error" });
  });
});

describe("fetchPages (cursor paging)", () => {
  const page = (items: number[], next: string | null) => ({ items, next_cursor: next });

  it("follows the cursor until the server says there is no more, and returns oldest first", async () => {
    const seen: (string | undefined)[] = [];
    const pages: Record<string, ReturnType<typeof page>> = { start: page([9, 8, 7], "c1"), c1: page([6, 5], "c2"), c2: page([4], null) };
    const out = await fetchPages(async (cursor) => { seen.push(cursor); return pages[cursor ?? "start"]; }, 1000);
    expect(seen).toEqual([undefined, "c1", "c2"]);
    expect(out.items).toEqual([4, 5, 6, 7, 8, 9]);
    expect(out.truncated).toBe(false);
  });

  it("stops at the cap and says the history was cut", async () => {
    let n = 0;
    const out = await fetchPages(async () => page([n++, n++], "more"), 4, 10);
    expect(out.items).toHaveLength(4);
    expect(out.truncated).toBe(true);
  });

  it("never loops forever on a server that always has more", async () => {
    let calls = 0;
    const out = await fetchPages(async () => { calls++; return page([1], "again"); }, 1_000_000, 5);
    expect(calls).toBe(5);
    expect(out.truncated).toBe(true);
  });
});
