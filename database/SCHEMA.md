# BharatMarketDB: MongoDB schema

Source of truth: [`backend/app/db/schemas.py`](../backend/app/db/schemas.py).
Create or update everything with `.venv\Scripts\python scripts\init_db.py` (safe to re-run, never deletes data).

## Conventions

| Topic | Rule |
|---|---|
| Timestamps | BSON `Date`, always UTC. Client uses `tz_aware=True`, so values read back are UTC-aware. |
| Daily/weekly/monthly bars | `date` = 00:00 UTC of the trading date (a calendar date, not an instant). |
| Intraday bars | `ts` = the real UTC instant the bar opened (09:15 IST = 03:45 UTC). |
| Symbols | Canonical: upper case, no spaces, no exchange suffix. Pattern `^[A-Z0-9&_-]{1,30}$` (e.g. `RELIANCE`, `M&M`, `NIFTY_50`). Provider tickers such as `RELIANCE.NS` live in `instruments.yfinance_symbol`. |
| Exchanges | `NSE`, `BSE` |
| Intervals | Daily collections: `1d`, `1w`, `1M`. Intraday: `1m`, `5m`, `15m`, `30m`, `1h`. |
| Source tracking | Every price row has `source` (a `market_sources.source_id`) and `retrieved_at`. |
| Real vs synthetic | Every price row has `is_synthetic`. Benchmark data is always `true`; real market data is always `false`. |
| Numbers | Prices accept int, long, double or decimal. Prices must be `> 0`, volume `>= 0`. |

## Two kinds of collections (and why)

Tested on MongoDB 8.2.6: **time-series collections cannot have validators or unique indexes**, and silently store duplicates.

| Kind | Collections | Validation | De-duplication |
|---|---|---|---|
| Regular | all except the two below | strict `$jsonSchema` (+ OHLC rule) enforced by the database | unique indexes enforced by the database |
| Time-series | `intraday_prices`, `indicators` | by the importer (Phase 5) | `import_files` (SHA-256 per file) + `intraday_coverage` (first/last timestamp per symbol/interval) |

Daily bars stay in a regular collection because they need exact de-duplication and later updates (corporate-action adjustment).

## Collections

### Required by the project brief (12)

| Collection | Type | Purpose | Unique key | Other indexes |
|---|---|---|---|---|
| `stock_prices` | regular | Daily/weekly/monthly equity and ETF bars | `symbol, exchange, interval, date` | `date desc, interval` |
| `intraday_prices` | time-series | 1m-1h bars | (importer-enforced) | `meta.symbol, meta.exchange, meta.interval, ts desc` |
| `market_indices` | regular | Daily/weekly/monthly index bars (volume optional) | `symbol, exchange, interval, date` | `date desc, interval` |
| `instruments` | regular | Master list of equities, indices, ETFs, derivatives | `exchange, symbol, instrument_type, expiry, strike, option_type` | `sector` |
| `corporate_actions` | regular | Splits, bonuses, dividends | `symbol, exchange, action_type, ex_date` | none |
| `market_sources` | regular | Data providers and their access rules | `source_id` | none |
| `indicators` | time-series | Computed indicators per bar | (recompute = delete by meta, re-insert) | `meta.symbol, meta.exchange, meta.interval, ts desc` |
| `trading_signals` | regular | Strategy buy/sell signals | none | `symbol, interval, ts desc`; `run_id` |
| `backtest_results` | regular | One document per backtest run | `run_id` | `strategy, symbol, created_at desc` |
| `processing_jobs` | regular | Job status, progress, resume checkpoint | `job_id` | `status, created_at desc`; `job_type, created_at desc` |
| `analytics_results` | regular | Cached analytics outputs | none | `analysis_type, computed_at desc` |
| `economic_data` | regular | RBI / MOSPI macro series | `series_id, date` | none |

### Supporting collections (added)

| Collection | Purpose | Unique key |
|---|---|---|
| `import_files` | One document per imported file: SHA-256, source, row counts, retrieval time. Blocks importing the same file twice. | `sha256` |
| `intraday_coverage` | First/last timestamp and row count already loaded per `exchange, symbol, interval, source`. | `exchange, symbol, interval, source` |
| `data_quality_reports` | Cleaning results per dataset (valid / rejected / duplicate / repaired). | none |
| `benchmark_results` | Performance test results per technology, operation and record count. | none |

## Key document shapes

### `stock_prices` / `market_indices`
```json
{
  "symbol": "RELIANCE", "exchange": "NSE", "interval": "1d",
  "date": "2026-01-05T00:00:00Z",
  "open": 1250.5, "high": 1262.0, "low": 1244.1, "close": 1258.3, "volume": 5400000,
  "instrument_type": "equity", "adj_close": 1258.3, "prev_close": 1249.0,
  "source": "nse_bhavcopy", "retrieved_at": "2026-11-22T10:00:00Z", "is_synthetic": false
}
```
Required: `symbol, exchange, interval, date, open, high, low, close, source, retrieved_at, is_synthetic` (plus `volume` for stocks).
Rule enforced by the database: `high >= low, open, close` and `low <= open, close`.

### `intraday_prices` (time-series: `timeField: ts`, `metaField: meta`, granularity `minutes`)
```json
{
  "ts": "2026-01-05T03:45:00Z",
  "meta": {"symbol": "NIFTY_50", "exchange": "NSE", "interval": "1m",
           "instrument_type": "index", "source": "kaggle", "is_synthetic": false},
  "open": 23500.1, "high": 23510.0, "low": 23498.2, "close": 23507.4, "volume": 0
}
```
`meta` is kept low-cardinality on purpose so MongoDB buckets data efficiently.

### `processing_jobs`
```json
{
  "job_id": "imp_20261122_0001", "job_type": "import", "status": "running", "progress": 42.5,
  "checkpoint": {"file": "nifty_1m.csv", "row_offset": 120000},
  "counts": {"read": 120000, "inserted": 119500, "duplicates": 400, "rejected": 100},
  "created_at": "2026-11-22T10:00:00Z"
}
```
`checkpoint` lets an interrupted import resume where it stopped.

## Seed data (created by `init_db.py`)

- **`market_sources` (9):** `nse_bhavcopy`, `bse_bhavcopy`, `kaggle`, `yfinance`, `broker_api`, `rbi`, `mospi`, `manual_upload`, `synthetic_benchmark`.
  `data_class` is one of `historical`, `delayed`, `real_time`, `synthetic`.
- **`instruments` (16):** 10 NSE equities (RELIANCE, TCS, HDFCBANK, ICICIBANK, INFY, SBIN, ITC, BHARTIARTL, LT, AXISBANK)
  and 6 indices (NIFTY_50, NIFTY_BANK, NIFTY_IT, NIFTY_FIN_SERVICE, SENSEX, BSE_500).
  Yahoo tickers are verified against real downloads in Phase 4.

Re-running the script never overwrites values you changed (`active`, `last_success_at`, `created_at`).

## Importer notes for Phase 5

- Convert pandas/NumPy values to plain Python types before inserting (`np.int64` is not a valid BSON type).
- Use `insert_many(..., ordered=False)` for daily data: duplicates are rejected by the unique index while the rest still load.
- For `intraday_prices`: check `import_files` (file hash), then skip bars inside the stored `intraday_coverage` range, validate in Python, then insert.
