"""Technical indicators and market analytics (Phase 7).

Run from the project root. In order (or just `all`):

    .venv\\Scripts\\python scripts\\analyze_data.py sectors                       # assign sectors to the stocks
    .venv\\Scripts\\python scripts\\analyze_data.py indicators                    # daily indicators -> indicators
    .venv\\Scripts\\python scripts\\analyze_data.py indicators --intervals 5m 15m 1h --symbols NIFTY_50 NIFTY_BANK
    .venv\\Scripts\\python scripts\\analyze_data.py analytics                     # market analytics -> analytics_results
    .venv\\Scripts\\python scripts\\analyze_data.py report                        # readable highlights
    .venv\\Scripts\\python scripts\\analyze_data.py all

Every step replaces its own earlier results, so it is safe to re-run.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import indicator_pipeline, market, sectors  # noqa: E402
from app.db.mongo import get_db  # noqa: E402
from app.processing.jobs import tracked_job  # noqa: E402


def cmd_sectors(args) -> int:
    res = sectors.assign_sectors(get_db(), dry_run=getattr(args, "dry_run", False))
    print(f"Sectors assigned: {res['assigned']}, already had one: {res['already_had_sector']}, no mapping: {res['unmapped'] or 'none'}")
    print("(assigned sectors are labelled sector_source = assigned_by_project; check them against NSE before relying on one stock)")
    return 0


def cmd_indicators(args) -> int:
    db = get_db()
    intervals = args.intervals
    print(f"{'DRY RUN: ' if args.dry_run else ''}indicators for intervals {intervals}"
          f"{' (symbols: ' + ', '.join(args.symbols) + ')' if args.symbols else ''}\n")
    with tracked_job(db, "indicators", {"intervals": intervals}, job_type="indicators") as job:
        results = indicator_pipeline.run_indicators(db, intervals, symbols=args.symbols, dry_run=args.dry_run, progress=print)
        job["counts"] = {"series": len(results), "documents": sum(r["documents"] for r in results),
                         "bars": sum(r["bars"] for r in results)}
    total = job["counts"]
    print(f"\n{total['series']} series, {total['bars']:,} bars -> {total['documents']:,} indicator documents")
    return 0


def cmd_analytics(args) -> int:
    db = get_db()
    print("Computing market analytics...")
    with tracked_job(db, "analytics", job_type="analytics") as job:
        summary = market.run_analytics(db, progress=print)
        job["counts"] = {"analyses_stored": summary["stored"]}
    print(f"\n{summary['stored']} analysis documents stored in analytics_results")
    return 0


def _top(rows, n=3):
    return ", ".join(f"{r['symbol']} {r['return_pct']:+.1f}%" for r in rows[:n])


def cmd_report(args) -> int:
    db = get_db()
    for name in ("latest", "full_universe"):
        doc = db["analytics_results"].find_one({"analysis_type": "trend", "scope.snapshot": name}, {"scope": 1})
        if not doc:
            print(f"No analytics for snapshot {name}; run `analytics` first.")
            return 1
        scope = doc["scope"]
        get = lambda t: market.get_result(db, t, scope)  # noqa: E731
        trend, breadth, sec, movers, idx = get("trend"), get("market_breadth"), get("sector_performance"), get("top_movers"), get("index_analysis")
        uni = trend["universe"]
        print(f"\n{'=' * 78}\n{name.upper()}: as of {scope['as_of']}  ({uni['stocks']} stocks, {uni['indices']} indices"
              f"{', ' + str(len(uni['stale_excluded'])) + ' stale excluded' if uni['stale_excluded'] else ''})\n{'=' * 78}")
        s = trend["stock_summary"]
        print(f"Trend (rule-based):  bullish {s['bullish']}  neutral {s['neutral']}  bearish {s['bearish']}")
        print(f"Breadth today:       {breadth['advancers']} up / {breadth['decliners']} down;  "
              f"{breadth['pct_above_sma50']}% above 50-day, {breadth['pct_above_sma200']}% above 200-day;  "
              f"52-week highs {breadth['new_52w_highs']}, lows {breadth['new_52w_lows']}")
        print(f"Top gainers 1m:      {_top(movers['1m']['gainers'])}")
        print(f"Top losers 1m:       {_top(movers['1m']['losers'])}")
        print("Sectors by 1m:       " + "; ".join(f"{n} {sec['sectors'][n]['1m']:+.1f}%" for n in sec["ranking_by_1m"][:4]
                                                   if sec["sectors"][n]["1m"] is not None))
        for sym, d in idx.get("indices", {}).items():
            p = d["performance_pct"]
            print(f"{d['name']:<14} {d['close']:>10,.2f}  1y {p['1y']:+.1f}%  ytd {p['ytd']:+.1f}%  drawdown {d['current_drawdown_pct']:.1f}%  "
                  f"vol(1y) {d['annualised_vol_pct_last_1y']}%  trend {d['trend']['trend'] if d.get('trend') else 'n/a'}")
        if "nifty_vs_bank" in idx:
            n = idx["nifty_vs_bank"]
            print(f"NIFTY vs BANK NIFTY: correlation {n['correlation']}, beta of Bank on Nifty {n['beta_of_bank_on_nifty']} ({n['window_days']} days)")

    scope = {"history": "all"}
    stats = market.get_result(db, "historical_statistics", scope)["symbols"]
    # only series without a suspicious >30% day: those can have a distorted CAGR / drawdown
    stocks = {k: v for k, v in stats.items() if v["type"] == "stock" and v["years"] >= 10 and v["days_over_30pct_move"] == 0}
    best = sorted(stocks, key=lambda k: -stocks[k]["cagr_pct"])[:3]
    risky = sorted(stocks, key=lambda k: stocks[k]["max_drawdown_pct"])[:3]
    out = market.get_result(db, "outliers", scope)["largest_moves"][:3]
    print(f"\n{'=' * 78}\nHISTORY (all data)\n{'=' * 78}")
    print(f"(ranked among the {len(stocks)} stocks with 10+ years and no unexplained >30% one-day move)")
    print("Highest CAGR (10+ years): " + ", ".join(f"{k} {stocks[k]['cagr_pct']}%" for k in best))
    print("Deepest drawdowns:        " + ", ".join(f"{k} {stocks[k]['max_drawdown_pct']}%" for k in risky))
    print("Largest one-day moves:    " + ", ".join(f"{o['symbol']} {o['date']} {o['return_pct']:+.1f}%" for o in out))
    return 0


def cmd_all(args) -> int:
    args.dry_run, args.symbols, args.intervals = False, None, ["1d"]
    print("===== SECTORS =====")
    cmd_sectors(args)
    print("\n===== INDICATORS: daily =====")
    cmd_indicators(args)
    print("\n===== INDICATORS: intraday (the four indices) =====")
    args.intervals, args.symbols = ["5m", "15m", "1h"], ["NIFTY_50", "NIFTY_BANK", "NIFTY_IT", "NIFTY_FIN_SERVICE"]
    cmd_indicators(args)
    print("\n===== ANALYTICS =====")
    cmd_analytics(args)
    print("\n===== REPORT =====")
    return cmd_report(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Technical indicators and market analytics")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sectors", help="assign sectors to stocks that have none")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_sectors)

    p = sub.add_parser("indicators", help="compute and store indicators")
    p.add_argument("--intervals", nargs="+", default=["1d"], choices=["1d", "1m", "5m", "15m", "30m", "1h"])
    p.add_argument("--symbols", nargs="+", help="only these symbols (default: all instruments)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_indicators)

    p = sub.add_parser("analytics", help="compute and store market analytics")
    p.set_defaults(func=cmd_analytics)

    p = sub.add_parser("report", help="print highlights from the stored analytics")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("all", help="sectors, indicators, analytics, report")
    p.set_defaults(func=cmd_all)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
