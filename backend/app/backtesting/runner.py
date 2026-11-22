"""Run backtests against the database and store the results.

    run_backtest_for(db, "RELIANCE", "ema_crossover", ...)   one stock, one strategy  -> backtest_results (+ trading_signals)
    compare(db, symbols, strategies, ...)                     the whole grid, each result next to buy-and-hold
    sweep(db, "TCS", "ema_crossover", {"fast": [...], ...})   parameter grid: optimise in-sample, test out-of-sample

What every stored result carries so it cannot be misread:
  * `metrics`      the strategy, after costs and slippage
  * `benchmark`    buy-and-hold of the same instrument over the same dates, same costs
  * `periods`      the same strategy in-sample (first 70% of the dates) and out-of-sample (last 30%)
  * `warnings`     survivorship bias, index-as-proxy, missing volume, stale data
  * `costs`        every cost and slippage assumption used
"""
import logging
import uuid
from datetime import datetime, timezone
from itertools import product
from typing import Optional

import numpy as np
import pandas as pd
from pymongo.database import Database

from app.analytics.indicator_pipeline import load_series
from app.backtesting.costs import estimate_capital_gains_tax, preset
from app.backtesting.engine import BacktestConfig, BacktestResult, buy_and_hold, run_backtest
from app.backtesting.metrics import compute_metrics, downsample_curve
from app.backtesting.strategies import STRATEGIES, generate_signals, resolve_params

logger = logging.getLogger(__name__)

SPLIT_FRACTION = 0.70               # share of the dates used as in-sample
MAX_STORED_TRADES = 1500
MAX_STORED_SIGNALS = 5000
MIN_TRADES_FOR_RANKING = 10
SURVIVORSHIP_WARNING = ("survivorship bias: the stock list is today's index members, so companies that were delisted or "
                        "dropped out are missing and results look better than a real investor's would have been")
SUMMARY_KEYS = ("total_trades", "win_rate_pct", "net_pnl", "total_return_pct", "cagr_pct", "profit_factor",
                "max_drawdown_pct", "sharpe", "avg_r", "avg_risk_reward", "exposure_pct")


# ----------------------------------------------------------------------------------------------- helpers
def _clean(x):
    """Make anything BSON-safe: numpy numbers to Python, NaN/inf to None, Timestamps to datetimes."""
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return x.to_pydatetime()
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return float(x) if np.isfinite(x) else None
    return x


def _utc(x) -> Optional[pd.Timestamp]:
    if x is None:
        return None
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def default_config(interval: str, *, cost_preset: Optional[str] = None, **overrides) -> BacktestConfig:
    """Daily runs assume delivery trading; intraday runs assume square-off at the end of the day and intraday charges."""
    intraday = interval != "1d"
    cost_name = cost_preset or ("intraday" if intraday else "delivery")
    fields = {"cost_model": preset(cost_name), "force_eod": intraday, **overrides}
    return BacktestConfig(**fields)


def instrument_info(db: Database, symbol: str, exchange: str = "NSE") -> dict:
    doc = db["instruments"].find_one({"symbol": symbol, "exchange": exchange},
                                     {"_id": 0, "instrument_type": 1, "sector": 1, "name": 1})
    return doc or {}


def tradable_universe(db: Database, min_bars: int = 750) -> list[str]:
    """Active equities and ETFs with enough daily history to backtest (indices cannot be traded directly)."""
    symbols = [d["symbol"] for d in db["instruments"].find(
        {"instrument_type": {"$in": ["equity", "etf"]}, "exchange": "NSE", "active": {"$ne": False}},
        {"symbol": 1}).sort("symbol", 1)]
    counts = {d["_id"]: d["n"] for d in db["adjusted_prices"].aggregate(
        [{"$match": {"interval": "1d", "symbol": {"$in": symbols}}}, {"$group": {"_id": "$symbol", "n": {"$sum": 1}}}])}
    return [s for s in symbols if counts.get(s, 0) >= min_bars]


def _subset(metrics: dict) -> dict:
    return {k: metrics.get(k) for k in SUMMARY_KEYS}


def _split_date(df: pd.DataFrame, cfg: BacktestConfig) -> pd.Timestamp:
    """The date where in-sample ends and out-of-sample begins (70% of the TRADED dates)."""
    ts = pd.to_datetime(df["ts"], utc=True)
    mask = pd.Series(True, index=df.index)
    if cfg.start is not None:
        mask &= ts >= _utc(cfg.start)
    if cfg.end is not None:
        mask &= ts <= _utc(cfg.end)
    traded = ts[mask].reset_index(drop=True)
    return traded.iloc[int(len(traded) * SPLIT_FRACTION)] if len(traded) > 10 else traded.iloc[-1]


def _evaluate(df: pd.DataFrame, signals, cfg: BacktestConfig, interval: str) -> tuple[BacktestResult, dict]:
    res = run_backtest(df, signals, cfg)
    return res, compute_metrics(res.trades, res.equity, cfg.initial_capital, interval)


def _with_range(cfg: BacktestConfig, start, end) -> BacktestConfig:
    return BacktestConfig(**{**cfg.__dict__, "start": start, "end": end})


# ------------------------------------------------------------------------------------------- one backtest
def backtest(db: Database, symbol: str, strategy: str, *, params: Optional[dict] = None, interval: str = "1d",
             exchange: str = "NSE", cfg: Optional[BacktestConfig] = None, store: bool = True,
             batch: Optional[str] = None, with_periods: bool = True) -> dict:
    """Run one strategy on one instrument and (optionally) store the result. Returns the result document."""
    params = resolve_params(strategy, params)
    cfg = cfg or default_config(interval)
    prices, source = load_series(db, symbol, exchange, interval)
    if len(prices) < 60:
        raise ValueError(f"{symbol} {interval}: only {len(prices)} bars in the database; nothing to backtest")
    info = instrument_info(db, symbol, exchange)

    signals = generate_signals(strategy, prices, params)
    res, metrics = _evaluate(prices, signals, cfg, interval)
    # The benchmark is an investor who buys and holds, so it pays delivery charges even when the strategy trades intraday.
    bench_cfg = BacktestConfig(**{**cfg.__dict__, "cost_model": preset("delivery")}) if cfg.force_eod else cfg
    bench = buy_and_hold(prices, bench_cfg)
    bench_metrics = compute_metrics(bench.trades, bench.equity, cfg.initial_capital, interval)

    is_index = info.get("instrument_type") == "index"
    warnings = [] if is_index else [SURVIVORSHIP_WARNING]
    if is_index:
        warnings.append("this is an INDEX: it cannot be bought directly, so the result only shows how good the signals are "
                        "on the index level (an index fund or futures would have its own costs)")
    if STRATEGIES[strategy].needs_volume and prices["volume"].fillna(0).sum() == 0:
        warnings.append(f"{strategy} needs traded volume and {symbol} has none, so it never traded")
    if metrics.get("total_trades", 0) < 30:
        warnings.append(f"only {metrics.get('total_trades', 0)} trades: too few for the statistics to mean much")
    last_bar = prices["ts"].iloc[-1]
    if (pd.Timestamp.now(tz="UTC") - last_bar).days > 60:
        warnings.append(f"price history ends {last_bar.date()}, so nothing after that is tested")

    doc = {
        "run_id": f"bt_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}",
        "strategy": strategy, "symbol": symbol, "exchange": exchange, "interval": interval,
        "start": res.equity["ts"].iloc[0] if len(res.equity) else None,
        "end": res.equity["ts"].iloc[-1] if len(res.equity) else None,
        "params": params, "target_r": signals.target_r,
        "costs": {"model": cfg.cost_model.to_dict(), "slippage_bps": cfg.slippage_bps,
                  "position_pct": cfg.position_pct, "risk_pct": cfg.risk_pct, "force_eod": cfg.force_eod,
                  "initial_capital": cfg.initial_capital,
                  "execution": "signal at the close of bar t, fill at the open of bar t+1; stop checked before target; gaps fill at the open"},
        "metrics": metrics, "benchmark": bench_metrics,
        "excess_return_pct": None if metrics.get("total_return_pct") is None or bench_metrics.get("total_return_pct") is None
        else round(metrics["total_return_pct"] - bench_metrics["total_return_pct"], 4),
        "input": {"collection": source, "bars": len(prices), "first_bar": prices["ts"].iloc[0], "last_bar": last_bar},
        "instrument_type": info.get("instrument_type"), "sector": info.get("sector"),
        "warnings": warnings, "created_at": datetime.now(timezone.utc),
    }
    if batch:
        doc["batch"] = batch

    if with_periods and len(res.equity) > 40:
        split = _split_date(prices, cfg)
        periods = {"split_date": split}
        for name, (a, b) in {"in_sample": (cfg.start, split - pd.Timedelta(seconds=1)), "out_of_sample": (split, cfg.end)}.items():
            _, m = _evaluate(prices, signals, _with_range(cfg, a, b), interval)
            bres = buy_and_hold(prices, _with_range(bench_cfg, a, b))
            periods[name] = {**_subset(m), "buy_hold_return_pct": compute_metrics(
                bres.trades, bres.equity, cfg.initial_capital, interval).get("total_return_pct")}
        doc["periods"] = periods

    curve = downsample_curve(res.equity)
    bcurve = downsample_curve(bench.equity)
    if len(curve) == len(bcurve):
        for a, b in zip(curve, bcurve):
            a["benchmark_equity"] = b["equity"]
    doc["equity_curve"] = curve
    trades = res.trades.copy()
    doc["trades_total"] = len(trades)
    doc["trades"] = trades.head(MAX_STORED_TRADES).to_dict("records")
    if len(trades) > MAX_STORED_TRADES:
        doc["trades_truncated"] = True
    if cfg.cost_model.name.startswith(("delivery", "stress")) and not cfg.force_eod:
        doc["tax_estimate"] = estimate_capital_gains_tax(trades)
        doc["tax_estimate"]["net_pnl_after_tax"] = round(float(trades["net_pnl"].sum()) - doc["tax_estimate"]["total_tax"], 2)
    doc = _clean(doc)

    if store:
        db["backtest_results"].insert_one(dict(doc))
        _store_signals(db, doc, res)
    doc["_signals"] = len(res.signal_log)
    return doc


def _store_signals(db: Database, doc: dict, res: BacktestResult) -> None:
    rows = []
    for s in res.signal_log[:MAX_STORED_SIGNALS]:
        if s.get("action") == "pending":
            continue                                  # a signal on the very last bar that could never be filled
        rows.append(_clean({"run_id": doc["run_id"], "symbol": doc["symbol"], "exchange": doc["exchange"],
                            "interval": doc["interval"], "ts": s["ts"], "strategy": doc["strategy"],
                            "signal": "buy" if s["side"] == "buy" else "exit_long", "price": s["price"],
                            "params": doc["params"], "stop": s.get("stop"), "action": s["action"],
                            "fill_price": s.get("fill_price"), "created_at": doc["created_at"]}))
    if rows:
        db["trading_signals"].insert_many(rows)


# ------------------------------------------------------------------------------------- the whole grid
def compare(db: Database, symbols: list[str], strategies: Optional[list[str]] = None, *, interval: str = "1d",
            cfg: Optional[BacktestConfig] = None, params: Optional[dict[str, dict]] = None, store: bool = True,
            batch: str = "compare", progress=None) -> list[dict]:
    """Every strategy on every symbol. Replaces the earlier results of the same `batch` when storing."""
    strategies = strategies or list(STRATEGIES)
    if store:
        old = [d["run_id"] for d in db["backtest_results"].find({"batch": batch}, {"run_id": 1})]
        if old:
            db["trading_signals"].delete_many({"run_id": {"$in": old}})
            db["backtest_results"].delete_many({"run_id": {"$in": old}})
    docs = []
    for sym in symbols:
        for strat in strategies:
            try:
                d = backtest(db, sym, strat, params=(params or {}).get(strat), interval=interval, cfg=cfg, store=store,
                             batch=batch if store else None)
            except ValueError as exc:
                logger.warning("skipped %s %s: %s", sym, strat, exc)
                continue
            docs.append(d)
            if progress:
                progress(f"{sym:<12} {strat:<20} trades {d['metrics'].get('total_trades', 0):>4}  "
                         f"return {d['metrics'].get('total_return_pct')}%  buy&hold {d['benchmark'].get('total_return_pct')}%")
    return docs


def summarise(docs: list[dict]) -> pd.DataFrame:
    """One row per (symbol, strategy): the headline numbers next to buy-and-hold and the out-of-sample result."""
    rows = []
    for d in docs:
        m, p = d["metrics"], d.get("periods", {})
        oos = p.get("out_of_sample", {})
        rows.append({"symbol": d["symbol"], "strategy": d["strategy"], "trades": m.get("total_trades"),
                     "return_pct": m.get("total_return_pct"), "buy_hold_pct": d["benchmark"].get("total_return_pct"),
                     "excess_pct": d.get("excess_return_pct"), "win_rate_pct": m.get("win_rate_pct"),
                     "profit_factor": m.get("profit_factor"), "max_dd_pct": m.get("max_drawdown_pct"),
                     "sharpe": m.get("sharpe"), "oos_return_pct": oos.get("total_return_pct"),
                     "oos_buy_hold_pct": oos.get("buy_hold_return_pct"), "oos_trades": oos.get("total_trades")})
    return pd.DataFrame(rows)


# -------------------------------------------------------------------------------- parameter sweeping
def sweep(db: Database, symbol: str, strategy: str, grid: dict[str, list], *, interval: str = "1d",
          exchange: str = "NSE", cfg: Optional[BacktestConfig] = None, objective: str = "sharpe") -> dict:
    """Try every parameter combination in-sample, then judge the winners out-of-sample.

    Picking the best combination on the data you tested it on is how overfitting happens, so the table shows
    both: `is_*` (the period the parameters were chosen on) and `oos_*` (the period they never saw). If the
    in-sample ranking does not predict the out-of-sample ranking (`rank_correlation` near 0 or negative),
    the optimisation found noise.
    """
    cfg = cfg or default_config(interval)
    prices, _ = load_series(db, symbol, exchange, interval)
    if len(prices) < 60:
        raise ValueError(f"{symbol} {interval}: not enough bars")
    split = _split_date(prices, cfg)
    is_cfg = _with_range(cfg, cfg.start, split - pd.Timedelta(seconds=1))
    oos_cfg = _with_range(cfg, split, cfg.end)
    names = list(grid)
    rows = []
    for combo in product(*(grid[k] for k in names)):
        p = dict(zip(names, combo))
        try:
            sig = generate_signals(strategy, prices, p)
        except ValueError:
            continue                                      # an invalid combination (e.g. fast >= slow)
        _, m_is = _evaluate(prices, sig, is_cfg, interval)
        _, m_oos = _evaluate(prices, sig, oos_cfg, interval)
        rows.append({**{f"p_{k}": v for k, v in p.items()}, "is_trades": m_is.get("total_trades"),
                     "is_objective": m_is.get(objective), "is_return_pct": m_is.get("total_return_pct"),
                     "oos_trades": m_oos.get("total_trades"), "oos_objective": m_oos.get(objective),
                     "oos_return_pct": m_oos.get("total_return_pct")})
    table = pd.DataFrame(rows)
    out = {"symbol": symbol, "strategy": strategy, "objective": objective, "split_date": split.isoformat(),
           "combinations": len(table), "table": table}
    eligible = table[(table["is_trades"] >= MIN_TRADES_FOR_RANKING) & table["is_objective"].notna()] if len(table) else table
    if len(eligible) >= 3:
        both = eligible.dropna(subset=["oos_objective"])
        out["rank_correlation"] = float(both["is_objective"].rank().corr(both["oos_objective"].rank())) if len(both) >= 3 else None
        best = eligible.sort_values("is_objective", ascending=False).iloc[0]
        out["best_in_sample"] = best.to_dict()
        out["median_oos_objective_all"] = _num(both["oos_objective"].median()) if len(both) else None
    bh_is, bh_oos = buy_and_hold(prices, is_cfg), buy_and_hold(prices, oos_cfg)
    out["buy_hold_oos_return_pct"] = compute_metrics(bh_oos.trades, bh_oos.equity, cfg.initial_capital, interval).get("total_return_pct")
    out["buy_hold_is_return_pct"] = compute_metrics(bh_is.trades, bh_is.equity, cfg.initial_capital, interval).get("total_return_pct")
    return out


def _num(x):
    return None if x is None or not np.isfinite(x) else float(x)


# ------------------------------------------------------------------------------------------- leaderboard
LEADERBOARD_WARNING = ("survivorship bias: the stock list is today's index members; compare strategies with each other, "
                       "not with returns you could have earned")


def leaderboard(db: Database, batch: str = "compare", interval: str = "1d") -> Optional[dict]:
    """Per-strategy medians over a stored comparison batch, best Sharpe first. None when the batch has no runs."""
    docs = list(db["backtest_results"].find({"batch": batch, "interval": interval},
                                            {"_id": 0, "strategy": 1, "symbol": 1, "metrics": 1, "benchmark.total_return_pct": 1,
                                             "benchmark.sharpe": 1, "excess_return_pct": 1, "periods": 1}))
    if not docs:
        return None
    rows = [{"strategy": d["strategy"], **{k: d["metrics"].get(k) for k in ("total_trades", "win_rate_pct", "profit_factor", "total_return_pct",
            "max_drawdown_pct", "sharpe", "avg_risk_reward")}, "buy_hold_return_pct": d["benchmark"].get("total_return_pct"),
            "buy_hold_sharpe": d["benchmark"].get("sharpe"), "excess_return_pct": d.get("excess_return_pct"),
            "oos_return_pct": (d.get("periods") or {}).get("out_of_sample", {}).get("total_return_pct")} for d in docs]
    frame = pd.DataFrame(rows)
    out = []
    for name, g in frame.groupby("strategy"):
        med = g.drop(columns="strategy").apply(pd.to_numeric, errors="coerce").median()
        out.append({"strategy": name, "stocks": int(len(g)), "beat_buy_and_hold": int((g["excess_return_pct"] > 0).sum()),
                    **{f"median_{k}": (None if pd.isna(v) else round(float(v), 4)) for k, v in med.items()}})
    out.sort(key=lambda r: (r["median_sharpe"] is None, -(r["median_sharpe"] or 0)))
    return {"batch": batch, "interval": interval, "runs": len(docs), "strategies": out, "warning": LEADERBOARD_WARNING}


# ------------------------------------------------------------------------------------------ reading back
def list_runs(db: Database, *, symbol: Optional[str] = None, strategy: Optional[str] = None, limit: int = 50) -> list[dict]:
    query = {k: v for k, v in (("symbol", symbol), ("strategy", strategy)) if v}
    return list(db["backtest_results"].find(query, {"_id": 0, "trades": 0, "equity_curve": 0}).sort("created_at", -1).limit(limit))


def get_run(db: Database, run_id: str) -> Optional[dict]:
    return db["backtest_results"].find_one({"run_id": run_id}, {"_id": 0})
