# Performance testing at scale (Phase 14)

How ingestion, storage, queries and aggregations behave at **100,000, 1 million, 5 million and 10 million records**, on three technologies
(pandas, MongoDB, Apache Spark), with the CPU and memory each one used. The results are on the dashboard's **Performance** page (charts) and
in the **Database performance report** (CSV, Excel, PDF).

```powershell
.venv\Scripts\python scripts\performance.py run                          # all four sizes, every engine (close heavy programs first)
.venv\Scripts\python scripts\performance.py run --sizes 100000 1000000   # smaller sizes only
.venv\Scripts\python scripts\performance.py run --no-spark               # pandas and MongoDB only
.venv\Scripts\python scripts\performance.py report                       # the stored synthetic results in one table
.venv\Scripts\python scripts\performance.py clean --results              # remove leftover synthetic data and the stored synthetic timings
```
Needs MongoDB running and (for Spark) the Phase 8 setup: `pip install pyspark pyarrow`, JDK 17 in `.env`. Every size is stored as soon as it finishes, so a
failure at 10 million never loses the smaller sizes. `scripts\bigdata.py benchmark` remains the **real-data** benchmark (Phase 8).

## Real data and synthetic data

The database holds 5.4 million real intraday bars at most, so the larger sizes need generated data. The two are never mixed:

| | Real market data | Synthetic benchmark data |
|---|---|---|
| What | Actual NSE/BSE bars (Phase 8 benchmark, up to 5.4 million rows) | Generated 1-minute bars, 50 made-up symbols `SYN0001` ... `SYN0050` |
| Where | `BharatMarketDB` | its own database `BharatMarketBenchmark` (collection `synthetic_1m`) and `data/benchmark/` (Parquet), both deleted after each size |
| Label | `is_synthetic: false` | `is_synthetic: true` on every result **and** on every generated bar's `meta` |
| Use | what the market did | how the technologies scale: **the prices carry no information** |

**How the synthetic bars are made** (`backend/app/bigdata/synthetic.py`): 375 one-minute bars a day (09:15 to 15:30 IST), weekdays only from 2 January 2023
(no holidays), a random walk of about 12% annual volatility, prices on the 0.05 tick, log-normal volume, `high >= max(open, close)` and `low <= min(open, close)` always.
Seed 14: the same size always produces the same table, so every engine and every rerun sees identical input. The shape matters because grouping by day, combining
bars and filtering by range do work that depends on it. The number of symbols is fixed at 50, so the history per symbol grows with the size
(2,000 bars each at 100,000 records, 200,000 each at 10 million).

## What is measured

| Kind | Job | pandas | MongoDB | Spark |
|---|---|---|---|---|
| Ingestion | write every bar to Parquet (snappy) | yes (the writer) | | |
| Ingestion | bulk insert into a time-series collection **with the production index** | | yes | |
| Storage | disk used: MongoDB data plus index (after a checkpoint) against Parquet | | yes | |
| Query | one symbol, five trading days (1,875 bars): MongoDB uses its index, pandas and Spark read only that symbol's file | yes | yes | yes |
| Query | filter that no index can help: close > 1,000 and volume > 3,000, count every match | yes | yes | yes |
| Aggregation | bars, high, low, volume, mean close per symbol and day | yes | yes | yes |
| Aggregation | combine 1-minute bars into 5-minute bars | yes | yes | yes |

For every job: the time, the **records per second**, the **CPU cores** used and the **peak memory**.

## How the comparison stays honest

The Phase 8 rules apply unchanged ([BIGDATA.md](BIGDATA.md)): the same input (Parquet for pandas and Spark, the collection for MongoDB), every engine returns a small
fingerprint of its answer and a timing only counts if the fingerprints agree, an untimed warm-up then the median of repeated runs (aggregations 3, queries 5).
What Phase 14 adds:

- **Failures and skips are reported, never hidden, and never stored as timings.** An exception is recorded as *failed*; a job whose data cannot fit in the memory
  that is free is *skipped* with the numbers behind the decision (pandas about 400 bytes per row, Spark about 700, from Phase 8). A zero-second timing would be taken for a
  win, so neither goes into `benchmark_results`; both are kept in `benchmark_runs` and listed on the Performance page.
- **Memory is counted per engine.** Peak memory is the process tree that engine used: pandas only this process, Spark this process and its JVM, MongoDB this process and
  the `mongod` server. (An early version counted Spark's always-running JVM in pandas' figure, and the generated table in everyone's; both are fixed.) It is still a measure of
  "what that engine needed", not of who is lighter: `mongod` keeps its cache permanently and the JVM reserves its heap.
- **What the timings include.** pandas and Spark read the Parquet files inside every timing (on Windows Spark gets them through PyArrow, see BIGDATA.md); MongoDB reads
  nothing, the data is already there. The MongoDB ingestion figure includes **building the documents from the table**, as the importer does, but **not** the importer's
  validation and duplicate checks, so a real import is slower than this.
- **Ingestion is not warmed up** (it replaces the data): 3 timed runs up to 1 million records, 1 run above, each from a freshly created collection.
- **The generated table is freed after ingestion**, before any query or aggregation, so it does not sit in every memory figure.

## Results (Nov 2026, one machine: 12 cores, 15.7 GB RAM, Windows 11; MongoDB 8.2.6, Spark 4.3.0, median timings, every engine gave the same answer on every job)

All times in seconds. `-` = not measured (see "What did not fit" below). **Synthetic data**, so read the *shape* of each line, not the prices.

| Job | Engine | 100K | 1M | 5M | 10M |
|---|---|---|---|---|---|
| **Daily range per symbol** (aggregation) | pandas | **0.08** | **0.57** | **2.67** | **5.23** |
| | Spark | 0.88 | 0.99 | 3.94 | - |
| | MongoDB | 0.16 | 1.49 | 7.43 | 15.47 |
| **1-minute to 5-minute bars** (aggregation) | pandas | **0.14** | **0.71** | **3.32** | **6.67** |
| | Spark | 0.78 | 1.25 | 5.36 | - |
| | MongoDB | 0.15 | 1.64 | 16.39 | 32.86 |
| **Filter scanning every bar** (query) | MongoDB | **0.006** | **0.044** | **0.22** | **0.43** |
| | pandas | 0.09 | 0.21 | 0.99 | 1.94 |
| | Spark | 0.31 | 0.51 | 2.11 | - |
| **One symbol, five days, indexed** (query) | MongoDB | **0.004** | **0.004** | **0.004** | **0.006** |
| | pandas | 0.019 | 0.020 | 0.034 | 0.052 |
| | Spark | 0.25 | 0.15 | 0.32 | 0.43 |

| Ingestion and storage | 100K | 1M | 5M | 10M |
|---|---|---|---|---|
| Write to Parquet: time (s) | 0.24 | 0.50 | 1.41 | 2.55 |
| Write to Parquet: rows per second | 418,000 | 2.0 million | 3.5 million | 3.9 million |
| Insert into MongoDB: time (s) | 1.09 | 9.0 | 47.2 | 96.4 |
| Insert into MongoDB: rows per second | 92,000 | 111,000 | 106,000 | 104,000 |
| Storage, Parquet (MB; bytes per row) | 2.4; 25.0 | 18.9; 19.8 | 85.6; 18.0 | 162.6; 17.0 |
| Storage, MongoDB, data plus index (MB; bytes per row) | 0.7; 7.4 | 6.5; 6.8 | 34.4; 7.2 | 67.9; 7.1 |

**Resources at 5 million records** (the largest size every engine ran): pandas used about 1.2 CPU cores and peaked at 2.3 to 2.7 GB; Spark used 5.8 to 6.3 cores and peaked at 3.6 to 3.8 GB;
MongoDB used about 1.1 cores per aggregation and 2.0 GB (almost all of it the server's resident cache, which barely changes with the data: 1.9 GB at 100K, 2.1 GB at 10M).
At **10 million** pandas needed 4.6 to 5.1 GB.

### What this shows

- **pandas was the fastest for aggregations at every size, up to 10 million.** The data fits in memory, which is what pandas is best at. Its cost is memory: it grew from 0.2 GB at 100K to 5 GB at 10M,
  roughly 400 to 500 bytes per row, so a table that no longer fits in memory is where pandas stops being an option (the other two engines do not have that limit).
- **Spark pays a fixed cost, then scales.** Its time barely moves from 100K to 1M (0.88 s to 0.99 s: scheduling and the data transfer dominate), so it is 6 to 11 times slower than pandas at 100K but 1.5 to 1.6 times slower at
  5M, using five to six cores. The gap was still narrowing, no crossover was reached up to 5M, and 10M was not measured, so the data is not extrapolated.
  Spark is **faster than MongoDB's aggregation from 1M up** (1.9 times at 5M for the daily range, 3.1 times for resampling) and slower below.
- **MongoDB wins every query, by a wide margin where an index applies.** The indexed five-day query took **4 ms at every size** (100K to 10M): the index makes it independent of the data size, while pandas
  grew from 19 to 52 ms and Spark took 0.15 to 0.43 s. The full-scan filter is also MongoDB's (0.43 s at 10M, 4.5 times faster than pandas), because the server filters where the data lives and returns a count.
- **MongoDB's aggregations are the slowest and use one core.** The daily range scales in a straight line (a steady 640,000 to 670,000 rows per second); the resample job halves its rate from 1M to 5M
  (610,000 to 305,000 rows per second, then steady to 10M), a change in behaviour that this benchmark did not investigate.
- **Ingestion: Parquet is much faster than MongoDB, and the gap widens**: 4.5 times at 100K, 18 times at 1M, 37 times at 10M (3.9 million against 104,000 rows per second). MongoDB's rate is flat from 100K to 10M, so ten times the data takes ten times as long.
  That is the price of a server that validates, indexes and compresses each document, and these figures include only building the documents and inserting them (not the importer's validation and duplicate checks).
- **Storage: MongoDB holds the same bars in 40% of Parquet's space** (7.1 against 17 bytes per row at 10M), because time-series buckets compress them column by column. The real data agrees:
  Phase 8 measured 9 bytes per row in MongoDB and 24 in Parquet for the real intraday bars.
- **The synthetic timings match the real ones.** At about 1 million rows the Phase 8 resample on real data took 0.65 s (pandas), 1.25 s (Spark) and 1.89 s (MongoDB); the synthetic data took 0.71, 1.25 and 1.64. That is
  the evidence that these generated bars exercise the engines like real ones.

### What did not fit

Spark's three heavy jobs at **10 million records were not run**: the memory check measured about 6.2 GB free and the job needs about 6.5 GB by the estimate (Spark's measured peak at 5M was 3.8 GB, about 760 bytes
per row, which puts 10M near 7.6 GB). Running it anyway would have pushed this 15.7 GB machine into paging and made the timing meaningless. They appear as *skipped* on the Performance page and in `benchmark_runs`. Spark's
indexed-style query (one symbol) did run at 10M because it reads one file. On a machine with more memory, or a Linux machine where Spark reads Parquet directly instead of through the driver, rerun with
`scripts\performance.py run --sizes 10000000 --engines pyspark --driver-memory 8g`.

### How these numbers were produced, and what limits them

- **Several runs, one machine, one hour.** The full ladder ran first; Spark's skipped 5M job and the 10M sizes were rerun with only pandas and Spark in separate processes (Spark's JVM keeps its memory between sizes, which had
  starved the 10M jobs of free memory); MongoDB was rerun at all four sizes after the storage fix below. Each cell shows the newest timing. Repeats of the same job agreed within a few percent for aggregations and queries (pandas' 5M daily range: 2.78 s then 2.67 s; MongoDB's 10M daily range: 15.1 s then 15.5 s); ingestion times varied more (Parquet at 1M: 0.59 s then 0.50 s).
- **MongoDB's storage figure had a trap.** A bulk load longer than about a minute leaves reusable free space inside MongoDB's data file (a 5-million-row load gave a 63 MB file holding 33 MB of live data; the
  same data loaded faster gave 33 MB), so the file size depended on timing. The runner now reports the live data plus the index; the raw file size is kept in `benchmark_runs`.
- **Memory is not comparable across engines** (see above). pandas' figure is what the job needed; Spark's includes a JVM that reserves its heap; MongoDB's is the server's resident cache.
- **The prices are random,** so compression ratios are indicative (the real data agreed), and nothing here says anything about the market. Fifty symbols were used at every size, so the largest sizes are long histories of few
  symbols, not many symbols.
- **Windows.** Spark reads Parquet through PyArrow and the driver, which adds a transfer cost to every Spark timing and sets the memory ceiling; on Linux or a cluster Spark would do better.

## Layout

| Part | File |
|---|---|
| The synthetic data generator | `backend/app/bigdata/synthetic.py` |
| The scale runner (ingestion, storage, jobs, failure and skip handling, storing) | `backend/app/bigdata/scale_benchmark.py` |
| The Phase 8 harness it reuses (timing, resource sampling, answer checks) | `backend/app/bigdata/benchmark.py` |
| Command line | `scripts/performance.py` |
| API | `GET /api/benchmarks/scaling` (charts), `GET /api/benchmarks` (flat list), the `benchmarks` report |
| Dashboard | `frontend/src/pages/Performance.tsx`, `frontend/src/components/ScalingChart.tsx` |
| Tests | `tests/test_scale_benchmark.py`, `frontend/src/test/performance.test.tsx`, the scaling shape in the shared API contract |
| Stored in | `benchmark_results` (`is_synthetic: true`, `answers_match`), `benchmark_runs` (outcomes, machine, dataset) |

## Troubleshooting

| Problem | Fix |
|---|---|
| A job is reported as **skipped: not enough free memory** | close browsers and other heavy programs and run that size again (`--sizes 10000000`); the check reads free memory at that moment |
| A job is reported as **failed** with `OutOfMemoryError` | raise Spark's driver memory (`--driver-memory 6g`) or run `--engines pandas mongodb_aggregation` |
| `Spark is not ready` | `pip install pyspark pyarrow`, `JAVA_HOME` in `.env` (see BIGDATA.md); the other engines still run |
| MongoDB inserts look slow | the 10 million size is the longest job (minutes); check free disk space and that no other heavy MongoDB job is running |
| The Performance page says "No ... measured yet" | run `scripts\performance.py run` |
| Leftover `BharatMarketBenchmark` database or `data\benchmark` folder after an interrupted run | `scripts\performance.py clean` |
