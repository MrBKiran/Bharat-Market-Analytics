"""Synthetic benchmark data for the Phase 14 scale tests (100,000 to 10,000,000 bars).

Real market data is only 5.4 million intraday bars, so the larger sizes need generated data. It is:

  * LABELLED   every document carries `meta.is_synthetic = true` and `meta.source = "synthetic_benchmark"`, the
               symbols are SYN0001, SYN0002 ... (no real company), and it is stored in its own database
               (settings.bench_db_name), never in the real collections.
  * REALISTIC IN SHAPE  1-minute bars of the Indian session (09:15 to 15:30 IST = 375 bars a day, weekdays only,
               no holidays), prices on the 0.05 tick, a random walk of about 12% annual volatility, volume
               log-normal. Realistic shape matters because grouping by day, resampling and range filters do
               work that depends on it. The PRICES carry no information: do not draw market conclusions from them.
  * REPRODUCIBLE  the same (rows, symbols, seed) always gives the same table, so every engine and every rerun
               sees identical input.
  * VALID      high >= max(open, close), low <= min(open, close), every price above zero.
"""
import numpy as np
import pandas as pd

SYNTHETIC_SOURCE = "synthetic_benchmark"
DEFAULT_SYMBOLS = 50
DEFAULT_SEED = 14
FIRST_DAY = "2023-01-02"            # a Monday
SESSION_MINUTES = 375               # 09:15 to 15:30 IST
SESSION_OPEN_UTC_MINUTE = 225       # 03:45 UTC = 09:15 IST
TICK = 0.05
MINUTE_VOLATILITY = 0.0004          # standard deviation of one minute's log return
NUMERIC = ["open", "high", "low", "close", "volume"]


def symbol_names(count: int) -> list[str]:
    """SYN0001, SYN0002 ... (valid canonical symbols: upper case letters and digits)."""
    return [f"SYN{i + 1:04d}" for i in range(count)]


def rows_per_symbol(total: int, symbols: int) -> list[int]:
    """Split `total` rows over `symbols` series as evenly as possible (the first few get one extra)."""
    if total < symbols or symbols < 1:
        raise ValueError(f"need at least one row per symbol: {total} rows for {symbols} symbols")
    base, extra = divmod(total, symbols)
    return [base + (1 if i < extra else 0) for i in range(symbols)]


def _tick(values: np.ndarray) -> np.ndarray:
    return np.round(np.round(values / TICK) * TICK, 2)


def generate_symbol(index: int, rows: int, symbol: str, seed: int = DEFAULT_SEED, first_day: str = FIRST_DAY) -> pd.DataFrame:
    """One symbol's 1-minute bars: columns symbol, interval, ts (UTC), open, high, low, close, volume."""
    rng = np.random.default_rng([seed, index])
    position = np.arange(rows)
    session_day, minute = position // SESSION_MINUTES, position % SESSION_MINUTES
    days = np.busday_offset(np.datetime64(first_day), session_day, roll="forward")
    stamps = (days.astype("datetime64[m]") + np.timedelta64(SESSION_OPEN_UTC_MINUTE, "m")
              + minute.astype("timedelta64[m]")).astype("datetime64[us]")

    start = rng.uniform(50.0, 5000.0)
    close = np.maximum(_tick(start * np.exp(np.cumsum(rng.normal(0.0, MINUTE_VOLATILITY, rows)))), TICK)
    open_ = np.empty(rows)
    open_[0] = max(float(_tick(np.array([start]))[0]), TICK)
    open_[1:] = close[:-1]
    wick_up = _tick(np.abs(rng.normal(0.0, 0.0003, rows)) * close)
    wick_down = _tick(np.abs(rng.normal(0.0, 0.0003, rows)) * close)
    high = np.round(np.maximum(open_, close) + wick_up, 2)
    low = np.round(np.maximum(np.minimum(open_, close) - wick_down, TICK), 2)
    volume = np.round(rng.lognormal(7.0, 1.0, rows))
    return pd.DataFrame({"symbol": symbol, "interval": "1m", "ts": pd.DatetimeIndex(stamps).tz_localize("UTC"),
                         "open": open_.round(2), "high": high, "low": low, "close": close, "volume": volume})


def generate(total_rows: int, symbols: int = DEFAULT_SYMBOLS, seed: int = DEFAULT_SEED, first_day: str = FIRST_DAY) -> list[pd.DataFrame]:
    """Exactly `total_rows` bars spread over `symbols` series, one DataFrame per symbol."""
    names = symbol_names(symbols)
    return [generate_symbol(i, n, names[i], seed, first_day) for i, n in enumerate(rows_per_symbol(total_rows, symbols))]


def meta_for(symbol: str) -> dict:
    """The `meta` sub-document of a time-series bar: the same fields as the real `intraday_prices`, flagged synthetic."""
    return {"symbol": symbol, "exchange": "NSE", "interval": "1m", "instrument_type": "equity", "source": SYNTHETIC_SOURCE,
            "is_synthetic": True, "price_basis": "split_adjusted"}


def documents(frame: pd.DataFrame) -> list[dict]:
    """MongoDB time-series documents for one symbol's frame (what the importer builds before inserting)."""
    meta = meta_for(str(frame["symbol"].iloc[0]))
    stamps = pd.DatetimeIndex(frame["ts"]).to_pydatetime()
    columns = [frame[c].to_numpy().tolist() for c in NUMERIC]
    return [{"ts": t, "meta": meta, "open": o, "high": h, "low": lo, "close": c, "volume": v}
            for t, o, h, lo, c, v in zip(stamps, *columns)]
