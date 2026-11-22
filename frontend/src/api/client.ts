// The only place that talks to the server. Every call goes through `request`, so errors, the API key and timeouts are
// handled once.

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
const KEY_STORAGE = "bm-api-key";

/** An error the user can be shown: the server's message plus the request id to quote when reporting it. */
export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  requestId: string | null;
  constructor(status: number, code: string, message: string, details: unknown = null, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
  get isAuth(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

// The API key (needed only for endpoints that change data) lives in sessionStorage: it disappears when the tab closes and is
// never part of the built code.
export function getApiKey(): string {
  try {
    return sessionStorage.getItem(KEY_STORAGE) ?? "";
  } catch {
    return "";
  }
}
export function setApiKey(key: string): void {
  try {
    if (key) sessionStorage.setItem(KEY_STORAGE, key);
    else sessionStorage.removeItem(KEY_STORAGE);
  } catch {
    /* private mode: the key just is not remembered */
  }
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return `${BASE}${path}${qs ? `?${qs}` : ""}`;
}

/** Turn a failed response into an ApiError, whatever the body looks like. */
export async function toApiError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("x-request-id");
  try {
    const body = await response.json();
    const err = body?.error;
    if (err && typeof err === "object") {
      return new ApiError(response.status, String(err.code ?? "error"), String(err.message ?? response.statusText), err.details ?? null, err.request_id ?? requestId);
    }
  } catch {
    /* not JSON */
  }
  return new ApiError(response.status, "http_error", `The server answered ${response.status} ${response.statusText}`.trim(), null, requestId);
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
}

export async function request<T>(path: string, { method = "GET", query, body, signal }: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), { method, headers, signal, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (err) {
    if ((err as Error)?.name === "AbortError") throw err;
    throw new ApiError(0, "network_error", "Cannot reach the server. Is the API running (uvicorn on port 8000)?");
  }
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query) => request<T>(path, { method: "POST", body, query }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

export interface DownloadedReport {
  filename: string;
  rows: number | null;
  bytes: number;
}

/** The file name the server chose, from its Content-Disposition header (never from user input). */
export function filenameFrom(response: Response, fallback: string): string {
  const header = response.headers.get("content-disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(header) ?? /filename=([^;]+)/.exec(header);
  const name = match?.[1]?.trim() ?? response.headers.get("x-report-filename") ?? fallback;
  return name.replace(/[^A-Za-z0-9._-]/g, "_");
}

/**
 * Ask the server to build a report and save the file in the browser. Errors (a missing symbol, no data in the range, too many rows)
 * arrive as normal ApiErrors so the page can show the server's message. `fetch` is used rather than a plain link so a failure shows a
 * message instead of replacing the page with JSON.
 */
export async function downloadReport(reportId: string, format: "csv" | "xlsx" | "pdf", params: Query = {}, table?: string): Promise<DownloadedReport> {
  let response: Response;
  try {
    response = await fetch(buildUrl(`/api/reports/${encodeURIComponent(reportId)}`, { ...params, format, table }), { headers: { Accept: "*/*" } });
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the server. Is the API running (uvicorn on port 8000)?");
  }
  if (!response.ok) throw await toApiError(response);
  const blob = await response.blob();
  const filename = filenameFrom(response, `${reportId}.${table === "all" ? "zip" : format}`);
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
  const rows = Number(response.headers.get("x-report-rows"));
  return { filename, rows: Number.isFinite(rows) && response.headers.has("x-report-rows") ? rows : null, bytes: blob.size };
}

/** Upload a file with progress (fetch cannot report upload progress, XMLHttpRequest can). */
export function uploadFile<T>(path: string, form: FormData, onProgress?: (fraction: number) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", buildUrl(path));
    xhr.setRequestHeader("Accept", "application/json");
    const key = getApiKey();
    if (key) xhr.setRequestHeader("X-API-Key", key);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onerror = () => reject(new ApiError(0, "network_error", "The upload could not reach the server."));
    xhr.onload = () => {
      let body: any = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body as T);
      const err = body?.error;
      reject(new ApiError(xhr.status, err?.code ?? "http_error", err?.message ?? `The server answered ${xhr.status}`, err?.details ?? null, err?.request_id ?? xhr.getResponseHeader("x-request-id")));
    };
    xhr.send(form);
  });
}
