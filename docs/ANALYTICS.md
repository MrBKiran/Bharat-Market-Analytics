# Technical indicators and market analytics (Phase 7)

Reads the processed dataset (`adjusted_prices`, Phase 6) and the intraday bars, and writes the `indicators`
(time-series) and `analytics_results` collections.

```powershell
.venv\Scripts\python scripts\analyze_data.py sectors                      # sectors for stocks that have none
.venv\Scripts\python scripts\analyze_data.py indicators                   # daily indicators, all 55 instruments
.venv\Scripts\python scripts\analyze_data.py indicators --intervals 5m 15m 1h --symbols NIFTY_50 NIFTY_BANK
.venv\Scripts\python scripts\analyze_data.py analytics                    # 21 analysis documents
.venv\Scripts\python scripts\analyze_data.py report                       # readable highlights
.venv\Scripts\python scripts\analyze_data.py all
```
Every step replaces its own earlier results, so it is safe to re-run. Steps are recorded in `processing_jobs`
(`job_type` `indicators` and `analytics`).

## Indicators

Written directly in pandas ([backend/app/analytics/indicators.py](../backend/app/analytics/indicators.py)), so every formula is visible.

| Field | Indicator | Notes |
|---|---|---|
| `sma_20/50/200`, `ema_20/50/200` | simple and exponential moving averages | EMA span n, seeded with the first value |
| `rsi_14` | Relative Strength Index | Wilder smoothing; flat = 50, only gains = 100 |
| `macd`, `macd_signal`, `macd_hist` | MACD 12/26/9 | |
| `atr_14` | Average True Range | Wilder, in price points |
| `bb_upper/mid/lower`, `bb_pct_b`, `bb_width` | Bollinger Bands 20, 2 | population standard deviation (ddof 0) |
| `stoch_k`, `stoch_d` | Stochastic oscillator 14, 3 | |
| `vwap_20` / `vwap_session` | VWAP | rolling 20-day on daily bars; restarts every IST day on intraday bars |
| `adr_14`, `adr_pct_14` | average daily range | points and % of close |
| `hist_vol_20` | historical volatility | annualised, in percent; intraday uses bars per session x 252 |
| `vol_sma_20`, `vol_sma_50` | volume moving averages | |

Warm-up values are omitted, nothing is back-filled. **Volume-based fields (VWAP, volume averages) exist only for
instruments with volume.** The indices have none (the sources report 0), so their VWAP stays empty rather than being faked.

**Verification.** The library was checked against the independent `ta` package on 1,500 bars: SMA, EMA, MACD, Bollinger and
Stochastic differ by exactly 0; RSI by 1.5e-8 and ATR by 1.7e-11 (how the first value seeds the smoothing). This check is part
of the test suite.

**What is stored (first run):**

| Interval | Series | Bars | Documents |
|---|---|---|---|
| 1d (all 55 instruments) | 55 | 295,086 | 294,371 |
| 5m, 15m, 1h (the 4 indices) | 12 | 1,197,725 | 1,197,569 |
| Total in `indicators` | 67 | | 1,491,940 (479 MB on disk) |

NIFTY 50 took 9.6 s for 5-minute (209,888 bars), 3.3 s for 15-minute and 1.5 s for hourly. One-minute indicators are not
stored (about 4 million more documents): compute them on demand with `compute_indicators` for any symbol and timeframe.

## Market analytics

Each analysis is a document in `analytics_results` with `scope` `{snapshot, as_of}` (or `{history: all}`).

| `analysis_type` | What it answers |
|---|---|
| `performance` | returns over 1 day, 1 week, 1/3/6 months, 1 year and year-to-date for every instrument |
| `top_movers` | top gainers and losers over 1 day, 1 week, 1 month (gainers only rose, losers only fell) |
| `trend` | rule-based label per instrument: five checks (close vs 50 and 200-day average, EMA20 vs EMA50, MACD sign, RSI vs 50); 4-5 bullish, 0-1 bearish |
| `market_breadth` | advancers/decliners, % above the 50 and 200-day averages, 52-week highs/lows, monthly advance-decline line |
| `sector_performance` | equal-weighted return per sector, best and worst member |
| `volatility` | daily and weekly volatility (and annualised), 20-day historical volatility, most/least volatile |
| `volume_analysis` | volume against the prior 20-day average, 2x spikes, most active by value traded |
| `correlation` | return correlation matrix (3 years), most and least correlated stock pairs |
| `index_analysis` | NIFTY 50 and NIFTY BANK: returns, calendar-year returns, drawdown, volatility, best/worst days, 52-week range, trend, correlation and beta |
| `return_distribution` | histogram, skew, kurtosis, percentiles and VaR of daily returns, all history |
| `historical_statistics` | CAGR, volatility, maximum drawdown, best/worst day, share of up days, average volume |
| `outliers` | the largest one-day moves and robust (median/MAD) outlier counts |

**Two snapshots.** Only 10 stocks have data to 2026; the other 39 stop on 2021-04-30. So every as-of analysis is computed
twice: `latest` (2026-11-20: 10 stocks and 6 indices, the 39 stale stocks excluded and listed) and `full_universe`
(2021-04-30, the latest day 90% of stocks have a price: 49 stocks and 6 indices). Breadth over 10 stocks is thin; the second
snapshot gives a market-wide picture, but of 2021.

## First results (Nov 2026)

| | Latest (2026-11-20) | Full universe (2021-04-30) |
|---|---|---|
| Trend (stocks) | 8 bullish, 2 neutral, 0 bearish | 15 bullish, 25 neutral, 9 bearish |
| Breadth | 8 up / 2 down; 100% above 50-day, 80% above 200-day | 12 up / 37 down; 42.9% above 50-day, 87.8% above 200-day |
| NIFTY 50 | 25,800.60; 1y -1.5%; vol 14.4%; trend bullish | 14,631.10; 1y +48.4%; vol 19.7%; trend neutral |
| NIFTY BANK | 64,635.20; 1y +8.9%; vol 18.0%; trend bullish | 32,781.80; 1y +52.2%; vol 32.5%; trend neutral |
| NIFTY vs BANK NIFTY | correlation 0.87, beta of Bank on Nifty 1.05 | correlation 0.91, beta 1.25 |
| Top sector, 1 month | Financial Services +18.6% | Metals & Mining +26.6% |

History (stocks with 10+ years and no unexplained >30% one-day move): highest CAGR BAJFINANCE 38.7%, EICHERMOT 33.8%,
SHREECEM 32.9%.

## Data problems the analytics exposed (and what was done)

The first run produced figures that could not be real, which led to fixes in the processed data (Phase 6 rules, see
[PROCESSING.md](PROCESSING.md)):

- **Phantom rows:** RELIANCE +337% and TCS +100% on 2005-07-28 were Yahoo bars with zero volume on a day with no trading.
  Dropped (2 rows).
- **TCS before its listing:** 531 Yahoo rows from 2002 to 2004 with tiny volumes. Dropped; the NSE record begins on the listing day.
- **Missed splits:** TITAN 2011 and VEDL 2008 (-94.7%) and BRITANNIA 2010, UPL 2005 (-78%) were splits the detector did not
  match. The ratio list was extended; the record against Yahoo did not change.
- **"Top losers" listed a stock that rose** (the bottom five of a ten-stock universe). Losers now only include stocks that fell.
- **A reliability marker:** each stock's statistics carry `days_over_30pct_move`; the report ranks CAGR and drawdown only among
  stocks with none.

What remains: the deepest drawdowns (ZEEL -96%, HCLTECH -92%, M&M -92%) run from the February 2000 bubble peak to the September
2001 trough, with no unexplained jump in between, and for HCLTECH and ZEEL the raw prices show the same fall. M&M's early-2000
prices could not be verified independently.

## Limits

- **"Bullish" and "bearish" are simple rule-based labels, not predictions or advice.**
- **Sectors:** 40 of the 50 stocks got their sector from a table in [sectors.py](../backend/app/analytics/sectors.py) based on general knowledge of
  NSE's classification (`sector_source = assigned_by_project`), not a downloaded file. Check it before relying on one stock.
- **Survivorship bias:** sector and breadth figures use today's stock list and sectors for all dates, and stocks that left the
  index are absent.
- **Returns exclude dividends.** Prices are split-adjusted only.
- **Undetected splits** (CIPLA 2006, KOTAKBANK 2005, WIPRO 2010, ASIANPAINT 2000) and Yahoo glitches (LT 2006-09-27/28) remain
  in the data, flagged `return_outlier`.
- **Thin latest breadth:** only 10 stocks are current. Downloading Yahoo history for the other 39 would fix that.

## Troubleshooting

| Problem | Fix |
|---|---|
| `adjusted_prices is empty` | run `scripts/process_data.py adjust` first |
| `analytics` shows few stocks in `latest` | the other stocks end on 2021-04-30 and are excluded as stale (listed in `universe.stale_excluded`) |
| An indicator field is missing on a document | warm-up period, or the instrument has no volume |
| Intraday indicators missing for a symbol | `indicators --intervals 5m --symbols X` only runs where `intraday_coverage` shows data |
