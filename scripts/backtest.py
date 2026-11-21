"""Strategy backtesting (Phase 9).

Run from the project root:

    .venv\\Scripts\\python scripts\\backtest.py list                                         # strategies and their parameters
    .venv\\Scripts\\python scripts\\backtest.py run RELIANCE ema_crossover --start 2010-01-01  # one stock, one strategy
    .venv\\Scripts\\python scripts\\backtest.py run TCS rsi_reversal --param oversold=25 --param overbought=75
    .venv\\Scripts\\python scripts\\backtest.py run NIFTY_50 ma_trend_following --interval 1d  # an index: signal-quality proxy
    .venv\\Scripts\\python scripts\\backtest.py compare                                       # all strategies x all tradable stocks
    .venv\\Scripts\\python scripts\\backtest.py compare --symbols RELIANCE TCS --strategies ema_crossover breakout_retest
    .venv\\Scripts\\python scripts\\backtest.py sweep TCS ema_crossover --grid fast=10,20,30 --grid slow=50,100,200
    .venv\\Scripts\\python scripts\\backtest.py show <run_id>                                 # a stored result

Common options: --start/--end (YYYY-MM-DD), --interval (1d or an intraday interval), --capital, --slippage-bps,
--costs (delivery | intraday | stress | none), --no-store.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pandas as pd  # noqa: E402

from app.backtesting import runner  # noqa: E402
from app.backtesting.strategies import STRATEGIES  # noqa: E402
from app.db.mongo import get_db  # noqa: E402
from app.processing.jobs import tracked_job  # noqa: E402

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)


def _value(text: str):
    try:
        f = float(text)
        return int(f) if f == int(f) and "." not in text else f
    except ValueError:
        return None if text.lower() == "none" else text


def _parse_params(items) -> dict:
    out = {}
    for item in items or []:
        key, _, val = item.partition("=")
        if not val:
            raise SystemExit(f"--param needs name=value, got {item!r}")
        out[key.strip()] = _value(val.strip())
    return out


def _parse_grid(items) -> dict:
    grid = {}
    for item in items or []:
        key, _, vals = item.partition("=")
        if not vals:
            raise SystemExit(f"--grid needs name=v1,v2,..., got {item!r}")
        grid[key.strip()] = [_value(v) for v in vals.split(",")]
    return grid


def _config(args):
    overrides = {"initial_capital": args.capital, "slippage_bps": args.slippage_bps, "position_pct": args.position_pct,
                 "risk_pct": args.risk_pct, "start": args.start, "end": args.end}
    return runner.default_config(args.interval, cost_preset=args.costs, **overrides)


def _fmt(v, unit=""):
    return "n/a" if v is None else (f"{v:,.2f}{unit}" if isinstance(v, float) else f"{v}{unit}")


def print_result(d: dict) -> None:
    m, b = d["metrics"], d["benchmark"]
    print(f"\n{d['strategy']} on {d['symbol']} ({d['interval']}), {str(d['start'])[:10]} to {str(d['end'])[:10]}   run {d['run_id']}")
    print(f"params {d['params']}  target_r {d.get('target_r')}")
    c = d["costs"]
    print(f"costs: {c['model']['name']} preset, slippage {c['slippage_bps']} bps per fill, capital {c['initial_capital']:,.0f}")
    rows = [("Total trades", m.get("total_trades")), ("Winners / losers", f"{m.get('winners')} / {m.get('losers')}"),
            ("Win rate", _fmt(m.get("win_rate_pct"), "%")), ("Net P&L (after costs)", _fmt(m.get("net_pnl"))),
            ("Total return", _fmt(m.get("total_return_pct"), "%")), ("CAGR", _fmt(m.get("cagr_pct"), "%")),
            ("Profit factor", _fmt(m.get("profit_factor"))), ("Max drawdown", _fmt(m.get("max_drawdown_pct"), "%")),
            ("Avg risk-to-reward (R win / R loss)", _fmt(m.get("avg_risk_reward"))), ("Payoff ratio (avg win / avg loss)", _fmt(m.get("payoff_ratio"))),
            ("Average R multiple", _fmt(m.get("avg_r"))), ("Sharpe (approx.)", _fmt(m.get("sharpe"))),
            ("Time in market", _fmt(m.get("exposure_pct"), "%")), ("Costs paid", _fmt(m.get("total_costs")))]
    for k, v in rows:
        print(f"  {k:<38} {v}")
    print(f"  {'BUY AND HOLD (same dates, same costs)':<38} return {_fmt(b.get('total_return_pct'), '%')}, max drawdown {_fmt(b.get('max_drawdown_pct'), '%')}")
    print(f"  {'Strategy minus buy and hold':<38} {_fmt(d.get('excess_return_pct'), ' points')}")
    p = d.get("periods")
    if p:
        print(f"  In-sample / out-of-sample split at {str(p['split_date'])[:10]}:")
        for name in ("in_sample", "out_of_sample"):
            x = p[name]
            print(f"    {name:<14} trades {x['total_trades']:>3}  return {_fmt(x['total_return_pct'], '%'):>12}  buy&hold {_fmt(x['buy_hold_return_pct'], '%'):>12}"
                  f"  profit factor {_fmt(x['profit_factor'])}")
    if d.get("tax_estimate"):
        t = d["tax_estimate"]
        print(f"  Estimated capital-gains tax {_fmt(t['total_tax'])} (net P&L after tax {_fmt(t['net_pnl_after_tax'])}); {t['assumptions']}")
    for w in d["warnings"]:
        print(f"  ! {w}")


def cmd_list(args) -> int:
    for s in STRATEGIES.values():
        print(f"{s.name:<20} {s.title}{' (needs volume)' if s.needs_volume else ''}\n    {s.description}\n    defaults: {s.defaults}")
    return 0


def cmd_run(args) -> int:
    db = get_db()
    d = runner.backtest(db, args.symbol, args.strategy, params=_parse_params(args.param), interval=args.interval,
                        cfg=_config(args), store=not args.no_store)
    print_result(d)
    print(f"\n{'Not stored (--no-store).' if args.no_store else 'Stored in backtest_results and trading_signals.'}")
    return 0


def cmd_compare(args) -> int:
    db = get_db()
    symbols = args.symbols or runner.tradable_universe(db)
    strategies = args.strategies or list(STRATEGIES)
    print(f"{len(symbols)} symbols x {len(strategies)} strategies, {args.interval}, from {args.start or 'first bar'}\n")
    with tracked_job(db, "backtest", {"symbols": len(symbols), "strategies": strategies}, job_type="backtest") as job:
        docs = runner.compare(db, symbols, strategies, interval=args.interval, cfg=_config(args), store=not args.no_store,
                              progress=None if args.quiet else print)
        job["counts"] = {"runs": len(docs)}
    table = runner.summarise(docs)
    if table.empty:
        print("Nothing ran.")
        return 1
    print("\nBy strategy (medians across stocks; excess = strategy return minus buy-and-hold over the same dates):")
    by = table.groupby("strategy").agg(stocks=("symbol", "count"), trades=("trades", "median"), win_pct=("win_rate_pct", "median"),
                                       pf=("profit_factor", "median"), ret_pct=("return_pct", "median"), bh_pct=("buy_hold_pct", "median"),
                                       beat_bh=("excess_pct", lambda s: f"{(s > 0).sum()}/{s.notna().sum()}"),
                                       maxdd=("max_dd_pct", "median"), oos_ret=("oos_return_pct", "median"),
                                       oos_bh=("oos_buy_hold_pct", "median"))
    print(by.round(2).to_string())
    print("\nSurvivorship bias: the stock list is today's index members. Treat these as a comparison between strategies, not as returns you could have earned.")
    return 0


def cmd_sweep(args) -> int:
    db = get_db()
    grid = _parse_grid(args.grid)
    if not grid:
        raise SystemExit("give at least one --grid name=v1,v2,...")
    out = runner.sweep(db, args.symbol, args.strategy, grid, interval=args.interval, cfg=_config(args), objective=args.objective)
    t = out["table"]
    print(f"\n{args.strategy} on {args.symbol}: {out['combinations']} combinations; in-sample before {out['split_date'][:10]}, out-of-sample after; objective {args.objective}\n")
    print(t.sort_values("is_objective", ascending=False).head(args.top).round(3).to_string(index=False))
    if "best_in_sample" in out:
        b = out["best_in_sample"]
        print(f"\nBest in-sample: {({k: v for k, v in b.items() if k.startswith('p_')})}  "
              f"in-sample {args.objective} {b['is_objective']:.2f}  ->  out-of-sample {args.objective} {b['oos_objective']:.2f}")
        print(f"Median out-of-sample {args.objective} over ALL combinations: {out['median_oos_objective_all']}")
        print(f"Rank correlation in-sample vs out-of-sample: {out['rank_correlation']:.2f}  (near 0 or negative = the optimisation found noise)")
    print(f"Buy and hold: in-sample {out['buy_hold_is_return_pct']}%, out-of-sample {out['buy_hold_oos_return_pct']}%")
    return 0


def cmd_show(args) -> int:
    d = runner.get_run(get_db(), args.run_id)
    if not d:
        print("No such run.")
        return 1
    print_result(d)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--interval", default="1d")
        sp.add_argument("--start", default="2000-01-01", help="first traded date (earlier bars only warm the indicators up); the 1990s data is thin")
        sp.add_argument("--end", default=None)
        sp.add_argument("--capital", type=float, default=1_000_000.0)
        sp.add_argument("--slippage-bps", type=float, default=5.0)
        sp.add_argument("--position-pct", type=float, default=100.0, help="%% of cash committed to each trade")
        sp.add_argument("--risk-pct", type=float, default=None, help="also cap the loss at the stop to this %% of equity")
        sp.add_argument("--costs", choices=["delivery", "intraday", "stress", "none"], default=None)
        sp.add_argument("--no-store", action="store_true")

    sub.add_parser("list").set_defaults(fn=cmd_list)
    r = sub.add_parser("run")
    r.add_argument("symbol")
    r.add_argument("strategy", choices=sorted(STRATEGIES))
    r.add_argument("--param", action="append", help="name=value, repeatable")
    common(r)
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("compare")
    c.add_argument("--symbols", nargs="+")
    c.add_argument("--strategies", nargs="+", choices=sorted(STRATEGIES))
    c.add_argument("--quiet", action="store_true")
    common(c)
    c.set_defaults(fn=cmd_compare)
    s = sub.add_parser("sweep")
    s.add_argument("symbol")
    s.add_argument("strategy", choices=sorted(STRATEGIES))
    s.add_argument("--grid", action="append", help="name=v1,v2,v3, repeatable")
    s.add_argument("--objective", default="sharpe", choices=["sharpe", "total_return_pct", "profit_factor", "calmar"])
    s.add_argument("--top", type=int, default=12)
    common(s)
    s.set_defaults(fn=cmd_sweep)
    sh = sub.add_parser("show")
    sh.add_argument("run_id")
    sh.set_defaults(fn=cmd_show)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
