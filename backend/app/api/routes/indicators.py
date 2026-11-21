"""Technical indicators: read the stored series, or (re)compute one instrument's indicators.

Indicators are computed in Phase 7 and stored in the `indicators` time-series collection, one document per bar. Warm-up
values are omitted: an indicator that needs 200 bars has no value for the first 199. Volume-based indicators (`vwap_*`,
`vol_sma_*`) exist only for instruments that have volume, so an index returns none of them.
"""
from datetime import datetime
from typing import Literal, Optional

from fastapi import Depends, Query
from pymongo.database import Database

from app.analytics.indicator_pipeline import compute_and_store
from app.analytics.indicators import INDICATOR_COLUMNS
from app.api.serialize import SafeAPIRouter
from app.api.deps import (check_write_access, csv_list, get_database, heavy_rate_limit, norm_symbol, require_instrument,
                          time_filter, utc)
from app.api.errors import ApiError

router = SafeAPIRouter(tags=["Indicators"])


@router.get("/indicators", summary="Names of the stored indicators")
def indicator_names():
    return {"indicators": list(INDICATOR_COLUMNS)}


@router.get("/indicators/{symbol}", summary="Stored indicator values for one instrument")
def get_indicators(
    symbol: str,
    interval: Literal["1d", "1m", "5m", "15m", "30m", "1h"] = "1d",
    exchange: Literal["NSE", "BSE"] = "NSE",
    names: Optional[str] = Query(None, description="comma-separated, e.g. rsi_14,ema_20; omit for all (see GET /api/indicators)"),
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = Query(500, ge=1, le=5000),
    cursor: Optional[datetime] = Query(None, description="`next_cursor` of the previous response"),
    order: Literal["asc", "desc"] = "desc",
    db: Database = Depends(get_database),
):
    sym = norm_symbol(symbol)
    require_instrument(db, sym, exchange)
    wanted = csv_list(names, name="names", max_items=len(INDICATOR_COLUMNS))
    unknown = [n for n in wanted if n not in INDICATOR_COLUMNS]
    if unknown:
        raise ApiError(422, "unknown_indicator", f"Unknown indicator(s): {', '.join(unknown)}.", {"available": list(INDICATOR_COLUMNS)})
    fields = wanted or list(INDICATOR_COLUMNS)
    query: dict = {"meta.symbol": sym, "meta.exchange": exchange, "meta.interval": interval}
    cond = time_filter(start, end)
    if cursor:
        cond["$gt" if order == "asc" else "$lt"] = utc(cursor)
    if cond:
        query["ts"] = cond
    docs = list(db["indicators"].find(query, {"_id": 0, "ts": 1, **{f: 1 for f in fields}})
                .sort("ts", 1 if order == "asc" else -1).limit(limit + 1))
    more = len(docs) > limit
    items = docs[:limit]
    return {"symbol": sym, "exchange": exchange, "interval": interval, "indicators": fields, "order": order,
            "count": len(items), "items": items, "next_cursor": items[-1]["ts"] if more and items else None}


@router.post("/indicators/{symbol}/compute", dependencies=[Depends(check_write_access), Depends(heavy_rate_limit)],
             summary="Recompute and store the indicators of one instrument")
def compute_indicators(symbol: str, interval: Literal["1d", "5m", "15m", "30m", "1h", "1m"] = "1d",
                       exchange: Literal["NSE", "BSE"] = "NSE", db: Database = Depends(get_database)):
    sym = norm_symbol(symbol)
    require_instrument(db, sym, exchange)
    result = compute_and_store(db, sym, exchange, interval)
    if result["status"] == "no_data":
        raise ApiError(404, "no_data", f"No {interval} prices are stored for {sym}, so there is nothing to compute.")
    return result
