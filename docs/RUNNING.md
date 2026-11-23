# Running the whole app (Phase 12)

The API and the dashboard are one application: build the dashboard once and the API serves it, so everything lives at one address
(`http://127.0.0.1:8000`). No second server, no CORS, no copied addresses.

## First time

```powershell
# 1. Python side (see README: venv, requirements, .env)
.venv\Scripts\python scripts\init_db.py

# 2. Data (skip anything you already ran): import, clean, indicators and analytics
.venv\Scripts\python scripts\import_data.py import data\raw\yfinance data\raw\kaggle --auto-register
.venv\Scripts\python scripts\process_data.py all
.venv\Scripts\python scripts\analyze_data.py all
.venv\Scripts\python scripts\backtest.py compare          # optional: fills the strategy comparison table

# 3. Build the dashboard and start everything (needs Node.js 24; installs the packages on the first --build)
.venv\Scripts\python scripts\serve.py --build
```
Open **http://127.0.0.1:8000** (dashboard) or **http://127.0.0.1:8000/docs** (API documentation). Stop with Ctrl+C.

## Every day

| Goal | Command |
|---|---|
| Run the app | `.venv\Scripts\python scripts\serve.py` |
| Run after changing the dashboard code | `.venv\Scripts\python scripts\serve.py --build` (a rebuilt dashboard is picked up even by a server that is already running) |
| Change the port | `scripts\serve.py --port 9000` |
| Develop the API (restarts on code changes) | `scripts\serve.py --reload` |
| Develop the dashboard (instant hot reload) | terminal 1: `scripts\serve.py` (or uvicorn); terminal 2: `cd frontend; npm run dev`, then open http://localhost:5173 |
| API only, no dashboard | `SERVE_FRONTEND=false` in `.env` |

`serve.py` tells you if MongoDB is unreachable or the dashboard has not been built yet, instead of failing silently.

## How the pieces connect

| Mode | Page comes from | `/api` comes from | CORS |
|---|---|---|---|
| **One address** (`serve.py`) | the API process, from `frontend/dist` | the same process | not involved |
| **Development** (`npm run dev`) | Vite on port 5173 | Vite forwards `/api` to port 8000 | not involved (same origin from the browser's view) |
| **Separate hosts** (advanced) | any static host | the API | set `CORS_ORIGINS` to the page's origin and build the dashboard with `VITE_API_BASE=https://api.example.com` |

What the API process does for the page (`backend/app/frontend.py`):

- Every page address (for example `/stock?symbol=TCS`) returns the dashboard, so **refresh and bookmarks work**. `/api/...` is never answered with
  a web page: an unknown API address is a JSON 404.
- Hashed JavaScript and CSS are cached for a year; `index.html` is never cached, so a new build appears immediately.
- A strict **Content-Security-Policy**: scripts only from this origin plus the single small inline theme script (allowed by its SHA-256 hash,
  not a blanket permission), no framing, no plug-ins, connections only to this origin. Also `X-Frame-Options: DENY` and a same-origin referrer policy.
- `HEAD` requests work (uptime monitors), and the browser's `/favicon.ico` request is answered quietly.

## Keeping the two halves in step

`frontend/src/test/contract.json` lists the fields the dashboard reads from each of the 20 API responses it uses. Two tests share it:

| Test | Checks |
|---|---|
| `tests/test_contract.py` | the **real** API (real analytics, a real backtest, a real import, a processing job on a throwaway database) returns every listed field, with the right type |
| `frontend/src/test/contract.test.ts` | the dashboard's test data has every listed field |

So if someone renames or removes a field the dashboard needs, the backend test fails; if the dashboard starts reading a new field, it must be added to the
contract, which makes the backend test prove it exists. Nothing relies on a mock that drifted from reality.

## What was checked in a real browser (single address, real data)

Overview loads from `/`; refreshing a deep link (`/stock?symbol=TCS`) works; the price chart draws with indicators under the strict security policy with
no policy violations; a dry-run upload from the Data page reports what would happen and stores nothing (verified against the database); a backtest runs
and shows its warnings, buy-and-hold comparison and out-of-sample result; every request returned 200; a rebuilt dashboard appeared without restarting.
Two defects found this way and fixed: the dry-run summary said "nothing was stored" and then "6 stored", and the server answered `HEAD /` with 405.

## Exposing it beyond your machine

`serve.py --host 0.0.0.0` makes the app reachable from your network and prints a warning, because **reading data has no login**. The API key protects only
actions that change data. Before exposing it: set `API_KEY` in `.env`, set `ENVIRONMENT=production` (which disables writes when no key is set), put
authentication or a firewall in front, and serve over HTTPS through a reverse proxy (the rate limiter trusts the direct connection address, not
`X-Forwarded-For`). The Docker setup ([DOCKER.md](DOCKER.md)) already publishes only on `127.0.0.1` and keeps MongoDB private.

## Troubleshooting

| Problem | Fix |
|---|---|
| `/` shows `{"error": ...}` (JSON) instead of the dashboard | there is no build: run `scripts\serve.py --build`, or `SERVE_FRONTEND` is false |
| Dashboard loads but every panel errors | MongoDB is not running, or the API address differs: check the top bar ("API online") |
| `--build` says Node.js was not found | install Node.js 24 from https://nodejs.org and reopen the terminal |
| Port already in use | `scripts\serve.py --port 9000`, or stop the other server |
| Old dashboard after rebuilding | hard-refresh (Ctrl+F5); a rebuilt `index.html` itself is never cached, only the hashed files are |
| Blocked by the browser's security policy | you added an inline script or an outside resource: allow it deliberately in `frontend.py::content_security_policy` |
