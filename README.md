# BharatMarket Analytics

MongoDB-based Indian stock market big-data storage, processing and analytics system.

> Status: **Phase 3 complete** (MongoDB collections, validators, indexes, seed data).
> Schema documentation: [database/SCHEMA.md](database/SCHEMA.md)

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
- Interactive API docs: http://127.0.0.1:8000/docs

## Set up the database

```powershell
.venv\Scripts\python scripts\init_db.py
```

Creates `BharatMarketDB` with all collections, validators and indexes, and seeds the
data sources and instruments. Safe to re-run; it never deletes data.

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
