# BharatMarket Analytics

MongoDB-based Indian stock market big-data storage, processing and analytics system.

> Status: **Phase 14 complete** (performance tests from 100,000 to 10 million records; CSV, Excel and PDF reports; dashboard and API served together at one address; React dashboard with 9 pages; 37 REST endpoints; 5.76 million real rows imported, cleaned, adjusted; 1.5 million indicator documents, 21 analyses, 6 Spark analyses and a pandas / MongoDB / Spark benchmark; 7 backtested strategies with 343 stored runs; NSE/BSE bhavcopy files are optional and manual).
> Docs: [database/SCHEMA.md](database/SCHEMA.md) | [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | [docs/IMPORTER.md](docs/IMPORTER.md) | [docs/PROCESSING.md](docs/PROCESSING.md) | [docs/ANALYTICS.md](docs/ANALYTICS.md) | [docs/BIGDATA.md](docs/BIGDATA.md) | [docs/BACKTESTING.md](docs/BACKTESTING.md) | [docs/API.md](docs/API.md) | [docs/DASHBOARD.md](docs/DASHBOARD.md) | [docs/REPORTS.md](docs/REPORTS.md) | [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | [docs/RUNNING.md](docs/RUNNING.md)

## Requirements (Windows)

Python 3.11, Node.js 24, MongoDB Community 8.x (running on `localhost:27017`),
JDK 17 (for PySpark, Phase 8), Docker Desktop (Phase 15).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
```

## Run the API

```powershell
.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --reload
```

- Health check: http://127.0.0.1:8000/api/health
- Interactive API docs (try every endpoint in the browser): http://127.0.0.1:8000/docs
- 34 endpoints: market data, instruments, indicators, analytics, comparison, import, processing, backtesting. Endpoints that change
  data need the `X-API-Key` header when `API_KEY` is set in `.env`; all endpoints are rate limited. See [docs/API.md](docs/API.md).

## Run the whole app (one address)

```powershell
.venv\Scripts\python scripts\serve.py --build    # builds the dashboard (needs Node.js), then serves it AND the API
# open http://127.0.0.1:8000        (API docs: /docs)        later: scripts\serve.py
```
See [docs/RUNNING.md](docs/RUNNING.md) for the first-time order, the development modes and how to expose it safely.

## Develop the dashboard

```powershell
cd frontend
npm install          # first time only
npm run dev          # http://localhost:5173 with hot reload (needs the API running)
npm test             # 93 frontend tests (incl. the shared API contract)
```
Overview, interactive stock charts, comparison, sectors, search, strategy backtesting and data import with live progress; light and
dark themes; works on phones. See [docs/DASHBOARD.md](docs/DASHBOARD.md).

## Set up the database

```powershell
.venv\Scripts\python scripts\init_db.py
```

Creates `BharatMarketDB` with all collections, validators and indexes, and seeds the
data sources and instruments. Safe to re-run; it never deletes data.

## Download and import data

```powershell
.venv\Scripts\python scripts\download_data.py yfinance --interval 1d     # Phase 4 (Kaggle: see docs/DATA_SOURCES.md)
.venv\Scripts\python scripts\import_data.py import data\raw\yfinance data\raw\kaggle --auto-register   # Phase 5
.venv\Scripts\python scripts\import_data.py summary
```

## Clean and process the data

```powershell
.venv\Scripts\python scripts\process_data.py all      # Phase 6: corporate actions, symbols, adjusted prices, audit
```

## Indicators and market analytics

```powershell
.venv\Scripts\python scripts\analyze_data.py all      # Phase 7: sectors, indicators, analytics, report
```

## Big-data processing (optional: needs `pip install pyspark pyarrow` and JDK 17)

```powershell
.venv\Scripts\python scripts\bigdata.py check        # does Spark start?
.venv\Scripts\python scripts\bigdata.py export       # MongoDB -> Parquet
.venv\Scripts\python scripts\bigdata.py analytics    # Spark analyses -> analytics_results
.venv\Scripts\python scripts\bigdata.py benchmark    # pandas vs MongoDB vs Spark
```

## Backtest trading strategies

```powershell
.venv\Scripts\python scripts\backtest.py list                                       # 7 strategies and their parameters
.venv\Scripts\python scripts\backtest.py run RELIANCE ema_crossover --start 2010-01-01
.venv\Scripts\python scripts\backtest.py compare                                    # all strategies x all stocks -> backtest_results
.venv\Scripts\python scripts\backtest.py sweep TCS ema_crossover --grid fast=10,20,30 --grid slow=50,100,200
```
Signals act at the next bar's open, costs and slippage are charged, every result is shown next to buy-and-hold, and the
look-ahead guarantees are tested. Read [docs/BACKTESTING.md](docs/BACKTESTING.md) before quoting a number: the stock list has survivorship bias.

## Performance testing at scale

```powershell
.venv\Scripts\python scripts\performance.py run      # 100K, 1M, 5M, 10M records: ingestion, storage, queries, aggregations on pandas, MongoDB, Spark
.venv\Scripts\python scripts\performance.py report   # the stored results
```
The 100K to 10M sizes use clearly labelled **synthetic** data in a separate database; results are charted on the dashboard's **Performance** page, which keeps them apart from the
real-data timings. Close heavy programs first (10M needs several GB of free memory). Method, results and limits: [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

## Reports and exports

Eight reports (historical prices, indicators, performance, comparison, sectors, a backtest, strategy comparison, database performance) as CSV, Excel or PDF:

```powershell
curl.exe -o tcs.xlsx "http://127.0.0.1:8000/api/reports/prices?symbol=TCS&start=2025-01-01&format=xlsx"
```
Or use **Reports** in the dashboard, or the download buttons beside the data. Excel carries up to 100,000 rows per table, CSV up to 250,000; spreadsheet formulas in text
are defused. Details, limits and safety rules: [docs/REPORTS.md](docs/REPORTS.md).

## Run the tests

```powershell
.venv\Scripts\python -m pytest -v
```

## Project layout

| Folder | Purpose |
|---|---|
| `backend/app/core` | Settings (from `.env`) and logging |
| `backend/app/db` | MongoDB connection (schemas and indexes in Phase 3) |
| `backend/app/api/routes` | FastAPI endpoints |
| `backend/app/collection` | Data downloaders and bulk importers |
| `backend/app/processing` | Cleaning and processing |
| `backend/app/analytics` | Indicators and market analytics |
| `backend/app/backtesting` | Strategy backtesting |
| `backend/app/reports` | CSV / Excel / PDF exports |
| `database/` | Schema documentation and setup scripts |
| `frontend/` | React dashboard (Phase 11) |
| `data/` | Raw, processed, downloaded and uploaded files (git-ignored) |
| `tests/` | Automated tests |
| `docs/` | Documentation |
| `docker/` | Docker configuration (Phase 15) |

## Security notes

- Secrets live only in `.env`, which is git-ignored. Never commit it.
- The frontend never receives database credentials.
- Real market data and synthetic benchmark data are always labelled separately.
