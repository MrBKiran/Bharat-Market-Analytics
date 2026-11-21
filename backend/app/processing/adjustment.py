"""Build `adjusted_prices`: one consistent, split-adjusted daily series per instrument.

The raw collections (stock_prices, market_indices) are never changed. This step READS them and
writes a separate processed collection that can be rebuilt at any time.

Which raw series feeds each instrument:
  stocks   Yahoo if we have it (already adjusted by Yahoo, complete to today), otherwise the raw NSE
           data from Kaggle with our own split factors (corporate_actions) applied.
           One source per stock: mixing sources would mix adjustment bases inside one series.
  indices  nothing to adjust; Yahoo first, older dates filled from Kaggle (same basis: index level).

Missing data is never invented. Rows that are not real trading are dropped and COUNTED: placeholder
rows before a stock's first trade (flat, zero volume), phantom rows (zero volume with a >30% price
jump), and rows before a confirmed listing date (LISTING_DATES). Other suspicious rows are FLAGGED
(quality_flags) and kept:
  return_outlier  one-day move above 30% (stocks) / 15% (indices): a crash, a demerger or bad data
  gap_before      more than 7 calendar days since the previous row
  no_trade        open = high = low = close and zero volume (stocks only)
"""
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from pymongo.database import Database

from app.processing.corporate_actions import load_raw_bars, split_events

logger = logging.getLogger(__name__)

STOCK_PRIORITY = ("yfinance", "kaggle")
INDEX_PRIORITY = ("yfinance", "kaggle")
OUTLIER_LIMIT = {"index": 0.15, "stock": 0.30}
GAP_FLAG_DAYS = 7
BATCH_SIZE = 50_000
PRICE_COLUMNS = ("open", "high", "low", "close")


def cumulative_factors(dates: pd.Series, events: list[tuple[datetime, float]]) -> np.ndarray:
    """Price multiplier for each date: the product of the factors of all events AFTER that date.

    The ex-date itself already trades at the new price, so it is not adjusted by its own event.
    """
    if not events:
        return np.ones(len(dates))
    events = sorted(events)
    ex = pd.DatetimeIndex([pd.Timestamp(e[0]).tz_convert("UTC") if pd.Timestamp(e[0]).tzinfo
                           else pd.Timestamp(e[0]).tz_localize("UTC") for e in events])
    factors = np.array([e[1] for e in events], dtype=float)
    suffix = np.append(np.cumprod(factors[::-1])[::-1], 1.0)        # suffix[k] = product of factors[k:]
    how_many_started = ex.searchsorted(pd.DatetimeIndex(dates), side="right")   # events with ex_date <= date
    return suffix[how_many_started]


def adjust_bars(bars: pd.DataFrame, events: list[tuple[datetime, float]]) -> pd.DataFrame:
    """Apply split factors to raw bars. Volume moves the opposite way (a 2-for-1 doubles old volume)."""
    df = bars.copy()
    factors = cumulative_factors(df["date"], events)
    df["raw_close"] = df["close"]
    for col in PRICE_COLUMNS:
        df[col] = df[col] * factors
    df["volume"] = df["volume"] / factors
    df["adj_factor"] = factors
    return df


def compute_flags(df: pd.DataFrame, kind: str) -> list[list[str]]:
    """Quality flags per row (an empty list = clean). `df` must be sorted by date."""
    move = (df["close"] / df["close"].shift(1) - 1).abs()
    gap = (df["date"] - df["date"].shift(1)).dt.days
    outlier = (move > OUTLIER_LIMIT[kind]).to_numpy()
    gap_before = (gap > GAP_FLAG_DAYS).to_numpy()
    if kind == "stock":
        flat = (df["open"] == df["high"]) & (df["high"] == df["low"]) & (df["low"] == df["close"])
        no_trade = (flat & (df["volume"].fillna(0) == 0)).to_numpy()
    else:
        no_trade = np.zeros(len(df), dtype=bool)
    return [[name for name, on in (("return_outlier", o), ("gap_before", g), ("no_trade", n)) if on]
            for o, g, n in zip(outlier, gap_before, no_trade)]


def stock_series(db: Database, inst: dict) -> tuple[Optional[pd.DataFrame], Optional[str], int]:
    """(bars, method, number of split events applied) for one stock."""
    for source in STOCK_PRIORITY:
        bars = load_raw_bars(db, inst["symbol"], inst["exchange"], source)
        if bars.empty:
            continue
        bars["source"] = source
        if source == "yfinance":                      # Yahoo already adjusted these prices
            bars["adj_factor"] = 1.0
            return bars, "provider_adjusted", 0
        events = split_events(db, inst["symbol"], inst["exchange"])
        return adjust_bars(bars, events), "derived_adjustment", len(events)
    return None, None, 0


def index_series(db: Database, inst: dict) -> Optional[pd.DataFrame]:
    frames = []
    for source in INDEX_PRIORITY:
        bars = load_raw_bars(db, inst["symbol"], inst["exchange"], source, "market_indices")
        if not bars.empty:
            bars["source"] = source
            frames.append(bars)
    if not frames:
        return None
    df = pd.concat(frames).drop_duplicates("date", keep="first").sort_values("date").reset_index(drop=True)
    df["adj_factor"] = 1.0
    return df


def trim_placeholder_rows(df: pd.DataFrame, kind: str) -> tuple[pd.DataFrame, int]:
    """Drop the run of flat, zero-volume rows at the START of a stock's series.

    A provider may back-fill days before a company listed with flat zero-volume rows. They are not
    trading, so they must not enter the processed dataset. (None exist in the data imported so far;
    this guards future imports.) Flat zero-volume days in the MIDDLE of a series are no-trade days
    (a provider repeats the previous close): kept and flagged instead.
    """
    if kind != "stock" or df.empty:
        return df, 0
    flat = (df["open"] == df["high"]) & (df["high"] == df["low"]) & (df["low"] == df["close"])
    placeholder = (flat & (df["volume"].fillna(0) == 0)).to_numpy()
    first_real = len(df) if placeholder.all() else int((~placeholder).argmax())
    return df.iloc[first_real:].reset_index(drop=True), first_real


def drop_phantom_rows(df: pd.DataFrame, kind: str) -> tuple[pd.DataFrame, int]:
    """Drop stock rows with ZERO volume whose price jumped more than 30% from the previous row.

    These are phantom rows: a provider invented a bar for a day with no trading (RELIANCE and TCS on
    2005-07-28 at Yahoo: volume 0, price 4x and 2x their neighbours, on a day NSE has no row at all).
    A real move comes with volume; without it the price is not a trade. Real big moves are kept.
    """
    if kind != "stock" or df.empty:
        return df, 0
    move = (df["close"] / df["close"].shift(1) - 1).abs()
    phantom = ((df["volume"].fillna(0) == 0) & (move > OUTLIER_LIMIT["stock"])).to_numpy()
    return (df[~phantom].reset_index(drop=True), int(phantom.sum())) if phantom.any() else (df, 0)


# Listing dates confirmed by the NSE record itself (the NSE series begins on that very day), for stocks
# where a provider has older rows. Yahoo has TCS from 2002-08-12, two years before its IPO, with tiny
# volumes: not exchange trading. Add a symbol here only with evidence like this.
LISTING_DATES = {"TCS": datetime(2004, 8, 25, tzinfo=timezone.utc)}


def drop_pre_listing_rows(df: pd.DataFrame, symbol: str) -> tuple[pd.DataFrame, int]:
    listing = LISTING_DATES.get(symbol)
    if listing is None or df.empty:
        return df, 0
    keep = df["date"] >= pd.Timestamp(listing)
    return (df[keep].reset_index(drop=True), int((~keep).sum())) if (~keep).any() else (df, 0)


def to_documents(df: pd.DataFrame, inst: dict, method: str, flags: list[list[str]], job_id: str,
                 processed_at: datetime) -> list[dict]:
    dates = [t.to_pydatetime() for t in df["date"]]
    cols = {c: df[c].tolist() for c in (*PRICE_COLUMNS, "volume", "adj_factor", "source")}
    raw_close = df["raw_close"].tolist() if "raw_close" in df and method == "derived_adjustment" else None
    docs = []
    for i, date in enumerate(dates):
        doc = {"symbol": inst["symbol"], "exchange": inst["exchange"], "interval": "1d", "date": date,
               "open": cols["open"][i], "high": cols["high"][i], "low": cols["low"][i], "close": cols["close"][i],
               "adj_factor": float(cols["adj_factor"][i]), "source": cols["source"][i], "method": method,
               "processed_at": processed_at, "job_id": job_id}
        volume = cols["volume"][i]
        if volume == volume:                                  # not NaN
            doc["volume"] = int(round(volume))
        if raw_close is not None:
            doc["raw_close"] = raw_close[i]
        if flags[i]:
            doc["quality_flags"] = flags[i]
        docs.append(doc)
    return docs


def build_adjusted_prices(db: Database, *, job_id: str = "adhoc", dry_run: bool = False) -> dict:
    """Rebuild `adjusted_prices` for every active equity, ETF and index. Safe to re-run."""
    now = datetime.now(timezone.utc)
    summary: dict = {"instruments": 0, "skipped": [], "rows": 0, "placeholder_rows_dropped": 0,
                     "phantom_rows_dropped": 0, "pre_listing_rows_dropped": 0,
                     "by_method": {}, "flags": {}, "series": []}
    flag_counts: Counter = Counter()
    method_rows: Counter = Counter()

    for inst in db["instruments"].find({"active": True, "instrument_type": {"$in": ["equity", "etf", "index"]}},
                                       {"_id": 0}).sort("symbol", 1):
        summary["instruments"] += 1
        kind = "index" if inst["instrument_type"] == "index" else "stock"
        if kind == "index":
            df, method, events_applied = index_series(db, inst), "index_level", 0
        else:
            df, method, events_applied = stock_series(db, inst)
        if df is None or df.empty:
            summary["skipped"].append(inst["symbol"])
            continue

        df = df.sort_values("date").reset_index(drop=True)
        df, trimmed = trim_placeholder_rows(df, kind)
        df, phantom = drop_phantom_rows(df, kind)
        df, pre_listing = (drop_pre_listing_rows(df, inst["symbol"]) if kind == "stock" else (df, 0))
        summary["placeholder_rows_dropped"] += trimmed
        summary["phantom_rows_dropped"] += phantom
        summary["pre_listing_rows_dropped"] += pre_listing
        if df.empty:
            summary["skipped"].append(inst["symbol"])
            continue
        flags = compute_flags(df, kind)
        docs = to_documents(df, inst, method, flags, job_id, now)
        if not dry_run:
            db["adjusted_prices"].delete_many({"symbol": inst["symbol"], "exchange": inst["exchange"]})
            for start in range(0, len(docs), BATCH_SIZE):
                db["adjusted_prices"].insert_many(docs[start:start + BATCH_SIZE], ordered=False)

        own_flags = Counter(f for row in flags for f in row)
        flag_counts.update(own_flags)
        method_rows[method] += len(docs)
        summary["rows"] += len(docs)
        summary["series"].append({
            "symbol": inst["symbol"], "rows": len(docs), "method": method, "source": sorted(set(df["source"])),
            "first": df["date"].min().date().isoformat(), "last": df["date"].max().date().isoformat(),
            "splits_applied": events_applied, "placeholder_rows_dropped": trimmed, "phantom_rows_dropped": phantom,
            "pre_listing_rows_dropped": pre_listing, "flags": dict(own_flags)})

    summary["by_method"], summary["flags"] = dict(method_rows), dict(flag_counts)
    return summary
