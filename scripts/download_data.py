"""Download market data into data/raw/ (Phase 4).

Run from the project root. Examples:

    .venv\\Scripts\\python scripts\\download_data.py verify
    .venv\\Scripts\\python scripts\\download_data.py yfinance --interval 1d
    .venv\\Scripts\\python scripts\\download_data.py yfinance --interval 5m --symbols NIFTY_50 RELIANCE
    .venv\\Scripts\\python scripts\\download_data.py kaggle-info debashis74017/nifty-50-minute-data
    .venv\\Scripts\\python scripts\\download_data.py kaggle-download debashis74017/nifty-50-minute-data
    .venv\\Scripts\\python scripts\\download_data.py inventory

NSE/BSE bhavcopy files are NOT downloaded by this script: NSE's terms forbid
automated collection. Download them by hand into data/raw/nse_bhavcopy/ (see docs/DATA_SOURCES.md).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.collection import kaggle_downloader as kg  # noqa: E402
from app.collection import yfinance_downloader as yfd  # noqa: E402
from app.collection.common import inventory  # noqa: E402
from app.db.mongo import get_db  # noqa: E402


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{n} B"


def cmd_verify(args) -> int:
    instruments = yfd.load_instruments(get_db(), args.types, args.symbols)
    print(f"Checking {len(instruments)} Yahoo tickers (5 days of daily data each, nothing saved)...\n")
    report = yfd.verify_tickers(instruments, pause=args.pause)
    for r in report:
        if r["ok"]:
            print(f"  OK     {r['symbol']:<20} {r['ticker']:<24} {r['rows']} rows, last {r['last_date']}")
        else:
            print(f"  FAIL   {r['symbol']:<20} {r['ticker']:<24} {r['error']}")
    bad = [r for r in report if not r["ok"]]
    print(f"\n{len(report) - len(bad)} OK, {len(bad)} failed.")
    return 1 if bad else 0


def cmd_yfinance(args) -> int:
    instruments = yfd.load_instruments(get_db(), args.types, args.symbols)
    if not instruments:
        print("No matching instruments found.")
        return 1
    print(f"Downloading {len(instruments)} instruments at interval {args.interval} from Yahoo Finance\n")
    results = yfd.download_many(instruments, args.interval, pause=args.pause, force=args.force,
                                start=args.start, end=args.end)
    done = [r for r in results if r["status"] == "downloaded"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]
    print(f"\nDownloaded {len(done)} ({sum(r['rows'] for r in done):,} rows), "
          f"skipped {len(skipped)} (already present), failed {len(failed)}.")
    return 1 if failed else 0


def cmd_kaggle_info(args) -> int:
    files = kg.list_files(args.dataset)
    print(f"Dataset {args.dataset}: {len(files)} files, {_human(sum(f['size_bytes'] for f in files))} total\n")
    for f in files:
        print(f"  {f['name']:<60} {_human(f['size_bytes']):>12}")
    print("\nNothing was downloaded. Check the dataset's license on its Kaggle page first.")
    return 0


def cmd_kaggle_download(args) -> int:
    listing = kg.list_files(args.dataset)
    if args.files:
        sizes = {f["name"]: f["size_bytes"] for f in listing}
        unknown = [n for n in args.files if n not in sizes]
        if unknown:
            raise ValueError(f"Not in {args.dataset}: {', '.join(unknown)}")
        chosen = [{"name": n, "size_bytes": sizes[n]} for n in args.files]
    else:
        chosen = listing
    total = sum(f["size_bytes"] for f in chosen)
    print(f"About to download from {args.dataset}: {len(chosen)} files, {_human(total)}")
    for f in chosen[:40]:
        print(f"  {f['name']:<55} {_human(f['size_bytes']):>12}")
    if len(chosen) > 40:
        print(f"  ... and {len(chosen) - 40} more")
    if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1
    result = kg.download_dataset(args.dataset, force=args.force, files=args.files)
    print(f"\nDone. License: {result['license']}\nSaved to: {result['directory']}")
    print(f"Downloaded {len(result['files'])} files; skipped {len(result['skipped'])} already present.")
    return 0


def cmd_inventory(args) -> int:
    rows = inventory()
    if not rows:
        print("No raw files with metadata yet.")
        return 0
    print(f"{'source':<10} {'file':<46} {'rows':>9} {'size':>10}  retrieved_at")
    for r in rows:
        print(f"{r.get('source', '?'):<10} {r['path']:<46} {r.get('rows', '-'):>9} "
              f"{_human(r.get('size_bytes', 0)):>10}  {r.get('retrieved_at', '?')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Download market data into data/raw/")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_filters(p):
        p.add_argument("--symbols", nargs="+", help="only these canonical symbols, e.g. RELIANCE NIFTY_50")
        p.add_argument("--types", nargs="+", choices=["equity", "index"], help="only these instrument types")
        p.add_argument("--pause", type=float, default=1.0, help="seconds between Yahoo requests (default 1)")

    p = sub.add_parser("verify", help="check every Yahoo ticker works (saves nothing)")
    add_filters(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("yfinance", help="download history from Yahoo Finance")
    add_filters(p)
    p.add_argument("--interval", default="1d", choices=sorted(yfd.INTERVAL_MAP), help="bar size (default 1d)")
    p.add_argument("--start", help="YYYY-MM-DD (default: the longest history Yahoo gives)")
    p.add_argument("--end", help="YYYY-MM-DD")
    p.add_argument("--force", action="store_true", help="re-download files that already exist")
    p.set_defaults(func=cmd_yfinance)

    p = sub.add_parser("kaggle-info", help="list a Kaggle dataset's files and sizes (no download)")
    p.add_argument("dataset", help="owner/dataset-name")
    p.set_defaults(func=cmd_kaggle_info)

    p = sub.add_parser("kaggle-download", help="download a Kaggle dataset (asks to confirm)")
    p.add_argument("dataset", help="owner/dataset-name")
    p.add_argument("--files", nargs="+", help="download only these file names (quote names with spaces)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_kaggle_download)

    p = sub.add_parser("inventory", help="list everything downloaded so far")
    p.set_defaults(func=cmd_inventory)

    args = parser.parse_args()
    try:
        return args.func(args)
    except kg.KaggleAuthError as exc:
        print(f"ERROR: {exc}")
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
