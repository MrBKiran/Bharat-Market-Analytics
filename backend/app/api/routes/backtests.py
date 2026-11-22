"""Strategy backtesting over the API.

  GET    /backtests/strategies        the strategies, their default parameters and the cost presets
  POST   /backtests/run               run one backtest (stored only if `store` is true: that needs the API key)
  POST   /backtests/sweep             try a parameter grid in-sample and judge it out-of-sample
  GET    /backtests                   stored runs (paginated, filterable)
  GET    /backtests/leaderboard       stored `compare` runs summarised per strategy, next to buy-and-hold
  GET    /backtests/{run_id}          one stored run (+ /trades, /signals)
  DELETE /backtests/{run_id}          remove a stored run

Execution rules, costs and the bias controls are in docs/BACKTESTING.md. Every result carries its own `warnings`
(survivorship bias, index-as-proxy, missing volume ...) and a buy-and-hold `benchmark`: show them with the numbers.
"""
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from pymongo.database import Database

from app.api.serialize import SafeAPIRouter
from app.api.deps import (Page, check_write_access, get_database, heavy_rate_limit, norm_symbol, page_params, paginate, require_instrument)
from app.api.errors import ApiError
from app.backtesting import costs, runner
from app.backtesting.strategies import STRATEGIES

router = SafeAPIRouter(tags=["Backtesting"])

RUN_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,80}$"
MAX_INTRADAY_DAYS = 400
MAX_GRID_VALUES = 20
MAX_GRID_COMBINATIONS = 150
SUMMARY_PROJECTION = {"run_id": 1, "strategy": 1, "symbol": 1, "exchange": 1, "interval": 1, "start": 1, "end": 1, "params": 1,
                      "batch": 1, "created_at": 1, "excess_return_pct": 1, "benchmark.total_return_pct": 1,
                      **{f"metrics.{k}": 1 for k in runner.SUMMARY_KEYS}}


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=30)
    strategy: str = Field(min_length=1, max_length=40, description="see GET /api/backtests/strategies")
    params: dict[str, Optional[float]] = Field(default_factory=dict, max_length=12, description="overrides of the strategy's defaults")
    interval: Literal["1d", "1m", "5m", "15m", "30m", "1h"] = "1d"
    exchange: Literal["NSE", "BSE"] = "NSE"
    start: Optional[datetime] = Field(None, description="first traded date; earlier bars only warm the indicators up")
    end: Optional[datetime] = None
    initial_capital: float = Field(1_000_000.0, ge=10_000, le=10_000_000_000)
    slippage_bps: float = Field(5.0, ge=0, le=500, description="adverse price move per fill, basis points")
    position_pct: float = Field(100.0, gt=0, le=100, description="% of cash committed to each trade")
    risk_pct: Optional[float] = Field(None, gt=0, le=100, description="also cap the loss at the stop to this % of equity")
    cost_preset: Optional[Literal["delivery", "intraday", "stress", "none"]] = Field(None, description="default: delivery (daily) or intraday")
    store: bool = Field(False, description="save the result in backtest_results (needs the API key)")
    include_trades: bool = True


class SweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=30)
    strategy: str = Field(min_length=1, max_length=40)
    grid: dict[str, list[float]] = Field(max_length=4, description="parameter -> values to try, e.g. {\"fast\": [10, 20], \"slow\": [50, 100]}")
    objective: Literal["sharpe", "total_return_pct", "profit_factor", "calmar"] = "sharpe"
    exchange: Literal["NSE", "BSE"] = "NSE"
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    initial_capital: float = Field(1_000_000.0, ge=10_000, le=10_000_000_000)
    slippage_bps: float = Field(5.0, ge=0, le=500)
    cost_preset: Optional[Literal["delivery", "stress", "none"]] = None


def _check_strategy(name: str) -> None:
    if name not in STRATEGIES:
        raise ApiError(422, "unknown_strategy", f"Unknown strategy {name!r}.", {"available": sorted(STRATEGIES)})


def _config(body, interval: str = "1d"):
    extra = {k: getattr(body, k) for k in ("position_pct", "risk_pct") if hasattr(body, k)}
    return runner.default_config(interval, cost_preset=body.cost_preset, initial_capital=body.initial_capital,
                                 slippage_bps=body.slippage_bps, start=body.start, end=body.end, **extra)


@router.get("/backtests/strategies", summary="Strategies, default parameters and cost presets")
def list_strategies():
    return {
        "strategies": [{"name": s.name, "title": s.title, "description": s.description, "defaults": s.defaults,
                        "needs_volume": s.needs_volume} for s in STRATEGIES.values()],
        "cost_presets": {name: model.to_dict() for name, model in costs.PRESETS.items()},
        "notes": ["long-only; signals act at the next bar's open", "cost rates are assumptions, not any broker's real schedule"],
    }


@router.post("/backtests/run", dependencies=[Depends(heavy_rate_limit)], summary="Run one backtest")
def run_backtest(body: BacktestRequest, request: Request, db: Database = Depends(get_database)):
    if body.store:
        check_write_access(request)
    _check_strategy(body.strategy)
    sym = norm_symbol(body.symbol)
    require_instrument(db, sym, body.exchange)
    if body.interval != "1d":
        span = ((body.end or datetime.now(timezone.utc)).replace(tzinfo=None) - body.start.replace(tzinfo=None)).days if body.start else None
        if span is None or span > MAX_INTRADAY_DAYS:
            raise ApiError(422, "range_required", f"Intraday backtests need a start date and at most {MAX_INTRADAY_DAYS} days.")
    try:
        doc = runner.backtest(db, sym, body.strategy, params=body.params, interval=body.interval, exchange=body.exchange,
                              cfg=_config(body, body.interval), store=body.store)
    except ValueError as exc:
        raise ApiError(422, "invalid_backtest", str(exc))
    doc.pop("_signals", None)
    if not body.include_trades:
        doc.pop("trades", None)
    doc["stored"] = body.store
    return doc


@router.post("/backtests/sweep", dependencies=[Depends(heavy_rate_limit)], summary="Parameter sweep: in-sample choice, out-of-sample test")
def run_sweep(body: SweepRequest, db: Database = Depends(get_database)):
    _check_strategy(body.strategy)
    sym = norm_symbol(body.symbol)
    require_instrument(db, sym, body.exchange)
    combos = 1
    for key, values in body.grid.items():
        if not 1 <= len(values) <= MAX_GRID_VALUES:
            raise ApiError(422, "invalid_grid", f"{key}: give between 1 and {MAX_GRID_VALUES} values.")
        combos *= len(values)
    if combos > MAX_GRID_COMBINATIONS:
        raise ApiError(422, "grid_too_large", f"{combos} combinations; the limit is {MAX_GRID_COMBINATIONS}.")
    try:
        result = runner.sweep(db, sym, body.strategy, body.grid, exchange=body.exchange, cfg=_config(body), objective=body.objective)
    except ValueError as exc:
        raise ApiError(422, "invalid_sweep", str(exc))
    table = result.pop("table")
    result["table"] = table.to_dict("records")
    return result


@router.get("/backtests", summary="Stored backtest runs")
def list_runs(symbol: Optional[str] = None, strategy: Optional[str] = Query(None, max_length=40), batch: Optional[str] = Query(None, max_length=40),
              interval: Optional[Literal["1d", "1m", "5m", "15m", "30m", "1h"]] = None,
              sort_by: Literal["created_at", "total_return_pct", "sharpe"] = "created_at", order: Literal["asc", "desc"] = "desc",
              page: Page = Depends(page_params), db: Database = Depends(get_database)):
    query: dict = {}
    if symbol:
        query["symbol"] = norm_symbol(symbol)
    for key, value in (("strategy", strategy), ("batch", batch), ("interval", interval)):
        if value:
            query[key] = value
    field = "created_at" if sort_by == "created_at" else f"metrics.{sort_by}"
    return paginate(db["backtest_results"], query, page, sort=[(field, 1 if order == "asc" else -1), ("run_id", 1)], projection=SUMMARY_PROJECTION)


@router.get("/backtests/leaderboard", summary="Per-strategy summary of stored comparison runs")
def leaderboard(batch: str = Query("compare", max_length=40), interval: Literal["1d", "1m", "5m", "15m", "30m", "1h"] = "1d",
                db: Database = Depends(get_database)):
    board = runner.leaderboard(db, batch, interval)
    if board is None:
        raise ApiError(404, "no_runs", f"No stored runs in batch {batch!r}. Run `scripts/backtest.py compare` first.")
    return board


def _run_id(run_id: str) -> str:
    if not re.match(RUN_ID_PATTERN, run_id):
        raise ApiError(422, "invalid_run_id", "run_id may contain letters, digits, _ . and - only.")
    return run_id


@router.get("/backtests/{run_id}", summary="One stored run")
def get_run(run_id: str, include_trades: bool = False, db: Database = Depends(get_database)):
    projection = {"_id": 0} if include_trades else {"_id": 0, "trades": 0}
    doc = db["backtest_results"].find_one({"run_id": _run_id(run_id)}, projection)
    if not doc:
        raise ApiError(404, "not_found", f"No backtest run {run_id}.")
    return doc


@router.get("/backtests/{run_id}/trades", summary="Trades of a stored run")
def run_trades(run_id: str, page: Page = Depends(page_params), db: Database = Depends(get_database)):
    doc = db["backtest_results"].find_one({"run_id": _run_id(run_id)}, {"_id": 0, "trades_total": 1, "trades_truncated": 1,
                                                                        "trades": {"$slice": [page.skip, page.page_size]}})
    if not doc:
        raise ApiError(404, "not_found", f"No backtest run {run_id}.")
    total = doc.get("trades_total", len(doc.get("trades", [])))
    return {"run_id": run_id, "items": doc.get("trades", []), "page": page.page, "page_size": page.page_size, "total": total,
            "pages": -(-total // page.page_size) if total else 0, "stored_trades_truncated": bool(doc.get("trades_truncated"))}


@router.get("/backtests/{run_id}/signals", summary="Signals of a stored run")
def run_signals(run_id: str, page: Page = Depends(page_params), db: Database = Depends(get_database)):
    if not db["backtest_results"].count_documents({"run_id": _run_id(run_id)}, limit=1):
        raise ApiError(404, "not_found", f"No backtest run {run_id}.")
    return paginate(db["trading_signals"], {"run_id": run_id}, page, sort=[("ts", 1)])


@router.delete("/backtests/{run_id}", dependencies=[Depends(check_write_access)], summary="Delete a stored run")
def delete_run(run_id: str, db: Database = Depends(get_database)):
    result = db["backtest_results"].delete_one({"run_id": _run_id(run_id)})
    if result.deleted_count == 0:
        raise ApiError(404, "not_found", f"No backtest run {run_id}.")
    signals = db["trading_signals"].delete_many({"run_id": run_id}).deleted_count
    return {"deleted": run_id, "signals_deleted": signals}
