"""Format adapters: turn each source's file layout into one common, normalised table.

Supported formats (auto-detected from the header and file name):
  yfinance      files written by scripts/download_data.py yfinance
  kaggle_index  Kaggle 'debashis74017' index files:  <NAME>_<minute|5minute|15minute|60minute|day>.csv
  kaggle_stock  Kaggle 'rohanrao' per-stock daily files (raw NSE bhavcopy columns)
  generic       any CSV/Excel with date/open/high/low/close/volume (you give symbol and interval)

Normalised columns: ts, symbol, open, high, low, close, volume (+ optional extras).
  * daily bars:    ts = 00:00 UTC of the trading date (a calendar date)
  * intraday bars: ts = the real UTC instant the bar opened
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import pandas as pd

from app.collection.common import read_sidecar

IST = "Asia/Kolkata"
INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "1h"}
DAILY_INTERVALS = {"1d", "1w", "1M"}
ALLOWED_SUFFIXES = {".csv", ".xlsx"}
# Files in a Kaggle folder that must not be imported as price data
# (NIFTY50_all.csv is the union of the per-stock files, so importing both would double-count).
SKIP_FILE_NAMES = {"stock_metadata.csv", "NIFTY50_all.csv"}
FORMATS = ("auto", "yfinance", "kaggle_index", "kaggle_stock", "generic")

KAGGLE_BARS = {"minute": "1m", "5minute": "5m", "15minute": "15m", "60minute": "1h", "day": "1d"}
_KAGGLE_INDEX_RE = re.compile(r"^(?P<name>.+)_(?P<bar>minute|5minute|15minute|60minute|day)\.csv$")
# The Kaggle author strips '&' from file names; NSE's real symbol keeps it.
SYMBOL_ALIASES = {"MM": "M&M"}

_COLUMN_ALIASES = {
    "timestamp": ("datetime", "date", "timestamp", "time"),
    "open": ("open",), "high": ("high",), "low": ("low",), "close": ("close",),
    "volume": ("volume",),
}


@dataclass
class ImportPlan:
    path: Path
    adapter: str
    source: str
    price_basis: str
    symbol: str
    interval: str
    exchange: Optional[str] = None            # None = look it up in `instruments`
    instrument_type: Optional[str] = None
    dataset: Optional[str] = None
    tz: str = IST                             # time zone of timestamps that carry no offset
    timestamps_have_offset: bool = False
    retrieved_at: Optional[datetime] = None
    extras: dict = field(default_factory=dict)          # output name -> source column
    instrument_info: dict = field(default_factory=dict)  # name/isin for auto-registration
    is_synthetic: bool = False

    @property
    def intraday(self) -> bool:
        return self.interval in INTRADAY_INTERVALS


def canonical_symbol(raw: str) -> str:
    """Standard form: upper case, spaces -> underscores, known aliases applied."""
    s = re.sub(r"\s+", "_", str(raw).strip().upper())
    return SYMBOL_ALIASES.get(s, s)


def _read_header(path: Path) -> list[str]:
    if path.suffix.lower() == ".csv":
        return [str(c) for c in pd.read_csv(path, nrows=0).columns]
    return [str(c) for c in pd.read_excel(path, nrows=0).columns]


def detect_format(path: Path, header: list[str]) -> str:
    cols = set(header)
    if {"Date", "Symbol", "Series", "Prev Close"} <= cols:
        return "kaggle_stock"
    if {"Adj Close", "Dividends", "Stock Splits"} <= cols:
        return "yfinance"
    if {"date", "open", "high", "low", "close", "volume"} <= cols and _KAGGLE_INDEX_RE.match(path.name):
        return "kaggle_index"
    return "generic"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _load_stock_metadata(folder: Path) -> dict:
    meta_file = folder / "stock_metadata.csv"
    if not meta_file.exists():
        return {}
    out = {}
    for _, row in pd.read_csv(meta_file).iterrows():
        info = {"name": row.get("Company Name"), "isin": row.get("ISIN Code"),
                "industry_legacy": row.get("Industry")}
        out[canonical_symbol(row.get("Symbol", ""))] = {k: v for k, v in info.items() if isinstance(v, str)}
    return out


def detect_plan(path: Path, overrides: Optional[dict] = None) -> ImportPlan:
    """Work out how to read `path`. `overrides` may set: format, symbol, exchange, interval,
    instrument_type, tz, source, price_basis."""
    path = Path(path)
    ov = {k: v for k, v in (overrides or {}).items() if v is not None}
    header = _read_header(path)
    fmt = ov.get("format", "auto")
    if fmt == "auto":
        fmt = detect_format(path, header)
    if fmt not in FORMATS[1:]:
        raise ValueError(f"Unknown format {fmt!r}; use one of {FORMATS[1:]}")

    side = read_sidecar(path) or {}
    common = dict(
        path=path, adapter=fmt, dataset=side.get("dataset"), tz=ov.get("tz", IST),
        retrieved_at=_parse_iso(side.get("retrieved_at"))
        or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
    )

    if fmt == "yfinance":
        interval = side.get("interval") or (path.parent.name if path.parent.name in
                                            INTRADAY_INTERVALS | DAILY_INTERVALS else None)
        stem_symbol, _, stem_exchange = path.stem.rpartition("_")
        plan = ImportPlan(
            source="yfinance", price_basis="split_adjusted", interval=ov.get("interval", interval),
            symbol=ov.get("symbol") or side.get("symbol") or stem_symbol,
            exchange=ov.get("exchange") or side.get("exchange") or stem_exchange or None,
            # default "equity"; an instrument already in the database overrides this
            instrument_type=ov.get("instrument_type") or side.get("instrument_type") or "equity",
            timestamps_have_offset=True, extras={"adj_close": "Adj Close"}, **common)
    elif fmt == "kaggle_index":
        m = _KAGGLE_INDEX_RE.match(path.name)
        if not m:
            raise ValueError(f"{path.name}: expected <NAME>_<minute|5minute|15minute|60minute|day>.csv")
        plan = ImportPlan(
            source="kaggle", price_basis="index_level", interval=KAGGLE_BARS[m["bar"]],
            symbol=ov.get("symbol") or canonical_symbol(m["name"]), exchange=ov.get("exchange"),
            instrument_type="index", **common)
    elif fmt == "kaggle_stock":
        symbol = ov.get("symbol") or canonical_symbol(path.stem)   # file name, not the historic Symbol column
        plan = ImportPlan(
            source="kaggle", price_basis="raw", interval="1d", symbol=symbol,
            exchange=ov.get("exchange", "NSE"), instrument_type="equity",
            extras={"prev_close": "Prev Close", "turnover": "Turnover", "trades": "Trades",
                    "series": "Series", "vwap": "VWAP", "deliverable_volume": "Deliverable Volume",
                    "pct_deliverable": "%Deliverble"},
            instrument_info=_load_stock_metadata(path.parent).get(symbol, {}), **common)
    else:  # generic
        if not ov.get("symbol"):
            raise ValueError("generic files need a symbol (--symbol)")
        plan = ImportPlan(
            source=ov.get("source", "manual_upload"), price_basis=ov.get("price_basis", "raw"),
            interval=ov.get("interval", "1d"), symbol=canonical_symbol(ov["symbol"]),
            exchange=ov.get("exchange", "NSE"), instrument_type=ov.get("instrument_type", "equity"),
            extras={"adj_close": "Adj Close"}, **common)

    plan.symbol = canonical_symbol(plan.symbol)
    if plan.interval not in INTRADAY_INTERVALS | DAILY_INTERVALS:
        raise ValueError(f"{path.name}: unsupported interval {plan.interval!r}")
    return plan


def _find_column(raw: pd.DataFrame, names: tuple[str, ...]) -> Optional[str]:
    lowered = {str(c).strip().lower(): c for c in raw.columns}
    for name in names:
        if name in lowered:
            return lowered[name]
    return None


def normalise(raw: pd.DataFrame, plan: ImportPlan) -> pd.DataFrame:
    """Raw file rows -> the common table (see module docstring)."""
    ts_col = _find_column(raw, _COLUMN_ALIASES["timestamp"])
    if ts_col is None:
        raise ValueError(f"{plan.path.name}: no date/time column found")

    if plan.timestamps_have_offset:
        moment = pd.to_datetime(raw[ts_col], utc=True, errors="coerce")
    else:
        moment = pd.to_datetime(raw[ts_col], errors="coerce")
    if moment.dt.tz is None:
        local = moment.dt.tz_localize(plan.tz, ambiguous="NaT", nonexistent="NaT")
    else:
        local = moment.dt.tz_convert(plan.tz)

    out = pd.DataFrame(index=raw.index)
    if plan.intraday:
        out["ts"] = local.dt.tz_convert("UTC")
    else:   # a daily bar is a calendar date: keep the local date, stored as 00:00 UTC
        out["ts"] = local.dt.normalize().dt.tz_localize(None).dt.tz_localize("UTC")
    out["symbol"] = plan.symbol

    for name in ("open", "high", "low", "close", "volume"):
        col = _find_column(raw, _COLUMN_ALIASES[name])
        # always float64, so a price written as `100` is stored as a double like `100.5` (consistent types)
        out[name] = pd.to_numeric(raw[col], errors="coerce").astype("float64") if col else float("nan")

    for out_name, source_col in plan.extras.items():
        col = _find_column(raw, (source_col.lower(),))
        if col is None:
            continue
        out[out_name] = raw[col].astype("string").str.strip() if out_name == "series" \
            else pd.to_numeric(raw[col], errors="coerce")
    return out


def read_chunks(plan: ImportPlan, chunk_size: int) -> Iterator[pd.DataFrame]:
    """Yield normalised chunks. CSV is streamed; Excel is read whole, then split."""
    if plan.path.suffix.lower() == ".csv":
        for raw in pd.read_csv(plan.path, chunksize=chunk_size):
            yield normalise(raw, plan)
    else:
        whole = pd.read_excel(plan.path)
        for start in range(0, max(len(whole), 1), chunk_size):
            yield normalise(whole.iloc[start:start + chunk_size], plan)


def expand_paths(paths: list[Path]) -> list[Path]:
    """Files and folders -> the list of importable files (skips metadata and duplicate-union files)."""
    found: list[Path] = []
    for p in paths:
        p = Path(p)
        candidates = sorted(p.rglob("*")) if p.is_dir() else [p]
        for f in candidates:
            if f.is_file() and f.suffix.lower() in ALLOWED_SUFFIXES and f.name not in SKIP_FILE_NAMES:
                found.append(f)
    return found
