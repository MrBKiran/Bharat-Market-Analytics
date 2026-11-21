"""Clean and process the imported data (Phase 6).

Run from the project root, in this order (or just `all`):

    .venv\\Scripts\\python scripts\\process_data.py actions --dry-run   # preview corporate actions
    .venv\\Scripts\\python scripts\\process_data.py actions            # splits and dividends -> corporate_actions
    .venv\\Scripts\\python scripts\\process_data.py symbols            # symbol history and aliases -> instruments
    .venv\\Scripts\\python scripts\\process_data.py adjust             # build adjusted_prices
    .venv\\Scripts\\python scripts\\process_data.py audit              # data quality report
    .venv\\Scripts\\python scripts\\process_data.py all

Raw collections are never changed. Every step is safe to re-run.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.mongo import get_db  # noqa: E402
from app.processing import adjustment, corporate_actions, quality, symbols  # noqa: E402
from app.processing.jobs import tracked_job, write_report  # noqa: E402

KAGGLE_STOCKS = "kaggle/rohanrao__nifty50-stock-market-data"


def cmd_actions(args) -> int:
    db = get_db()
    yahoo_dir = get_settings().data_dir / "raw" / "yfinance" / "1d"
    if args.dry_run:
        s = corporate_actions.build_corporate_actions(db, yahoo_dir, dry_run=True)
    else:
        with tracked_job(db, "corporate_actions") as job:
            s = corporate_actions.build_corporate_actions(db, yahoo_dir)
            job["counts"] = {k: v for k, v in s.items() if isinstance(v, int)}
            write_report(db, "processed:corporate_actions", job["job_id"], {k: v for k, v in s.items() if k != "events"})
    print(("DRY RUN: nothing written\n" if args.dry_run else "") +
          f"Yahoo: {s['yahoo_files']} files, {s['yahoo_splits']} splits, {s['yahoo_dividends']} dividends")
    print(f"Price-jump detector: {s['stocks_scanned']} stocks scanned, {s['inferred_events']} split-like events")
    if not args.dry_run:
        print(f"  stored: {s['inferred_new']} new inferred, {s['inferred_confirmed_yahoo']} confirm a Yahoo split")
        print("\nDetector vs Yahoo (only Yahoo splits inside the raw data's date range):")
        for r in corporate_actions.reconcile_with_yahoo(db):
            print(f"  {r['symbol']:<11} yahoo {r['yahoo_splits_in_range']}  found {r['found_by_detector']}"
                  f"  missed {r['missed'] or '-'}  detector-only {r['detector_only'] or '-'}")
    if args.events:
        print("\nInferred events:")
        for e in s["events"]:
            print(f"  {e['symbol']:<11} {e['ex_date']}  factor {e['factor']:<6} observed {e['observed_ratio']}")
    return 0


def cmd_symbols(args) -> int:
    db = get_db()
    folder = get_settings().data_dir / "raw" / KAGGLE_STOCKS
    history = symbols.build_symbol_history(folder)
    with tracked_job(db, "symbols") as job:
        stored = symbols.store_symbol_history(db, history)
        indices = symbols.store_index_aliases(db)
        job["counts"] = {**stored, "index_aliases": indices}
    print(f"Instruments with an earlier ticker or alias: {len(history)} (updated {stored['instruments_updated']}, "
          f"not in instruments {stored['not_in_instruments']}); index alias sets: {indices}\n")
    for sym, info in sorted(history.items()):
        chain = " -> ".join(f"{e['symbol']} ({e['first_date']:%Y}-{e['last_date']:%Y})" for e in info["symbol_history"])
        note = "  [typo in source]" if any("note" in e for e in info["symbol_history"]) else ""
        print(f"  {sym:<11} {chain or '(alias only)'}{note}  {('aliases: ' + ', '.join(info['aliases'])) if info['aliases'] else ''}")
    for q in ("reliance.ns", "NSE:TCS", "UTIBANK", "MM", "banknifty", "^NSEI"):
        r = symbols.resolve_symbol(db, q)
        print(f"  resolve {q!r:<14} -> {r['symbol'] if r else None}")
    return 0


def cmd_adjust(args) -> int:
    db = get_db()
    if args.dry_run:
        s = adjustment.build_adjusted_prices(db, dry_run=True)
    else:
        with tracked_job(db, "adjust") as job:
            s = adjustment.build_adjusted_prices(db, job_id=job["job_id"])
            job["counts"] = {"rows": s["rows"], "instruments": s["instruments"], "skipped": len(s["skipped"])}
            write_report(db, "processed:adjusted_prices", job["job_id"],
                         {"rows": s["rows"], "by_method": s["by_method"], "flags": s["flags"], "skipped": s["skipped"],
                          "placeholder_rows_dropped": s["placeholder_rows_dropped"],
                          "phantom_rows_dropped": s["phantom_rows_dropped"],
                          "pre_listing_rows_dropped": s["pre_listing_rows_dropped"]})
    print(("DRY RUN: nothing written\n" if args.dry_run else "") +
          f"{s['instruments']} instruments, {s['rows']:,} adjusted rows, skipped: {s['skipped'] or 'none'}")
    print(f"By method: {s['by_method']}\nFlags:     {s['flags']}\n"
          f"Rows dropped as not real trading: {s['placeholder_rows_dropped']:,} leading placeholders, "
          f"{s['phantom_rows_dropped']:,} phantom (zero volume, >30% jump), {s['pre_listing_rows_dropped']:,} before a confirmed listing date")
    if args.verbose:
        print()
        for r in s["series"]:
            print(f"  {r['symbol']:<18} {r['rows']:>6,} rows  {r['first']} -> {r['last']}  {r['method']:<18} "
                  f"{'+'.join(r['source']):<14} splits {r['splits_applied']}  flags {r['flags'] or ''}")
    return 0


def cmd_audit(args) -> int:
    db = get_db()
    with tracked_job(db, "audit") as job:
        a = quality.run_audit(db)
        job["counts"] = {"duplicate_groups": sum(v for v in a["duplicates"].values() if isinstance(v, int))}
        write_report(db, "processed:audit", job["job_id"], a)
    d = a["duplicates"]
    print("DUPLICATES (all must be 0):", d)
    adj = a["adjusted"]
    print(f"\nPROCESSED DATA: {adj['rows']:,} rows; by method {adj['by_method']}; flags {adj['flags']}")
    print(f"  most return-outlier days: {adj['most_outlier_days'][:5]}")
    g = a["calendar_gaps"]
    print(f"\nCALENDAR GAPS vs {g.get('reference')}: {g.get('total_missing_days', 'n/a')} missing stock-days in total")
    for r in g.get("worst", [])[:5]:
        print(f"  {r['symbol']:<11} missing {r['missing_days']:>4} of {r['calendar_days']:>5} days ({r['missing_pct']}%)")
    print("\nINTRADAY COMPLETENESS:")
    for interval, r in a["intraday"].get("per_interval", {}).items():
        print(f"  {interval:<4} {r['series_days']:>6} series-days, {r['full_sessions_pct']}% full, "
              f"{r['short_days']} short, {r['overfull_days']} overfull")
    print("  shortest NIFTY 50 1m days:", a["intraday"].get("nifty50_1m_shortest_days", [])[:5])
    print(f"\nCORPORATE ACTIONS: {a['corporate_actions']['counts']}")
    print("\nOUR ADJUSTMENT vs YAHOO (days where daily returns differ by more than 1 point):")
    for r in a["cross_source_returns"]:
        print(f"  {r['symbol']:<11} {r['mismatch_days']:>3} of {r['overlap_days']:>5} days  {r['mismatch_dates'] or ''}")
    print("\nYAHOO HISTORY THAT STARTS BEFORE THE NSE RECORD (informational, nothing changed):")
    for r in a["series_starts"]:
        print(f"  {r['symbol']:<11} yahoo from {r['yahoo_first']}, NSE from {r['nse_first']} "
              f"({r['yahoo_rows_before_nse_start']} yahoo rows earlier)")
    return 0


def cmd_all(args) -> int:
    args.dry_run = False
    args.events = False
    args.verbose = False
    for name, fn in (("ACTIONS", cmd_actions), ("SYMBOLS", cmd_symbols), ("ADJUST", cmd_adjust), ("AUDIT", cmd_audit)):
        print(f"\n===== {name} =====")
        fn(args)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean and process the imported data")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("actions", help="splits and dividends -> corporate_actions")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--events", action="store_true", help="list every inferred event")
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser("symbols", help="earlier tickers and aliases -> instruments")
    p.set_defaults(func=cmd_symbols)

    p = sub.add_parser("adjust", help="rebuild adjusted_prices")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true", help="one line per instrument")
    p.set_defaults(func=cmd_adjust)

    p = sub.add_parser("audit", help="data quality audit")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("all", help="actions, symbols, adjust, audit")
    p.set_defaults(func=cmd_all)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
