"""Data quality audit across the raw and processed collections.

Everything here only READS. The result is a nested dict, stored by scripts/process_data.py in
`data_quality_reports` (dataset "processed:audit") and printed for humans.
"""
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
from pymongo.database import Database

from app.processing.adjustment import adjust_bars
from app.processing.corporate_actions import load_raw_bars, reconcile_with_yahoo, split_events

# Bars in a normal NSE session (09:15-15:30 IST) for each intraday interval.
EXPECTED_BARS = {"1m": 375, "5m": 75, "15m": 25, "1h": 7}
RETURN_MISMATCH = 0.01       # two sources disagree on a day's return by more than 1 percentage point
NSE_DATASET_START = datetime(2000, 3, 1, tzinfo=timezone.utc)   # the Kaggle NSE files begin 2000-01-03


def duplicate_audit(db: Database) -> dict:
    """Groups of rows sharing a unique key. The unique indexes make these 0; this proves it."""
    keys = {"stock_prices": ["symbol", "exchange", "interval", "source", "date"],
            "market_indices": ["symbol", "exchange", "interval", "source", "date"],
            "adjusted_prices": ["symbol", "exchange", "interval", "date"]}
    out = {}
    for coll, fields in keys.items():
        found = list(db[coll].aggregate([
            {"$group": {"_id": {f: f"${f}" for f in fields}, "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}}, {"$count": "groups"}], allowDiskUse=True))
        out[coll] = found[0]["groups"] if found else 0
    # time-series collection: the number of bars must equal what the coverage registry recorded
    coverage = {(c["symbol"], c["interval"]): c["row_count"] for c in db["intraday_coverage"].find()}
    mismatches = 0
    for g in db["intraday_prices"].aggregate([{"$group": {"_id": {"s": "$meta.symbol", "i": "$meta.interval"},
                                                          "n": {"$sum": 1}}}]):
        if coverage.get((g["_id"]["s"], g["_id"]["i"])) != g["n"]:
            mismatches += 1
    out["intraday_series_count_mismatches_vs_coverage"] = mismatches
    return out


def adjusted_summary(db: Database) -> dict:
    by_method = {r["_id"]: {"rows": r["rows"], "symbols": len(r["symbols"])} for r in db["adjusted_prices"].aggregate(
        [{"$group": {"_id": "$method", "rows": {"$sum": 1}, "symbols": {"$addToSet": "$symbol"}}}])}
    flags = {r["_id"]: r["n"] for r in db["adjusted_prices"].aggregate(
        [{"$unwind": "$quality_flags"}, {"$group": {"_id": "$quality_flags", "n": {"$sum": 1}}}])}
    outliers = [{"symbol": r["_id"], "days": r["n"]} for r in db["adjusted_prices"].aggregate(
        [{"$match": {"quality_flags": "return_outlier"}}, {"$group": {"_id": "$symbol", "n": {"$sum": 1}}},
         {"$sort": {"n": -1}}, {"$limit": 8}])]
    return {"rows": db["adjusted_prices"].estimated_document_count(), "by_method": by_method,
            "flags": flags, "most_outlier_days": outliers}


def calendar_gaps(db: Database, reference: str = "NIFTY_50") -> dict:
    """Trading days present in the NIFTY 50 series but missing from each stock's series (between its first and last day)."""
    cal = pd.DatetimeIndex(sorted(d["date"] for d in db["adjusted_prices"].find(
        {"symbol": reference}, {"date": 1, "_id": 0})))
    if cal.empty:
        return {"reference": reference, "note": "no reference series; run `adjust` first"}
    per_symbol = []
    for sym in db["adjusted_prices"].distinct("symbol", {"method": {"$ne": "index_level"}}):
        dates = pd.DatetimeIndex(sorted(d["date"] for d in db["adjusted_prices"].find({"symbol": sym}, {"date": 1, "_id": 0})))
        in_range = cal[(cal >= dates.min()) & (cal <= dates.max())]
        missing = len(in_range.difference(dates))
        per_symbol.append({"symbol": sym, "rows": len(dates), "calendar_days": len(in_range), "missing_days": missing,
                           "missing_pct": round(100.0 * missing / max(len(in_range), 1), 2)})
    per_symbol.sort(key=lambda r: -r["missing_pct"])
    return {"reference": reference, "reference_days": len(cal), "symbols": len(per_symbol),
            "worst": per_symbol[:8], "total_missing_days": sum(r["missing_days"] for r in per_symbol)}


def intraday_completeness(db: Database) -> dict:
    """Bars per trading day against a full session. Short days are reported, never filled."""
    rows = list(db["intraday_prices"].aggregate([
        {"$group": {"_id": {"s": "$meta.symbol", "i": "$meta.interval",
                            "d": {"$dateToString": {"format": "%Y-%m-%d", "date": "$ts", "timezone": "Asia/Kolkata"}}},
                    "n": {"$sum": 1}}}], allowDiskUse=True))
    if not rows:
        return {"series": 0}
    df = pd.DataFrame([{"symbol": r["_id"]["s"], "interval": r["_id"]["i"], "day": r["_id"]["d"], "bars": r["n"]}
                       for r in rows])
    df["expected"] = df["interval"].map(EXPECTED_BARS)
    df["ratio"] = df["bars"] / df["expected"]
    per_interval = {}
    for interval, g in df.groupby("interval"):
        per_interval[interval] = {
            "series_days": int(len(g)),
            "full_sessions_pct": round(100.0 * float((g["ratio"] >= 0.98).mean()), 1),
            "short_days": int((g["ratio"] < 0.9).sum()),
            "overfull_days": int((g["ratio"] > 1.02).sum()),
        }
    sample = df[(df["interval"] == "1m") & (df["symbol"] == "NIFTY_50")]
    shortest = sample[sample["ratio"] < 0.9].sort_values("ratio").head(8)
    return {"expected_bars": EXPECTED_BARS, "per_interval": per_interval,
            "nifty50_1m_shortest_days": [{"day": r.day, "bars": int(r.bars)} for r in shortest.itertuples()]}


def cross_source_returns(db: Database) -> list[dict]:
    """Our split adjustment of raw NSE prices vs Yahoo's own adjusted prices, day by day.

    If both adjust the same events, the daily returns agree. A day where they differ by more than
    1 percentage point marks an event one source adjusted and the other did not (e.g. a demerger).
    """
    out = []
    for symbol in sorted(db["stock_prices"].distinct("symbol", {"source": "yfinance"})):
        exchange = "NSE"
        raw = load_raw_bars(db, symbol, exchange, "kaggle")
        yahoo = load_raw_bars(db, symbol, exchange, "yfinance")
        if raw.empty or yahoo.empty:
            continue
        ours = adjust_bars(raw, split_events(db, symbol, exchange, inferred_only=True))
        both = ours.merge(yahoo, on="date", suffixes=("_ours", "_yahoo")).sort_values("date").reset_index(drop=True)
        if len(both) < 2:
            continue
        diff = (both["close_ours"].pct_change() - both["close_yahoo"].pct_change()).abs()
        bad = both.loc[diff > RETURN_MISMATCH, "date"]
        out.append({"symbol": symbol, "overlap_days": int(len(both)), "mismatch_days": int(len(bad)),
                    "mismatch_dates": [d.date().isoformat() for d in bad.head(6)]})
    return out


def series_starts(db: Database) -> list[dict]:
    """INFORMATIONAL: stocks whose Yahoo history begins earlier than their NSE record.

    This is not necessarily an error (the NSE file may simply be shorter), so nothing is changed or
    flagged. It is listed so a human can decide, e.g. TCS has Yahoo rows about two years before the
    NSE record begins (which matches its August 2004 IPO).
    """
    first = {}
    for r in db["stock_prices"].aggregate([{"$match": {"interval": "1d"}},
                                           {"$group": {"_id": {"s": "$symbol", "src": "$source"},
                                                       "first": {"$min": "$date"}}}]):
        first.setdefault(r["_id"]["s"], {})[r["_id"]["src"]] = r["first"]
    out = []
    for symbol, by_source in sorted(first.items()):
        y, k = by_source.get("yfinance"), by_source.get("kaggle")
        # The Kaggle NSE dataset begins on 2000-01-03 for every stock, so an earlier Yahoo start there is
        # normal. Only an NSE record that starts LATER than that is a real signal (listing date or short file).
        if y and k and (k - y).days > 30 and k > NSE_DATASET_START:
            rows = db["stock_prices"].count_documents({"symbol": symbol, "source": "yfinance", "date": {"$lt": k}})
            out.append({"symbol": symbol, "yahoo_first": y.date().isoformat(), "nse_first": k.date().isoformat(),
                        "yahoo_rows_before_nse_start": rows})
    return out


def run_audit(db: Database) -> dict:
    actions = Counter()
    for a in db["corporate_actions"].find({}, {"action_type": 1, "source": 1, "_id": 0}):
        actions[f"{a['action_type']}:{a['source']}"] += 1
    return {
        "duplicates": duplicate_audit(db),
        "adjusted": adjusted_summary(db),
        "calendar_gaps": calendar_gaps(db),
        "intraday": intraday_completeness(db),
        "corporate_actions": {"counts": dict(actions), "detector_vs_yahoo": reconcile_with_yahoo(db)},
        "cross_source_returns": cross_source_returns(db),
        "series_starts": series_starts(db),
    }
