"""Download Indian market history from Yahoo Finance (via the `yfinance` package).

Notes
  * yfinance is UNOFFICIAL: not a licensed exchange feed. We label its data
    `delayed` and use it for personal/research purposes.
  * We ask for `auto_adjust=False` plus dividends/splits (`actions=True`).
    IMPORTANT: Yahoo's Open/High/Low/Close are ALREADY SPLIT-ADJUSTED (the price
    is continuous across splits and bonuses). Only dividends are left out of
    OHLC; `Adj Close` includes them. So Phase 6 must NOT apply splits to this
    source again (that would double-adjust).
  * Depth is limited by Yahoo: about 7 days of 1m, 60 days of 5m-30m and
    730 days of 1h bars. Daily/weekly/monthly go back many years.
  * Downloads are polite: a pause between tickers and retries with back-off.
  * Resumable: a finished file (CSV + sidecar) is skipped unless `force=True`.
"""
import logging
import time
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from app.collection.common import raw_dir, read_sidecar, utc_now_iso, write_sidecar

logger = logging.getLogger(__name__)

SOURCE_ID = "yfinance"

# Our interval names -> yfinance names.
INTERVAL_MAP = {"1d": "1d", "1w": "1wk", "1M": "1mo", "1m": "1m", "5m": "5m",
                "15m": "15m", "30m": "30m", "1h": "1h"}
# Longest history Yahoo serves for each interval.
DEFAULT_PERIOD = {"1d": "max", "1w": "max", "1M": "max", "1m": "7d", "5m": "60d",
                  "15m": "60d", "30m": "60d", "1h": "730d"}
REQUIRED_COLUMNS = ("Open", "High", "Low", "Close")


class NoDataError(RuntimeError):
    """Yahoo returned nothing for this ticker (wrong ticker, delisted, or rate-limited)."""


def fetch_history(ticker: str, interval: str, period: Optional[str] = None,
                  start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """One network call to Yahoo Finance. Kept separate so tests can replace it."""
    import yfinance as yf  # imported here so the module loads without network libs ready

    if interval not in INTERVAL_MAP:
        raise ValueError(f"Unsupported interval {interval!r}; use one of {sorted(INTERVAL_MAP)}")
    kwargs: dict = {"interval": INTERVAL_MAP[interval], "auto_adjust": False, "actions": True}
    if start or end:
        kwargs.update(start=start, end=end)
    else:
        kwargs["period"] = period or DEFAULT_PERIOD[interval]
    return yf.Ticker(ticker).history(**kwargs)


def _with_retries(fetcher: Callable, ticker: str, interval: str, *, retries: int, backoff: float,
                  sleep: Callable[[float], None], **kwargs) -> pd.DataFrame:
    last_error: Exception = NoDataError(f"{ticker}: no data returned")
    for attempt in range(1, retries + 1):
        try:
            df = fetcher(ticker, interval, **kwargs)
            if df is None or df.empty:
                raise NoDataError(f"{ticker}: no data returned for interval {interval}")
            missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
            if missing:
                raise ValueError(f"{ticker}: response is missing columns {missing}")
            return df
        except Exception as exc:  # noqa: BLE001 - retry on anything, report the last error
            last_error = exc
            logger.warning("%s attempt %d/%d failed: %s", ticker, attempt, retries, exc)
            if attempt < retries:
                sleep(backoff * (2 ** (attempt - 1)))
    raise last_error


def download_instrument(instrument: dict, interval: str, out_root: Optional[Path] = None, *,
                        fetcher: Callable = fetch_history, force: bool = False, retries: int = 3,
                        backoff: float = 2.0, sleep: Callable[[float], None] = time.sleep,
                        period: Optional[str] = None, start: Optional[str] = None,
                        end: Optional[str] = None) -> dict:
    """Download one instrument to `<out_root>/<interval>/<SYMBOL>_<EXCHANGE>.csv` (+ sidecar)."""
    ticker = instrument.get("yfinance_symbol")
    if not ticker:
        raise ValueError(f"{instrument.get('symbol')}: no yfinance_symbol set")

    out_dir = (out_root or raw_dir("yfinance")) / interval
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{instrument['symbol']}_{instrument['exchange']}.csv"

    if csv_path.exists() and read_sidecar(csv_path) and not force:
        return {"symbol": instrument["symbol"], "ticker": ticker, "status": "skipped",
                "path": str(csv_path)}

    df = _with_retries(fetcher, ticker, interval, retries=retries, backoff=backoff, sleep=sleep,
                       period=period, start=start, end=end)
    df = df.dropna(subset=list(REQUIRED_COLUMNS), how="all")

    tmp = csv_path.with_suffix(".csv.part")      # write-then-rename: no half-written files
    df.to_csv(tmp)
    tmp.replace(csv_path)

    write_sidecar(csv_path, {
        "source": SOURCE_ID,
        "data_class": "delayed",
        "ticker": ticker,
        "symbol": instrument["symbol"],
        "exchange": instrument["exchange"],
        "instrument_type": instrument.get("instrument_type"),
        "interval": interval,
        "period": period or (None if (start or end) else DEFAULT_PERIOD[interval]),
        "start": start,
        "end": end,
        "retrieved_at": utc_now_iso(),
        "rows": int(len(df)),
        "first_ts": str(df.index.min()),
        "last_ts": str(df.index.max()),
        "timezone": str(getattr(df.index, "tz", None)),
        "columns": [str(c) for c in df.columns],
        "price_adjustment": "OHLC split-adjusted by Yahoo (do not re-apply splits); dividends not applied to OHLC; Adj Close includes dividends",
        "is_synthetic": False,
        "license_notes": "Unofficial Yahoo Finance data via yfinance; personal/research use only.",
    })
    return {"symbol": instrument["symbol"], "ticker": ticker, "status": "downloaded",
            "rows": int(len(df)), "path": str(csv_path)}


def download_many(instruments: list[dict], interval: str, out_root: Optional[Path] = None, *,
                  pause: float = 1.0, progress: Callable[[str], None] = print,
                  sleep: Callable[[float], None] = time.sleep, **kwargs) -> list[dict]:
    """Download several instruments; one failure never stops the rest."""
    results = []
    for i, inst in enumerate(instruments, 1):
        label = f"[{i}/{len(instruments)}] {inst['symbol']} ({inst.get('yfinance_symbol')}, {interval})"
        try:
            res = download_instrument(inst, interval, out_root, sleep=sleep, **kwargs)
            progress(f"{label}: {res['status']}" + (f", {res['rows']} rows" if "rows" in res else ""))
        except Exception as exc:  # noqa: BLE001
            res = {"symbol": inst["symbol"], "ticker": inst.get("yfinance_symbol"),
                   "status": "failed", "error": str(exc)}
            progress(f"{label}: FAILED - {exc}")
        results.append(res)
        if res["status"] != "skipped" and i < len(instruments):
            sleep(pause)
    return results


def verify_tickers(instruments: list[dict], *, fetcher: Callable = fetch_history, pause: float = 1.0,
                   sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """Tiny 5-day request per ticker: does Yahoo know this ticker? Writes nothing to disk."""
    report = []
    for inst in instruments:
        ticker = inst.get("yfinance_symbol")
        try:
            df = fetcher(ticker, "1d", period="5d")
            if df is None or df.empty:
                raise NoDataError("no data returned")
            report.append({"symbol": inst["symbol"], "ticker": ticker, "ok": True,
                           "rows": int(len(df)), "last_date": str(df.index.max())[:10]})
        except Exception as exc:  # noqa: BLE001
            report.append({"symbol": inst["symbol"], "ticker": ticker, "ok": False, "error": str(exc)})
        sleep(pause)
    return report


def load_instruments(db, instrument_types: Optional[list[str]] = None,
                     symbols: Optional[list[str]] = None) -> list[dict]:
    """Active instruments that have a Yahoo ticker, from the `instruments` collection."""
    query: dict = {"active": True, "yfinance_symbol": {"$exists": True}}
    if instrument_types:
        query["instrument_type"] = {"$in": instrument_types}
    if symbols:
        query["symbol"] = {"$in": [s.upper() for s in symbols]}
    return list(db["instruments"].find(query, {"_id": 0}).sort([("instrument_type", 1), ("symbol", 1)]))
