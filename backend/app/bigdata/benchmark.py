"""Benchmark pandas, MongoDB aggregation and PySpark on the same jobs and the same data.

Rules that keep the comparison honest:
  * SAME INPUT   pandas and Spark both read the same Parquet files; MongoDB computes inside the database.
  * SAME ANSWER  every engine returns a small "fingerprint" of its result (counts and means). A timing only
                 counts if the fingerprints agree; disagreements are reported, never hidden.
  * WARM-UP      one untimed run first (JVM start, caches), then `runs` timed runs; the MEDIAN is reported with
                 the minimum and maximum. Spark's one-off start-up is measured separately by the caller.
  * RESOURCES    peak memory and CPU time are sampled for this process, its children (Spark's JVM) and, for the
                 MongoDB engine, the mongod server. Memory is NOT comparable across engines (mongod caches data
                 permanently; Spark reserves a heap), so read it as "what the engine needed", not "who is lighter".
  * MEANINGFUL   a job is only run on an engine that can express it (MongoDB has no correlation operator).

Results go to `benchmark_results` with is_synthetic = false (real market data).
"""
import math
import statistics
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import psutil
from pymongo.database import Database

from app.bigdata import mongo_jobs, pandas_jobs

ENGINES = ("pandas", "mongodb_aggregation", "pyspark")
INDEX_SYMBOLS = ["NIFTY_50", "NIFTY_BANK", "NIFTY_IT", "NIFTY_FIN_SERVICE"]


# ----------------------------------------------------------------------------- measuring
class ResourceSampler:
    """Samples memory (peak resident set) and CPU seconds of this process tree while a block runs."""

    def __init__(self, include_mongod: bool = False, every: float = 0.1):
        self.include_mongod, self.every = include_mongod, every
        self.peak_rss = 0
        self.baseline_rss = None          # resident memory when the block started
        self._first: dict = {}
        self._last: dict = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _processes(self) -> list:
        me = psutil.Process()
        procs = [me, *me.children(recursive=True)]
        if self.include_mongod:
            procs += [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("mongod")]
        return procs

    def _sample(self) -> None:
        total = 0
        for p in self._processes():
            try:
                total += p.memory_info().rss
                t = p.cpu_times()
                cpu = t.user + t.system
                self._first.setdefault(p.pid, cpu)
                self._last[p.pid] = cpu
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self.peak_rss = max(self.peak_rss, total)
        if self.baseline_rss is None:
            self.baseline_rss = total

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            self._sample()

    def __enter__(self):
        self._sample()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self._sample()

    @property
    def peak_mb(self) -> float:
        return self.peak_rss / 2 ** 20

    @property
    def growth_mb(self) -> float:
        """How much resident memory grew during the block (the peak minus what was already in use)."""
        return max(self.peak_rss - (self.baseline_rss or 0), 0) / 2 ** 20

    @property
    def cpu_seconds(self) -> float:
        return sum(self._last[pid] - self._first[pid] for pid in self._last)


def measure(fn: Callable[[], dict], *, runs: int = 3, warmup: bool = True, include_mongod: bool = False) -> dict:
    """Time `fn` (which returns a fingerprint). One untimed warm-up, then `runs` timed runs."""
    first_run = None
    if warmup:
        t0 = time.perf_counter()
        fn()
        first_run = time.perf_counter() - t0
    samples, fingerprint = [], None
    for _ in range(runs):
        with ResourceSampler(include_mongod) as sampler:
            t0 = time.perf_counter()
            fingerprint = fn()
            seconds = time.perf_counter() - t0
        samples.append({"seconds": seconds, "cpu_seconds": sampler.cpu_seconds, "peak_mb": sampler.peak_mb,
                        "growth_mb": sampler.growth_mb})
    ordered = sorted(samples, key=lambda s: s["seconds"])
    median = ordered[len(ordered) // 2]
    return {"seconds": median["seconds"], "min_seconds": ordered[0]["seconds"], "max_seconds": ordered[-1]["seconds"],
            "runs": [round(s["seconds"], 3) for s in samples], "cpu_cores_used": median["cpu_seconds"] / median["seconds"],
            "peak_mb": max(s["peak_mb"] for s in samples), "growth_mb": max(s["growth_mb"] for s in samples),
            "first_run_seconds": first_run, "fingerprint": fingerprint}


def fingerprints_match(a: dict, b: dict, rel: float = 1e-6, absolute: float = 1e-9) -> bool:
    """Same keys; whole numbers exactly equal; other numbers equal within a tiny tolerance."""
    if set(a) != set(b):
        return False
    for key in a:
        x, y = a[key], b[key]
        if x is None or y is None or (isinstance(x, float) and math.isnan(x)) or (isinstance(y, float) and math.isnan(y)):
            if not (x is None or (isinstance(x, float) and math.isnan(x))) or not (y is None or (isinstance(y, float) and math.isnan(y))):
                return False
        elif isinstance(x, int) and isinstance(y, int):
            if x != y:
                return False
        elif not math.isclose(float(x), float(y), rel_tol=rel, abs_tol=absolute):
            return False
    return True


# ----------------------------------------------------------------------------- tasks
@dataclass
class Task:
    name: str
    description: str
    records: int                   # rows read by the job
    engines: dict                  # engine name -> callable returning a fingerprint


def fp_latest(df: pd.DataFrame) -> dict:
    return {"symbols": int(len(df)), "mean_ret": float(df["ret"].mean()), "mean_vol": float(df["hist_vol_20"].mean())}


def fp_range(groups: int, bars: int, volume: float) -> dict:
    return {"groups": int(groups), "bars": int(bars), "volume": float(volume)}


def fp_bars(df: pd.DataFrame) -> dict:
    return {"bars": int(len(df)), "volume": float(df["volume"].sum()), "mean_close": float(df["close"].mean())}


def fp_corr(corr: pd.DataFrame, n: int) -> dict:
    k = len(corr)
    return {"observations": int(n), "mean_offdiag": float((corr.values.sum() - sum(corr.values[i][i] for i in range(k))) / (k * k - k))}


def fp_trend(df: pd.DataFrame) -> dict:
    return {"days": int(len(df)), "mean_pct_above": float(df["pct_above_sma200"].mean())}


def fp_realised(df: pd.DataFrame) -> dict:
    return {"groups": int(len(df)), "mean_ann_vol": float((df["rv"] * pandas_jobs.TRADING_DAYS).pow(0.5).mul(100).mean())}


def current_universe(db: Database) -> tuple[list[str], pd.Timestamp, pd.Timestamp]:
    """(symbols with a price in the last 10 days, start of a 3-year window, newest date): the correlation universe."""
    last_dates = {r["_id"]: r["last"] for r in db["adjusted_prices"].aggregate(
        [{"$group": {"_id": "$symbol", "last": {"$max": "$date"}}}])}
    newest = max(last_dates.values())
    current = sorted(s for s, d in last_dates.items() if (newest - d).days <= 10)
    end = pd.Timestamp(newest)
    return current, end - pd.DateOffset(years=3), end


def build_tasks(db: Database, base: Path, *, spark=None, include_scaling: bool = True) -> list[Task]:
    """The benchmark jobs. Spark engines are added only when a Spark session is given."""
    base = Path(base)
    daily_path, intraday_path = base / "daily", base / "intraday"
    daily_rows = db["adjusted_prices"].estimated_document_count()
    intraday_rows = db["intraday_prices"].estimated_document_count()
    coverage = {(c["symbol"], c["interval"]): c["row_count"] for c in db["intraday_coverage"].find()}

    def pd_daily() -> pd.DataFrame:
        return pd.read_parquet(daily_path)

    def pd_intraday(interval: Optional[str] = None, symbols: Optional[list[str]] = None) -> pd.DataFrame:
        filters = ([("interval", "=", interval)] if interval else []) + ([("symbol", "in", symbols)] if symbols else [])
        df = pd.read_parquet(intraday_path, filters=filters or None)
        df["symbol"], df["interval"] = df["symbol"].astype(str), df["interval"].astype(str)
        return df

    current, start, end = current_universe(db)

    tasks = [
        Task("daily_returns_volatility", "return and 20-day volatility for every symbol, latest value per symbol (daily bars)",
             daily_rows, {
                 "pandas": lambda: fp_latest(pandas_jobs.latest_per_symbol(
                     pandas_jobs.returns_and_volatility(pd_daily()), ["ret", "hist_vol_20"])),
                 "mongodb_aggregation": lambda: fp_latest(pd.DataFrame(mongo_jobs.returns_volatility_latest(db))),
             }),
        Task("intraday_daily_range_stats", "bars, high, low, volume, mean close per symbol, interval and day (all intraday bars)",
             intraday_rows, {
                 "pandas": lambda: (lambda r: fp_range(len(r), r["bars"].sum(), r["volume"].sum()))(
                     pandas_jobs.daily_range_stats(pd_intraday())),
                 "mongodb_aggregation": lambda: (lambda r: fp_range(r["groups"], r["bars"], r["volume"]))(
                     mongo_jobs.daily_range_stats(db)),
             }),
        Task("return_correlation", f"correlation of daily returns, {len(current)} current symbols, 3 years",
             daily_rows, {
                 "pandas": lambda: fp_corr(*pandas_jobs.correlation_matrix(
                     pandas_jobs.returns_and_volatility(pd_daily()), current, start, end)),
             }),
        Task("market_trend_200dma", "share of stocks above their 200-day average, every day",
             daily_rows, {"pandas": lambda: fp_trend(pandas_jobs.market_trend(pd_daily()))}),
        Task("intraday_realised_volatility_5m", "realised variance per symbol and day from 5-minute bars",
             sum(v for (s, i), v in coverage.items() if i == "5m"), {
                 "pandas": lambda: fp_realised(pandas_jobs.intraday_realised_variance(pd_intraday("5m"))),
             }),
    ]
    sizes = [1, 2, 4] if include_scaling else [4]
    for k in sizes:
        syms = INDEX_SYMBOLS[:k]
        tasks.append(Task(f"intraday_resample_1m_to_5m[{k}_indices]", f"combine 1-minute bars into 5-minute bars, {k} index series",
                          sum(v for (s, i), v in coverage.items() if i == "1m" and s in syms), {
                              "pandas": (lambda syms=syms: fp_bars(pandas_jobs.resample_bars(pd_intraday("1m", syms), 5))),
                              "mongodb_aggregation": (lambda syms=syms: mongo_jobs.resample_totals(db, syms, 5)),
                          }))

    if spark is not None:
        from pyspark.sql import functions as F

        from app.bigdata import spark_jobs as sj
        by_name = {t.name: t for t in tasks}

        def spark_daily():
            return sj.read_daily(spark, base)

        by_name["daily_returns_volatility"].engines["pyspark"] = lambda: fp_latest(
            sj.latest_per_symbol(sj.with_volatility(sj.with_returns(spark_daily()))).toPandas())
        by_name["intraday_daily_range_stats"].engines["pyspark"] = lambda: (lambda r: fp_range(r[0], r[1], r[2]))(
            sj.daily_range_stats(sj.read_intraday(spark, base)).agg(
                F.count("*"), F.sum("bars"), F.sum("volume")).collect()[0])
        by_name["return_correlation"].engines["pyspark"] = lambda: fp_corr(*sj.correlation_matrix(
            sj.with_returns(spark_daily()), current, start, end))
        by_name["market_trend_200dma"].engines["pyspark"] = lambda: fp_trend(sj.market_trend(spark_daily()).toPandas())
        by_name["intraday_realised_volatility_5m"].engines["pyspark"] = lambda: fp_realised(
            sj.intraday_realised_variance(sj.read_intraday(spark, base, interval="5m")).toPandas())
        for k in sizes:
            syms = INDEX_SYMBOLS[:k]
            by_name[f"intraday_resample_1m_to_5m[{k}_indices]"].engines["pyspark"] = (lambda syms=syms: (lambda r: {
                "bars": int(r[0]), "volume": float(r[1]), "mean_close": float(r[2])})(
                sj.resample_bars(sj.read_intraday(spark, base, interval="1m", symbols=syms), 5).agg(
                    F.count("*"), F.sum("volume"), F.avg("close")).collect()[0]))
    return tasks


# ----------------------------------------------------------------------------- running and storing
def run_tasks(tasks: list[Task], *, engines: Optional[list[str]] = None, runs: int = 3, warmup: bool = True,
              only: Optional[list[str]] = None, progress: Optional[Callable[[str], None]] = None) -> list[dict]:
    say = progress or (lambda _m: None)
    results = []
    for task in tasks:
        if only and not any(task.name.startswith(o) for o in only):
            continue
        reference = None
        for engine in ENGINES:
            fn = task.engines.get(engine)
            if fn is None or (engines and engine not in engines):
                continue
            say(f"  {task.name:<42} {engine:<20} ...")
            m = measure(fn, runs=runs, warmup=warmup, include_mongod=(engine == "mongodb_aggregation"))
            if reference is None:
                reference, m["matches_reference"] = m["fingerprint"], True
            else:
                m["matches_reference"] = fingerprints_match(reference, m["fingerprint"])
            m.update(task=task.name, engine=engine, records=task.records, description=task.description,
                     records_per_second=task.records / m["seconds"] if m["seconds"] else 0.0)
            results.append(m)
            say(f"      {m['seconds']:.2f}s (runs {m['runs']})  {m['cpu_cores_used']:.1f} cores  {m['peak_mb']:.0f} MB  "
                f"{'same answer' if m['matches_reference'] else 'DIFFERENT ANSWER'}")
    return results


def store_results(db: Database, results: list[dict], *, run_id: Optional[str] = None, notes_extra: str = "") -> str:
    run_id = run_id or f"bench_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)
    docs = [{"run_id": run_id, "technology": r["engine"], "operation": r["task"], "record_count": int(r["records"]),
             "duration_seconds": float(r["seconds"]), "records_per_second": float(r["records_per_second"]),
             "cpu_percent": float(r["cpu_cores_used"] * 100), "memory_mb": float(r["peak_mb"]), "is_synthetic": False,
             "notes": (f"runs {r['runs']}s; min {r['min_seconds']:.2f}s max {r['max_seconds']:.2f}s; "
                       f"cpu_percent is percent of ONE core (can exceed 100); memory_mb is the peak resident memory of the "
                       f"whole process tree (earlier tasks' leftovers included), growth during the task +{r.get('growth_mb', 0):.0f} MB; "
                       f"{'same answer as the first engine' if r['matches_reference'] else 'DIFFERENT ANSWER'}; "
                       f"{r['description']}. {notes_extra}").strip(),
             "created_at": now} for r in results]
    if docs:
        db["benchmark_results"].insert_many(docs)
    return run_id


def format_table(results: list[dict]) -> str:
    lines = []
    for task in dict.fromkeys(r["task"] for r in results):
        rows = [r for r in results if r["task"] == task]
        fastest = min(r["seconds"] for r in rows)
        lines.append(f"\n{task}  ({rows[0]['records']:,} rows)  {rows[0]['description']}")
        lines.append(f"  {'engine':<22}{'median s':>10}{'min-max s':>14}{'rows/s':>14}{'cores':>8}{'peak MB':>9}{'growth':>8}  {'vs fastest':>10}  answer")
        for r in sorted(rows, key=lambda r: r["seconds"]):
            lines.append(f"  {r['engine']:<22}{r['seconds']:>10.2f}{r['min_seconds']:>8.2f}-{r['max_seconds']:<5.2f}"
                         f"{r['records_per_second']:>14,.0f}{r['cpu_cores_used']:>8.1f}{r['peak_mb']:>9.0f}{r.get('growth_mb', 0):>8.0f}  "
                         f"{r['seconds'] / fastest:>9.1f}x  {'same' if r['matches_reference'] else 'DIFFERENT'}")
    return "\n".join(lines)
