"""Technical indicators, written directly in pandas (no database, no hidden state).

Every function takes pandas Series and returns a Series of the same length. The first values are NaN
until enough history exists ("warm-up"); nothing is back-filled.

Conventions
  * Wilder smoothing (RSI, ATR) is `ewm(alpha=1/n, adjust=False)`; it converges to the textbook value
    within about 100 bars. EMA is `ewm(span=n, adjust=False)`.
  * Bollinger bands use the population standard deviation (ddof=0), as in the original definition.
  * Historical volatility is the annualised standard deviation of log returns, in PERCENT.
  * VWAP needs traded volume. Indices have none (the sources report 0), so their VWAP stays empty.

`compute_indicators(df, interval)` computes the full set used by the database and the dashboard.
"""
import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
TRADING_DAYS = 252
# Bars in one normal NSE session (09:15-15:30 IST), used to annualise intraday volatility.
BARS_PER_DAY = {"1m": 375, "5m": 75, "15m": 25, "30m": 12.5, "1h": 7}
PERIODS_PER_YEAR = {"1d": TRADING_DAYS, "1w": 52, "1M": 12,
                    **{k: v * TRADING_DAYS for k, v in BARS_PER_DAY.items()}}

# The columns compute_indicators() can produce, in the order they are stored.
INDICATOR_COLUMNS = (
    "sma_20", "sma_50", "sma_200", "ema_20", "ema_50", "ema_200",
    "rsi_14", "macd", "macd_signal", "macd_hist", "atr_14",
    "bb_upper", "bb_mid", "bb_lower", "bb_pct_b", "bb_width",
    "stoch_k", "stoch_d", "vwap_20", "vwap_session", "adr_14", "adr_pct_14",
    "hist_vol_20", "vol_sma_20", "vol_sma_50",
)


def sma(close: pd.Series, n: int) -> pd.Series:
    """Simple moving average."""
    return close.rolling(n, min_periods=n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    """Exponential moving average with span n."""
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder), 0-100. A flat series is 50; only gains is 100."""
    delta = close.diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100 - 100 / (1 + avg_gain / avg_loss)
    out[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    out[(avg_loss == 0) & (avg_gain == 0)] = 50.0
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line (fast EMA - slow EMA), its signal line and the histogram."""
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Average True Range (Wilder), in price points."""
    tr = true_range(high, low, close)
    tr.iloc[0] = np.nan          # the first bar has no previous close, so no true range
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """Bollinger bands, %B (position inside the bands, 0 = lower, 1 = upper) and band width (% of the middle)."""
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * std, mid - k * std
    return pd.DataFrame({"bb_upper": upper, "bb_mid": mid, "bb_lower": lower,
                         "bb_pct_b": (close - lower) / (upper - lower).replace(0, np.nan),
                         "bb_width": (upper - lower) / mid * 100})


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k: int = 14, d: int = 3) -> pd.DataFrame:
    """Stochastic oscillator: %K (where the close sits in the last k bars' range, 0-100) and %D (its d-bar mean)."""
    lowest = low.rolling(k, min_periods=k).min()
    highest = high.rolling(k, min_periods=k).max()
    pct_k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    return pd.DataFrame({"stoch_k": pct_k, "stoch_d": pct_k.rolling(d, min_periods=d).mean()})


def vwap_rolling(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, n: int = 20) -> pd.Series:
    """Volume-weighted average of the typical price (H+L+C)/3 over the last n bars."""
    typical = (high + low + close) / 3
    traded = (typical * volume).rolling(n, min_periods=n).sum()
    total = volume.rolling(n, min_periods=n).sum()
    return traded / total.replace(0, np.nan)


def vwap_session(ts: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Intraday VWAP that restarts every trading day (IST date)."""
    day = ts.dt.tz_convert(IST).dt.date
    typical = (high + low + close) / 3
    cum_value = (typical * volume).groupby(day).cumsum()
    cum_volume = volume.groupby(day).cumsum()
    return cum_value / cum_volume.replace(0, np.nan)


def average_range(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    """Average (high - low) per bar over n bars, in points and as % of the close."""
    spread = high - low
    return pd.DataFrame({"adr_14": spread.rolling(n, min_periods=n).mean(),
                         "adr_pct_14": (spread / close * 100).rolling(n, min_periods=n).mean()})


def historical_volatility(close: pd.Series, n: int = 20, periods_per_year: float = TRADING_DAYS) -> pd.Series:
    """Annualised standard deviation of log returns over n bars, in percent."""
    log_return = np.log(close / close.shift(1))
    return log_return.rolling(n, min_periods=n).std(ddof=1) * np.sqrt(periods_per_year) * 100


def compute_indicators(df: pd.DataFrame, interval: str = "1d") -> pd.DataFrame:
    """The full indicator set for one instrument.

    `df` needs columns ts (tz-aware), open, high, low, close, volume, sorted by ts.
    Returns a frame with `ts` plus every column in INDICATOR_COLUMNS that applies (others are all-NaN).
    """
    if interval not in PERIODS_PER_YEAR:
        raise ValueError(f"Unsupported interval {interval!r}")
    close, high, low = df["close"], df["high"], df["low"]
    volume = df["volume"].fillna(0) if "volume" in df else pd.Series(0.0, index=df.index)
    has_volume = bool((volume > 0).any())

    out = pd.DataFrame({"ts": df["ts"]})
    for n in (20, 50, 200):
        out[f"sma_{n}"] = sma(close, n)
        out[f"ema_{n}"] = ema(close, n)
    out["rsi_14"] = rsi(close, 14)
    out = out.join(macd(close))
    out["atr_14"] = atr(high, low, close, 14)
    out = out.join(bollinger(close, 20, 2.0))
    out = out.join(stochastic(high, low, close, 14, 3))
    out["vwap_20"] = vwap_rolling(high, low, close, volume, 20) if has_volume and interval == "1d" else np.nan
    out["vwap_session"] = (vwap_session(df["ts"], high, low, close, volume)
                           if has_volume and interval not in ("1d", "1w", "1M") else np.nan)
    out = out.join(average_range(high, low, close, 14))
    out["hist_vol_20"] = historical_volatility(close, 20, PERIODS_PER_YEAR[interval])
    out["vol_sma_20"] = sma(volume, 20) if has_volume else np.nan
    out["vol_sma_50"] = sma(volume, 50) if has_volume else np.nan
    return out[["ts", *INDICATOR_COLUMNS]]
