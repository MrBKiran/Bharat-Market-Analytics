"""Market data retrieval and historical search.

  GET /prices/{symbol}          price bars (daily adjusted/raw, or intraday), time-range filtered, cursor-paged
  GET /prices/{symbol}/latest   the latest daily close and the change from the previous close
  GET /search/prices            find daily bars across instruments by date, price, volume or quality flag

Daily bars are split-adjusted (`basis=adjusted`, the processed series from Phase 6, the default). `basis=raw` returns the
stored provider rows; a bar can exist from two providers, so raw requests must name a `source`. Intraday bars are raw.
Timestamps: a daily bar's `ts` is 00:00 UTC of its trading date; an intraday `ts` is the UTC instant the bar opened.
"""
from datetime import datetime
from typing import Literal, Optional

from fastapi import Depends, Query
from pymongo.database import Database

from app.api.serialize import SafeAPIRouter
from app.api.deps import (Page, csv_list, get_database, norm_symbol, page_params, paginate, require_instrument, time_filter,
                          utc)
from app.api.errors import ApiError

router = SafeAPIRouter(tags=["Market data"])

DAILY_ADJUSTED_FIELDS = {"date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "adj_factor": 1, "source": 1,
                         "method": 1, "quality_flags": 1}
DAILY_RAW_FIELDS = {"date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "adj_close": 1, "prev_close": 1,
                    "source": 1, "price_basis": 1}
INTRADAY_FIELDS = {"ts": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "meta.source": 1}
SOURCE_PATTERN = r"^[a-z0-9_]{1,40}$"


def _shape(doc: dict, time_field: str) -> dict:
    out = dict(doc)
    out["ts"] = out.pop(time_field)
    meta = out.pop("meta", None)
    if meta:
        out["source"] = meta.get("source")
    return out


@router.get("/prices/{symbol}", summary="Price bars for one instrument")
def get_prices(
    symbol: str,
    interval: Literal["1d", "1m", "5m", "15m", "30m", "1h"] = "1d",
    exchange: Literal["NSE", "BSE"] = "NSE",
    basis: Literal["adjusted", "raw"] = Query("adjusted", description="daily bars only: split-adjusted series or stored provider rows"),
    source: Optional[str] = Query(None, pattern=SOURCE_PATTERN, description="data source, e.g. yfinance or kaggle (required for basis=raw)"),
    start: Optional[datetime] = Query(None, description="first bar, ISO date or datetime (UTC)"),
    end: Optional[datetime] = Query(None, description="last bar, ISO date or datetime (UTC)"),
    limit: int = Query(500, ge=1, le=5000, description="bars per request"),
    cursor: Optional[datetime] = Query(None, description="`next_cursor` of the previous response"),
    order: Literal["asc", "desc"] = Query("desc", description="desc = newest first"),
    db: Database = Depends(get_database),
):
    sym = norm_symbol(symbol)
    inst = require_instrument(db, sym, exchange)
    if interval == "1d" and basis == "adjusted":
        coll, field, proj = db["adjusted_prices"], "date", DAILY_ADJUSTED_FIELDS
        query = {"symbol": sym, "exchange": exchange, "interval": "1d"}
        if source:
            query["source"] = source
    elif interval == "1d":
        if not source:
            raise ApiError(422, "source_required", "basis=raw needs a source (e.g. yfinance, kaggle): the same bar can exist from two providers.")
        coll = db["market_indices" if inst.get("instrument_type") == "index" else "stock_prices"]
        field, proj = "date", DAILY_RAW_FIELDS
        query = {"symbol": sym, "exchange": exchange, "interval": "1d", "source": source}
    else:
        coll, field, proj = db["intraday_prices"], "ts", INTRADAY_FIELDS
        query = {"meta.symbol": sym, "meta.exchange": exchange, "meta.interval": interval}
        if source:
            query["meta.source"] = source
    cond = time_filter(start, end)
    if cursor:
        cond["$gt" if order == "asc" else "$lt"] = utc(cursor)
    if cond:
        query[field] = cond
    direction = 1 if order == "asc" else -1
    docs = list(coll.find(query, {"_id": 0, **proj}).sort(field, direction).limit(limit + 1))
    more = len(docs) > limit
    items = [_shape(d, field) for d in docs[:limit]]
    return {"symbol": sym, "exchange": exchange, "interval": interval,
            "basis": basis if interval == "1d" else "raw", "order": order, "count": len(items), "items": items,
            "next_cursor": items[-1]["ts"] if more and items else None}


@router.get("/prices/{symbol}/latest", summary="Latest daily close and change")
def latest_price(symbol: str, exchange: Literal["NSE", "BSE"] = "NSE", db: Database = Depends(get_database)):
    sym = norm_symbol(symbol)
    require_instrument(db, sym, exchange)
    bars = list(db["adjusted_prices"].find({"symbol": sym, "exchange": exchange, "interval": "1d"},
                                           {"_id": 0, **DAILY_ADJUSTED_FIELDS}).sort("date", -1).limit(2))
    if not bars:
        raise ApiError(404, "no_data", f"No daily prices stored for {sym}.")
    last = _shape(bars[0], "date")
    prev = bars[1]["close"] if len(bars) > 1 else None
    change = None if prev is None else last["close"] - prev
    return {"symbol": sym, "exchange": exchange, "as_of": last["ts"], "close": last["close"], "previous_close": prev,
            "change": change, "change_pct": None if prev in (None, 0) else change / prev * 100, "bar": last}


@router.get("/search/prices", summary="Historical search across daily bars")
def search_prices(
    symbols: Optional[str] = Query(None, description="comma-separated symbols, at most 50; omit to search every instrument"),
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    min_close: Optional[float] = Query(None, gt=0),
    max_close: Optional[float] = Query(None, gt=0),
    min_volume: Optional[int] = Query(None, ge=0),
    flag: Optional[Literal["return_outlier", "gap_before", "no_trade"]] = Query(None, description="data-quality flag set in Phase 6"),
    sort_by: Literal["date", "close", "volume"] = "date",
    order: Literal["asc", "desc"] = "desc",
    page: Page = Depends(page_params),
    db: Database = Depends(get_database),
):
    """Searches the split-adjusted daily series, for example every day RELIANCE closed above 1,500 on volume over 10 million."""
    if min_close is not None and max_close is not None and min_close > max_close:
        raise ApiError(422, "invalid_range", "min_close must not exceed max_close.")
    query: dict = {"interval": "1d"}
    syms = [norm_symbol(s) for s in csv_list(symbols, name="symbols", max_items=50)]
    if syms:
        query["symbol"] = {"$in": syms}
    cond = time_filter(start, end)
    if cond:
        query["date"] = cond
    close: dict = {}
    if min_close is not None:
        close["$gte"] = min_close
    if max_close is not None:
        close["$lte"] = max_close
    if close:
        query["close"] = close
    if min_volume is not None:
        query["volume"] = {"$gte": min_volume}
    if flag:
        query["quality_flags"] = flag
    sort = [(sort_by, 1 if order == "asc" else -1), ("symbol", 1)]
    result = paginate(db["adjusted_prices"], query, page, sort=sort,
                      projection={"symbol": 1, "exchange": 1, **DAILY_ADJUSTED_FIELDS})
    result["items"] = [_shape(d, "date") for d in result["items"]]
    return result
