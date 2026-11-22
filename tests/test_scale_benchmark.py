"""Phase 14 tests: the synthetic data generator and the scale benchmark (ingestion, storage, queries, aggregations,
resources), including the honest-reporting rules: failures and skips are recorded, never stored as timings, and an engine
that gives a different answer is flagged. Spark parts are skipped when Spark is not installed.
"""
import uuid

import pandas as pd
import pytest
from test_bigdata import db, spark  # noqa: F401 - fixtures: a throwaway database with the real schema, and a Spark session

from app.bigdata import mongo_jobs, pandas_jobs
from app.bigdata import scale_benchmark as sb
from app.bigdata import synthetic
from app.core.config import get_settings
from app.db.mongo import get_client
from app.db.schemas import INTRADAY_META_FIELDS

ROWS, SYMBOLS = 3_000, 5


# ------------------------------------------------------------------ the generator
def test_generator_gives_exactly_the_rows_asked_for_spread_over_the_symbols():
    frames = synthetic.generate(1_003, symbols=7)
    assert [len(f) for f in frames] == [144, 144, 143, 143, 143, 143, 143] and sum(map(len, frames)) == 1_003
    assert [f["symbol"].iloc[0] for f in frames] == [f"SYN000{i}" for i in range(1, 8)]
    with pytest.raises(ValueError):
        synthetic.generate(3, symbols=7)                      # fewer rows than symbols


def test_generator_is_reproducible_and_the_seed_changes_it():
    a, b, c = synthetic.generate(2_000, 4), synthetic.generate(2_000, 4), synthetic.generate(2_000, 4, seed=99)
    assert all(x.equals(y) for x, y in zip(a, b))
    assert not a[0]["close"].equals(c[0]["close"])


def test_generated_bars_are_valid_ohlc_on_the_tick_in_session_hours():
    df = pd.concat(synthetic.generate(20_000, 4))
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all() and (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert (df["low"] > 0).all() and (df["volume"] >= 0).all()
    ticks = (df[["open", "high", "low", "close"]] / synthetic.TICK).round(6)
    assert ((ticks - ticks.round()).abs() < 1e-3).all().all()                       # every price is a multiple of 0.05
    ist = df["ts"].dt.tz_convert("Asia/Kolkata")
    assert ist.dt.dayofweek.max() <= 4                                              # weekdays only
    minutes = ist.dt.hour * 60 + ist.dt.minute
    assert minutes.min() == 9 * 60 + 15 and minutes.max() == 15 * 60 + 29           # 09:15 to 15:29 (375 one-minute bars)
    assert df.groupby("symbol")["ts"].apply(lambda s: s.is_monotonic_increasing).all()
    assert df.groupby(["symbol", ist.dt.date]).size().max() == synthetic.SESSION_MINUTES


def test_documents_are_labelled_synthetic_and_have_every_meta_field_of_the_real_collection():
    frame = synthetic.generate(1_000, 2)[0]
    docs = synthetic.documents(frame)
    assert len(docs) == len(frame) and set(INTRADAY_META_FIELDS) <= set(docs[0]["meta"])
    assert all(d["meta"]["is_synthetic"] is True and d["meta"]["source"] == synthetic.SYNTHETIC_SOURCE for d in docs)
    assert docs[0]["ts"] == frame["ts"].iloc[0].to_pydatetime() and docs[0]["close"] == float(frame["close"].iloc[0])


def test_the_benchmark_database_can_never_be_the_real_database():
    with pytest.raises(ValueError, match="real database"):
        sb.bench_database(get_settings().mongo_db_name)


# ------------------------------------------------------------------ the runner
@pytest.fixture()
def bench():
    client = get_client()
    name = f"BharatMarketTest_bench_{uuid.uuid4().hex[:8]}"
    assert name.startswith("BharatMarketTest_")
    yield client[name]
    client.drop_database(name)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")
    return tmp_path / "data"


def run_small(bench, **kw):
    return sb.run_scale(ROWS, bench=bench, symbols=SYMBOLS, runs=1, query_runs=1, ingest_runs=1, **kw)


def by_key(run):
    return {(o["task"], o["engine"]): o for o in run["outcomes"]}


def test_a_scale_run_measures_every_job_and_the_engines_agree(bench, data_dir, db):
    run = run_small(bench, keep=True)
    out = by_key(run)
    assert all(o["status"] == "ok" for o in run["outcomes"]), [o for o in run["outcomes"] if o["status"] != "ok"]
    assert set(out) == {(sb.INGEST_PARQUET, "ingestion"), (sb.INGEST_MONGO, "ingestion"),
                        *((t, e) for t in (sb.QUERY_WINDOW, sb.QUERY_SCAN, sb.AGG_RANGE, sb.AGG_RESAMPLE) for e in ("pandas", "mongodb_aggregation"))}
    assert all(o["answers_match"] for o in run["outcomes"] if "answers_match" in o)
    assert out[(sb.QUERY_WINDOW, "pandas")]["fingerprint"]["bars"] == out[(sb.QUERY_WINDOW, "mongodb_aggregation")]["fingerprint"]["bars"] > 0
    assert out[(sb.AGG_RANGE, "pandas")]["fingerprint"]["bars"] == ROWS
    for o in run["outcomes"]:
        assert o["seconds"] > 0 and o["peak_mb"] > 0 and o["cpu_cores_used"] >= 0
    assert out[(sb.INGEST_MONGO, "ingestion")]["records_per_second"] > 0 and out[(sb.QUERY_WINDOW, "pandas")]["records_per_second"] is None
    assert run["storage"]["parquet_mb"] > 0 and run["storage"]["disk_mb"] > 0 and run["environment"]["cpu_cores"] >= 1
    s = run["storage"]                                                                # the compared figure is the live data, not the file with its reusable free space
    assert s["live_mb"] == pytest.approx(max(s["disk_mb"] - s["free_mb"], 0)) and out[(sb.INGEST_MONGO, "ingestion")]["storage_mb"] == pytest.approx(s["live_mb"] + s["index_mb"])
    # what was loaded: the right number of bars, every one labelled synthetic, with the production index
    assert bench[sb.COLLECTION].count_documents({}) == ROWS
    assert bench[sb.COLLECTION].distinct("meta.is_synthetic") == [True] and len(bench[sb.COLLECTION].distinct("meta.symbol")) == SYMBOLS
    assert "idx_symbol_exchange_interval_ts" in bench[sb.COLLECTION].index_information()
    assert db["intraday_prices"].count_documents({}) == 0 and sb.COLLECTION not in db.list_collection_names()   # real collections untouched


def test_a_scale_run_cleans_up_after_itself_unless_asked_to_keep_the_data(bench, data_dir):
    run_small(bench)
    assert sb.COLLECTION not in bench.list_collection_names() and not (data_dir / "benchmark" / f"n{ROWS}").exists()


def test_timings_are_stored_as_synthetic_and_validate_against_the_schema(bench, data_dir, db):
    run = run_small(bench)
    stored = sb.store_run(db, run)
    docs = list(db["benchmark_results"].find({"run_id": run["run_id"]}))
    assert stored == len(docs) == 10 and all(d["is_synthetic"] is True and d["record_count"] == ROWS and d["answers_match"] for d in docs)
    assert {d["technology"] for d in docs} == {"ingestion", "pandas", "mongodb_aggregation"}
    ingest = next(d for d in docs if d["operation"] == sb.INGEST_MONGO)
    assert ingest["storage_mb"] > 0 and ingest["records_per_second"] > 0 and "SYNTHETIC" in ingest["notes"]
    assert "records_per_second" not in next(d for d in docs if d["operation"] == sb.QUERY_WINDOW and d["technology"] == "pandas")
    meta = db["benchmark_runs"].find_one({"run_id": run["run_id"]})
    assert meta["records"] == ROWS and meta["is_synthetic"] is True and meta["dataset"]["symbols"] == SYMBOLS and len(meta["outcomes"]) == 10


def test_a_job_that_raises_is_recorded_as_failed_and_the_run_carries_on(bench, data_dir, db, monkeypatch):
    def boom(_df):
        raise MemoryError("Unable to allocate 4.2 GiB")
    monkeypatch.setattr(pandas_jobs, "daily_range_stats", boom)
    run = run_small(bench)
    out = by_key(run)
    failed = out[(sb.AGG_RANGE, "pandas")]
    assert failed["status"] == "failed" and "MemoryError" in failed["error"] and "seconds" not in failed
    assert out[(sb.AGG_RANGE, "mongodb_aggregation")]["status"] == "ok" and out[(sb.AGG_RESAMPLE, "pandas")]["status"] == "ok"
    sb.store_run(db, run)
    assert not list(db["benchmark_results"].find({"operation": sb.AGG_RANGE, "technology": "pandas"}))      # no timing for it ...
    recorded = next(o for o in db["benchmark_runs"].find_one({"run_id": run["run_id"]})["outcomes"]
                    if o["operation"] == sb.AGG_RANGE and o["technology"] == "pandas")
    assert recorded["status"] == "failed" and "MemoryError" in recorded["error"]                       # ... but the failure is on record


def test_a_job_that_does_not_fit_in_free_memory_is_skipped_with_the_reason(bench, data_dir, monkeypatch):
    monkeypatch.setattr(sb, "_free_mb", lambda: 1.0)
    out = by_key(run_small(bench))
    skipped = out[(sb.QUERY_SCAN, "pandas")]
    assert skipped["status"] == "skipped" and "not enough free memory" in skipped["error"] and "is free" in skipped["error"]
    assert out[(sb.QUERY_SCAN, "mongodb_aggregation")]["status"] == "ok"            # MongoDB computes inside the server: no check
    assert out[(sb.QUERY_WINDOW, "pandas")]["status"] == "ok"                       # reads one symbol, not the whole dataset


def test_an_engine_that_gives_a_different_answer_is_flagged_on_every_engine_of_that_job(bench, data_dir, db, monkeypatch):
    real = mongo_jobs.resample_totals
    monkeypatch.setattr(mongo_jobs, "resample_totals", lambda *a, **k: {**real(*a, **k), "bars": 1})
    run = run_small(bench)
    out = by_key(run)
    assert out[(sb.AGG_RESAMPLE, "mongodb_aggregation")]["matches_reference"] is False
    assert out[(sb.AGG_RESAMPLE, "pandas")]["answers_match"] is False and out[(sb.AGG_RESAMPLE, "mongodb_aggregation")]["answers_match"] is False
    assert out[(sb.AGG_RANGE, "pandas")]["answers_match"] is True                   # other jobs unaffected
    sb.store_run(db, run)
    assert db["benchmark_results"].count_documents({"run_id": run["run_id"], "answers_match": False}) == 2


def test_the_report_text_lists_the_jobs_and_the_storage(bench, data_dir):
    text = sb.format_run(run_small(bench))
    assert "ingest_parquet" in text and "bytes per row" in text and "query_symbol_window" in text


def test_spark_gives_the_same_answers_as_pandas_and_mongodb(spark, bench, data_dir):
    run = sb.run_scale(ROWS, bench=bench, spark=spark, symbols=SYMBOLS, runs=1, query_runs=1, ingest_runs=1)
    out = by_key(run)
    assert all(out[(t, "pyspark")]["status"] == "ok" for t in (sb.QUERY_WINDOW, sb.QUERY_SCAN, sb.AGG_RANGE, sb.AGG_RESAMPLE)), run["outcomes"]
    assert all(o["answers_match"] for o in run["outcomes"] if "answers_match" in o)
    assert out[(sb.QUERY_SCAN, "pyspark")]["fingerprint"]["matches"] == out[(sb.QUERY_SCAN, "pandas")]["fingerprint"]["matches"]
