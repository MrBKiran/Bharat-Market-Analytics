# Big-data processing with PySpark (Phase 8)

The same jobs run on three engines (pandas, MongoDB aggregation, Apache Spark), their answers are checked against each
other, and they are timed. Spark also produces six analyses that are stored in `analytics_results`.

```powershell
.venv\Scripts\python scripts\bigdata.py check        # is Spark installed, and does it start?
.venv\Scripts\python scripts\bigdata.py export       # MongoDB -> Parquet (data\processed\parquet, git-ignored)
.venv\Scripts\python scripts\bigdata.py analytics    # 6 Spark analyses -> analytics_results (scope {"engine": "spark"})
.venv\Scripts\python scripts\bigdata.py benchmark    # pandas vs MongoDB vs Spark -> benchmark_results
.venv\Scripts\python scripts\bigdata.py report       # show the latest benchmark
```
Needs `pip install pyspark pyarrow` (in `requirements.txt`) and JDK 17 (`JAVA_HOME` in `.env`). Spark and PyArrow are optional:
nothing else in the project imports them.

## Layout

| Part | File |
|---|---|
| Spark session (local mode, UTC, ANSI off, Arrow on) | `backend/app/bigdata/spark_session.py` |
| MongoDB to Parquet | `export.py` |
| The jobs: pandas, MongoDB aggregation, Spark | `pandas_jobs.py`, `mongo_jobs.py`, `spark_jobs.py` |
| Spark analyses stored in MongoDB | `spark_analytics.py` |
| Benchmark harness | `benchmark.py` |

**Parquet.** `daily/part-0.parquet` (295,086 rows, 8 MB: adjusted prices joined with sector and instrument type) and
`intraday/symbol=S/interval=I/part-0.parquet` (5,392,391 rows in 16 files, 128 MB, Hive layout: the folder names become
columns). Timestamps are written with microsecond precision because Spark cannot read nanosecond Parquet timestamps.

**Spark settings, on purpose.** ANSI mode is off: Spark 4 defaults to ANSI mode, where a division by zero raises an error, and price
data has zero prices and volumes (we want NULL, like pandas' NaN). The session time zone is UTC (every timestamp in the database is UTC).

## The six jobs

| Job | What it computes | MongoDB can do it? |
|---|---|---|
| daily returns and volatility | return and 20-day historical volatility per symbol (window functions) | yes (`$setWindowFields`) |
| intraday daily range stats | bars, high, low, volume, mean close per symbol, interval and day | yes |
| return correlation | Pearson correlation of daily returns for the 16 current symbols, 3 years | no operator |
| market trend | share of stocks above their 200-day average, every day | not practical |
| intraday realised volatility | sum of squared 5-minute returns per day (returns never cross the overnight gap) | not practical |
| resample 1m to 5m | combine 1-minute bars into 5-minute bars | yes (`$dateTrunc`, `$top`, `$bottom`) |

## How the benchmark stays honest

- **Same input.** pandas and Spark both read the same Parquet files; MongoDB computes inside the database.
- **Same answer.** Each engine returns a small fingerprint (counts and means). A timing only counts if the fingerprints agree; a
  disagreement is printed as `DIFFERENT ANSWER` and fails the run. **This caught a real bug:** in the first run Spark's market-trend
  answer differed from pandas' on one day in 7,587 (2011-05-13: POWERGRID closed at 101.35, exactly equal to its 200-day average, and
  the two engines' sums differ in the 15th decimal, so `close > average` flipped). Both engines now count a stock as above its average
  only if it beats it by more than a 1e-9 relative tolerance, and there are regression tests for both engines.
- **Warm-up and repeats.** One untimed run, then three timed runs; the median is reported with the minimum and maximum.
- **Meaningful.** A job is only run on an engine that can express it.

## Results (Nov 2026, one machine: 12 cores, 15.7 GB RAM; median of 3 runs, all engines gave the same answer)

| Job | Rows | pandas | Spark | MongoDB |
|---|---|---|---|---|
| daily returns and volatility | 295,086 | **0.18 s** | 1.75 s (9.7x) | 3.17 s (17.5x) |
| intraday daily range stats | 5,392,391 | **3.55 s** | 4.66 s (1.3x) | 8.27 s (2.3x) |
| return correlation | 295,086 | **0.13 s** | 2.22 s (17.2x) | n/a |
| market trend (200-day) | 295,086 | **0.15 s** | 1.72 s (11.7x) | n/a |
| intraday realised volatility (5m) | 839,439 | **0.84 s** | 1.17 s (1.4x) | n/a |
| resample 1m to 5m, 1 index | 1,048,738 | **0.65 s** | 1.25 s (1.9x) | 1.89 s (2.9x) |
| resample 1m to 5m, 2 indices | 2,097,443 | **1.60 s** | 2.14 s (1.3x) | 7.01 s (4.4x) |
| resample 1m to 5m, 4 indices | 4,194,666 | **3.32 s** | 4.50 s (1.4x) | 14.38 s (4.3x) |

Multiples are against the fastest engine for that job. Spark's one-off session start-up (7.6 s) is not in these timings.

**What this shows**

- **pandas was fastest on every job.** This data (at most 5.4 million rows, about 2 GB in memory) fits comfortably in RAM, which is
  what pandas is best at.
- **Spark pays fixed costs.** On small jobs (under 300,000 rows) it is 10 to 17 times slower: scheduling, JVM and the data transfer
  dominate. On the large jobs it is 1.3 to 1.4 times slower while using about 6 CPU cores against pandas' 1.2, so it does scale
  across cores. In the resample series its ratio to pandas went 1.9x, 1.3x, 1.4x as the data grew 4x: no crossover was reached, and
  the data was not extrapolated further.
- **MongoDB was the slowest engine wherever it could run** (2.3 to 4.4 times pandas), and its aggregation uses one core per query. Its
  advantage is that it needs no export: the data is already there.
- **Memory.** The sampler records the peak resident memory of the whole process tree. pandas grew by about 2 GB on the 4 to 5 million
  row jobs because it holds all the data. Spark's growth looks tiny (1 to 300 MB) only because its JVM reserves its heap up front, and
  MongoDB's because its server cache is already allocated; so growth is not comparable across engines. What the numbers do show is that
  pandas' memory grows with the data, which is where it would stop being the right tool.

**The honest conclusion for this project.** For this data size, use pandas. Spark's value here is that the identical jobs would run
unchanged on data that no longer fits in memory (or on a cluster), and that it independently reproduces the pandas results:

| Check | Result |
|---|---|
| Spark 20-day volatility against the Phase 7 `indicators` | 55 symbols compared, max difference 4.9e-7 (rounding to 6 decimals in storage) |
| 1-minute bars rebuilt as 5-minute bars against the source's 5-minute bars (NIFTY 50) | 209,762 bars compared: close and OHLC **100.00% identical**. 126 source bars have no 1-minute bars because the 1-minute series ends 3 days earlier (2026-05-15 vs 2026-05-18); 3 rebuilt bars have no source bar |
| Every Spark job against its pandas version (tests, including symbols with `&` and `-`) | identical |

## Storage

| | Size |
|---|---|
| MongoDB `intraday_prices` (time-series, 5,392,391 bars) | 67 MB data, **49 MB on disk** |
| MongoDB `adjusted_prices` (295,086 rows) | 92 MB data, 39 MB on disk |
| Parquet (daily 8 MB + intraday 128 MB, snappy) | 130 MB |

Here MongoDB's time-series bucketing and block compression beat snappy Parquet. Parquet's advantage is not size but that every
big-data tool reads it.

## Windows limitation (read this before relying on the Spark timings)

On Windows, Spark's own file reader needs two native files, `winutils.exe` and `hadoop.dll`. Without them, `spark.read.parquet(...)`
fails with `UnsatisfiedLinkError: NativeIO$Windows.access0`, although computing works. The usual remedy is downloading those binaries
from a third-party repository; this project does not, because running unverified executables is not safe.

Instead PyArrow reads the Parquet files and hands them to Spark through Arrow (`spark_jobs.read_daily` / `read_intraday`); every
job still runs inside Spark. The transfer (daily 1.7 s, all intraday data 4.0 s) is **inside every Spark timing**, so the Spark numbers
above are somewhat pessimistic compared with Linux, where Spark reads Parquet directly. The data also passes through the driver's memory
first, so this route does not remove the memory ceiling that native reading would. If you obtain and vet `winutils.exe` and `hadoop.dll`
yourself (set `HADOOP_HOME`), replace the two reader functions with `spark.read.parquet(path.as_uri())`.

## Things to know

- **Sector returns** (`spark_sector_returns`) are equal-weighted, rebalanced-daily averages of the stocks in each sector, compounded
  over all history, so the cumulative figures (the Auto sector shows over 7,000% since 2000) are large and include survivorship bias.
  Use them for comparing sectors, not as investable returns.
- **Realised volatility** (`spark_intraday_realised_vol`) for NIFTY 50 from 5-minute bars ranges from about 6.9% (2017) to 16.0% (2020).
- **Market trend** on 2026-11-20: 80% of the 10 current stocks are above their 200-day average (all history: mean 65.3%).
- The warning `Missing Python executable ... defaulting to ...` at Spark start-up comes from Spark's Windows launcher and the space in the
  project path. It is harmless here because no Python workers are used (results come back through Arrow).
- `winutils.exe` and `netlib-blas ... libopenblas` warnings are the usual harmless Windows messages.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Spark is not ready: pyspark is not installed` | `pip install pyspark pyarrow` |
| `JAVA_HOME is not set` | put `JAVA_HOME=C:\Program Files\Eclipse Adoptium\jdk-17...` in `.env` (Spark 4 needs Java 17 or newer) |
| `NativeIO$Windows.access0` | Spark's own file reader on Windows; use the Arrow readers in `spark_jobs.py` (already the default) |
| `Java heap space` | raise `driver_memory` in `get_spark(...)` (default 4g) |
| `benchmark` says `DIFFERENT ANSWER` | an engine is wrong or the data changed between runs: compare the fingerprints before trusting any timing |
| `No Parquet data yet` | run `scripts/bigdata.py export` first (and again after the data changes) |
