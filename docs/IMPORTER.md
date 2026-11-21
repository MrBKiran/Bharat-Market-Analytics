# Bulk importer (Phase 5)

Loads the files in `data/raw/` into MongoDB: validated, de-duplicated, tracked, and safe to interrupt.

```powershell
.venv\Scripts\python scripts\import_data.py import data\raw\yfinance data\raw\kaggle --dry-run   # check first, writes nothing
.venv\Scripts\python scripts\import_data.py import data\raw\yfinance data\raw\kaggle --auto-register
.venv\Scripts\python scripts\import_data.py summary    # what is in the database now
.venv\Scripts\python scripts\import_data.py jobs       # recent import jobs
```

Other options: `--force` (re-process a finished file; stored bars are not duplicated), `--chunk-size N`,
and for your own CSV/Excel files `--format generic --symbol TCS --interval 1d [--exchange --tz --source]`.

## What happens to each file

1. **Hash and check.** The SHA-256 is looked up in `import_files`. A completed file is skipped
   (`already_imported`); a failed or interrupted one resumes.
2. **Detect the format** from the header and file name (table below) and find the instrument in
   `instruments`. Unknown instruments are skipped unless you pass `--auto-register`.
3. **Read in chunks** (default 100,000 rows), so memory stays small even for million-row files.
4. **Validate** each chunk (rules below). Rejected rows go to `data/rejected/<job>__<file>.rejected.csv`.
5. **De-duplicate and insert.** Then save progress and a checkpoint in `processing_jobs`.
6. **Finish.** Update `import_files`, write a `data_quality_reports` document, update `intraday_coverage`.

## Formats

| Format | Detected by | Goes to | Notes |
|---|---|---|---|
| `yfinance` | `Adj Close` + `Dividends` + `Stock Splits` columns | `stock_prices` / `market_indices` / `intraday_prices` | `price_basis = split_adjusted` |
| `kaggle_index` | lower-case `date,open,high,low,close,volume` and a name like `NIFTY 50_minute.csv` | `intraday_prices` (minute/5/15/60) or `market_indices` (day) | `price_basis = index_level`; times are IST |
| `kaggle_stock` | `Date, Symbol, Series, Prev Close` columns | `stock_prices` | `price_basis = raw` (as traded); symbol comes from the file name |
| `generic` | anything else (CSV or Excel) | by interval and type | you give `--symbol` and `--interval` |

`NIFTY50_all.csv` and `stock_metadata.csv` are skipped: the first duplicates the per-stock files.

## Rules

| Rejected as | When |
|---|---|
| `bad_timestamp` | the date/time cannot be read |
| `invalid_symbol` | not upper case letters/digits/`&`/`_`/`-`, max 30 |
| `missing_price` | open, high, low or close is empty |
| `non_positive_price` | any price is 0 or negative |
| `negative_volume` / `missing_volume` | volume below 0; empty volume on a stock (indices may have none) |
| `ohlc_inconsistent` | high is below low/open/close, or low is above open/close |
| `duplicate_in_file` | the same symbol and time appears twice in a chunk; the first is kept |
| `rejected_by_database` | MongoDB's own validator refused the document (should be rare) |

Invalid **optional** values (previous close 0 on a listing day, negative turnover) drop only that field and
are counted as `optional_fields_dropped`; the price bar itself is kept.

## Duplicates and resume

- **Daily bars:** the unique index (`symbol, exchange, interval, source, date`) rejects repeats; they are counted as `duplicates`.
- **Intraday bars** (time-series collection, no unique index possible): before inserting a chunk the importer
  looks up which timestamps already exist for that exact series and drops them. This is exact, not a range guess.
- **Crash safety:** tests simulate a crash both before and after a chunk is stored; re-running resumes the same
  job and ends with every bar stored exactly once.
- **Same bar, two sources** (Yahoo and Kaggle): both are kept, because `source` is part of the key.

## First real import (Nov 2026)

| | |
|---|---|
| Files | 86 (16 Yahoo, 20 Kaggle index files, 50 Kaggle stock files) |
| Rows read | 5,755,676 |
| Stored | 5,755,578 (`stock_prices` 305,038; `market_indices` 58,149; `intraday_prices` 5,392,391) |
| Rejected | 98 (86 Yahoo high/low inconsistencies, 12 repeated dates in three daily index files) |
| Speed | about 64,000 rows/s overall (90 s for everything); about 67,000 rows/s for 1-million-row files |
| Size | 85 MB on disk (210 MB data, 17 MB indexes) |

Checks after the import: all 16 intraday series match their `intraday_coverage` row counts; no duplicate
timestamps in the largest series.

## Things found in the real data

- **Diwali Muhurat sessions:** 10 evenings (2015 to 2024) have 1-minute bars at about 18:00 to 19:30 IST. They are
  real, and they confirm the IST-to-UTC conversion. Phase 6/7 should not treat them as errors.
- **Two "daily" rows for one date:** four dates (2015-06-29, 2015-07-02, 2015-08-10, 2015-11-11) appear twice in
  the NIFTY BANK, IT and FIN SERVICE daily files with odd times of day. The first is kept; the second is in the rejected file.
- **Listing day:** BHARTIARTL's first day (2002-02-18) has `Prev Close = 0`. An earlier importer version rejected
  the whole bar; it now keeps the bar and drops only that field (this was found by comparing dry-run and real counts).
- **Yahoo adjusts more than splits**, see [DATA_SOURCES.md](DATA_SOURCES.md).

## Troubleshooting

| Problem | Fix |
|---|---|
| `skipped: X is not in the instruments collection` | add `--auto-register`, or add the instrument first |
| `FAILED ...` on a file | run the same command again; it resumes from the checkpoint |
| `already_imported` but you want to re-process | add `--force` |
| Counts differ between `--dry-run` and the real run | look for `rejected_by_database` in `data_quality_reports`; the database checks more fields than the importer |
| `cannot read file: ...` | wrong format or damaged file; try `--format generic` with `--symbol` |
