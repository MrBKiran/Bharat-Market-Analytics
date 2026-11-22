# Reports and exports (Phase 13)

Eight reports, each downloadable as **CSV** (plain data), **Excel** (a formatted workbook with native charts) or **PDF** (a readable report with
charts, key figures and warnings). They are built from the database on request, from the dashboard (the Reports page and the download buttons next to the
data) or straight from the API.

```
GET /api/reports                                   the list of reports, their parameters and limits
GET /api/reports/{report}?format=csv|xlsx|pdf&...  download one
```

## The reports

| Report | Contents | Parameters | Tables (for CSV) |
|---|---|---|---|
| `prices` Historical prices | OHLC and volume for one instrument, with summary figures and a price chart | `symbol` (required), `exchange`, `interval` (1d, 1h, 15m, 5m, 1m), `start`, `end`, `basis` (adjusted or raw), `source` | `prices` |
| `indicators` Technical indicators | stored indicator values (RSI, MACD, moving averages, Bollinger bands ...) with the latest values and a chart | `symbol` (required), `interval`, `start`, `end`, `names` | `indicators` |
| `performance` Stock performance analysis | returns over several horizons, volatility, RSI and distance from the 200-day average for the instruments in the stored analysis | `symbols` (optional filter) | `performance` |
| `comparison` Stock comparison | 2 to 10 instruments over the same dates: returns, CAGR, volatility, drawdown, best and worst day, correlation matrix, rebased prices | `symbols` (required), `exchange`, `start`, `end` | `statistics`, `correlation`, `rebased_series` |
| `sectors` Sector analytics | sector returns over the same horizons, and the stocks in each sector | none | `sectors`, `members` |
| `backtest` Backtest result | one stored run: key figures, trades, equity curve against buy and hold, in-sample vs out-of-sample, assumptions, tax estimate | `run_id` (required) | `trades`, `equity_curve`, `periods` |
| `strategies` Strategy comparison | median results of every strategy across the stored comparison run, next to buy and hold | `batch`, `interval` | `strategies` |
| `benchmarks` Database performance report | stored pandas / MongoDB / Spark timings, the fastest technology per job, document counts and database size | `technology`, `operation` | `benchmarks`, `fastest`, `collections` |

`GET /api/reports` is the authoritative list: it returns every report's exact parameters, tables and formats.

Examples:

```powershell
curl.exe -o tcs.xlsx "http://127.0.0.1:8000/api/reports/prices?symbol=TCS&start=2025-01-01&format=xlsx"
curl.exe -o cmp.pdf  "http://127.0.0.1:8000/api/reports/comparison?symbols=RELIANCE,TCS,NIFTY_50&format=pdf"
curl.exe -o cmp.zip  "http://127.0.0.1:8000/api/reports/comparison?symbols=RELIANCE,TCS&format=csv&table=all"
```

In the dashboard: **Reports** in the sidebar has a card per report, and the Stock chart, Compare, Sectors, Overview, Backtesting and Data pages have download
buttons next to their data. A backtest can be exported only once it has been saved (tick "Save this result").

## What is in each format

**CSV**: one table per file (`table=` picks it; `table=all` returns every table as CSV files in a ZIP). UTF-8 with a byte-order mark so Excel reads it
correctly, CRLF line ends, ISO dates, numbers with no symbols or digit grouping so a script or spreadsheet can use them as they are, and empty cells for
missing values. Units are in the column header.

**Excel (.xlsx)**: a **Summary** sheet (what was asked for, key figures, warnings), **one sheet per table**, and a **Charts** sheet with native Excel line
charts. Numbers stay numbers (sortable, summable); only the display is formatted: rupees with Indian grouping, dates as dates, percentages with a sign.
Header rows are frozen and filterable.

**PDF**: A4, a header, what was asked for, the warnings, key figures, charts, tables with repeating headers, notes and a disclaimer, and "Page X of Y" on every
page. Long tables show their **newest 40 rows** and say so; the CSV and Excel exports contain every row. The built-in PDF font has no rupee sign, so rupees are
written **Rs 12,34,567.00** (Excel and the dashboard use ₹).

Every report carries its **warnings**: split-adjusted not dividend-adjusted prices, the survivorship bias of the stock list, sector labels assigned by this
project, data that ends early for some stocks, benchmark caveats, and "a backtest is not a forecast".

## Limits (measured)

| | |
|---|---|
| CSV | up to 250,000 rows per table. 209,888 five-minute bars took 4.8 s, +28 MB of memory |
| Excel | up to **100,000** rows per table. The same 209,888 rows took 20.9 s and about 615 MB, so Excel refuses more and says to narrow the range or use CSV |
| PDF | the PDF itself stays small (the same data: 2.6 s) because it shows only the newest rows |
| Over the limit | a 422 error (`too_many_rows` or `too_many_rows_for_excel`) with advice, never a silently truncated file |

Downloads share the **heavy-endpoint rate limit** (20 a minute by default), because building a file costs CPU.

## Safety

| Risk | What is done |
|---|---|
| **Spreadsheet formula injection**: text starting with `=`, `+`, `-` or `@` runs as a formula when opened in Excel, and text such as an instrument name can come from the database | every text cell, header, title, subtitle, label and warning is defused with a leading apostrophe, in CSV and Excel. Tests open the workbook and check that no cell anywhere is a formula |
| Markup in a name breaking a PDF | all PDF text is escaped and printed literally |
| Unsafe download names | built only from letters, digits and `. _ -`, no `..`, capped in length; the dashboard re-checks the name the server sent |
| Header injection through parameters | symbols, sources, table names and run ids are pattern-checked (422 otherwise) |
| Unbounded memory | row limits per table and format, enforced before rendering |
| Float noise | prices are stored as 32-bit floats (3138.1 is held as 3138.10009765625); exports clean this before writing |

## What was checked

Reports in every format were generated from the real database, the PDFs were rendered to images and inspected page by page, the Excel files were opened and
their formats, freeze panes, filters and charts checked, and in a real browser at the single address the dashboard downloads were exercised, including the 22-year TCS
history (5,493 rows) in all three formats. Defects found this way and fixed: wide-number cells wrapping in PDF, symbol columns splitting, a serif font on chart axes,
a win rate shown without its %, float noise in CSV, `..` in file names, and **a live formula in an Excel title** (the sheet title and subtitle skipped the defusing).

## Layout

| Part | File |
|---|---|
| Report structure, number formatting, formula and file-name safety | `backend/app/reports/model.py` |
| One builder per report (reads the database) | `backend/app/reports/builders.py` |
| CSV, Excel, PDF writers | `csv_out.py`, `excel_out.py`, `pdf_out.py` |
| Package entry (`generate`) | `backend/app/reports/__init__.py` |
| HTTP endpoints | `backend/app/api/routes/reports.py` |
| Dashboard | `frontend/src/components/ExportMenu.tsx`, `frontend/src/pages/Reports.tsx`, `downloadReport` in `frontend/src/api/client.ts` |
| Tests | `tests/test_reports.py`, `frontend/src/test/reports.test.tsx`, the reports catalog in the shared API contract |

`pypdfium2` (optional, in `requirements.txt`) lets the tests read generated PDFs back; without it those checks are skipped.

## Troubleshooting

| Problem | Fix |
|---|---|
| 404 `not_computed` (performance, sectors) | run the analytics job (`POST /api/processing/analytics`, or `scripts\analyze_data.py all`) |
| 404 `no_runs` (strategies) | run `scripts\backtest.py compare` |
| 404 `no_indicators` | compute them: `POST /api/indicators/{symbol}/compute` |
| 422 `too_many_rows` / `too_many_rows_for_excel` | choose a shorter date range, a coarser timeframe, or CSV |
| 429 `rate_limited` | wait the `Retry-After` seconds, or raise the heavy rate limit setting |
| The backtest report button is disabled | save the run first ("Save this result" on the Backtesting page) |
