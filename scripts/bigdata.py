"""Big-data processing with PySpark, and the engine benchmark (Phase 8).

Run from the project root. In order:

    .venv\\Scripts\\python scripts\\bigdata.py check                    # is Spark installed and does it start?
    .venv\\Scripts\\python scripts\\bigdata.py export                   # MongoDB -> Parquet (data/processed/parquet)
    .venv\\Scripts\\python scripts\\bigdata.py analytics                # Spark jobs -> analytics_results
    .venv\\Scripts\\python scripts\\bigdata.py benchmark                # pandas vs MongoDB vs Spark -> benchmark_results
    .venv\\Scripts\\python scripts\\bigdata.py benchmark --engines pandas mongodb_aggregation --only daily
    .venv\\Scripts\\python scripts\\bigdata.py report                   # show the latest benchmark

Needs `pip install pyspark pyarrow` (and JDK 17 via JAVA_HOME in .env). Safe to re-run.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.bigdata import benchmark, export, spark_session  # noqa: E402
from app.db.mongo import get_db  # noqa: E402
from app.processing.jobs import tracked_job  # noqa: E402


def _spark_or_exit():
    status = spark_session.spark_status()
    if status["problems"]:
        print("Spark is not ready:\n  - " + "\n  - ".join(status["problems"]))
        raise SystemExit(2)
    return spark_session.get_spark()


def cmd_check(args) -> int:
    status = spark_session.spark_status()
    print(f"PySpark {status['pyspark']}, PyArrow {status['pyarrow']}, JAVA_HOME {status['java_home']} (found: {status['java_ok']})")
    if status["problems"]:
        print("Not ready:\n  - " + "\n  - ".join(status["problems"]))
        return 2
    t0 = time.perf_counter()
    spark = spark_session.get_spark()
    started = time.perf_counter() - t0
    t1 = time.perf_counter()
    total = spark.range(1, 10_000_001).selectExpr("sum(id)").collect()[0][0]
    print(f"Spark {spark.version} started in {started:.1f}s; summed 10 million numbers in {time.perf_counter() - t1:.1f}s "
          f"(= {total:,}); {spark.sparkContext.defaultParallelism} parallel tasks")
    return 0 if total == 50_000_005_000_000 else 1


def cmd_export(args) -> int:
    db = get_db()
    base = export.parquet_dir()
    with tracked_job(db, "parquet_export", job_type="spark") as job:
        counts = {}
        if args.what in ("all", "daily"):
            r = export.export_daily(db)
            print(f"daily:    {r['rows']:>10,} rows, {r['mb']} MB, {r['seconds']}s")
            counts["daily_rows"] = r["rows"]
        if args.what in ("all", "intraday"):
            r = export.export_intraday(db, intervals=args.intervals, progress=print)
            print(f"intraday: {r['rows']:>10,} rows in {r['files']} files, {r['mb']} MB, {r['seconds']}s")
            counts["intraday_rows"] = r["rows"]
        job["counts"] = counts
    print(f"\nParquet folder: {base}  ({export.folder_size_mb(base)} MB)")
    return 0


def cmd_analytics(args) -> int:
    from app.bigdata.spark_analytics import run_spark_analytics
    db = get_db()
    spark = _spark_or_exit()
    print(f"Running Spark jobs on {spark.sparkContext.defaultParallelism} cores...")
    t0 = time.perf_counter()
    with tracked_job(db, "spark_analytics", job_type="spark") as job:
        out = run_spark_analytics(db, spark, export.parquet_dir(), progress=print)
        job["counts"] = {"analyses_stored": out["stored"]}
    r = out["results"]
    cc = r["spark_returns_volatility"]["cross_check_vs_indicators"]
    rv = r["spark_resample_validation"]
    print(f"\n{out['stored']} analyses stored in {time.perf_counter() - t0:.0f}s")
    print(f"Check 1: Spark volatility vs the Phase 7 indicators: {cc['compared']} symbols compared, max difference {cc['max_abs_difference']}")
    print(f"Check 2: 1-minute bars rebuilt as 5-minute bars vs the source's 5-minute bars ({rv['symbol']}): "
          f"{rv['timestamps_in_both']:,} bars in both, close identical {rv['close_identical_pct']}%, "
          f"OHLC identical {rv['ohlc_identical_pct']}%, only in rebuilt {rv['only_in_rebuilt']}, only in source {rv['only_in_source']}")
    t = r["spark_market_trend"]
    print(f"Market trend: {t['latest_pct_above_sma200']}% of {t['latest_stocks']} stocks above their 200-day average on {t['last_date']}")
    return 0


def cmd_benchmark(args) -> int:
    db = get_db()
    base = export.parquet_dir()
    if not (base / "daily").exists() or not (base / "intraday").exists():
        print("No Parquet data yet: run `export` first.")
        return 1
    want_spark = not args.engines or "pyspark" in args.engines
    spark, startup, extra = None, None, ""
    if want_spark:
        status = spark_session.spark_status()
        if status["problems"]:
            print("Spark is not ready, so it is left out of the comparison:\n  - " + "\n  - ".join(status["problems"]))
        else:
            t0 = time.perf_counter()
            spark = spark_session.get_spark()
            spark.range(1000).count()
            startup = time.perf_counter() - t0
            extra = f"Spark session start-up (one-off, not in the timings): {startup:.1f}s."
            print(f"Spark {spark.version} ready ({startup:.1f}s start-up, reported separately)")
            # On Windows the Parquet data reaches Spark through PyArrow (see spark_jobs.read_daily); that transfer
            # is INSIDE every pyspark timing, so measure it once here and report it next to the results.
            from app.bigdata import spark_jobs as sj
            t0 = time.perf_counter()
            n_daily = sj.read_daily(spark, base).count()
            daily_s = time.perf_counter() - t0
            t0 = time.perf_counter()
            n_intra = sj.read_intraday(spark, base).count()
            intra_s = time.perf_counter() - t0
            extra += (f" Loading Parquet into Spark through PyArrow (included in every pyspark timing; "
                      f"on Linux Spark reads Parquet directly): daily {n_daily:,} rows {daily_s:.1f}s, "
                      f"intraday {n_intra:,} rows {intra_s:.1f}s.")
            print(f"Arrow transfer into Spark (inside the pyspark timings): daily {daily_s:.1f}s, all intraday {intra_s:.1f}s")
    tasks = benchmark.build_tasks(db, base, spark=spark, include_scaling=not args.no_scaling)
    print(f"Benchmark: {args.runs} timed runs per engine after one warm-up run\n")
    with tracked_job(db, "benchmark", {"runs": args.runs}, job_type="benchmark") as job:
        results = benchmark.run_tasks(tasks, engines=args.engines, runs=args.runs, warmup=not args.no_warmup,
                                      only=args.only, progress=print)
        run_id = benchmark.store_results(db, results, notes_extra=extra)
        job["counts"] = {"measurements": len(results)}
    print(benchmark.format_table(results))
    bad = [r for r in results if not r["matches_reference"]]
    print(f"\nrun_id {run_id}: {len(results)} measurements stored in benchmark_results"
          + (f"; WARNING {len(bad)} gave a different answer" if bad else "; every engine gave the same answer"))
    return 1 if bad else 0


def cmd_report(args) -> int:
    db = get_db()
    last = db["benchmark_results"].find_one({"technology": {"$in": list(benchmark.ENGINES)}}, sort=[("created_at", -1)])
    if not last:
        print("No benchmark results yet.")
        return 1
    rows = list(db["benchmark_results"].find({"run_id": last["run_id"]}, {"_id": 0}))
    print(f"run {last['run_id']}  ({last['created_at']:%Y-%m-%d %H:%M} UTC)\n")
    results = [{"task": r["operation"], "engine": r["technology"], "records": r["record_count"], "seconds": r["duration_seconds"],
                "min_seconds": r["duration_seconds"], "max_seconds": r["duration_seconds"], "records_per_second": r["records_per_second"],
                "cpu_cores_used": r["cpu_percent"] / 100, "peak_mb": r["memory_mb"], "description": "",
                "matches_reference": "DIFFERENT" not in r["notes"]} for r in rows]
    print(benchmark.format_table(results))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Big-data processing and the engine benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="is Spark installed and does it start?").set_defaults(func=cmd_check)

    p = sub.add_parser("export", help="MongoDB -> Parquet")
    p.add_argument("what", nargs="?", default="all", choices=["all", "daily", "intraday"])
    p.add_argument("--intervals", nargs="+", help="only these intraday intervals, e.g. 1m 5m")
    p.set_defaults(func=cmd_export)

    sub.add_parser("analytics", help="run the Spark jobs and store the results").set_defaults(func=cmd_analytics)

    p = sub.add_parser("benchmark", help="compare pandas, MongoDB and Spark")
    p.add_argument("--runs", type=int, default=3, help="timed runs per engine (default 3)")
    p.add_argument("--engines", nargs="+", choices=list(benchmark.ENGINES))
    p.add_argument("--only", nargs="+", help="only tasks whose name starts with this, e.g. daily intraday_resample")
    p.add_argument("--no-warmup", action="store_true")
    p.add_argument("--no-scaling", action="store_true", help="skip the 1 and 2 index resample sizes")
    p.set_defaults(func=cmd_benchmark)

    sub.add_parser("report", help="show the latest benchmark").set_defaults(func=cmd_report)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
