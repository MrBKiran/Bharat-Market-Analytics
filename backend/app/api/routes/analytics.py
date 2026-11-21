"""Market analytics: the stored analyses from Phase 7/8, a one-call market overview, and live stock comparison.

  GET /analytics                  which analyses exist and when they were computed
  GET /analytics/{type}           one stored analysis (e.g. trend, market_breadth, correlation, spark_market_trend)
  GET /market/overview            the headline numbers for a dashboard home page
  GET /compare                    2-10 instruments over the same dates, computed live from the adjusted prices
Analyses are computed by `POST /api/processing/analytics` (or `scripts/analyze_data.py analytics`) and cached in
`analytics_results`; reading them is instant.
"""
import re
from datetime import datetime
from typing import Literal, Optional

import pandas as pd
from fastapi import Depends, Query
from pymongo.database import Database

from app.analytics.compare import MIN_COMMON_BARS, compare_closes
from app.api.serialize import SafeAPIRouter
from app.api.deps import csv_list, get_database, norm_symbol, require_instrument, time_filter
from app.api.errors import ApiError

router = SafeAPIRouter(tags=["Analytics"])

HISTORY_TYPES = {"return_distribution", "historical_statistics", "outliers"}
TYPE_PATTERN = re.compile(r"^[a-z0-9_]{1,50}$")


def _scope_filter(analysis_type: str, snapshot: str) -> dict:
    if analysis_type in HISTORY_TYPES:
        return {"scope.history": "all"}
    if analysis_type.startswith("spark_"):
        return {"scope.engine": "spark"}
    return {"scope.snapshot": snapshot}


@router.get("/analytics", summary="Stored analyses")
def list_analytics(db: Database = Depends(get_database)):
    docs = list(db["analytics_results"].find({}, {"_id": 0, "analysis_type": 1, "scope": 1, "computed_at": 1, "data_range": 1})
                .sort([("analysis_type", 1), ("computed_at", -1)]))
    return {"count": len(docs), "items": docs}


@router.get("/analytics/{analysis_type}", summary="One stored analysis")
def get_analysis(analysis_type: str, snapshot: Literal["latest", "full_universe"] = Query("latest", description="as-of analyses only"),
                 db: Database = Depends(get_database)):
    if not TYPE_PATTERN.match(analysis_type):
        raise ApiError(422, "invalid_analysis_type", "analysis_type must be lower-case letters, digits and underscores.")
    doc = db["analytics_results"].find_one({"analysis_type": analysis_type, **_scope_filter(analysis_type, snapshot)}, {"_id": 0})
    if not doc:
        available = sorted(db["analytics_results"].distinct("analysis_type"))
        raise ApiError(404, "not_found", f"No stored analysis {analysis_type!r} for this scope.", {"available": available})
    return doc


@router.get("/market/overview", summary="Headline market numbers for a dashboard")
def market_overview(snapshot: Literal["latest", "full_universe"] = "latest", db: Database = Depends(get_database)):
    def stored(kind):
        doc = db["analytics_results"].find_one({"analysis_type": kind, "scope.snapshot": snapshot}, {"_id": 0})
        return doc
    trend = stored("trend")
    if not trend:
        raise ApiError(404, "not_computed", "Market analytics have not been computed yet: POST /api/processing/analytics first.")
    parts = {kind: (stored(kind) or {}).get("result") for kind in ("market_breadth", "top_movers", "sector_performance", "index_analysis")}
    return {"snapshot": snapshot, "as_of": trend["scope"].get("as_of"), "computed_at": trend["computed_at"],
            "universe": trend["result"].get("universe"), "trend": trend["result"].get("stock_summary"),
            "breadth": parts["market_breadth"], "top_movers": parts["top_movers"], "sectors": parts["sector_performance"],
            "indices": parts["index_analysis"]}


@router.get("/compare", summary="Compare 2-10 instruments over the same dates")
def compare(
    symbols: str = Query(..., description="comma-separated, 2 to 10 symbols, e.g. RELIANCE,TCS,NIFTY_50"),
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    exchange: Literal["NSE", "BSE"] = "NSE",
    db: Database = Depends(get_database),
):
    syms = [norm_symbol(s) for s in csv_list(symbols, name="symbols", max_items=10)]
    if len(syms) < 2:
        raise ApiError(422, "too_few_symbols", "Give at least two distinct symbols.")
    for s in syms:
        require_instrument(db, s, exchange)
    query: dict = {"symbol": {"$in": syms}, "exchange": exchange, "interval": "1d"}
    cond = time_filter(start, end)
    if cond:
        query["date"] = cond
    rows = list(db["adjusted_prices"].find(query, {"_id": 0, "symbol": 1, "date": 1, "close": 1}))
    if not rows:
        raise ApiError(404, "no_data", "None of these symbols has daily prices in that range.")
    frame = pd.DataFrame(rows).pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    missing = [s for s in syms if s not in frame.columns]
    if missing:
        raise ApiError(404, "no_data", f"No daily prices in that range for: {', '.join(missing)}.")
    try:
        result = compare_closes(frame[syms])
    except ValueError as exc:
        raise ApiError(422, "not_enough_common_data", f"{exc}. Widen the date range or choose symbols with overlapping history.",
                       {"minimum_common_bars": MIN_COMMON_BARS})
    return {"symbols": syms, **result}
