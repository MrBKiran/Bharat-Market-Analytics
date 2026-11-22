# REST API (Phase 10)

36 endpoints under `/api` for market data, instruments, indicators, analytics, comparison, import, processing, backtesting, reports and
system information. Interactive documentation with a "Try it out" button is generated automatically:

```powershell
.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --reload
```
| What | Where |
|---|---|
| Interactive docs (Swagger) | http://127.0.0.1:8000/docs |
| Alternative docs (ReDoc) | http://127.0.0.1:8000/redoc |
| Machine-readable schema | http://127.0.0.1:8000/openapi.json |

## Endpoints

`R` = read-only and open. `W` = changes data: needs the API key (see Security). `H` = heavy: rate limited more strictly.

| Group | Method and path | Purpose |
|---|---|---|
| System | `GET /health` | API and database status (never rate limited) |
| | `GET /stats` | collection sizes, database size, daily-data date range |
| | `GET /benchmarks` | stored performance benchmark results (filter: `technology`, `operation`, `is_synthetic`) |
| | `GET /quality/reports` | data-quality reports (`dataset`, `include_issues`) |
| Instruments | `GET /instruments` | list, search (`q`), filter (`instrument_type`, `sector`, `exchange`, `active`), paginated |
| | `GET /instruments/{symbol}` | one instrument with its data coverage (daily bars, first/last date, intraday coverage) |
| | `POST /instruments` **W** | add an instrument |
| | `PATCH /instruments/{symbol}` **W** | edit fields or deactivate (`{"active": false}`); instruments are never hard-deleted |
| | `GET /sectors` | sectors with their stocks, and any stock without one |
| | `GET /sectors/performance` | stored sector performance analysis |
| Market data | `GET /prices/{symbol}` | bars: `interval` 1d/1m/5m/15m/30m/1h, `basis` adjusted/raw, `start`, `end`, `limit`, `cursor`, `order` |
| | `GET /prices/{symbol}/latest` | latest daily close and change from the previous close |
| | `GET /search/prices` | historical search across all daily bars: symbols, dates, `min_close`/`max_close`, `min_volume`, `flag`, sorting, paginated |
| Indicators | `GET /indicators` | names of the stored indicators |
| | `GET /indicators/{symbol}` | stored values; choose `names`, `interval`, date range, cursor |
| | `POST /indicators/{symbol}/compute` **W H** | recompute and store one instrument's indicators |
| Analytics | `GET /analytics` | which analyses exist and when they were computed |
| | `GET /analytics/{type}` | one stored analysis (trend, market_breadth, correlation, spark_market_trend ...) |
| | `GET /market/overview` | trend, breadth, top movers, sectors and indices in one call (a dashboard home page) |
| | `GET /compare` | 2-10 instruments over the same dates: returns, volatility, drawdown, correlation, rebased series |
| Import | `POST /imports/upload` **W H** | upload one CSV/XLSX price file and import it |
| | `GET /imports` | recent import jobs |
| Processing | `POST /processing/{step}` **W H** | start `adjust`, `audit`, `indicators` or `analytics` in the background (returns 202 and a `job_id`) |
| | `GET /jobs`, `GET /jobs/{job_id}` | job status: queued, running, completed or failed, with counts or the error |
| Backtesting | `GET /backtests/strategies` | strategies, default parameters, cost presets |
| | `POST /backtests/run` **H** | run one backtest (`store: true` also saves it and needs the key) |
| | `POST /backtests/sweep` **H** | parameter grid: in-sample choice, out-of-sample test |
| | `GET /backtests` | stored runs, filter and sort, paginated |
| | `GET /backtests/leaderboard` | per-strategy medians from the stored `compare` batch, next to buy-and-hold |
| | `GET /backtests/{run_id}`, `/trades`, `/signals` | one run, its trades, its signals |
| | `DELETE /backtests/{run_id}` **W** | remove a stored run and its signals |
| Reports | `GET /reports` | the eight reports, their parameters, tables, formats and row limits |
| | `GET /reports/{report}` **H** | download one as `format=csv`, `xlsx` or `pdf` (CSV: `table=` picks a table, `table=all` gives a ZIP); the file comes as an attachment with `X-Report-Rows` and `X-Report-Filename` headers |

What each report contains, the limits and the safety rules are in [REPORTS.md](REPORTS.md).

## Conventions

**Timestamps** are ISO-8601 UTC ending in `Z`. A daily bar's `ts` is 00:00 UTC of its trading date; an intraday `ts` is the UTC
instant the bar opened (09:15 IST = 03:45Z). Query parameters accept a date (`2024-01-31`) or a full datetime.

**JSON is always valid.** NaN and infinity become `null`; MongoDB's `_id` is never exposed.

**Pagination, two styles.**
- *Lists* (instruments, jobs, backtests, search ...) use `page` (from 1) and `page_size` (1 to 500, default 50), and return
  `{"items": [...], "page": 1, "page_size": 50, "total": 123, "pages": 3}`. A page past the end returns empty `items`.
- *Time series* (prices, indicators) use `limit` (1 to 5,000, default 500), `order` (`desc` = newest first, the default) and a
  `cursor`. When more data exists the response has `next_cursor`; pass it back as `cursor` to continue. Following it visits every bar
  exactly once, and no `total` is counted because that would be slow on millions of intraday bars.

**Errors** always look the same, so a frontend needs one handler:
```json
{"error": {"code": "not_found", "message": "No instrument called XYZ on NSE.", "details": null, "request_id": "9a862fa73fc54f78"}}
```
`code` is a stable word to program against, `message` is for people, `details` has structure when useful (for example the list of
valid strategy names), and `request_id` matches the `X-Request-ID` response header and the server log line. Statuses: 400/422
invalid input, 401 key missing or wrong, 403 writes disabled, 404 not found, 409 conflict, 413 file too large, 429 rate limited,
503 database unreachable, 500 unexpected (details are logged, never sent).

**Raw vs adjusted prices.** Daily prices default to `basis=adjusted`: the cleaned, split-adjusted series from Phase 6. `basis=raw`
returns the stored provider rows and must name a `source` (`yfinance`, `kaggle`), because the same bar can exist from two providers.
Intraday bars are always raw.

## Security and reliability

| Concern | What the API does |
|---|---|
| Who may change data | Endpoints marked **W** need the header `X-API-Key: <key>` when `API_KEY` is set in `.env`. With no key set they are open in `development` (localhost use) and **disabled** (403) in `production`. The key is compared in constant time and never logged or echoed. |
| Reading | Open, like any local analytics app. Put your own authentication or reverse proxy in front before exposing the API to a network. |
| Rate limits | Per client address, per minute: `RATE_LIMIT_PER_MINUTE` (default 300) for everything, `HEAVY_RATE_LIMIT_PER_MINUTE` (default 20) for backtests, imports and processing. Exceeding returns 429 with a `Retry-After` header. `/health` is never limited. The limiter is in memory, so it is per server process; `X-Forwarded-For` is deliberately not trusted because it can be forged, so apply the limit at your proxy as well if you have one. |
| Uploads | Only `.csv` and `.xlsx`; size limit `MAX_UPLOAD_MB` (default 200) enforced while streaming; an `.xlsx` must really be a zip and a `.csv` must be text; each upload is stored in its own random folder under `data/uploads/` with a sanitised name, so a name like `../../x.csv` cannot escape; then the same row-by-row validation, de-duplication and resume logic as the command-line importer. Rejected, dry-run and duplicate uploads are deleted. |
| Input | Every parameter is validated and bounded (symbol pattern, enums, ranges, page and list sizes). Search text is escaped, never used as a regular expression. Request bodies reject unknown fields. Grids and intraday backtests are capped (150 combinations; 400 days). |
| Database credentials | Only in `.env`, never in a response. An unreachable database gives a plain 503 without the address. |
| CORS | Only the origins in `CORS_ORIGINS` (the React dev server by default) may call the API from a browser. |
| Traceability | Every response has `X-Request-ID` (a client-supplied id is honoured only if it is 8-64 safe characters) and `X-Response-Time-ms`; every request is logged with method, path, status, time, client and id. |

## Uploading a file

The importer reads hints from the file name (for example `TCS_NSE.csv`). Send `symbol`, `exchange` and `interval` as form fields
whenever the name does not say them. A missing or unrecognisable exchange is reported as `exchange_unknown`.

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/imports/upload -H "X-API-Key: <key>" `
  -F "file=@TCS_NSE.csv" -F "symbol=TCS" -F "exchange=NSE" -F "interval=1d" -F "dry_run=true"
```
Set `dry_run=true` first to see what would happen. `auto_register=true` creates the instrument if it is not in the master list.
The reply has `status` (`completed`, `already_imported`, `dry_run`) and `counts` (read, inserted, duplicates, rejected).

## Background processing

Long steps do not hold the request open:
```powershell
curl.exe -X POST http://127.0.0.1:8000/api/processing/analytics -H "X-API-Key: <key>"
#  202 {"job_id": "analytics_analytics_20261122_101500_ab12cd", "status": "queued", "status_url": "/api/jobs/analytics_analytics_..."}
curl.exe http://127.0.0.1:8000/api/jobs/analytics_analytics_20261122_101500_ab12cd
```
One run of each step at a time (a second request gets 409 with the running `job_id`). A job that has not updated for two hours is
treated as dead (for example after a server restart) and no longer blocks. A failed step is recorded with its error, never hidden.
The `adjust` step rebuilds `adjusted_prices` from the raw collections (Phase 6); `audit` writes a data-quality report; `indicators` (optional
`{"symbols": [...]}`) recomputes daily indicators; `analytics` recomputes all market analyses (Phase 7).

## Backtests over the API

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/backtests/run -H "Content-Type: application/json" `
  -d '{"symbol":"RELIANCE","strategy":"ema_crossover","params":{"fast":20,"slow":50},"start":"2010-01-01","include_trades":false}'
```
Without `store`, a run is computed and returned but not saved, so exploring needs no key. The response is the same document the
CLI stores: `metrics`, a buy-and-hold `benchmark`, in-sample/out-of-sample `periods`, `equity_curve`, `trades`, `costs`, and `warnings`.
**Show the warnings and the benchmark with the numbers**: the stock list has survivorship bias (see [BACKTESTING.md](BACKTESTING.md)).

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `API_KEY` | empty | key for write endpoints (see above) |
| `ENVIRONMENT` | `development` | `production` disables writes when no key is set |
| `RATE_LIMIT_ENABLED` | `true` | switch the limiter off (for load tests) |
| `RATE_LIMIT_PER_MINUTE` / `HEAVY_RATE_LIMIT_PER_MINUTE` | 300 / 20 | per client address |
| `CORS_ORIGINS` | the Vite dev server | comma-separated browser origins |
| `MAX_UPLOAD_MB` | 200 | upload size limit |
| `SERVE_FRONTEND` | `true` | serve the built dashboard (`frontend/dist`) at `/` when it exists; `false` for an API-only server (see [RUNNING.md](RUNNING.md)) |

## Layout

| Part | File |
|---|---|
| App assembly, middleware, router wiring | `backend/app/main.py` |
| Error envelope, request logging | `backend/app/api/errors.py` |
| JSON conversion and the safe router | `backend/app/api/serialize.py` |
| Database dependency, pagination, input checks, rate limiter, write access | `backend/app/api/deps.py` |
| Endpoints | `backend/app/api/routes/{instruments,prices,indicators,analytics,imports,backtests,system,health}.py` |
| Stock comparison maths | `backend/app/analytics/compare.py` |
| Serving the dashboard from the same process | `backend/app/frontend.py` |
| Tests (37) | `tests/test_api.py` |

## Troubleshooting

| Problem | Fix |
|---|---|
| 401 `unauthorized` | send `X-API-Key` with the value of `API_KEY` from `.env` |
| 403 `write_disabled` | production mode with no `API_KEY`: set one, or run in development |
| 429 `rate_limited` | wait `Retry-After` seconds, or raise the limits in `.env` |
| 503 `database_unavailable` | MongoDB is not running: start the MongoDB service |
| 422 `exchange_unknown` on upload | send `exchange=NSE` (or BSE) as a form field |
| `source_required` on raw prices | add `source=yfinance` or `source=kaggle` |
| `not_computed` | run `POST /api/processing/analytics` (or `scripts/analyze_data.py all`) first |
| Browser blocked by CORS | add the page's origin to `CORS_ORIGINS` and restart |
