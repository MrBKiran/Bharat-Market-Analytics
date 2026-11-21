"""Export processed data from MongoDB to Parquet.

Parquet is the standard columnar format of the big-data world. Spark and pandas both read these files, so
the benchmark compares the engines on IDENTICAL input.

  data/processed/parquet/daily/part-0.parquet                        from adjusted_prices + instruments
  data/processed/parquet/intraday/symbol=S/interval=I/part-0.parquet from intraday_prices (Hive layout:
                                                                      Spark reads the folder names as columns)

Timestamps are written with MICROSECOND precision: Spark cannot read nanosecond Parquet timestamps.
The output folder is derived data (git-ignored) and is rebuilt on every export.
"""
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from pymongo.database import Database

from app.core.config import get_settings

logger = logging.getLogger(__name__)

NUMERIC = ["open", "high", "low", "close", "volume"]


def parquet_dir() -> Path:
    return get_settings().data_dir / "processed" / "parquet"


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is not installed (pip install pyarrow)") from exc
    return pa, pq


def _fresh_dir(path: Path) -> Path:
    """Empty and recreate a derived-data folder, refusing to touch anything outside the data folder."""
    data_root = get_settings().data_dir.resolve()
    if data_root not in path.resolve().parents:
        raise ValueError(f"refusing to clear {path}: it is outside {data_root}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _write(df: pd.DataFrame, path: Path) -> int:
    pa, pq = _require_pyarrow()
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path, compression="snappy",
                   coerce_timestamps="us", allow_truncated_timestamps=True)
    return path.stat().st_size


def folder_size_mb(path: Path) -> float:
    return round(sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) / 1e6, 2)


def export_daily(db: Database, out: Optional[Path] = None) -> dict:
    started = time.perf_counter()
    target = _fresh_dir((out or parquet_dir()) / "daily")
    rows = list(db["adjusted_prices"].find({}, {"_id": 0, "symbol": 1, "date": 1, **{c: 1 for c in NUMERIC}}))
    if not rows:
        raise ValueError("adjusted_prices is empty: run scripts/process_data.py adjust first")
    df = pd.DataFrame(rows)
    inst = {i["symbol"]: i for i in db["instruments"].find({}, {"_id": 0, "symbol": 1, "sector": 1, "instrument_type": 1})}
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df["sector"] = df["symbol"].map(lambda s: inst.get(s, {}).get("sector"))
    df["instrument_type"] = df["symbol"].map(lambda s: inst.get(s, {}).get("instrument_type", "equity"))
    if "volume" not in df:
        df["volume"] = float("nan")
    df[NUMERIC] = df[NUMERIC].astype("float64")
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    size = _write(df, target / "part-0.parquet")
    return {"dataset": "daily", "rows": len(df), "files": 1, "mb": round(size / 1e6, 2), "seconds": round(time.perf_counter() - started, 1)}


def export_intraday(db: Database, out: Optional[Path] = None, *, intervals: Optional[list[str]] = None,
                    symbols: Optional[list[str]] = None, progress=None) -> dict:
    started = time.perf_counter()
    target = _fresh_dir((out or parquet_dir()) / "intraday")
    query: dict = {}
    if intervals:
        query["interval"] = {"$in": intervals}
    if symbols:
        query["symbol"] = {"$in": symbols}
    series = [(c["symbol"], c["interval"]) for c in db["intraday_coverage"].find(query, {"symbol": 1, "interval": 1}).sort(
        [("symbol", 1), ("interval", 1)])]
    rows = size = 0
    for symbol, interval in series:
        cursor = db["intraday_prices"].find({"meta.symbol": symbol, "meta.interval": interval},
                                            {"_id": 0, "ts": 1, **{c: 1 for c in NUMERIC}}).sort("ts", 1)
        df = pd.DataFrame(list(cursor))
        if df.empty:
            continue
        if "volume" not in df:
            df["volume"] = float("nan")
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df[NUMERIC] = df[NUMERIC].astype("float64")
        size += _write(df[["ts", *NUMERIC]], target / f"symbol={symbol}" / f"interval={interval}" / "part-0.parquet")
        rows += len(df)
        if progress:
            progress(f"  {symbol:<18} {interval:<4} {len(df):>9,} rows")
    return {"dataset": "intraday", "rows": rows, "files": len(series), "mb": round(size / 1e6, 2),
            "seconds": round(time.perf_counter() - started, 1)}
