"""Corporate actions: splits and dividends.

Two sources, kept apart and cross-checked:

  yfinance  the `Dividends` and `Stock Splits` columns of the Yahoo files (confidence "provider").
  inferred  RAW prices show a split or bonus as a one-day drop to an exact fraction of the previous
            close (0.5, 0.333, 0.2 ...), usually with a jump in volume. We look for those jumps
            (confidence "inferred"). This is how we get splits for stocks Yahoo does not cover, and it
            is validated against Yahoo for the stocks that have both.

An inferred event within 3 days of a Yahoo split is NOT stored twice: the Yahoo document gets
`confirmed_by: ["derived_nse_prices"]` instead.

`factor` is the multiplier for prices BEFORE the ex-date (2-for-1 -> 0.5; reverse split -> 2.0).
Only splits and bonus issues are inferred. Demergers, rights issues and special dividends do not look
like clean fractions; they are flagged as return outliers by the adjustment step, not guessed at.
"""
import logging
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Optional

import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from app.collection.common import read_sidecar

logger = logging.getLogger(__name__)

SOURCE_YAHOO = "yfinance"
SOURCE_INFERRED = "derived_nse_prices"
CONFIRM_WINDOW_DAYS = 3
MAX_GAP_DAYS = 7          # a jump after a longer data gap cannot be told apart from missing data

# price factor -> allowed relative error of the observed close-to-close ratio
NICE_FACTORS = {
    # 1/10 is wide on purpose: a one-day fall to 7-13% of the price is never an ordinary move, and on the
    # day of a 10-for-1 split the market move can be large (ITC 2005 closed at 0.073 of the previous day).
    1 / 2: 0.08, 1 / 3: 0.08, 1 / 4: 0.08, 1 / 5: 0.08, 1 / 10: 0.30, 2 / 3: 0.05,   # splits and bonus issues
    2.0: 0.04, 3.0: 0.04, 5.0: 0.04, 10.0: 0.04,                                       # reverse splits
}


def _utc_date(iso_text: str) -> datetime:
    """'2014-07-28 ...' (an IST wall date) -> 00:00 UTC of that calendar date."""
    y, m, d = (int(x) for x in iso_text[:10].split("-"))
    return datetime(y, m, d, tzinfo=timezone.utc)


def describe_factor(factor: float) -> str:
    if factor < 1:
        ratio = Fraction(1 / factor).limit_denominator(4)
        return f"{ratio.numerator}-for-{ratio.denominator} split or bonus (price factor {factor:.4g})"
    ratio = Fraction(factor).limit_denominator(4)
    return f"1-for-{ratio.numerator} reverse split (price factor {factor:.4g})"


# ----------------------------------------------------------------------------- inference
def infer_split_events(bars: pd.DataFrame) -> list[dict]:
    """Find split-like jumps in RAW bars (columns: date, close, volume).

    Returns [{ex_date, factor, observed_ratio, volume_ratio}], oldest first.
    """
    if len(bars) < 2:
        return []
    df = bars.sort_values("date").reset_index(drop=True)
    ratio = df["close"] / df["close"].shift(1)
    gap = (df["date"] - df["date"].shift(1)).dt.days
    volume_ratio = (df["volume"].fillna(0) + 1.0) / (df["volume"].fillna(0).shift(1) + 1.0)

    best_factor = pd.Series(float("nan"), index=df.index)
    best_error = pd.Series(float("inf"), index=df.index)
    for factor, tolerance in NICE_FACTORS.items():
        error = (ratio / factor - 1).abs()
        hit = (error <= tolerance) & (gap <= MAX_GAP_DAYS) & (error < best_error)
        best_factor[hit] = factor
        best_error[hit] = error[hit]

    events = []
    for i in df.index[best_factor.notna()]:
        ex = pd.Timestamp(df.loc[i, "date"])
        ex = ex.tz_localize("UTC") if ex.tzinfo is None else ex.tz_convert("UTC")
        events.append({"ex_date": ex.to_pydatetime(), "factor": float(best_factor[i]),
                       "observed_ratio": float(ratio[i]), "volume_ratio": float(volume_ratio[i])})
    return events


# ----------------------------------------------------------------------------- Yahoo
def yahoo_actions(csv_path: Path) -> list[dict]:
    """Split and dividend documents from one Yahoo CSV (indices have none)."""
    csv_path = Path(csv_path)
    meta = read_sidecar(csv_path) or {}
    stem_symbol, _, stem_exchange = csv_path.stem.rpartition("_")
    symbol, exchange = meta.get("symbol") or stem_symbol, meta.get("exchange") or stem_exchange
    if meta.get("instrument_type") == "index":
        return []
    try:
        retrieved_at = datetime.fromisoformat(meta["retrieved_at"])
    except (KeyError, ValueError):
        retrieved_at = datetime.fromtimestamp(csv_path.stat().st_mtime, tz=timezone.utc)

    df = pd.read_csv(csv_path, usecols=lambda c: c in ("Date", "Dividends", "Stock Splits"))
    common = {"symbol": symbol, "exchange": exchange, "source": SOURCE_YAHOO, "confidence": "provider",
              "retrieved_at": retrieved_at}
    docs = []
    if "Stock Splits" in df:
        for date_text, ratio in df.loc[df["Stock Splits"] > 0, ["Date", "Stock Splits"]].itertuples(index=False):
            factor = 1.0 / float(ratio)
            docs.append({**common, "action_type": "split", "ex_date": _utc_date(date_text), "factor": factor,
                         "details": f"Yahoo 'Stock Splits' = {float(ratio):g}: {describe_factor(factor)}"})
    if "Dividends" in df:
        for date_text, amount in df.loc[df["Dividends"] > 0, ["Date", "Dividends"]].itertuples(index=False):
            docs.append({**common, "action_type": "dividend", "ex_date": _utc_date(date_text),
                         "amount": float(amount),
                         "details": "cash dividend per share (INR), split-adjusted amount as listed by Yahoo"})
    return docs


def store_yahoo_actions(db: Database, docs: list[dict]) -> dict:
    if not docs:
        return {"inserted": 0, "updated": 0}
    ops = []
    for d in docs:
        key = {k: d[k] for k in ("symbol", "exchange", "action_type", "ex_date")}
        ops.append(UpdateOne(key, {"$set": {k: v for k, v in d.items() if k not in key}}, upsert=True))
    res = db["corporate_actions"].bulk_write(ops, ordered=False)
    return {"inserted": res.upserted_count, "updated": res.matched_count}


# ----------------------------------------------------------------------------- inferred
def store_inferred_actions(db: Database, symbol: str, exchange: str, events: list[dict],
                           retrieved_at: Optional[datetime] = None) -> dict:
    """Replace this symbol's inferred splits (so re-running is exact), confirming Yahoo ones where they agree."""
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    coll = db["corporate_actions"]
    coll.delete_many({"symbol": symbol, "exchange": exchange, "source": SOURCE_INFERRED})
    coll.update_many({"symbol": symbol, "exchange": exchange, "action_type": "split"},
                     {"$pull": {"confirmed_by": SOURCE_INFERRED}})
    yahoo = list(coll.find({"symbol": symbol, "exchange": exchange, "action_type": "split",
                            "source": SOURCE_YAHOO}, {"ex_date": 1}))

    confirmed = new = 0
    for ev in events:
        nearby = [y for y in yahoo if abs((y["ex_date"] - ev["ex_date"]).days) <= CONFIRM_WINDOW_DAYS]
        match = min(nearby, key=lambda y: abs(y["ex_date"] - ev["ex_date"])) if nearby else None   # the NEAREST one
        if match:
            coll.update_one({"_id": match["_id"]}, {"$addToSet": {"confirmed_by": SOURCE_INFERRED}})
            confirmed += 1
            continue
        coll.insert_one({
            "symbol": symbol, "exchange": exchange, "action_type": "split", "ex_date": ev["ex_date"],
            "factor": ev["factor"], "source": SOURCE_INFERRED, "confidence": "inferred",
            "observed_ratio": ev["observed_ratio"], "volume_ratio": ev["volume_ratio"],
            "details": f"inferred from a one-day price jump: {describe_factor(ev['factor'])}",
            "retrieved_at": retrieved_at})
        new += 1
    return {"events": len(events), "confirmed_yahoo": confirmed, "new_inferred": new}


def split_events(db: Database, symbol: str, exchange: str, *, inferred_only: bool = False) -> list[tuple[datetime, float]]:
    """(ex_date, factor) for a symbol's splits. `inferred_only` = what the price-jump detector alone found."""
    query: dict = {"symbol": symbol, "exchange": exchange, "action_type": "split", "factor": {"$exists": True}}
    if inferred_only:
        query["$or"] = [{"source": SOURCE_INFERRED}, {"confirmed_by": SOURCE_INFERRED}]
    return [(d["ex_date"], d["factor"]) for d in db["corporate_actions"].find(query).sort("ex_date", 1)]


def load_raw_bars(db: Database, symbol: str, exchange: str, source: str, collection: str = "stock_prices") -> pd.DataFrame:
    cursor = db[collection].find(
        {"symbol": symbol, "exchange": exchange, "interval": "1d", "source": source},
        {"_id": 0, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}).sort("date", 1)
    df = pd.DataFrame(list(cursor), columns=["date", "open", "high", "low", "close", "volume"])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], utc=True)
    return df


# ----------------------------------------------------------------------------- orchestration
def build_corporate_actions(db: Database, yahoo_dir: Path, *, dry_run: bool = False) -> dict:
    """Load Yahoo actions, then infer splits from raw NSE prices for every stock that has them."""
    summary = {"yahoo_files": 0, "yahoo_splits": 0, "yahoo_dividends": 0, "yahoo_stored": {"inserted": 0, "updated": 0},
               "stocks_scanned": 0, "inferred_events": 0, "inferred_new": 0, "inferred_confirmed_yahoo": 0,
               "events": []}

    for csv in sorted(Path(yahoo_dir).glob("*.csv")):
        docs = yahoo_actions(csv)
        summary["yahoo_files"] += 1
        summary["yahoo_splits"] += sum(d["action_type"] == "split" for d in docs)
        summary["yahoo_dividends"] += sum(d["action_type"] == "dividend" for d in docs)
        if not dry_run:
            stored = store_yahoo_actions(db, docs)
            summary["yahoo_stored"]["inserted"] += stored["inserted"]
            summary["yahoo_stored"]["updated"] += stored["updated"]

    raw_symbols = db["stock_prices"].aggregate([
        {"$match": {"source": "kaggle", "price_basis": "raw", "interval": "1d"}},
        {"$group": {"_id": {"symbol": "$symbol", "exchange": "$exchange"}}}, {"$sort": {"_id.symbol": 1}}])
    for entry in raw_symbols:
        symbol, exchange = entry["_id"]["symbol"], entry["_id"]["exchange"]
        events = infer_split_events(load_raw_bars(db, symbol, exchange, "kaggle"))
        summary["stocks_scanned"] += 1
        summary["inferred_events"] += len(events)
        summary["events"].extend({"symbol": symbol, "ex_date": e["ex_date"].date().isoformat(), "factor": round(e["factor"], 4),
                                  "observed_ratio": round(e["observed_ratio"], 3)} for e in events)
        if not dry_run:
            stored = store_inferred_actions(db, symbol, exchange, events)
            summary["inferred_new"] += stored["new_inferred"]
            summary["inferred_confirmed_yahoo"] += stored["confirmed_yahoo"]
    return summary


def reconcile_with_yahoo(db: Database) -> list[dict]:
    """How well did the price-jump detector reproduce Yahoo's splits?

    Only Yahoo splits that fall inside the raw NSE data's date range are counted: Yahoo reaches back
    further, so those older ones cannot be found from prices we do not have.
    """
    out = []
    for entry in db["stock_prices"].aggregate([
            {"$match": {"source": "kaggle", "price_basis": "raw", "interval": "1d"}},
            {"$group": {"_id": {"symbol": "$symbol", "exchange": "$exchange"}, "first": {"$min": "$date"},
                        "last": {"$max": "$date"}}}, {"$sort": {"_id.symbol": 1}}]):
        symbol, exchange = entry["_id"]["symbol"], entry["_id"]["exchange"]
        yahoo = list(db["corporate_actions"].find({"symbol": symbol, "exchange": exchange, "action_type": "split",
                                                   "source": SOURCE_YAHOO}))
        if not yahoo:
            continue
        window = [y for y in yahoo if entry["first"] <= y["ex_date"] <= entry["last"]]
        found = [y for y in window if SOURCE_INFERRED in y.get("confirmed_by", [])]
        missed = [y for y in window if SOURCE_INFERRED not in y.get("confirmed_by", [])]
        extra = list(db["corporate_actions"].find({"symbol": symbol, "exchange": exchange, "source": SOURCE_INFERRED}))
        out.append({"symbol": symbol, "yahoo_splits_in_range": len(window), "found_by_detector": len(found),
                    "missed": [f"{y['ex_date']:%Y-%m-%d} (factor {y['factor']:.3g})" for y in missed],
                    "detector_only": [f"{e['ex_date']:%Y-%m-%d} (factor {e['factor']:.3g})" for e in extra]})
    return out
