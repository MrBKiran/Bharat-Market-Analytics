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
| Numbers | Prices accept int, long, double or decimal (the importer always stores prices as doubles). Prices must be `> 0`, volume `>= 0`. |
| Several sources | `source` is part of the unique key, so the same bar from two providers can coexist (e.g. Yahoo and NSE-based Kaggle data). Always filter by `source` (or compare them) in queries. |
| `price_basis` | `raw` = as traded; `split_adjusted` = the provider already adjusted for splits (never adjust again); `index_level` = an index value. |

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
| `stock_prices` | regular | Daily/weekly/monthly equity and ETF bars | `symbol, exchange, interval, source, date` | `date desc, interval` |
| `intraday_prices` | time-series | 1m-1h bars | (importer-enforced) | `meta.symbol, meta.exchange, meta.interval, ts desc` |
| `market_indices` | regular | Daily/weekly/monthly index bars (volume optional) | `symbol, exchange, interval, source, date` | `date desc, interval` |
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
| `adjusted_prices` | PROCESSED daily bars: one split-adjusted series per instrument, rebuilt from the raw collections. | `symbol, exchange, interval, date` |

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

## Processed data (Phase 6)

`adjusted_prices` is the one **processed** collection: one consistent, split-adjusted daily series per instrument,
rebuilt from the raw collections by `scripts/process_data.py adjust` (never edited by hand). Key: `symbol, exchange,
interval, date`. Fields beyond the bar itself: `adj_factor`, `raw_close` (derived series only), `source`, `method`
(`provider_adjusted`, `derived_adjustment`, `index_level`), `quality_flags` (`return_outlier`, `gap_before`,
`no_trade`) and `job_id`. `corporate_actions` also carries `confidence` (`provider` or `inferred`), `confirmed_by`,
`observed_ratio` and `volume_ratio`; `instruments` carries `aliases` and `symbol_history`.
See [docs/PROCESSING.md](../docs/PROCESSING.md).

## Indicators and analytics (Phase 7)

### `indicators` (time-series)
One document per bar with the indicators that have a value (warm-up values are omitted, numbers rounded to 6 decimals):
```json
{
  "ts": "2026-11-20T00:00:00Z",
  "meta": {"symbol": "RELIANCE", "exchange": "NSE", "interval": "1d", "input": "adjusted_prices"},
  "sma_20": 1432.1, "sma_50": 1410.7, "sma_200": 1375.2, "ema_20": 1430.5, "ema_50": 1411.9, "ema_200": 1380.4,
  "rsi_14": 56.2, "macd": 8.1, "macd_signal": 7.4, "macd_hist": 0.7, "atr_14": 21.3,
  "bb_upper": 1460.0, "bb_mid": 1432.1, "bb_lower": 1404.2, "bb_pct_b": 0.62, "bb_width": 3.9,
  "stoch_k": 71.0, "stoch_d": 66.5, "vwap_20": 1429.8, "adr_14": 24.0, "adr_pct_14": 1.7,
  "hist_vol_20": 18.4, "vol_sma_20": 5100000, "vol_sma_50": 4900000
}
```
Daily series come from `adjusted_prices`; intraday series from `intraday_prices` (`meta.input`). Volume-based fields
(`vwap_20`, `vwap_session`, `vol_sma_*`) exist only for instruments that have volume; indices have none.
Re-computing a series deletes it by `meta` and re-inserts it (no unique index is possible on a time-series collection).

### `analytics_results`
One document per analysis: `{analysis_type, scope, interval, result, data_range, computed_at}`.
`scope` is `{"snapshot": "latest" | "full_universe", "as_of": "YYYY-MM-DD"}` for as-of analyses (performance, top_movers,
trend, market_breadth, sector_performance, volatility, volume_analysis, correlation, index_analysis) and
`{"history": "all"}` for return_distribution, historical_statistics and outliers.
Re-running replaces the document with the same `analysis_type` and `scope`. Details: [docs/ANALYTICS.md](../docs/ANALYTICS.md).
Phase 8 adds six Spark analyses with `scope {"engine": "spark"}` (`spark_returns_volatility`, `spark_sector_returns`,
`spark_correlation`, `spark_market_trend`, `spark_intraday_realised_vol`, `spark_resample_validation`) and the benchmark
measurements in `benchmark_results` (`technology` = pandas, mongodb_aggregation or pyspark; `is_synthetic` false).
See [docs/BIGDATA.md](../docs/BIGDATA.md).

## Backtesting (Phase 9)

### `backtest_results`
One document per run (key `run_id`), written by `scripts/backtest.py`:
```json
{
  "run_id": "bt_20261121_213058_eaaecd", "strategy": "ema_crossover", "symbol": "RELIANCE", "exchange": "NSE", "interval": "1d",
  "start": "2000-01-03T00:00:00Z", "end": "2026-11-20T00:00:00Z", "params": {"fast": 20, "slow": 50, "atr_stop_mult": 2.0, "target_r": null},
  "costs": {"model": {"name": "delivery", "stt_buy_pct": 0.1, "...": "..."}, "slippage_bps": 5.0, "position_pct": 100.0, "initial_capital": 1000000.0},
  "metrics": {"total_trades": 87, "winners": 22, "losers": 65, "win_rate_pct": 25.29, "net_pnl": 7374750.99, "profit_factor": 1.4, "max_drawdown_pct": -79.2, "avg_risk_reward": 8.03, "...": "..."},
  "benchmark": {"total_return_pct": 18636.04, "...": "buy and hold, same dates, same costs"},
  "periods": {"split_date": "2018-09-28T00:00:00Z", "in_sample": {"...": "..."}, "out_of_sample": {"...": "..."}},
  "equity_curve": [{"ts": "...", "equity": 1000000.0, "drawdown_pct": 0.0, "benchmark_equity": 1000000.0}],
  "trades": [{"entry_ts": "...", "exit_ts": "...", "entry_price": 7.3, "exit_price": 6.96, "qty": 136690, "net_pnl": -49767.67, "r_multiple": -1.07, "exit_reason": "signal"}],
  "warnings": ["survivorship bias: ..."], "tax_estimate": {"total_tax": 0.0, "...": "..."}, "batch": "compare", "created_at": "..."
}
```
(numbers above are illustrative). Required by the validator: `run_id, strategy, symbol, exchange, interval, params, metrics, created_at`.
Profit factor is `null` when there is no losing trade. `trades` is capped at 1,500 (`trades_truncated`), `equity_curve` at 800 points.

### `trading_signals`
One document per signal of a run: `{run_id, symbol, exchange, interval, ts, strategy, signal ("buy" | "exit_long"), price, params, stop, action, fill_price, created_at}`.
`action` is `executed`, `skipped_gap_through_stop`, `skipped_no_stop`, `skipped_too_small`, `skipped_end_of_day` or `ignored_already_in_position`.
Metrics, cost assumptions and the bias controls are explained in [docs/BACKTESTING.md](../docs/BACKTESTING.md).

## Seed data (created by `init_db.py`)

- **`market_sources` (9):** `nse_bhavcopy`, `bse_bhavcopy`, `kaggle`, `yfinance`, `broker_api`, `rbi`, `mospi`, `manual_upload`, `synthetic_benchmark`.
  `data_class` is one of `historical`, `delayed`, `real_time`, `synthetic`.
- **`instruments` (16):** 10 NSE equities (RELIANCE, TCS, HDFCBANK, ICICIBANK, INFY, SBIN, ITC, BHARTIARTL, LT, AXISBANK)
  and 6 indices (NIFTY_50, NIFTY_BANK, NIFTY_IT, NIFTY_FIN_SERVICE, SENSEX, BSE_500).
  Yahoo tickers were verified against real downloads in Phase 4 (Nov 2026).

Re-running the script never overwrites values you changed (`active`, `last_success_at`, `created_at`).

## How data gets in (Phase 5)

See [docs/IMPORTER.md](../docs/IMPORTER.md). In short: files are hashed (`import_files`), validated in
pandas, de-duplicated (unique index for daily data; an exact timestamp lookup for the time-series
collection), inserted in chunks, and tracked in `processing_jobs` so an interrupted import resumes
without storing any bar twice. Rejected rows are saved to `data/rejected/` and summarised in
`data_quality_reports`.
