# React dashboard (Phase 11)

A responsive Indian stock-market dashboard over the REST API: market overview, interactive charts, comparison, sectors, search,
strategy backtesting, and data import with live progress. Built with React 19, TypeScript, Vite, TanStack Query and TradingView's
open-source `lightweight-charts`.

## Run it

Two terminals, from the project root:

```powershell
# 1. the API (needs MongoDB running)
.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --reload

# 2. the dashboard (first time only: cd frontend; npm install)
cd frontend
npm run dev
```
Open http://localhost:5173. In development Vite forwards every `/api` request to the API on port 8000, so the browser sees one origin
(no CORS setup, no server address hard-coded). To point at another API: `VITE_API_TARGET=http://host:port npm run dev`.

| Command (in `frontend/`) | Does |
|---|---|
| `npm run dev` | development server with hot reload |
| `npm test` | 111 tests (Vitest, Testing Library), including the shared API contract |
| `npm run typecheck` | TypeScript strict check |
| `npm run build` | type-check and production build into `frontend/dist` (about 174 KB gzipped) |

## Pages

| Page | What it shows | Data from |
|---|---|---|
| **Overview** | Nifty 50 and Nifty Bank, advancers/decliners, share above the 200-day average, top movers (today, week, month), sector bars, breadth, index table, database contents | `/market/overview`, `/stats` |
| **Stock chart** | Candlesticks with volume, EMA 20/50, SMA 200, Bollinger bands, VWAP and an RSI pane; live price and change; period high/low/return; instrument details and data coverage; the newest 100 bars as a sortable table | `/instruments/{s}`, `/prices/{s}`, `/indicators/{s}`, `/prices/{s}/latest` |
| **Compare** | 2 to 6 instruments rebased to 100, returns, CAGR, volatility, drawdown, best/worst day, correlation heat-map | `/compare` |
| **Sectors** | Sector rankings (1 month, year to date), all periods, members | `/market/overview`, `/sectors` |
| **Search** | Instrument search with filters, add and activate/deactivate; historical price search (date, price, volume, data flag) | `/instruments`, `/search/prices` |
| **Backtesting** | Choose symbol, exchange, timeframe, dates, strategy and its parameters, costs, slippage, capital, sizing; results with equity curve against buy-and-hold, in-sample vs out-of-sample, trades, assumptions, rough tax; stored strategy comparison; saved runs | `/backtests/*` |
| **Reports** | A card per report (prices, indicators, performance, comparison, sectors, a saved backtest, strategy comparison, database performance) with its options and CSV / Excel / PDF buttons; the other pages have the same buttons beside their data | `/reports`, `/reports/{report}` |
| **Data & imports** | Upload a CSV/XLSX file with upload progress and a dry-run option; start adjust / indicators / analytics / audit; jobs with live progress; collection counts; benchmarks | `/imports/upload`, `/processing/*`, `/jobs`, `/stats`, `/benchmarks` |

## How it meets the interface requirements

| Requirement | How |
|---|---|
| Responsive desktop and mobile | Sidebar on desktop; below 900 px it becomes a slide-in drawer. Grids reflow to one column, tables scroll sideways, button groups scroll. Checked at 375 px with no horizontal page scroll. |
| Sidebar navigation, clear navigation | One link per page, the current page is marked (`aria-current`), keyboard reachable. |
| Dark and light themes | Colour tokens in `styles.css`; follows the operating-system setting, the toggle remembers your choice, and charts repaint with the theme. No flash of the wrong theme on load. |
| Interactive stock charts | Zoom with the wheel or pinch, drag to pan, crosshair with exact open/high/low/close/volume, overlay toggles. |
| Search and filter | Instrument search as you type (symbol or name), filters, sortable tables, price search. |
| Symbol, exchange (NSE/BSE), timeframe, date-range selectors | One shared controls bar. Timeframes with no data for the chosen symbol are disabled, not hidden. Date presets (1M to 10Y, All, Custom) are measured back from the symbol's **last available bar**, so a stock whose data stops in 2021 still shows its final year. |
| Loading indicators, error messages | Every query has a spinner or skeleton state and an error box showing the server's message, a hint for what to do (start the API, enter the key, wait, start MongoDB) and the **request id** to quote. |
| Data import progress | Upload progress bar, then a "validating and storing" state, then counts; background jobs show a progress bar and refresh every 1.5 seconds while running. |
| Indian conventions | ₹ with lakh/crore grouping (₹1,23,456.78), "₹1.23 Cr", "4.50 L" for volumes, IST times (09:15 IST), Nifty/Sensex, advancers/decliners, NSE and BSE. |

## Design decisions worth knowing

- **One place talks to the server** (`src/api/client.ts`): it turns every failure into one error type, adds the API key when set, and
  never retries a request the server understood and refused (4xx).
- **The API key** (only needed to change data) is typed into the top-bar dialog and kept in this tab's `sessionStorage`: it is forgotten
  when the tab closes and never ends up in the built files. Reading data needs no key.
- **Colour is never the only signal.** Gains and losses carry ▲ ▼ and a sign; the heat-map prints every number; charts have a text summary
  and the stock page has the bars as a table.
- **Long histories** are fetched page by page through the API's cursor (up to 20,000 daily bars, 15,000 intraday); beyond that the page says
  the history was cut and suggests a narrower range.
- **Daily bars use their trading date and intraday bars are shifted to IST** before they reach the chart, so 09:15 shows as 09:15.
- **Backtest results always show the warnings first** and put buy-and-hold beside every strategy, because the numbers are easy to misread.
  Intraday backtests are limited to about a year and close every position at the end of the day.

## What the dashboard does not do

- **No login.** It relies on the API: anyone who can open the page can read data. Put your own authentication in front of it before
  exposing it beyond your machine. Actions that change data need the API key when the server has one.
- **Data limits are the database's limits:** 39 stocks end in April 2021 (the free Kaggle data), so the Overview leaves them out of
  "today's" numbers (it says so) and the Stock page shows a notice; intraday bars exist only for the Nifty indices; BSE contains only
  Sensex and BSE 500; chart prices are split-adjusted, not dividend-adjusted.
- The production build is static files in `frontend/dist`. The API serves them itself (`scripts\serve.py`: one address, no Vite, strict
  content-security policy; see [RUNNING.md](RUNNING.md)). Docker packaging comes in Phase 15.

## Layout

| Folder | Content |
|---|---|
| `frontend/src/api` | `client.ts` (fetch, errors, upload progress), `hooks.ts` (data hooks), `types.ts` (response shapes) |
| `frontend/src/lib` | `format.ts` (INR, lakh/crore, IST dates), `dates.ts` (range presets, chart times) |
| `frontend/src/state`, `theme.tsx` | shared controls (symbol, exchange, timeframe, range) and the theme |
| `frontend/src/components` | layout, selectors, charts (`PriceChart`, `LineChart`), `ui.tsx` building blocks, heat-map and bars |
| `frontend/src/pages` | the eight pages |
| `frontend/src/test` | 111 tests: formatting, dates, API client and paging, components, every page against a mocked API, and `contract.json` shared with the backend tests |

## Troubleshooting

| Problem | Fix |
|---|---|
| "Cannot reach the server" | start the API (terminal 1 above); the top bar shows "API offline" |
| Page loads but every panel errors with 503 | MongoDB is not running |
| 401 when importing, saving or processing | click **API key** in the top bar and paste `API_KEY` from `.env` |
| "Market analytics have not been computed" | Data & imports, then run "Recompute market analytics" |
| A timeframe button is greyed out | that instrument has no data for it (intraday exists only for Nifty indices) |
| Port 5173 already in use | stop the other Vite process; the port is fixed so the API's CORS setting matches |
