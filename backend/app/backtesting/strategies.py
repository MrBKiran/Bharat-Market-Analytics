"""The seven trading strategies. Each one turns a price frame into SIGNALS and nothing else.

A strategy never sees positions, cash or fills. For every bar it answers three questions using only data up
to and including that bar's close:
    entry  should a long position be opened?   (executed by the engine at the NEXT bar's open)
    exit   should an open position be closed?  (executed at the NEXT bar's open)
    stop   where would the protective stop sit if we entered now?  (a price; NaN = no opinion)

That is how look-ahead is kept out: a signal at the close of bar t can only ever be filled at bar t+1.
`tests/test_backtesting.py` proves it for every strategy: signals computed on data cut off at bar t are
identical to the signals the full data gives for bars up to t (so nothing "repaints").

Long-only on purpose: in Indian cash equities a delivery position cannot be shorted, and indices are not
tradable at all (the engine labels index results as a signal-quality proxy).

Input frame columns: ts (UTC), open, high, low, close, volume (NaN or 0 for indices).
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from app.analytics import indicators as ind


@dataclass
class Signals:
    entry: pd.Series                       # bool, signal at the close of the bar
    exit: pd.Series                        # bool
    stop: pd.Series                        # float stop price proposed at the signal bar
    target_r: Optional[float] = None       # take-profit at this many times the initial risk (None = no target)
    lines: dict = field(default_factory=dict)   # indicator lines, for charts


@dataclass(frozen=True)
class Strategy:
    name: str
    title: str
    description: str
    defaults: dict
    fn: Callable
    needs_volume: bool = False


# ----------------------------------------------------------------------------------------------- helpers
def cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    """a moved from at-or-below b to above b on this bar. NaN anywhere (warm-up) gives False."""
    return ((a > b) & (a.shift(1) <= b.shift(1))).fillna(False)


def cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    return ((a < b) & (a.shift(1) >= b.shift(1))).fillna(False)


def _atr_stop(df: pd.DataFrame, mult: float, n: int = 14) -> pd.Series:
    return df["close"] - mult * ind.atr(df["high"], df["low"], df["close"], n)


def _falsy(df: pd.DataFrame) -> pd.Series:
    return pd.Series(False, index=df.index)


def _is_intraday(df: pd.DataFrame) -> bool:
    if len(df) < 3:
        return False
    return pd.Series(df["ts"]).diff().median() < pd.Timedelta(hours=20)


# ------------------------------------------------------------------------------------------- strategies
def ema_crossover(df, fast=20, slow=50, atr_stop_mult=2.0, target_r=None) -> Signals:
    """Buy when the fast EMA crosses above the slow EMA; sell when it falls back below."""
    f, s = ind.ema(df["close"], fast), ind.ema(df["close"], slow)
    return Signals(entry=cross_up(f, s), exit=(f < s).fillna(False), stop=_atr_stop(df, atr_stop_mult),
                   target_r=target_r, lines={f"ema_{fast}": f, f"ema_{slow}": s})


def rsi_reversal(df, period=14, oversold=30.0, overbought=70.0, atr_stop_mult=2.0, target_r=None) -> Signals:
    """Mean reversion: buy when RSI climbs back above the oversold level; sell when it reaches overbought."""
    r = ind.rsi(df["close"], period)
    return Signals(entry=cross_up(r, pd.Series(oversold, index=df.index)), exit=(r >= overbought).fillna(False),
                   stop=_atr_stop(df, atr_stop_mult), target_r=target_r, lines={f"rsi_{period}": r})


def vwap_trend(df, window=20, band_pct=0.3, atr_stop_mult=2.0, target_r=None) -> Signals:
    """Buy when the close crosses above VWAP (plus a small band); sell when it drops below VWAP (minus the band).

    Daily bars use a rolling VWAP of `window` bars; intraday bars use the session VWAP that restarts every
    day. VWAP needs traded volume: for an index (no volume) it stays empty and the strategy never trades.
    """
    if _is_intraday(df):
        v = ind.vwap_session(df["ts"], df["high"], df["low"], df["close"], df["volume"])
    else:
        v = ind.vwap_rolling(df["high"], df["low"], df["close"], df["volume"], window)
    up, down = v * (1 + band_pct / 100), v * (1 - band_pct / 100)
    return Signals(entry=cross_up(df["close"], up), exit=(df["close"] < down).fillna(False),
                   stop=_atr_stop(df, atr_stop_mult), target_r=target_r, lines={"vwap": v})


def trend_pullback(df, fast=20, slow=50, swing_bars=5, target_r=2.0) -> Signals:
    """Buy a dip inside an uptrend: fast EMA above slow EMA, the close dipped under the fast EMA and is back above it.

    Stop: just under the lowest low of the last `swing_bars` bars (the pullback low). Exit: close under the slow EMA.
    """
    f, s = ind.ema(df["close"], fast), ind.ema(df["close"], slow)
    atr = ind.atr(df["high"], df["low"], df["close"], 14)
    uptrend = (f > s) & (s > s.shift(10))
    resumed = cross_up(df["close"], f)
    entry = (uptrend & resumed).fillna(False)
    stop = df["low"].rolling(swing_bars, min_periods=swing_bars).min() - 0.1 * atr
    return Signals(entry=entry, exit=(df["close"] < s).fillna(False), stop=stop, target_r=target_r,
                   lines={f"ema_{fast}": f, f"ema_{slow}": s})


def breakout_retest(df, lookback=20, retest_window=10, exit_bars=10, target_r=2.0) -> Signals:
    """Buy the retest of a breakout: the close clears the highest high of the last `lookback` bars; within
    `retest_window` bars price comes back to touch that level and closes above it again.

    Stop: under the retest bar's low or the level (whichever is lower) minus a quarter ATR. A close well below
    the level cancels the setup. Exit: close under the lowest low of the last `exit_bars` bars.
    """
    n = len(df)
    close, high, low = df["close"].to_numpy(float), df["high"].to_numpy(float), df["low"].to_numpy(float)
    atr = ind.atr(df["high"], df["low"], df["close"], 14).to_numpy(float)
    prior_high = df["high"].shift(1).rolling(lookback, min_periods=lookback).max().to_numpy(float)
    entry, stop = np.zeros(n, bool), np.full(n, np.nan)
    level, broke_at = np.nan, -1
    for t in range(n):
        if np.isnan(atr[t]):
            continue
        if level == level:                                     # a breakout is waiting for its retest
            if t - broke_at > retest_window or close[t] < level - 0.5 * atr[t]:
                level = np.nan                                 # too late, or the breakout failed
            elif low[t] <= level + 0.25 * atr[t] and close[t] > level:
                entry[t] = True
                stop[t] = min(low[t], level) - 0.25 * atr[t]
                level = np.nan                                 # one entry per breakout
        if level != level and not np.isnan(prior_high[t]) and close[t] > prior_high[t]:
            level, broke_at = prior_high[t], t                 # a new breakout starts waiting
    channel_low = df["low"].shift(1).rolling(exit_bars, min_periods=exit_bars).min()
    return Signals(entry=pd.Series(entry, index=df.index), exit=(df["close"] < channel_low).fillna(False),
                   stop=pd.Series(stop, index=df.index), target_r=target_r, lines={"channel_low": channel_low})


def pivot_trendline(df, pivot_bars=5, tol_atr=0.5, max_age=120, target_r=2.0) -> Signals:
    """Buy a bounce off a rising support line drawn through the last two swing lows.

    A swing low is only known `pivot_bars` bars AFTER it happened (it needs that many higher lows on its right),
    so a pivot is used only from its confirmation bar onwards: the line never repaints into the past. Entry:
    the bar's low touches the line (within `tol_atr` ATRs) and it closes above it. Stop: one ATR under the
    line. Exit: close more than `tol_atr` ATRs under the line.
    """
    n, k = len(df), pivot_bars
    low, close = df["low"].to_numpy(float), df["close"].to_numpy(float)
    atr = ind.atr(df["high"], df["low"], df["close"], 14).to_numpy(float)
    entry, exit_, stop, line_out = np.zeros(n, bool), np.zeros(n, bool), np.full(n, np.nan), np.full(n, np.nan)
    pivots: list[int] = []
    for t in range(n):
        i = t - k                                              # the bar whose pivot status is decided now
        if i >= k and low[i] < low[i - k:i].min() and low[i] <= low[i + 1:t + 1].min():
            pivots.append(i)
        if len(pivots) < 2 or np.isnan(atr[t]):
            continue
        p1, p2 = pivots[-2], pivots[-1]
        if low[p2] <= low[p1] or t - p2 > max_age:             # support must be rising and still fresh
            continue
        line = low[p2] + (low[p2] - low[p1]) / (p2 - p1) * (t - p2)
        line_out[t] = line
        if low[t] <= line + tol_atr * atr[t] and close[t] > line:
            entry[t], stop[t] = True, line - atr[t]
        if close[t] < line - tol_atr * atr[t]:
            exit_[t] = True
    idx = df.index
    return Signals(entry=pd.Series(entry, index=idx), exit=pd.Series(exit_, index=idx), stop=pd.Series(stop, index=idx),
                   target_r=target_r, lines={"support": pd.Series(line_out, index=idx)})


def ma_trend_following(df, ma=200, band_pct=1.0, slope_bars=20, atr_stop_mult=3.0, target_r=None) -> Signals:
    """Long-term trend following: buy when the close crosses above a rising moving average (plus a band);
    sell when it closes below the average (minus the band). No target: winners are left to run."""
    m = ind.sma(df["close"], ma)
    rising = m > m.shift(slope_bars)
    entry = (cross_up(df["close"], m * (1 + band_pct / 100)) & rising).fillna(False)
    return Signals(entry=entry, exit=(df["close"] < m * (1 - band_pct / 100)).fillna(False),
                   stop=_atr_stop(df, atr_stop_mult), target_r=target_r, lines={f"sma_{ma}": m})


# ------------------------------------------------------------------------------------------- registry
STRATEGIES = {s.name: s for s in (
    Strategy("ema_crossover", "EMA crossover", ema_crossover.__doc__.split("\n")[0],
             {"fast": 20, "slow": 50, "atr_stop_mult": 2.0, "target_r": None}, ema_crossover),
    Strategy("rsi_reversal", "RSI overbought / oversold", rsi_reversal.__doc__.split("\n")[0],
             {"period": 14, "oversold": 30.0, "overbought": 70.0, "atr_stop_mult": 2.0, "target_r": None}, rsi_reversal),
    Strategy("vwap_trend", "VWAP trend", vwap_trend.__doc__.split("\n")[0],
             {"window": 20, "band_pct": 0.3, "atr_stop_mult": 2.0, "target_r": None}, vwap_trend, needs_volume=True),
    Strategy("trend_pullback", "Trend pullback", trend_pullback.__doc__.split("\n")[0],
             {"fast": 20, "slow": 50, "swing_bars": 5, "target_r": 2.0}, trend_pullback),
    Strategy("breakout_retest", "Breakout and retest", breakout_retest.__doc__.split("\n")[0],
             {"lookback": 20, "retest_window": 10, "exit_bars": 10, "target_r": 2.0}, breakout_retest),
    Strategy("pivot_trendline", "Pivot trendline bounce", pivot_trendline.__doc__.split("\n")[0],
             {"pivot_bars": 5, "tol_atr": 0.5, "max_age": 120, "target_r": 2.0}, pivot_trendline),
    Strategy("ma_trend_following", "Moving-average trend following", ma_trend_following.__doc__.split("\n")[0],
             {"ma": 200, "band_pct": 1.0, "slope_bars": 20, "atr_stop_mult": 3.0, "target_r": None}, ma_trend_following),
)}


def resolve_params(name: str, params: Optional[dict] = None) -> dict:
    """Defaults overlaid with the user's parameters, validated. Unknown names and silly values are errors."""
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}; use one of {sorted(STRATEGIES)}")
    merged = dict(STRATEGIES[name].defaults)
    for key, value in (params or {}).items():
        if key not in merged:
            raise ValueError(f"{name} has no parameter {key!r}; its parameters are {sorted(merged)}")
        merged[key] = value
    for key, value in merged.items():
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value or value < 0:
            raise ValueError(f"parameter {key} must be a non-negative number, got {value!r}")
    if "fast" in merged and "slow" in merged and merged["fast"] >= merged["slow"]:
        raise ValueError("fast must be smaller than slow")
    if "oversold" in merged and not 0 <= merged["oversold"] < merged["overbought"] <= 100:
        raise ValueError("need 0 <= oversold < overbought <= 100")
    for key in ("fast", "slow", "period", "window", "lookback", "retest_window", "exit_bars", "pivot_bars", "ma",
                "slope_bars", "max_age", "swing_bars"):
        if key in merged:
            if merged[key] != int(merged[key]) or merged[key] < 1:
                raise ValueError(f"parameter {key} must be a whole number of bars, at least 1")
            merged[key] = int(merged[key])
    return merged


def generate_signals(name: str, df: pd.DataFrame, params: Optional[dict] = None) -> Signals:
    """Signals for one strategy on one price frame (reset index expected)."""
    p = resolve_params(name, params)
    df = df.reset_index(drop=True)
    return STRATEGIES[name].fn(df, **p)
