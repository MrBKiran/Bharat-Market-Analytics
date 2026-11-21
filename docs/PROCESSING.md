# Data cleaning and processing (Phase 6)

Turns the raw imported data into a clean, consistent **processed** dataset. The raw collections
(`stock_prices`, `market_indices`, `intraday_prices`) are never modified; everything here is derived and can be rebuilt.

```powershell
.venv\Scripts\python scripts\process_data.py actions --dry-run   # preview corporate actions, writes nothing
.venv\Scripts\python scripts\process_data.py actions             # splits + dividends -> corporate_actions
.venv\Scripts\python scripts\process_data.py symbols             # earlier tickers and aliases -> instruments
.venv\Scripts\python scripts\process_data.py adjust --verbose    # rebuild adjusted_prices
.venv\Scripts\python scripts\process_data.py audit               # data quality report
.venv\Scripts\python scripts\process_data.py all                 # all four, in order
```

Every step is safe to re-run, is recorded in `processing_jobs` (`job_type = clean`) and writes a document to
`data_quality_reports` (`processed:corporate_actions`, `processed:adjusted_prices`, `processed:audit`).

## Raw versus processed

| Layer | Collections | Rule |
|---|---|---|
| Raw (as imported, one row per source) | `stock_prices`, `market_indices`, `intraday_prices` | never edited |
| Processed | `adjusted_prices` (daily) | rebuilt by `adjust`; one consistent series per instrument |

Intraday data needs no processing for adjustment: it is index levels, and its completeness is checked by the audit.

## Corporate actions

| Source | What | Confidence |
|---|---|---|
| Yahoo (`Dividends`, `Stock Splits` columns) | 31 splits, 379 dividends for the 10 stocks Yahoo covers | `provider` |
| Inferred from raw NSE prices | a split or bonus shows as a one-day drop to an exact fraction of the previous close (0.5, 0.333, 0.25, 0.2, 0.1, 0.667, or the reverse), with the gap between rows at most 7 days | `inferred` |

`factor` = the multiplier for prices **before** the ex-date (2-for-1 gives 0.5). An inferred event within 3 days of a Yahoo
split is stored as a confirmation (`confirmed_by`) instead of a duplicate.

**How well does the detector work?** Checked against Yahoo on the 10 stocks that have both sources:
24 of the 25 Yahoo splits inside the NSE data's date range were found, with **no false positives** (no detector event
that Yahoo does not list). The one miss is ICICIBANK 2017-06-20 (factor 0.909, an 11-for-10 event): adjustments below
about 10% cannot be told apart from an ordinary price move. Result: 83 inferred events, 24 confirming Yahoo and 59 new
(for the 39 stocks Yahoo does not cover). Those 59 are inferred from prices alone and unverified, though the detector's
record on the stocks where it can be checked is good.

The ratio list was extended after looking at the first Phase 7 results: 1/5 tolerates a big market move on the same day
(BRITANNIA 2010 closed at 0.227 of the previous day) and 1/20 is a 10-for-1 split plus a 1:1 bonus on one day (TITAN 2011
and VEDL 2008 both closed at 0.053). The record against Yahoo did not change (24 of 25, no false positives).
Some drops are still undetected (CIPLA 2006, KOTAKBANK 2005, WIPRO 2010, ASIANPAINT 2000 do not match a clean ratio); they
stay flagged as `return_outlier`.

Not handled (by design): demergers, rights issues and special dividends do not look like clean fractions, so they are
not guessed. RELIANCE's 2006-01-18 demerger is an example: Yahoo adjusts for it, our derived series does not.

## adjusted_prices

One series per instrument, chosen like this:

| Instrument | Source | `method` |
|---|---|---|
| 10 stocks with Yahoo data | Yahoo, already adjusted, used as is (never adjusted again) | `provider_adjusted` |
| 39 other stocks | raw NSE prices x our split factors; `raw_close` and `adj_factor` kept; volume scaled the other way | `derived_adjustment` |
| 6 indices | Yahoo first, older dates filled from Kaggle (same basis: index level) | `index_level` |

One source per stock on purpose: mixing sources would mix adjustment bases inside one series.
The ex-date itself is not adjusted (it already trades at the new price). Dividends are not applied.

**Missing data is never invented.** Rows that are not real trading are dropped and counted; other suspicious rows are
flagged (`quality_flags`) and kept.

Dropped (the raw collections keep every row; only the processed dataset omits them):

| Rule | Why | Rows |
|---|---|---|
| Phantom rows: a stock row with zero volume and a price jump above 30% | a provider invented a bar for a day with no trading (RELIANCE and TCS on 2005-07-28 at Yahoo: volume 0, price 4x and 2x their neighbours, on a day NSE has no row at all) | 2 |
| Before a confirmed listing date (`LISTING_DATES`, TCS: 2004-08-25) | Yahoo has TCS from 2002-08-12 with tiny volumes, two years before its IPO; the NSE record starts on the listing day. Add a symbol only with evidence like this | 531 |
| Leading flat zero-volume rows of a stock | pre-listing placeholders | 0 found |

Flagged and kept:

| Flag | Meaning | Count |
|---|---|---|
| `return_outlier` | one-day move above 30% (stocks) or 15% (indices): a crash, a demerger or bad data | 24 |
| `gap_before` | more than 7 calendar days since the previous row | 21 |
| `no_trade` | open = high = low = close and zero volume (stocks only; the provider repeated the previous close) | 804 |

Of the 24 outliers, the genuine ones are real market events (the 2009-05-18 election rally in every index, the March 2020
crash). The rest are splits the detector did not match (above) and Yahoo glitches that cannot be verified (LT 2006-09-27/28:
-50.6% then +95.1%; RELIANCE 1997). INFRATEL is skipped: its source file is empty.

Analytics that must not count padded days should exclude `no_trade` rows.

## Symbol standardisation

Canonical form: upper case, no spaces, no exchange suffix (`RELIANCE`, `M&M`, `NIFTY_50`).
`resolve_symbol(db, text)` finds an instrument from `reliance.ns`, `NSE:TCS`, `Nifty 50`, `^NSEI`, an alias such as
`MM` or `banknifty`, or an earlier ticker such as `UTIBANK`.

Earlier tickers were read from the Symbol column of the raw NSE files and stored in `instruments.symbol_history`:
15 stocks changed ticker, e.g. UTIBANK (to 2007) became AXISBANK, TELCO became TATAMOTORS, TISCO became TATASTEEL,
INFOSYSTCH became INFY, and SESAGOA became SSLT (2013 to 2015) and then VEDL. The source also contains a typo
(`HINDALC0`, digit zero), which is recorded with a note.

## Results on the real data (Nov 2026)

| | |
|---|---|
| `corporate_actions` | 469 documents (31 Yahoo + 59 inferred splits, 379 dividends) |
| `adjusted_prices` | 295,086 rows: 184,953 derived (39 stocks), 69,313 Yahoo (10 stocks), 40,820 index (6 indices) |
| Duplicates (raw and processed) | 0; every intraday series matches its coverage count |
| Intraday completeness | 99.2% to 99.5% of sessions are full; 56 to 66 short days per interval, mostly Diwali Muhurat evenings |
| Calendar gaps | 1,896 stock-days missing in total; GAIL, KOTAKBANK, SHREECEM and INDUSINDBK each lack 321 trading days (2000-03-21 to 2001-07-02: a block missing in the NSE source) |

**Our adjustment against Yahoo's, day by day** (days where the two daily returns differ by more than 1 percentage point):

| Stock | Days differing | Comment |
|---|---|---|
| HDFCBANK, INFY, TCS | 0 of about 4,100 to 5,300 | agree on every day |
| RELIANCE | 1 | 2006-01-18, the demerger that only Yahoo adjusts |
| ICICIBANK | 5 | includes 2017-06-20, the missed 11-for-10 event; the 2008 days are not investigated |
| ITC, LT, BHARTIARTL, SBIN | 3, 2, 1, 1 | isolated days, not investigated |
| AXISBANK | 173 | almost all in 2000 to 2002: the price levels agree (ratio 1.0 every year), but early Yahoo closes differ by a few percent and its volume is about a third of NSE's, consistent with an early history from another venue (not verified) |

## Things to know

- **Yahoo versus NSE for early years.** For stocks that use Yahoo, early rows (before about 2003) may differ from NSE. The
  processed series follows Yahoo; use `stock_prices` (kaggle, raw) to compare.
- **TCS and LT start earlier at Yahoo than at NSE** (the audit lists them as `series_starts`, using the raw data). For TCS the
  NSE record begins on its 2004-08-25 listing, so the 531 earlier Yahoo rows are dropped from the processed dataset (see
  above). For LT (517 Yahoo rows before 2004-06-23) there is no such evidence, since L&T was listed long before; the rows
  have real volume and are kept, and the cause is unexplained.
- **39 stocks end on 2021-04-30**, where the Kaggle NSE data ends. Downloading their Yahoo history would extend them to today.
- **Odd sessions:** 2021-02-24 has only 54 one-minute NIFTY 50 bars (an NSE outage was widely reported that day; not
  checked against our data). Diwali Muhurat evenings account for the other short days.

## Troubleshooting

| Problem | Fix |
|---|---|
| `adjust` skips a symbol | it has no price rows yet; import it, then run `adjust` again |
| An inferred split looks wrong | it is stored with `observed_ratio`, `volume_ratio` and `confidence: inferred`; delete that document and re-run `adjust` (a later `actions` run would recreate it, so adjust `NICE_FACTORS` in `corporate_actions.py` instead) |
| Counts changed after re-running | expected if raw data or actions changed; `adjusted_prices` is always rebuilt per symbol |
| `audit` is slow | the intraday completeness pass reads 5.4 million bars (about 15 seconds here) |
