"""Scale benchmark (Phase 14): the same jobs at 100,000, 1 million, 5 million and 10 million records.

For each size the runner generates synthetic 1-minute bars (see synthetic.py), then measures:

  INGESTION    writing the bars to Parquet, and bulk-inserting them into a MongoDB time-series collection that has
               the same index as the real `intraday_prices`. The MongoDB figure includes building the documents
               from the table (as the importer does) but NOT the importer's validation and de-duplication.
  STORAGE      what the data occupies: MongoDB on disk (compressed, with its index) against Parquet.
  QUERIES      a selective one (one symbol, five trading days: MongoDB uses its index) and a full-scan filter.
  AGGREGATION  per symbol and day range statistics, and combining 1-minute bars into 5-minute bars.
  RESOURCES    peak memory and CPU cores used by each measurement (the Phase 8 sampler, including mongod).

The rules of the Phase 8 benchmark apply unchanged (benchmark.py): identical input (Parquet for pandas and Spark,
the collection for MongoDB), a fingerprint of every answer that must agree across engines, an untimed warm-up, the
median of repeated runs. Two additions for big data:

  * A job that cannot run is REPORTED, never hidden: an exception is recorded as "failed", and a job whose data
    cannot fit in the memory that is free is "skipped" with the numbers that led to that decision. Neither is
    stored as a timing (a zero-second timing would be taken for a win); both appear in `benchmark_runs`.
  * Everything is labelled synthetic (`is_synthetic: true`) and lives in its own database.
"""
import gc
import logging
import platform
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
import psutil
from pymongo.database import Database

from app.bigdata import benchmark as bm
from app.bigdata import export, mongo_jobs, pandas_jobs, synthetic
from app.core.config import get_settings
from app.db.mongo import get_client
from app.db.schemas import INTRADAY_PRICES, TIMESERIES_OPTIONS

logger = logging.getLogger(__name__)

SIZES = (100_000, 1_000_000, 5_000_000, 10_000_000)
COLLECTION = "synthetic_1m"
INGEST_MONGO = "ingest_mongodb_timeseries"
INGEST_PARQUET = "ingest_parquet"
AGG_RANGE = "agg_daily_range_stats"
AGG_RESAMPLE = "agg_resample_1m_to_5m"
QUERY_WINDOW = "query_symbol_window"
QUERY_SCAN = "query_scan_filter"
SCAN_MIN_CLOSE, SCAN_MIN_VOLUME = 1000.0, 3000.0          # the full-scan filter: close > 1000 and volume > 3000
WINDOW_BARS = 1875                                         # five trading days of 1-minute bars
# Working memory per row that Phase 8 measured for jobs that load all the data (pandas ~2 GB for 5 million rows).
BYTES_PER_ROW = {"pandas": 400, "pyspark": 700}
INSERT_BATCH = 50_000


# ----------------------------------------------------------------------------- places
def bench_database(name: Optional[str] = None) -> Database:
    """The database that holds the synthetic collection. Refuses the real database."""
    settings = get_settings()
    name = name or settings.bench_db_name
    if name == settings.mongo_db_name:
        raise ValueError(f"the benchmark database must not be the real database ({name})")
    return get_client()[name]


def dataset_dir(rows: int) -> Path:
    return get_settings().data_dir / "benchmark" / f"n{rows}"


def remove_dataset(base: Path) -> None:
    """Delete a benchmark Parquet folder, refusing anything outside the data folder."""
    root = get_settings().data_dir.resolve()
    if base.exists():
        if root not in base.resolve().parents:
            raise ValueError(f"refusing to delete {base}: it is outside {root}")
        shutil.rmtree(base)


def folder_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def environment() -> dict:
    """The machine and software the numbers came from (a timing means nothing without this)."""
    import pymongo
    memory = psutil.virtual_memory()
    env = {"cpu_cores": psutil.cpu_count(), "ram_gb": round(memory.total / 2 ** 30, 1),
           "free_ram_gb_at_start": round(memory.available / 2 ** 30, 1), "os": platform.platform(),
           "python": platform.python_version(), "pandas": pd.__version__, "numpy": np.__version__, "pymongo": pymongo.version}
    try:
        env["mongod"] = get_client().server_info()["version"]
    except Exception:                                      # noqa: BLE001 - informational only
        env["mongod"] = None
    try:
        import pyspark
        env["pyspark"] = pyspark.__version__
    except ImportError:
        env["pyspark"] = None
    return env


# ----------------------------------------------------------------------------- ingestion and storage
def create_collection(bench: Database) -> None:
    """A fresh time-series collection with the same options and index as the real `intraday_prices`."""
    bench.drop_collection(COLLECTION)
    bench.create_collection(COLLECTION, timeseries=TIMESERIES_OPTIONS)
    index = INTRADAY_PRICES.indexes[0]
    bench[COLLECTION].create_index(list(index.keys), name=index.name)


def ingest_mongodb(bench: Database, frames: list[pd.DataFrame], batch: int = INSERT_BATCH) -> dict:
    create_collection(bench)
    collection, total = bench[COLLECTION], 0
    for frame in frames:
        docs = synthetic.documents(frame)
        for i in range(0, len(docs), batch):
            collection.insert_many(docs[i:i + batch], ordered=False)
        total += len(docs)
    return {"rows": total}


def ingest_parquet(base: Path, frames: list[pd.DataFrame]) -> dict:
    root = export._fresh_dir(base / "intraday")
    total = 0
    for frame in frames:
        symbol = str(frame["symbol"].iloc[0])
        export._write(frame[["ts", *export.NUMERIC]], root / f"symbol={symbol}" / "interval=1m" / "part-0.parquet")
        total += len(frame)
    return {"rows": total}


def mongo_storage(bench: Database) -> dict:
    """Sizes after a checkpoint: data = uncompressed BSON, disk = the data file, free = space inside it that MongoDB can reuse,
    live = disk - free (what the data really occupies), index.

    `live` is the figure to compare, not `disk`: a bulk load longer than WiredTiger's 60-second background checkpoint leaves
    reusable free space in the file (measured: a 5-million-row load gave a 63 MB file holding 33 MB of live data, the same
    data loaded faster gave 33 MB), so the file size depends on timing while the live size does not."""
    try:
        bench.client.admin.command("fsync")
    except Exception:                                      # noqa: BLE001 - sizes are then possibly a little low
        logger.warning("fsync failed; storage sizes may be read before MongoDB's checkpoint")
    stats = bench.command("collStats", COLLECTION)
    mb = 2 ** 20
    disk, free = stats.get("storageSize", 0) / mb, stats.get("freeStorageSize", 0) / mb
    return {"data_mb": stats.get("size", 0) / mb, "disk_mb": disk, "free_mb": free, "live_mb": max(disk - free, 0.0),
            "index_mb": stats.get("totalIndexSize", 0) / mb}


# ----------------------------------------------------------------------------- tasks
@dataclass
class ScaleTask:
    name: str
    description: str
    records: int                    # rows in the dataset
    engines: dict                   # engine -> callable returning a fingerprint
    runs: int
    rate_basis: Optional[int]       # rows used for rows-per-second; None where it is meaningless (an indexed query)
    loads_all: bool                 # the job reads the whole dataset into memory (drives the memory check)


def query_window(frames: list[pd.DataFrame]) -> tuple[str, datetime, datetime]:
    """One symbol and a window of about five trading days inside its history."""
    frame = frames[min(6, len(frames) - 1)]
    first = len(frame) // 4
    last = min(first + WINDOW_BARS - 1, len(frame) - 1)
    return str(frame["symbol"].iloc[0]), frame["ts"].iloc[first].to_pydatetime(), frame["ts"].iloc[last].to_pydatetime()


def _pd_read(path: Path, symbols: Optional[list[str]] = None) -> pd.DataFrame:
    df = pd.read_parquet(path, filters=[("symbol", "in", symbols)] if symbols else None)
    df["symbol"], df["interval"] = df["symbol"].astype(str), df["interval"].astype(str)
    return df


def _fp_window(df: pd.DataFrame) -> dict:
    return {"bars": int(len(df)), "volume": float(df["volume"].sum()), "mean_close": float(df["close"].mean()) if len(df) else None}


def _fp_scan(df: pd.DataFrame) -> dict:
    hit = df[(df["close"] > SCAN_MIN_CLOSE) & (df["volume"] > SCAN_MIN_VOLUME)]
    return {"matches": int(len(hit)), "mean_close": float(hit["close"].mean()) if len(hit) else None}


def build_scale_tasks(bench: Database, base: Path, rows: int, symbols: list[str], window: tuple[str, datetime, datetime], *,
                      spark=None, runs: int = 3, query_runs: int = 5) -> list[ScaleTask]:
    path = base / "intraday"
    symbol, lo, hi = window

    def mongo_window() -> dict:
        found = list(bench[COLLECTION].find({"meta.symbol": symbol, "meta.exchange": "NSE", "meta.interval": "1m",
                                             "ts": {"$gte": lo, "$lte": hi}}, {"_id": 0, "close": 1, "volume": 1}))
        return {"bars": len(found), "volume": float(sum(r["volume"] for r in found)),
                "mean_close": float(np.mean([r["close"] for r in found])) if found else None}

    def mongo_scan() -> dict:
        found = list(bench[COLLECTION].aggregate([
            {"$match": {"close": {"$gt": SCAN_MIN_CLOSE}, "volume": {"$gt": SCAN_MIN_VOLUME}}},
            {"$group": {"_id": None, "n": {"$sum": 1}, "mean_close": {"$avg": "$close"}}}], allowDiskUse=True))
        return {"matches": int(found[0]["n"]), "mean_close": found[0]["mean_close"]} if found else {"matches": 0, "mean_close": None}

    tasks = [
        ScaleTask(QUERY_WINDOW, f"one symbol, {WINDOW_BARS:,} consecutive 1-minute bars (five trading days): MongoDB uses its index, "
                  "pandas and Spark read only that symbol's Parquet file", rows, {
                      "pandas": lambda: (lambda d: _fp_window(d[(d["ts"] >= lo) & (d["ts"] <= hi)]))(_pd_read(path, [symbol])),
                      "mongodb_aggregation": mongo_window}, query_runs, None, False),
        ScaleTask(QUERY_SCAN, f"count the bars with close > {SCAN_MIN_CLOSE:g} and volume > {SCAN_MIN_VOLUME:g} (no index can help: every bar is read)",
                  rows, {"pandas": lambda: _fp_scan(_pd_read(path)), "mongodb_aggregation": mongo_scan}, query_runs, rows, True),
        ScaleTask(AGG_RANGE, "bars, high, low, volume and mean close per symbol and day (every bar)", rows, {
            "pandas": lambda: (lambda r: bm.fp_range(len(r), r["bars"].sum(), r["volume"].sum()))(pandas_jobs.daily_range_stats(_pd_read(path))),
            "mongodb_aggregation": lambda: (lambda r: bm.fp_range(r["groups"], r["bars"], r["volume"]))(
                mongo_jobs.daily_range_stats(bench, collection=COLLECTION))}, runs, rows, True),
        ScaleTask(AGG_RESAMPLE, "combine 1-minute bars into 5-minute bars (every bar)", rows, {
            "pandas": lambda: bm.fp_bars(pandas_jobs.resample_bars(_pd_read(path), 5)),
            "mongodb_aggregation": lambda: mongo_jobs.resample_totals(bench, symbols, 5, collection=COLLECTION)}, runs, rows, True),
    ]
    if spark is not None:
        from pyspark.sql import functions as F

        from app.bigdata import spark_jobs as sj
        by_name = {t.name: t for t in tasks}

        def spark_all():
            return sj.read_intraday(spark, base, interval="1m")

        def spark_window() -> dict:
            df = sj.read_intraday(spark, base, interval="1m", symbols=[symbol]).filter((F.col("ts") >= F.lit(lo)) & (F.col("ts") <= F.lit(hi)))
            r = df.agg(F.count("*"), F.sum("volume"), F.avg("close")).collect()[0]
            return {"bars": int(r[0]), "volume": float(r[1] or 0.0), "mean_close": float(r[2]) if r[2] is not None else None}

        def spark_scan() -> dict:
            df = spark_all().filter((F.col("close") > SCAN_MIN_CLOSE) & (F.col("volume") > SCAN_MIN_VOLUME))
            r = df.agg(F.count("*"), F.avg("close")).collect()[0]
            return {"matches": int(r[0]), "mean_close": float(r[1]) if r[1] is not None else None}

        by_name[QUERY_WINDOW].engines["pyspark"] = spark_window
        by_name[QUERY_SCAN].engines["pyspark"] = spark_scan
        by_name[AGG_RANGE].engines["pyspark"] = lambda: (lambda r: bm.fp_range(r[0], r[1], r[2]))(
            sj.daily_range_stats(spark_all()).agg(F.count("*"), F.sum("bars"), F.sum("volume")).collect()[0])
        by_name[AGG_RESAMPLE].engines["pyspark"] = lambda: (lambda r: {"bars": int(r[0]), "volume": float(r[1]), "mean_close": float(r[2])})(
            sj.resample_bars(spark_all(), 5).agg(F.count("*"), F.sum("volume"), F.avg("close")).collect()[0])
    return tasks


# ----------------------------------------------------------------------------- running
def _free_mb() -> float:
    return psutil.virtual_memory().available / 2 ** 20


def _attempt(task: str, engine: str, description: str, records: int, fn: Callable[[], dict], *, runs: int, warmup: bool,
             rate_basis: Optional[int], include_mongod: bool, say: Callable[[str], None]) -> dict:
    """Measure one job on one engine. Any failure becomes a 'failed' outcome instead of ending the whole run."""
    say(f"  {task:<30} {engine:<20} ...")
    base = {"task": task, "engine": engine, "records": records, "description": description, "with_mongod": include_mongod}
    try:
        # Only Spark has child processes worth counting (its JVM); for the others a live Spark session would inflate the figure.
        m = bm.measure(fn, runs=runs, warmup=warmup, include_mongod=include_mongod, children=(engine == "pyspark"))
    except Exception as exc:                               # noqa: BLE001 - recorded, not hidden
        gc.collect()
        say(f"      FAILED: {type(exc).__name__}: {str(exc)[:160]}")
        return {**base, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    m.update(base, status="ok", records_per_second=(rate_basis / m["seconds"] if rate_basis and m["seconds"] else None))
    say(f"      {m['seconds']:.2f}s (runs {m['runs']})  {m['cpu_cores_used']:.1f} cores  {m['peak_mb']:.0f} MB")
    return m


def run_scale(rows: int, *, bench: Optional[Database] = None, spark=None, engines: Optional[list[str]] = None, runs: int = 3,
              query_runs: int = 5, symbols: int = synthetic.DEFAULT_SYMBOLS, seed: int = synthetic.DEFAULT_SEED,
              keep: bool = False, ingest_runs: Optional[int] = None, progress: Optional[Callable[[str], None]] = None) -> dict:
    """Generate `rows` synthetic bars and measure everything. Returns the run (outcomes, storage, environment); store it with store_run."""
    say = progress or (lambda _m: None)
    bench = bench if bench is not None else bench_database()          # (a pymongo Database cannot be truth-tested)
    base = dataset_dir(rows)
    env = environment()
    say(f"\n=== {rows:,} synthetic 1-minute bars, {symbols} symbols (free RAM {env['free_ram_gb_at_start']} GB) ===")
    started = time.perf_counter()
    frames = synthetic.generate(rows, symbols, seed)
    generate_seconds = time.perf_counter() - started
    say(f"generated in {generate_seconds:.1f}s (not part of any timing)")
    symbol_list, window = [str(f["symbol"].iloc[0]) for f in frames], query_window(frames)
    outcomes: list[dict] = []
    storage: dict = {}
    n_ingest = ingest_runs or (3 if rows <= 1_000_000 else 1)
    try:
        ingest_steps = (
                (INGEST_PARQUET, False, lambda: ingest_parquet(base, frames), "bulk write of every bar to Parquet (snappy)"),
                (INGEST_MONGO, True, lambda: ingest_mongodb(bench, frames),
                 "bulk insert of every bar into a time-series collection with the production index; includes building the documents, "
                 "not validation or de-duplication"))
        if engines and "mongodb_aggregation" not in engines:
            ingest_steps = ingest_steps[:1]            # MongoDB is not part of this run: do not load (and heat up) its server
        for op, with_mongod, fn, what in ingest_steps:
            out = _attempt(op, "ingestion", what, rows, fn, runs=n_ingest, warmup=False, rate_basis=rows, include_mongod=with_mongod, say=say)
            if out["status"] == "ok":
                if out["fingerprint"] != {"rows": rows}:
                    out.update(status="failed", error=f"wrote {out['fingerprint']['rows']} rows, expected {rows}")
                elif op == INGEST_PARQUET:
                    storage["parquet_mb"] = out["storage_mb"] = folder_bytes(base / "intraday") / 2 ** 20
                else:
                    storage.update(mongo_storage(bench))
                    out["storage_mb"] = storage["live_mb"] + storage["index_mb"]
            outcomes.append(out)
        frames = None                          # the table is no longer needed: free it so it does not sit in every later memory figure
        gc.collect()
        ingested_ok = all(o["status"] == "ok" for o in outcomes)
        if not ingested_ok:
            say("ingestion failed: the queries and aggregations need the data, so they are not run")
        for task in (build_scale_tasks(bench, base, rows, symbol_list, window, spark=spark, runs=runs, query_runs=query_runs) if ingested_ok else []):
            reference = None
            for engine in bm.ENGINES:
                fn = task.engines.get(engine)
                if fn is None or (engines and engine not in engines):
                    continue
                need = BYTES_PER_ROW.get(engine, 0) * rows / 2 ** 20 if task.loads_all else 0
                free = _free_mb()
                if need and free < need:
                    reason = f"not enough free memory: this job needs about {need / 1024:.1f} GB and {free / 1024:.1f} GB is free"
                    say(f"  {task.name:<30} {engine:<20} SKIPPED: {reason}")
                    outcomes.append({"task": task.name, "engine": engine, "records": rows, "description": task.description,
                                     "status": "skipped", "error": reason})
                    continue
                out = _attempt(task.name, engine, task.description, rows, fn, runs=task.runs, warmup=True, rate_basis=task.rate_basis,
                               include_mongod=(engine == "mongodb_aggregation"), say=say)
                if out["status"] == "ok":
                    if reference is None:
                        reference, out["matches_reference"] = out["fingerprint"], True
                    else:
                        out["matches_reference"] = bm.fingerprints_match(reference, out["fingerprint"])
                        if not out["matches_reference"]:
                            say("      DIFFERENT ANSWER")
                outcomes.append(out)
            done = [o for o in outcomes if o["task"] == task.name and o["status"] == "ok"]
            for o in done:
                o["answers_match"] = all(d["matches_reference"] for d in done)
            gc.collect()
    finally:
        frames = None
        gc.collect()
        if not keep:
            bench.drop_collection(COLLECTION)
            remove_dataset(base)
    return {"run_id": f"scale_{rows}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}", "records": rows,
            "environment": env, "storage": storage, "outcomes": outcomes, "generate_seconds": generate_seconds,
            "dataset": {"rows": rows, "symbols": symbols, "seed": seed, "interval": "1m", "bars_per_symbol": rows // symbols,
                        "source": synthetic.SYNTHETIC_SOURCE, "database": bench.name, "collection": COLLECTION},
            "config": {"runs": runs, "query_runs": query_runs, "ingest_runs": n_ingest, "engines": engines or list(bm.ENGINES)}}


# ----------------------------------------------------------------------------- storing
def result_documents(run: dict) -> list[dict]:
    """benchmark_results documents for the successful measurements of a run (always is_synthetic)."""
    now, env = datetime.now(timezone.utc), run["environment"]
    where = f"{env['cpu_cores']} cores, {env['ram_gb']} GB RAM"
    docs = []
    for r in run["outcomes"]:
        if r["status"] != "ok":
            continue
        doc = {"run_id": run["run_id"], "technology": r["engine"], "operation": r["task"], "record_count": int(r["records"]),
               "duration_seconds": float(r["seconds"]), "cpu_percent": float(r["cpu_cores_used"] * 100),
               "memory_mb": float(r["peak_mb"]), "is_synthetic": True, "created_at": now,
               "answers_match": bool(r.get("answers_match", True)),
               "notes": (f"SYNTHETIC data: {run['dataset']['rows']:,} one-minute bars, {run['dataset']['symbols']} generated symbols, seed "
                         f"{run['dataset']['seed']}. runs {r['runs']}s; min {r['min_seconds']:.2f}s max {r['max_seconds']:.2f}s; "
                         f"cpu_percent is percent of ONE core (can exceed 100); memory_mb is the peak resident memory of the whole "
                         f"process tree{' plus mongod' if r.get('with_mongod') else ''}, growth during the task "
                         f"+{r.get('growth_mb', 0):.0f} MB; {r['description']}. Machine: {where}.")}
        if r.get("records_per_second"):
            doc["records_per_second"] = float(r["records_per_second"])
        if r.get("storage_mb") is not None:
            doc["storage_mb"] = float(r["storage_mb"])
        docs.append(doc)
    return docs


def store_run(results_db: Database, run: dict) -> int:
    """Save a run: its timings in benchmark_results and its full account (including failures) in benchmark_runs."""
    docs = result_documents(run)
    if docs:
        results_db["benchmark_results"].insert_many(docs)
    results_db["benchmark_runs"].insert_one({
        "run_id": run["run_id"], "records": int(run["records"]), "is_synthetic": True, "dataset": run["dataset"],
        "environment": run["environment"], "config": run["config"],
        "storage": {k: round(v, 2) for k, v in run["storage"].items()}, "generate_seconds": round(run["generate_seconds"], 2),
        "outcomes": [{"operation": o["task"], "technology": o["engine"], "status": o["status"],
                      **({"seconds": round(o["seconds"], 3)} if o["status"] == "ok" else {"error": o["error"]})} for o in run["outcomes"]],
        "created_at": datetime.now(timezone.utc)})
    return len(docs)


def format_run(run: dict) -> str:
    # rows per second is None for the indexed query (it does not read "rows"); the shared table formatter wants a number
    ok = [{**r, "records_per_second": r["records_per_second"] or 0.0} for r in run["outcomes"] if r["status"] == "ok" and r["engine"] != "ingestion"]
    lines = [bm.format_table(ok)] if ok else []
    for r in (r for r in run["outcomes"] if r["engine"] == "ingestion" and r["status"] == "ok"):
        lines.append(f"\n{r['task']}: {r['seconds']:.2f}s, {r['records_per_second']:,.0f} rows/s, {r['peak_mb']:.0f} MB peak, "
                     f"{r['storage_mb']:.1f} MB stored ({r['storage_mb'] * 2 ** 20 / r['records']:.1f} bytes per row)")
    lines += [f"\n{r['task']} on {r['engine']}: {r['status'].upper()} - {r['error']}" for r in run["outcomes"] if r["status"] != "ok"]
    return "\n".join(lines)
