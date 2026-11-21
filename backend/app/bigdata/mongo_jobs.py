"""The same jobs as aggregation pipelines inside MongoDB (no data leaves the database).

Only the jobs MongoDB can express are here. Correlation, the 200-day market trend and per-year realised
volatility need operators or joins MongoDB does not have, so for those the benchmark compares pandas
with Spark only.

Needs MongoDB 5.2+ ($setWindowFields, $top / $bottom) and 5.0+ ($dateTrunc, $shift); the project runs 8.2.
"""
import math

from pymongo.database import Database

TRADING_DAYS = 252
IST = "Asia/Kolkata"


def returns_volatility_latest(db: Database, window: int = 20) -> list[dict]:
    """Latest return and 20-day historical volatility for every symbol in `adjusted_prices`."""
    frame = {"documents": [-(window - 1), 0]}
    pipeline = [
        {"$project": {"_id": 0, "symbol": 1, "date": 1, "close": 1}},
        {"$setWindowFields": {"partitionBy": "$symbol", "sortBy": {"date": 1},
                              "output": {"prev_close": {"$shift": {"output": "$close", "by": -1}}}}},
        {"$addFields": {
            "ret": {"$cond": [{"$gt": ["$prev_close", 0]}, {"$subtract": [{"$divide": ["$close", "$prev_close"]}, 1]}, None]},
            "log_ret": {"$cond": [{"$gt": ["$prev_close", 0]}, {"$ln": {"$divide": ["$close", "$prev_close"]}}, None]}}},
        {"$setWindowFields": {"partitionBy": "$symbol", "sortBy": {"date": 1}, "output": {
            "sd": {"$stdDevSamp": "$log_ret", "window": frame},
            "n": {"$sum": {"$cond": [{"$ne": ["$log_ret", None]}, 1, 0]}, "window": frame}}}},
        {"$group": {"_id": "$symbol", "last": {"$bottom": {"sortBy": {"date": 1},
                                                           "output": {"date": "$date", "ret": "$ret", "sd": "$sd", "n": "$n"}}}}},
    ]
    out = []
    for row in db["adjusted_prices"].aggregate(pipeline, allowDiskUse=True):
        last = row["last"]
        vol = last["sd"] * math.sqrt(TRADING_DAYS) * 100 if last["n"] >= window and last["sd"] is not None else None
        out.append({"symbol": row["_id"], "date": last["date"], "ret": last["ret"], "hist_vol_20": vol})
    return out


def daily_range_stats(db: Database, interval: str = None, symbols: list[str] = None) -> dict:
    """Bars, highest high, lowest low, volume and mean close per symbol, interval and IST day.

    Returns only the totals ({groups, bars, volume}); the grouping over all the bars is the work being timed.
    """
    match: dict = {}
    if interval:
        match["meta.interval"] = interval
    if symbols:
        match["meta.symbol"] = {"$in": symbols}
    pipeline = ([{"$match": match}] if match else []) + [
        {"$group": {"_id": {"s": "$meta.symbol", "i": "$meta.interval",
                            "d": {"$dateToString": {"format": "%Y-%m-%d", "date": "$ts", "timezone": IST}}},
                    "bars": {"$sum": 1}, "high": {"$max": "$high"}, "low": {"$min": "$low"},
                    "volume": {"$sum": {"$ifNull": ["$volume", 0]}}, "mean_close": {"$avg": "$close"}}},
        {"$group": {"_id": None, "groups": {"$sum": 1}, "bars": {"$sum": "$bars"}, "volume": {"$sum": "$volume"}}},
    ]
    rows = list(db["intraday_prices"].aggregate(pipeline, allowDiskUse=True))
    return {"groups": rows[0]["groups"], "bars": rows[0]["bars"], "volume": float(rows[0]["volume"])} if rows else \
        {"groups": 0, "bars": 0, "volume": 0.0}


def resample_totals(db: Database, symbols: list[str], minutes: int = 5) -> dict:
    """Combine 1-minute bars into `minutes`-minute bars inside MongoDB; returns {bars, volume, mean_close}."""
    pipeline = [
        {"$match": {"meta.interval": "1m", "meta.symbol": {"$in": symbols}}},
        {"$group": {"_id": {"s": "$meta.symbol", "t": {"$dateTrunc": {"date": "$ts", "unit": "minute", "binSize": minutes}}},
                    "open": {"$top": {"sortBy": {"ts": 1}, "output": "$open"}},
                    "high": {"$max": "$high"}, "low": {"$min": "$low"},
                    "close": {"$bottom": {"sortBy": {"ts": 1}, "output": "$close"}},
                    "volume": {"$sum": {"$ifNull": ["$volume", 0]}}}},
        {"$group": {"_id": None, "bars": {"$sum": 1}, "volume": {"$sum": "$volume"}, "mean_close": {"$avg": "$close"}}},
    ]
    rows = list(db["intraday_prices"].aggregate(pipeline, allowDiskUse=True))
    return {"bars": rows[0]["bars"], "volume": float(rows[0]["volume"]), "mean_close": float(rows[0]["mean_close"])} if rows else \
        {"bars": 0, "volume": 0.0, "mean_close": 0.0}
