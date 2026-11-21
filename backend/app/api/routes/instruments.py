"""Instrument management: list, look up, add, edit and (de)activate instruments; sector groupings.

Instruments are never hard-deleted through the API: price history refers to them, so retiring one means
`PATCH ... {"active": false}`.
"""
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, WriteError

from app.api.serialize import SafeAPIRouter
from app.api.deps import Page, check_write_access, get_database, norm_symbol, page_params, paginate, require_instrument
from app.api.errors import ApiError

router = SafeAPIRouter(tags=["Instruments"])

ISIN_PATTERN = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"


class InstrumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=30, description="Canonical symbol: upper case, e.g. RELIANCE, M&M, NIFTY_50")
    exchange: Literal["NSE", "BSE"] = "NSE"
    name: str = Field(min_length=1, max_length=200)
    instrument_type: Literal["equity", "index", "etf"]
    sector: Optional[str] = Field(None, max_length=100)
    industry: Optional[str] = Field(None, max_length=100)
    isin: Optional[str] = Field(None, pattern=ISIN_PATTERN)
    yfinance_symbol: Optional[str] = Field(None, max_length=40, description="Yahoo Finance ticker, e.g. RELIANCE.NS")
    active: bool = True


class InstrumentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    sector: Optional[str] = Field(None, max_length=100)
    industry: Optional[str] = Field(None, max_length=100)
    isin: Optional[str] = Field(None, pattern=ISIN_PATTERN)
    yfinance_symbol: Optional[str] = Field(None, max_length=40)
    active: Optional[bool] = None

    @model_validator(mode="after")
    def at_least_one(self):
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        return self


@router.get("/instruments", summary="List instruments (paginated, searchable)")
def list_instruments(
    q: Optional[str] = Query(None, max_length=50, description="matches the start of the symbol or any part of the name"),
    instrument_type: Optional[Literal["equity", "index", "etf", "future", "option"]] = None,
    sector: Optional[str] = Query(None, max_length=100),
    exchange: Optional[Literal["NSE", "BSE"]] = None,
    active: Optional[bool] = None,
    page: Page = Depends(page_params),
    db: Database = Depends(get_database),
):
    query: dict = {}
    if q:
        safe = re.escape(q.strip())
        query["$or"] = [{"symbol": {"$regex": f"^{safe}", "$options": "i"}}, {"name": {"$regex": safe, "$options": "i"}}]
    for key, value in (("instrument_type", instrument_type), ("sector", sector), ("exchange", exchange), ("active", active)):
        if value is not None:
            query[key] = value
    return paginate(db["instruments"], query, page, sort=[("symbol", 1)])


@router.get("/instruments/{symbol}", summary="One instrument with its data coverage")
def get_instrument(symbol: str, exchange: Literal["NSE", "BSE"] = "NSE", db: Database = Depends(get_database)):
    sym = norm_symbol(symbol)
    inst = require_instrument(db, sym, exchange)
    daily = list(db["adjusted_prices"].aggregate([
        {"$match": {"symbol": sym, "exchange": exchange, "interval": "1d"}},
        {"$group": {"_id": None, "bars": {"$sum": 1}, "first": {"$min": "$date"}, "last": {"$max": "$date"}}}]))
    intraday = list(db["intraday_coverage"].find({"symbol": sym, "exchange": exchange}, {"_id": 0}).sort("interval", 1))
    return {**inst,
            "coverage": {"daily": ({"bars": daily[0]["bars"], "first": daily[0]["first"], "last": daily[0]["last"]} if daily else None),
                         "intraday": intraday}}


@router.post("/instruments", status_code=201, dependencies=[Depends(check_write_access)], summary="Add an instrument")
def create_instrument(body: InstrumentIn, db: Database = Depends(get_database)):
    doc = body.model_dump(exclude_none=True)
    doc["symbol"] = norm_symbol(doc["symbol"])
    doc["currency"] = "INR"
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db["instruments"].insert_one(dict(doc))
    except DuplicateKeyError:
        raise ApiError(409, "already_exists", f"{doc['symbol']} already exists on {doc['exchange']}.")
    except WriteError as exc:
        raise ApiError(422, "rejected_by_database", "The database validator rejected this instrument.", str((exc.details or {}).get("errmsg", ""))[:200])
    return doc


@router.patch("/instruments/{symbol}", dependencies=[Depends(check_write_access)], summary="Edit or (de)activate an instrument")
def update_instrument(symbol: str, body: InstrumentPatch, exchange: Literal["NSE", "BSE"] = "NSE",
                      db: Database = Depends(get_database)):
    sym = norm_symbol(symbol)
    changes = body.model_dump(exclude_unset=True)
    if any(changes.get(k) is None for k in ("name", "active") if k in changes):
        raise ApiError(422, "invalid_value", "name and active cannot be null.")
    sets = {k: v for k, v in changes.items() if v is not None}
    unset = {k: "" for k, v in changes.items() if v is None}            # sending null clears an optional field
    ops: dict = {}
    if sets:
        ops["$set"] = {**sets, "updated_at": datetime.now(timezone.utc)}
    if unset:
        ops["$unset"] = unset
    try:
        result = db["instruments"].update_one({"symbol": sym, "exchange": exchange}, ops)
    except WriteError as exc:
        raise ApiError(422, "rejected_by_database", "The database validator rejected this change.", str((exc.details or {}).get("errmsg", ""))[:200])
    if result.matched_count == 0:
        raise ApiError(404, "not_found", f"No instrument called {sym} on {exchange}.")
    return db["instruments"].find_one({"symbol": sym, "exchange": exchange}, {"_id": 0})


@router.get("/sectors", summary="Sectors and their stocks")
def list_sectors(db: Database = Depends(get_database)):
    groups = list(db["instruments"].aggregate([
        {"$match": {"instrument_type": {"$in": ["equity", "etf"]}, "active": {"$ne": False}}},
        {"$group": {"_id": {"$ifNull": ["$sector", None]}, "symbols": {"$push": "$symbol"}}},
        {"$sort": {"_id": 1}}]))
    sectors = [{"sector": g["_id"], "count": len(g["symbols"]), "symbols": sorted(g["symbols"])} for g in groups if g["_id"]]
    unassigned = next((sorted(g["symbols"]) for g in groups if not g["_id"]), [])
    return {"sectors": sectors, "unassigned": unassigned,
            "note": "sectors are labelled sector_source=assigned_by_project; verify them against NSE before relying on one stock"}


@router.get("/sectors/performance", summary="Stored sector performance analysis")
def sector_performance(snapshot: Literal["latest", "full_universe"] = "latest", db: Database = Depends(get_database)):
    doc = db["analytics_results"].find_one({"analysis_type": "sector_performance", "scope.snapshot": snapshot}, {"_id": 0})
    if not doc:
        raise ApiError(404, "not_computed", "Sector performance has not been computed yet: POST /api/processing/analytics first.")
    return {"snapshot": snapshot, "scope": doc["scope"], "computed_at": doc["computed_at"], **doc["result"]}
