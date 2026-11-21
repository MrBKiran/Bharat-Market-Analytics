"""The big-data jobs in pandas: the baseline every other engine is checked against.

Input tables (the same columns as the Parquet files, so every engine reads identical data):
  daily     symbol, date (UTC), open, high, low, close, volume, sector, instrument_type
  intraday  symbol, interval, ts (UTC), open, high, low, close, volume

Conventions shared by all three engines (pandas here, MongoDB aggregation, Spark):
  * return = close / previous close - 1, log return = ln(close / previous close), both within one symbol
  * historical volatility = std (ddof 1) of the last 20 log returns x sqrt(252) x 100, only once 20 exist
  * a "day" of intraday data is the IST calendar date; intraday returns never span two days
"""
import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252
IST = "Asia/Kolkata"
TIE_TOLERANCE = 1e-9          # relative: a close within 1e-9 of its average counts as equal, not above


def returns_and_volatility(daily: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    df = daily.sort_values(["symbol", "date"]).reset_index(drop=True)
    prev = df.groupby("symbol", sort=False)["close"].shift(1)
    df["prev_close"] = prev
    df["ret"] = df["close"] / prev - 1
    df["log_ret"] = np.log(df["close"] / prev)
    df["hist_vol_20"] = (df.groupby("symbol", sort=False)["log_ret"]
                         .transform(lambda s: s.rolling(window, min_periods=window).std(ddof=1))
                         * math.sqrt(TRADING_DAYS) * 100)
    return df


def latest_per_symbol(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return df.sort_values("date").groupby("symbol").tail(1)[["symbol", "date", *columns]].reset_index(drop=True)


def sector_daily_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Equal-weighted average return of the stocks in each sector, per day."""
    stocks = df[(df["instrument_type"] != "index") & df["sector"].notna() & df["ret"].notna()]
    return stocks.groupby(["sector", "date"], as_index=False).agg(ret=("ret", "mean"), n=("ret", "count"))


def summarise_sector_returns(sector_daily: pd.DataFrame) -> dict:
    out = {}
    for sector, g in sector_daily.groupby("sector"):
        r = g.sort_values("date")["ret"]
        out[sector] = {"days": int(len(r)), "first_date": str(g["date"].min().date()), "last_date": str(g["date"].max().date()),
                       "mean_daily_return_pct": round(float(r.mean() * 100), 4),
                       "annualised_vol_pct": round(float(r.std(ddof=1) * math.sqrt(TRADING_DAYS) * 100), 2),
                       "cumulative_return_pct": round(float((1 + r).prod() - 1) * 100, 1),
                       "avg_stocks_per_day": round(float(g["n"].mean()), 1)}
    return out


def correlation_matrix(df: pd.DataFrame, symbols: list[str], start, end) -> tuple[pd.DataFrame, int]:
    """Pearson correlation of daily returns over the days on which EVERY listed symbol has a return."""
    sub = df[df["symbol"].isin(symbols) & (df["date"] >= start) & (df["date"] <= end)]
    wide = sub.pivot(index="date", columns="symbol", values="ret")[symbols].dropna()
    return wide.corr(), int(len(wide))


def market_trend(daily: pd.DataFrame, window: int = 200) -> pd.DataFrame:
    """Per day: how many stocks have a 200-day average, and what share of them close above it."""
    df = daily[daily["instrument_type"] != "index"].sort_values(["symbol", "date"]).copy()
    df["sma"] = df.groupby("symbol", sort=False)["close"].transform(lambda s: s.rolling(window, min_periods=window).mean())
    # "Above" must beat the average by more than rounding noise: when a close EQUALS its average (a tie),
    # two engines can differ in the 15th decimal and disagree (POWERGRID 2011-05-13). Strictly above only.
    valid = df[df["sma"].notna()].assign(above=lambda d: (d["close"] > d["sma"] * (1 + TIE_TOLERANCE)).astype(int))
    out = valid.groupby("date", as_index=False).agg(stocks=("close", "size"), above=("above", "sum"))
    out["pct_above_sma200"] = 100.0 * out["above"] / out["stocks"]
    return out


def intraday_realised_variance(intraday: pd.DataFrame) -> pd.DataFrame:
    """Per symbol, interval and IST day: the sum of squared log returns between consecutive bars (realised variance)."""
    df = intraday.sort_values(["symbol", "interval", "ts"]).reset_index(drop=True)
    df["day"] = df["ts"].dt.tz_convert(IST).dt.date
    prev = df.groupby(["symbol", "interval", "day"], sort=False)["close"].shift(1)
    df["r"] = np.log(df["close"] / prev)
    df["r2"] = df["r"] * df["r"]
    return (df.dropna(subset=["r"]).groupby(["symbol", "interval", "day"], as_index=False)
            .agg(rv=("r2", "sum"), returns=("r", "count")))


def summarise_realised_variance(rv: pd.DataFrame) -> dict:
    """Annualised realised volatility (%) = sqrt(daily realised variance x 252) x 100, averaged per year."""
    df = rv.copy()
    df["year"] = pd.to_datetime(df["day"]).dt.year
    df["ann_vol"] = np.sqrt(df["rv"] * TRADING_DAYS) * 100
    out: dict = {}
    for (symbol, interval, year), g in df.groupby(["symbol", "interval", "year"]):
        out.setdefault(symbol, {}).setdefault(interval, {})[str(int(year))] = round(float(g["ann_vol"].mean()), 2)
    return out


def resample_bars(intraday: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    """Combine bars into larger bars: open of the first, high max, low min, close of the last, volume sum."""
    df = intraday.sort_values(["symbol", "ts"])
    out = (df.groupby(["symbol", pd.Grouper(key="ts", freq=f"{minutes}min")])
           .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                volume=("volume", "sum"), bars=("close", "count")).reset_index())
    return out[out["bars"] > 0].reset_index(drop=True)          # the grouper also creates empty overnight bins


def daily_range_stats(intraday: pd.DataFrame) -> pd.DataFrame:
    """Per symbol, interval and IST day: bar count, highest high, lowest low, total volume, mean close."""
    df = intraday.assign(day=intraday["ts"].dt.tz_convert(IST).dt.date)
    return (df.groupby(["symbol", "interval", "day"], as_index=False)
            .agg(bars=("close", "size"), high=("high", "max"), low=("low", "min"),
                 volume=("volume", "sum"), mean_close=("close", "mean")))
