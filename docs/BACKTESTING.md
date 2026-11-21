# Strategy backtesting (Phase 9)

Seven trading strategies, a backtest engine with realistic execution and Indian transaction costs, performance metrics, and
storage of every result in MongoDB (`backtest_results`, `trading_signals`).

```powershell
.venv\Scripts\python scripts\backtest.py list                                          # the strategies and their parameters
.venv\Scripts\python scripts\backtest.py run RELIANCE ema_crossover --start 2010-01-01   # one stock, one strategy (stored)
.venv\Scripts\python scripts\backtest.py run TCS rsi_reversal --param oversold=25 --param overbought=75
.venv\Scripts\python scripts\backtest.py run NIFTY_50 ema_crossover --interval 5m --start 2024-01-01 --param fast=12 --param slow=48
.venv\Scripts\python scripts\backtest.py compare                                        # all strategies x all tradable stocks
.venv\Scripts\python scripts\backtest.py sweep TCS ema_crossover --grid fast=10,20,30 --grid slow=50,100,200
.venv\Scripts\python scripts\backtest.py show <run_id>                                  # print a stored result
```
Options on `run`, `compare` and `sweep`: `--start`, `--end` (the date range traded; default start 2000-01-01 because the 1990s
data is thin), `--interval`, `--capital`, `--slippage-bps`, `--position-pct`, `--risk-pct`, `--costs delivery|intraday|stress|none`,
`--no-store`. A stock, a timeframe, a date range, a strategy and its parameters are all selectable.

## Layout

| Part | File |
|---|---|
| Brokerage, STT, exchange, SEBI, stamp duty, GST, DP charges; capital-gains tax estimate | `backend/app/backtesting/costs.py` |
| The seven strategies (signals only) and parameter validation | `strategies.py` |
| The simulator: fills, stops, targets, sizing | `engine.py` |
| Metrics and equity-curve thinning | `metrics.py` |
| Database runs, comparison, parameter sweep | `runner.py` |
| Command line | `scripts/backtest.py` |

## The strategies

All are **long-only** (a delivery position in Indian cash equities cannot be shorted). Every strategy only produces three
things for each bar: an entry signal, an exit signal and a proposed stop price. It never sees cash, positions or fills.

| Strategy | Buys when | Sells when |
|---|---|---|
| `ema_crossover` | fast EMA (20) crosses above slow EMA (50) | fast EMA is below slow EMA |
| `rsi_reversal` | RSI(14) climbs back above 30 | RSI reaches 70 |
| `vwap_trend` | close crosses above VWAP (+0.3%): rolling 20-bar VWAP on daily bars, session VWAP intraday | close below VWAP (-0.3%). Needs volume: an index never trades |
| `trend_pullback` | uptrend (EMA20 above a rising EMA50), price dipped under EMA20 and is back above it. Stop under the 5-bar low, target 2R | close under EMA50 |
| `breakout_retest` | close clears the 20-bar high, then within 10 bars price retests that level and closes above it. Stop under the retest low, target 2R | close under the 10-bar low |
| `pivot_trendline` | price bounces off a rising support line through the last two swing lows (pivots are used only after they are confirmed). Stop 1 ATR under the line, target 2R | close more than 0.5 ATR under the line |
| `ma_trend_following` | close crosses above a rising 200-day average (+1%), stop 3 ATR | close under the average (-1%) |

Strategies without their own stop use an ATR-based stop (`atr_stop_mult` ATRs under the signal close), so every trade has a
defined risk and an R multiple.

## How look-ahead bias, repainting and unrealistic fills are prevented

| Risk | What the code does | Proof |
|---|---|---|
| Trading on a price you could not have had | A signal is computed from bar *t*'s close and filled at bar *t+1*'s **open** | `test_signal_at_the_close_is_filled_at_the_next_open_not_the_close` |
| Strategy peeking at later bars | Indicators are causal (rolling / exponential); pivots are used only `pivot_bars` bars after they form | `test_signals_are_causal_...` (all 7): signals computed on data cut off at bar *t* equal the full-data signals up to *t* |
| Repainting | The same test: a signal, once given, never changes when more data arrives | same test, plus a "future shock" test that multiplies every later price by 3 |
| Engine peeking | A backtest of the first 600 bars gives the same closed trades as the first part of the full backtest | `test_a_backtest_of_the_first_part_matches_...` |
| Optimistic stops | The stop is checked against the bar's low; if the bar **opens** beyond it (a gap) the fill is the open, not the stop price | `test_stop_fills_at_the_stop_but_a_gap_fills_at_the_open` |
| Unknowable intrabar order | If one bar could hit both stop and target, the **stop wins** | `test_when_stop_and_target_are_both_possible_...` |
| Dead setups | If the next open is already past the proposed stop, the entry is skipped | `test_entry_is_skipped_...` |
| Free trading | Every fill is moved against you by `slippage_bps`; every order pays the itemised costs | `test_costs_and_slippage_are_charged_on_both_sides` |
| Overnight risk in intraday tests | `force_eod` (automatic for intraday intervals): flat at the open of the day's last bar, no entry on that bar | `test_intraday_runs_are_flat_...` |

I also checked that these tests **can fail**: a deliberately cheating strategy (buys when tomorrow's close is higher) and one using
a centred moving average were both caught at 3 of 6 cut points.

**Not prevented, so read the warnings stored with every result:**

- **Survivorship bias.** The stock list is today's index members. Companies that were delisted or dropped out are missing. This
  makes every stock-level result look better than a real investor's would have been, and **it is the main reason buy-and-hold
  looks so strong below**. Compare strategies with each other; do not read the returns as achievable.
- **Overfitting.** Changing parameters until the backtest looks good fits noise. Use the in-sample/out-of-sample split and `sweep`.
- **Data.** Prices are split-adjusted, not dividend-adjusted (dividends are missing from every return). 24 return outliers flagged in
  Phase 6 are left in. 39 stocks end on 2021-04-30 (the Kaggle data's end), so for them nothing after that date is tested; this is
  stored as a warning on each result.
- **Liquidity.** Fills assume you can trade your size at the open plus slippage. Fine for large caps, wrong for illiquid stocks.
- **Costs are assumptions** (below).

## Costs (all assumptions, all configurable)

`costs.py` models one order ("leg") with these charges. The defaults are an approximate recent discount-broker schedule, **not facts
about any particular day or broker** (stamp duty, STT and exchange charges changed over the 26 years tested).

| Charge | Delivery default | Intraday default |
|---|---|---|
| Brokerage | 0 | 0.03%, capped at Rs 20 per order |
| STT | 0.1% buy and sell | 0.025% sell only |
| Exchange transaction charge | 0.00297% | same |
| SEBI fee | Rs 10 per crore (0.0001%) | same |
| Stamp duty | 0.015% buy | 0.003% buy |
| GST | 18% on brokerage + exchange + SEBI + DP | same |
| DP charge | Rs 13.5 per sale | 0 |

Plus `slippage_bps` (default 5 per fill). Presets: `delivery`, `intraday`, `none`, and **`stress` (every charge doubled)**, which
shows how fragile a strategy is. Any single charge can be overridden in code with `costs.preset("delivery", dp_charge_sell=0.0)`.

A rough **capital-gains tax estimate** (20% short-term, 12.5% long-term above Rs 1.25 lakh a year, current rates applied to
all years, no loss carry-forward, delivery trades only) is stored as `tax_estimate`. It is an estimate, not tax advice.

## What is stored

`backtest_results`: one document per run. Beyond the required fields (`run_id`, `strategy`, `symbol`, `exchange`, `interval`,
`params`, `metrics`, `created_at`):

| Field | Content |
|---|---|
| `metrics` | total trades, winners, losers, win rate, net P&L, profit factor, max drawdown, average risk-to-reward, payoff ratio, average R, expectancy, CAGR, Sharpe (approx.), exposure, costs paid, exit-reason counts |
| `benchmark` | buy-and-hold of the same instrument over the same dates, same costs (and always delivery costs, even for intraday runs) |
| `periods` | the same strategy in-sample (first 70% of the dates) and out-of-sample (last 30%), each next to its buy-and-hold |
| `equity_curve` | at most 800 points: `ts`, `equity`, `drawdown_pct`, `benchmark_equity` |
| `trades` | up to 1,500 trades: entry/exit time and price, quantity, gross and net P&L, costs, return, holding time, stop, target, R multiple, worst and best price excursion, exit reason |
| `costs` | the whole cost model, slippage, sizing and the execution rules used |
| `warnings`, `input`, `tax_estimate`, `batch` | caveats, the bars used, tax estimate, `compare` batch tag |

`trading_signals`: every buy signal (executed or skipped, with the reason in `action`) and every sell signal that closed a trade,
tied to the run by `run_id`. Re-running `compare` replaces the earlier `compare` batch (results and signals), so nothing accumulates.

**Metric definitions.** *Profit factor* = gross profit / gross loss (stored as null when there is no losing trade, since it would be
infinite). *Average risk-to-reward* = average R of winning trades / average R of losing trades, where R is the net P&L divided by the
money risked at the stop. *Payoff ratio* = average win / average loss in rupees. *Sharpe* = mean / standard deviation of per-bar equity
returns, annualised, risk-free rate 0 (an approximation: flat days count as zero return). *Max drawdown* = largest peak-to-trough
fall of the equity curve.

## Results on the real data (Nov 2026)

`scripts\backtest.py compare`: 49 tradable stocks x 7 strategies = **343 runs**, daily bars, 2000-01-01 to the end of each stock's
data, Rs 10 lakh capital, all-in positions, delivery costs and 5 bps slippage. All 343 equity curves reconcile exactly with their
trades. Medians across the stocks:

| Strategy | Trades | Win rate | Profit factor | Return | Buy and hold | Beat buy-and-hold | Max drawdown | Sharpe | Out-of-sample return | Out-of-sample buy-and-hold |
|---|---|---|---|---|---|---|---|---|---|---|
| `ema_crossover` | 45 | 32.0% | 1.62 | 309% | 1,497% | 9 / 49 | -54.8% | 0.38 | 37.2% | 95.2% |
| `vwap_trend` | 245 | 32.8% | 1.13 | 140% | 1,497% | 3 / 49 | -66.5% | 0.32 | 17.4% | 95.2% |
| `ma_trend_following` | 28 | 24.4% | 1.56 | 90% | 1,497% | 2 / 49 | -53.1% | 0.29 | -3.3% | 95.2% |
| `trend_pullback` | 98 | 36.6% | 1.20 | 73% | 1,497% | 3 / 49 | -46.5% | 0.25 | 6.7% | 95.2% |
| `rsi_reversal` | 39 | 38.9% | 1.09 | 23% | 1,497% | 2 / 49 | -71.2% | 0.15 | 28.4% | 95.2% |
| `pivot_trendline` | 139 | 35.0% | 1.02 | 7% | 1,497% | 2 / 49 | -59.8% | 0.11 | 3.7% | 95.2% |
| `breakout_retest` | 111 | 34.0% | 0.86 | -26% | 1,497% | 2 / 49 | -54.2% | -0.06 | -8.9% | 95.2% |
| *buy and hold* | | | | | | | -75.1% | 0.60 | | |

**What this does and does not show**

- **Buy-and-hold beat nearly every strategy on nearly every stock**, with a much better Sharpe ratio (0.60 against at most 0.38). It also
  had a deeper worst drawdown (-75% against -47% to -71%): strategies were out of the market about 46% to 89% of the time (median), which
  avoided some falls but missed the long climbs. These stocks are today's index members that rose over 26 years, so the benchmark is
  flattered by survivorship bias and the strategies' relative result is worse than it would be on a fair universe. Neither side is
  a recommendation.
- **Only `ema_crossover` and `ma_trend_following` had a median profit factor above 1.5**, and both are slow trend followers. The lower win rates (24% to 39%) are normal for trend following: a few large winners pay for many small losses (`ema_crossover`'s
  median average risk-to-reward is 4.9, `ma_trend_following`'s is 7.8).
- **`breakout_retest` lost money** (profit factor 0.86, and a negative Sharpe). That is a real result of these default parameters on
  this data, not a bug (its trades reconcile and it passes the causality tests).
- **Costs matter but did not decide the result**: they were 3% to 11% of gross profit. Doubling every charge and slippage (the stress
  preset with 10 bps) cut `ema_crossover` from 309% to 237% median, `rsi_reversal` from 23% to 9% and `vwap_trend` from 140% to 23%, so
  the more often a strategy trades, the more fragile it is.
- **Few trades**: slow strategies make few trades (`ma_trend_following` median 28), and every result with fewer than 30 trades
  carries a warning saying its statistics are weak. A median across 49 stocks is also
  not 49 independent samples: Indian large caps move together.

**Overfitting, shown (RELIANCE, `ema_crossover`, 30 parameter combinations):** the best combination in-sample (fast 30 / slow 200,
Sharpe 0.70) had an out-of-sample Sharpe of **-0.16**, against a median of 0.26 over all combinations. The rank correlation between
in-sample and out-of-sample results was **-0.31**: the in-sample ranking did not predict the future, so "optimising" mostly fitted
noise. This is why `sweep` always prints both.

**Intraday example** (NIFTY_50, 5-minute bars, 2024 to May 2026, EMA 12/48, intraday costs): 484 trades, win rate 21%, profit factor
0.34, **-48%**; buy-and-hold on the same dates +8.9%. Frequent trading with a 5 bps slippage and per-trade charges loses money
on a signal this simple. An index cannot be traded directly (the result is stored with an INDEX warning).

## Things to know

- Indices (`NIFTY_50` ...) can be backtested as a signal-quality proxy, not as something you can trade. `compare` uses stocks and ETFs only.
- `vwap_trend` needs volume; on an index it never trades and says so in `warnings`.
- A position still open at the end of the range is closed at the last close (`end_of_data`) so every trade is counted.
- `risk_pct` sizes each trade so that being stopped out costs that percentage of equity; `position_pct` caps the money committed.
- Long-only, one position at a time, whole shares. Not modelled: partial fills, order-book impact, circuit limits, halts, dividends,
  margin and short selling.

## Troubleshooting

| Problem | Fix |
|---|---|
| `only N bars in the database` | import and process the data first (Phases 5-6), or pick another symbol/interval |
| A strategy never trades | check `warnings` (VWAP needs volume) and the date range; slow averages need 200+ bars of warm-up before the start date |
| `parameter ... must be ...` | parameters are validated (whole bars, `fast < slow`, `0 <= oversold < overbought <= 100`) |
| Results change between runs | they should not for the same data and parameters; the `run_id` and timestamps are the only differences |
