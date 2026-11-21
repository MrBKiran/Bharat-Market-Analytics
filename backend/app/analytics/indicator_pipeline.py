"""Compute indicators for stored prices and save them in the `indicators` time-series collection.

  daily   (1d)           from `adjusted_prices` (split-adjusted, one clean series per instrument)
  intraday (5m, 15m ...) from `intraday_prices`

Document shape:  {ts, meta: {symbol, exchange, interval, input}, <indicator fields...>}
Only indicators that have a value are stored (warm-up values are omitted), rounded to 6 decimals.

Re-running replaces a series exactly (delete by meta, then insert): the time-series collection has no
unique index, so replacing is how we stay free of duplicates.
"""
import logging
import time
from typing import Callable, Optional

import pandas as pd
from pymongo.database import Database

from app.analytics.indicators import INDICATOR_COLUMNS, compute_indicators

logger = logging.getLogger(__name__)

BATCH_SIZE = 50_000
BAR_FIELDS = {"_id": 0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}


def load_series(db: Database, symbol: str, exchange: str, interval: str) -> tuple[pd.DataFrame, str]:
    """Price bars for one instrument and interval, sorted by time. Returns (frame, name of the input collection)."""
    if interval == "1d":
        cursor = db["adjusted_prices"].find({"symbol": symbol, "exchange": exchange, "interval": "1d"},
                                            {**BAR_FIELDS, "date": 1}).sort("date", 1)
        df, source = pd.DataFrame(list(cursor)).rename(columns={"date": "ts"}), "adjusted_prices"
    else:
        cursor = db["intraday_prices"].find({"meta.symbol": symbol, "meta.exchange": exchange,
                                             "meta.interval": interval}, {**BAR_FIELDS, "ts": 1}).sort("ts", 1)
        df, source = pd.DataFrame(list(cursor)), "intraday_prices"
    if df.empty:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]), source
    if "volume" not in df:
        df["volume"] = float("nan")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").reset_index(drop=True), source


def to_documents(indicator_frame: pd.DataFrame, meta: dict) -> list[dict]:
    """One document per bar that has at least one indicator value."""
    stamps = [t.to_pydatetime() for t in indicator_frame["ts"]]
    columns = {c: indicator_frame[c].tolist() for c in INDICATOR_COLUMNS if c in indicator_frame}
    docs = []
    for i, ts in enumerate(stamps):
        doc = {"ts": ts, "meta": meta}
        for name, values in columns.items():
            v = values[i]
            if v == v:                       # skips NaN
                doc[name] = round(float(v), 6)
        if len(doc) > 2:
            docs.append(doc)
    return docs


def compute_and_store(db: Database, symbol: str, exchange: str, interval: str, *, dry_run: bool = False) -> dict:
    started = time.perf_counter()
    df, source = load_series(db, symbol, exchange, interval)
    if df.empty:
        return {"symbol": symbol, "interval": interval, "status": "no_data"}
    docs = to_documents(compute_indicators(df, interval),
                        {"symbol": symbol, "exchange": exchange, "interval": interval, "input": source})
    if not dry_run:
        db["indicators"].delete_many({"meta.symbol": symbol, "meta.exchange": exchange, "meta.interval": interval})
        for start in range(0, len(docs), BATCH_SIZE):
            db["indicators"].insert_many(docs[start:start + BATCH_SIZE], ordered=False)
    return {"symbol": symbol, "interval": interval, "status": "dry_run" if dry_run else "stored",
            "bars": len(df), "documents": len(docs), "seconds": round(time.perf_counter() - started, 2)}


def run_indicators(db: Database, intervals: list[str], *, symbols: Optional[list[str]] = None, dry_run: bool = False,
                   progress: Optional[Callable[[str], None]] = None) -> list[dict]:
    """Indicators for every active equity, ETF and index (or just `symbols`) at each interval."""
    query: dict = {"active": True, "instrument_type": {"$in": ["equity", "etf", "index"]}}
    if symbols:
        query["symbol"] = {"$in": [s.upper() for s in symbols]}
    results = []
    for inst in db["instruments"].find(query, {"_id": 0, "symbol": 1, "exchange": 1}).sort("symbol", 1):
        for interval in intervals:
            if interval != "1d" and not db["intraday_coverage"].find_one(
                    {"symbol": inst["symbol"], "exchange": inst["exchange"], "interval": interval}):
                continue                                           # nothing stored at this interval
            res = compute_and_store(db, inst["symbol"], inst["exchange"], interval, dry_run=dry_run)
            if res["status"] != "no_data":
                results.append(res)
                if progress:
                    progress(f"  {res['symbol']:<18} {interval:<4} {res['bars']:>9,} bars -> {res['documents']:>9,} docs  {res['seconds']}s")
    return results
