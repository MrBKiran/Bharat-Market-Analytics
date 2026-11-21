"""Import downloaded files into MongoDB (Phase 5).

Run from the project root. Examples:

    .venv\\Scripts\\python scripts\\import_data.py import data\\raw\\yfinance --dry-run
    .venv\\Scripts\\python scripts\\import_data.py import data\\raw\\yfinance
    .venv\\Scripts\\python scripts\\import_data.py import data\\raw\\kaggle --auto-register
    .venv\\Scripts\\python scripts\\import_data.py import "my prices.csv" --format generic --symbol TCS --interval 1d
    .venv\\Scripts\\python scripts\\import_data.py jobs
    .venv\\Scripts\\python scripts\\import_data.py summary

Re-running is safe: finished files are skipped, interrupted ones resume, and no bar is stored twice.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.collection.adapters import FORMATS, expand_paths  # noqa: E402
from app.collection.importer import import_file, recent_jobs  # noqa: E402
from app.db.mongo import get_db  # noqa: E402


def _bar(event: dict) -> None:
    total = event["total"]
    where = f"{event['read']:,}/{total:,}" if total else f"{event['read']:,}"
    print(f"\r    {where} rows  {event['percent']:5.1f}%  {event['rows_per_second']:,} rows/s   ", end="", flush=True)


def cmd_import(args) -> int:
    db = get_db()
    files = expand_paths([Path(p) for p in args.paths])
    if not files:
        print("No importable files (.csv / .xlsx) found.")
        return 1
    overrides = {"format": args.format, "symbol": args.symbol, "exchange": args.exchange, "interval": args.interval,
                 "instrument_type": args.instrument_type, "tz": args.tz, "source": args.source,
                 "price_basis": args.price_basis}
    mode = "DRY RUN (nothing is written)" if args.dry_run else "importing"
    print(f"{mode}: {len(files)} file(s)\n")

    totals = {"read": 0, "inserted": 0, "duplicates": 0, "rejected": 0}
    statuses: dict[str, int] = {}
    for n, f in enumerate(files, 1):
        print(f"[{n}/{len(files)}] {f.name}")
        res = import_file(db, f, overrides=overrides, auto_register=args.auto_register,
                          chunk_size=args.chunk_size, dry_run=args.dry_run, force=args.force, progress=_bar)
        statuses[res["status"]] = statuses.get(res["status"], 0) + 1
        print()
        c = res.get("counts")
        if res["status"] in ("completed", "dry_run"):
            print(f"    {res['status']}: {c['read']:,} read, {c['inserted']:,} {'valid' if args.dry_run else 'inserted'}, "
                  f"{c['duplicates']:,} duplicates, {c['rejected']:,} rejected  "
                  f"({res['duration_seconds']}s, {res['rows_per_second']:,} rows/s) -> {res['collection']}")
            for reason, count in sorted(res.get("reasons", {}).items()):
                print(f"      rejected {count:,} x {reason}")
            if c.get("optional_fields_dropped"):
                print(f"      kept the bar but dropped {c['optional_fields_dropped']:,} invalid optional value(s)")
            for k in totals:
                totals[k] += c[k]
        elif res["status"] == "failed":
            print(f"    FAILED: {res['error']}\n    (run the same command again to resume)")
        else:
            print(f"    {res['status']}: {res.get('reason', '')}")
    print(f"\nFiles: {', '.join(f'{v} {k}' for k, v in sorted(statuses.items()))}")
    print(f"Rows: {totals['read']:,} read, {totals['inserted']:,} {'valid' if args.dry_run else 'inserted'}, "
          f"{totals['duplicates']:,} duplicates, {totals['rejected']:,} rejected")
    return 1 if statuses.get("failed") or statuses.get("error") else 0


def cmd_jobs(args) -> int:
    jobs = recent_jobs(get_db(), args.limit)
    if not jobs:
        print("No import jobs yet.")
        return 0
    print(f"{'job_id':<26} {'status':<10} {'progress':>8}  {'inserted':>10}  {'rejected':>8}  file")
    for j in jobs:
        c = j.get("counts", {})
        print(f"{j['job_id']:<26} {j['status']:<10} {j.get('progress', 0):>7.1f}%  {c.get('inserted', 0):>10,}  "
              f"{c.get('rejected', 0):>8,}  {Path(j.get('params', {}).get('path', '')).name}")
    return 0


def cmd_summary(args) -> int:
    db = get_db()
    print("Documents per collection:")
    for name in ("stock_prices", "market_indices", "intraday_prices", "instruments"):
        print(f"  {name:<18} {db[name].estimated_document_count():>12,}")
    print("\nBy source and interval (stock_prices + market_indices):")
    for coll in ("stock_prices", "market_indices"):
        for r in db[coll].aggregate([{"$group": {"_id": {"source": "$source", "interval": "$interval"},
                                                 "n": {"$sum": 1}}}, {"$sort": {"_id": 1}}]):
            print(f"  {coll:<15} {r['_id']['source']:<10} {r['_id']['interval']:<4} {r['n']:>10,}")
    print("\nIntraday coverage:")
    for r in db["intraday_coverage"].find({}, {"_id": 0}).sort([("symbol", 1), ("interval", 1)]):
        print(f"  {r['symbol']:<20} {r['interval']:<4} {r['source']:<8} {r['row_count']:>10,}  "
              f"{r['first_ts']:%Y-%m-%d} -> {r['last_ts']:%Y-%m-%d}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import data files into MongoDB")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="import files or folders")
    p.add_argument("paths", nargs="+", help="files or folders (.csv / .xlsx)")
    p.add_argument("--format", choices=FORMATS, default="auto")
    p.add_argument("--auto-register", action="store_true", help="add unknown instruments to the instruments collection")
    p.add_argument("--dry-run", action="store_true", help="parse and validate only; write nothing")
    p.add_argument("--chunk-size", type=int, default=100_000)
    p.add_argument("--force", action="store_true",
                   help="re-process files already imported (safe: stored bars are not duplicated)")
    g = p.add_argument_group("for generic files (CSV/Excel you made yourself)")
    g.add_argument("--symbol")
    g.add_argument("--exchange", choices=["NSE", "BSE"])
    g.add_argument("--interval", help="1d, 1w, 1M, 1m, 5m, 15m, 30m or 1h")
    g.add_argument("--instrument-type", choices=["equity", "etf", "index"])
    g.add_argument("--tz", help="time zone of timestamps without an offset (default Asia/Kolkata)")
    g.add_argument("--source", help="market_sources id (default manual_upload)")
    g.add_argument("--price-basis", choices=["raw", "split_adjusted", "index_level"])
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("jobs", help="show recent import jobs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("summary", help="what is in the database now")
    p.set_defaults(func=cmd_summary)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
