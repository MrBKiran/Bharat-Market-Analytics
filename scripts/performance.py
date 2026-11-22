"""Big-data performance testing at 100,000 / 1 million / 5 million / 10 million records (Phase 14).

Run from the project root:

    .venv\\Scripts\\python scripts\\performance.py run                          # all four sizes, every engine
    .venv\\Scripts\\python scripts\\performance.py run --sizes 100000 1000000   # smaller sizes only
    .venv\\Scripts\\python scripts\\performance.py run --no-spark               # pandas and MongoDB only
    .venv\\Scripts\\python scripts\\performance.py report                       # the stored synthetic results, one table
    .venv\\Scripts\\python scripts\\performance.py clean                        # remove leftover synthetic data (add --results to also delete stored results)

The data is SYNTHETIC (see docs/PERFORMANCE.md): generated 1-minute bars in their own database (BharatMarketBenchmark),
labelled is_synthetic = true, never mixed with the real market data. The real-data benchmark is `scripts\\bigdata.py benchmark`.
Close other heavy programs first (browsers, the dev servers): the 5 and 10 million sizes need several GB of free memory,
and a job that does not fit is reported as skipped instead of being run.
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.bigdata import benchmark, scale_benchmark as sb, spark_session  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.mongo import get_client, get_db  # noqa: E402
from app.db.setup import setup_database  # noqa: E402


def _spark(args):
    if args.no_spark or (args.engines and "pyspark" not in args.engines):
        return None
    status = spark_session.spark_status()
    if status["problems"]:
        print("Spark is not ready, so it is left out:\n  - " + "\n  - ".join(status["problems"]))
        return None
    t0 = time.perf_counter()
    spark = spark_session.get_spark("bharatmarket-scale", driver_memory=args.driver_memory)
    spark.range(1000).count()
    print(f"Spark {spark.version} ready with a {args.driver_memory} driver ({time.perf_counter() - t0:.1f}s start-up, not in any timing)")
    return spark


def cmd_run(args) -> int:
    db = get_db()
    setup_database(db, seed=False)                    # makes sure benchmark_runs exists (idempotent, never drops data)
    sb.bench_database()                               # refuses the real database name
    spark = _spark(args)
    import psutil
    free = psutil.virtual_memory().available / 2 ** 30
    if free < 6:
        print(f"WARNING: only {free:.1f} GB of memory is free. Close other programs (browser, dev servers) before the 5M and 10M sizes; "
              f"jobs that do not fit are skipped and reported.")
    failures = 0
    try:
        for rows in args.sizes:
            run = sb.run_scale(rows, spark=spark, engines=args.engines, runs=args.runs, query_runs=args.query_runs,
                               symbols=args.symbols, seed=args.seed, keep=args.keep, progress=print)
            print(sb.format_run(run))
            bad = [o for o in run["outcomes"] if o["status"] == "ok" and o.get("answers_match") is False]
            failures += len(bad) + sum(1 for o in run["outcomes"] if o["status"] == "failed")
            if not args.no_store:
                stored = sb.store_run(db, run)
                print(f"\nrun {run['run_id']}: {stored} timings stored in benchmark_results (is_synthetic = true)"
                      + (f"; WARNING {len(bad)} job(s) gave different answers" if bad else ""))
    finally:
        spark_session.stop_spark()
    return 1 if failures else 0


def cmd_report(args) -> int:
    db = get_db()
    runs = list(db["benchmark_runs"].find({"is_synthetic": True}, {"_id": 0}).sort([("records", 1), ("created_at", -1)]))
    if not runs:
        print("No synthetic scale results yet: run `performance.py run` first.")
        return 1
    latest = {}
    for r in runs:
        latest.setdefault(r["records"], r)
    sizes = sorted(latest)
    cell = {}
    for size in sizes:
        for d in db["benchmark_results"].find({"run_id": latest[size]["run_id"]}, {"_id": 0}):
            cell[(d["operation"], d["technology"], size)] = d["duration_seconds"]
    keys = sorted({(op, tech) for op, tech, _ in cell})
    print("Synthetic data (seconds, median). '-' = not measured, skipped or failed.\n")
    print(f"{'operation':<30}{'technology':<22}" + "".join(f"{s:>12,}" for s in sizes))
    for op, tech in keys:
        print(f"{op:<30}{tech:<22}" + "".join(f"{cell[(op, tech, s)]:>12.2f}" if (op, tech, s) in cell else f"{'-':>12}" for s in sizes))
    for size in sizes:
        for o in latest[size]["outcomes"]:
            if o["status"] != "ok":
                print(f"\n{size:,} rows, {o['operation']} on {o['technology']}: {o['status'].upper()} - {o['error']}")
    return 0


def cmd_clean(args) -> int:
    settings = get_settings()
    client = get_client()
    name = settings.bench_db_name
    if name == settings.mongo_db_name:
        print("refusing: the benchmark database name equals the real database name")
        return 1
    client.drop_database(name)
    folder = settings.data_dir / "benchmark"
    if folder.exists():
        shutil.rmtree(folder)
    print(f"dropped database {name} and removed {folder}")
    if args.results:
        db = get_db()
        a = db["benchmark_results"].delete_many({"is_synthetic": True}).deleted_count
        b = db["benchmark_runs"].delete_many({"is_synthetic": True}).deleted_count
        print(f"deleted {a} synthetic timings and {b} run records (real-data benchmarks are untouched)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Big-data performance testing with synthetic data")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="generate synthetic data at each size and measure everything")
    p.add_argument("--sizes", type=int, nargs="+", default=list(sb.SIZES), help="record counts (default 100000 1000000 5000000 10000000)")
    p.add_argument("--engines", nargs="+", choices=list(benchmark.ENGINES))
    p.add_argument("--runs", type=int, default=3, help="timed runs per aggregation (default 3)")
    p.add_argument("--query-runs", type=int, default=5, help="timed runs per query (default 5)")
    p.add_argument("--symbols", type=int, default=50, help="generated symbols (default 50)")
    p.add_argument("--seed", type=int, default=14)
    p.add_argument("--driver-memory", default="4g", help="Spark driver memory (default 4g)")
    p.add_argument("--no-spark", action="store_true")
    p.add_argument("--no-store", action="store_true", help="measure and print, but do not save to MongoDB")
    p.add_argument("--keep", action="store_true", help="leave the synthetic collection and Parquet files after each size")
    p.set_defaults(func=cmd_run)
    sub.add_parser("report", help="show the stored synthetic results").set_defaults(func=cmd_report)
    p = sub.add_parser("clean", help="remove leftover synthetic data")
    p.add_argument("--results", action="store_true", help="also delete the stored synthetic timings")
    p.set_defaults(func=cmd_clean)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
