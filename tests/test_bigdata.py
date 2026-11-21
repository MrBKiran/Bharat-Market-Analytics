"""Phase 8 tests: the pandas baseline, the MongoDB pipelines checked against it, the benchmark harness,
and (skipped until pyarrow / PySpark are installed) the Parquet export and the Spark jobs.
"""
import math
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.analytics import indicators as ind
from app.bigdata import benchmark as bm
from app.bigdata import mongo_jobs, pandas_jobs, spark_session
from app.core.config import get_settings
from app.db.mongo import get_client
from app.db.setup import setup_database

UTC = timezone.utc


# ------------------------------------------------------------------ builders
def g(rate: float, n: int, start: float = 100.0) -> list[float]:
    return list(start * (1 + rate) ** np.arange(n))


def daily_frame(series: dict, sectors: dict = None, types: dict = None) -> pd.DataFrame:
    parts = []
    for sym, closes in series.items():
        dates = pd.date_range("2023-01-01", periods=len(closes), freq="D", tz="UTC")
        parts.append(pd.DataFrame({"symbol": sym, "date": dates, "open": closes, "high": closes, "low": closes, "close": closes,
                                   "volume": 1000.0, "sector": (sectors or {}).get(sym), "instrument_type": (types or {}).get(sym, "equity")}))
    return pd.concat(parts, ignore_index=True)


def bars(symbol: str, start: str, closes: list, interval: str = "1m", step_minutes: int = 1, volume: float = 10.0) -> pd.DataFrame:
    ts = pd.date_range(start, periods=len(closes), freq=f"{step_minutes}min", tz="UTC")
    c = np.array(closes, dtype=float)
    return pd.DataFrame({"symbol": symbol, "interval": interval, "ts": ts, "open": c - 0.05, "high": c + 0.1,
                         "low": c - 0.1, "close": c, "volume": volume})


def wiggly(n: int, seed: int = 0, start: float = 100.0) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(start * np.exp(np.cumsum(rng.normal(0, 0.01, n))))


# ------------------------------------------------------------------ pandas baseline
def test_returns_and_volatility_match_the_phase_7_indicator():
    closes = wiggly(80)
    out = pandas_jobs.returns_and_volatility(daily_frame({"A": closes}))
    assert out["ret"].isna().sum() == 1 and out["hist_vol_20"].first_valid_index() == 20
    expected = ind.historical_volatility(pd.Series(closes), 20)
    assert out["hist_vol_20"].iloc[-1] == pytest.approx(expected.iloc[-1])
    assert out["ret"].iloc[5] == pytest.approx(closes[5] / closes[4] - 1)
    latest = pandas_jobs.latest_per_symbol(out, ["ret", "hist_vol_20"])
    assert list(latest["symbol"]) == ["A"] and latest["date"].iloc[0] == out["date"].max()


def test_returns_never_cross_between_symbols():
    out = pandas_jobs.returns_and_volatility(daily_frame({"A": [100.0, 110.0], "B": [50.0, 45.0]}))
    first_rows = out.groupby("symbol").head(1)
    assert first_rows["ret"].isna().all()                              # B's first row is not compared with A's last


def test_sector_returns_average_stocks_and_skip_indices():
    df = pandas_jobs.returns_and_volatility(daily_frame(
        {"A": [100.0, 110.0, 121.0], "B": [100.0, 120.0, 144.0], "IDX": [100.0, 150.0, 225.0]},
        sectors={"A": "IT", "B": "IT"}, types={"IDX": "index"}))
    out = pandas_jobs.sector_daily_returns(df)
    assert list(out["sector"].unique()) == ["IT"] and out["n"].tolist() == [2, 2]
    assert out["ret"].tolist() == pytest.approx([0.15, 0.15])          # (10% + 20%) / 2, on both days
    summary = pandas_jobs.summarise_sector_returns(out)["IT"]
    assert summary["days"] == 2 and summary["cumulative_return_pct"] == pytest.approx(((1.15 ** 2) - 1) * 100, abs=0.06)


def test_correlation_uses_only_days_every_symbol_has():
    a = wiggly(120)
    df = pandas_jobs.returns_and_volatility(daily_frame({"A": a, "B": [2 * x for x in a], "C": [1 / x for x in a[30:]]}))
    # daily_frame starts every symbol on the same date; move C's dates so it starts 30 days later, aligned with A and B
    df.loc[df["symbol"] == "C", "date"] = df.loc[df["symbol"] == "C", "date"] + pd.Timedelta(days=30)
    corr, n = pandas_jobs.correlation_matrix(df, ["A", "B", "C"], df["date"].min(), df["date"].max())
    assert corr.loc["A", "B"] == pytest.approx(1.0) and corr.loc["A", "C"] < -0.95
    assert n == 89                                                    # C has 90 rows, so 89 returns; only those days count


def test_market_trend_counts_the_share_above_the_200_day_average():
    df = daily_frame({"UP": g(0.003, 230), "DOWN": g(-0.003, 230), "IDX": g(0.003, 230)}, types={"IDX": "index"})
    out = pandas_jobs.market_trend(df)
    assert len(out) == 31 and out["stocks"].eq(2).all()               # the 200th day onwards; the index is excluded
    assert out["pct_above_sma200"].eq(50.0).all()


def test_a_close_equal_to_its_average_is_not_above_it():
    """Real case: POWERGRID 2011-05-13 closed at 101.35, exactly its 200-day average. Two engines can differ in the
    15th decimal of that average, so 'above' must beat the average by more than rounding noise."""
    df = daily_frame({"FLAT": [101.35] * 230, "UP": g(0.003, 230)})
    out = pandas_jobs.market_trend(df)
    assert out["stocks"].eq(2).all() and out["above"].eq(1).all()      # only UP is above; FLAT sits exactly on its average


def test_realised_variance_uses_returns_within_a_day_only():
    day1 = bars("S", "2024-01-01 03:45", [100, 101, 102, 101, 103], interval="5m", step_minutes=5)
    day2 = bars("S", "2024-01-02 03:45", [200, 198, 199, 201, 200], interval="5m", step_minutes=5)
    out = pandas_jobs.intraday_realised_variance(pd.concat([day1, day2]))
    assert out["returns"].tolist() == [4, 4]                          # the overnight move 103 -> 200 is not a return
    exp1 = sum(math.log(b / a) ** 2 for a, b in zip([100, 101, 102, 101], [101, 102, 101, 103]))
    assert out["rv"].iloc[0] == pytest.approx(exp1)
    summary = pandas_jobs.summarise_realised_variance(out)
    assert summary["S"]["5m"]["2024"] == pytest.approx(np.mean([math.sqrt(v * 252) * 100 for v in out["rv"]]), abs=0.01)


def test_resample_combines_bars_correctly_and_drops_empty_bins():
    df = pd.concat([bars("S", "2024-01-01 03:45", [100, 101, 99, 102, 103, 104, 98, 105, 106, 107]),   # 03:45..03:54 UTC
                    bars("S", "2024-01-02 03:45", [200, 201])])                                         # next day: overnight gap between
    out = pandas_jobs.resample_bars(df, 5)
    assert len(out) == 3                                                                                # two bars on day 1, one on day 2
    first = out.iloc[0]
    assert first["ts"] == pd.Timestamp("2024-01-01 03:45", tz="UTC") and first["bars"] == 5
    assert [first["open"], first["high"], first["low"], first["close"], first["volume"]] == pytest.approx([99.95, 103.1, 98.9, 103.0, 50.0])


def test_daily_range_stats_assigns_bars_to_the_ist_day():
    # 20:00 UTC is 01:30 IST on the NEXT calendar day
    df = pd.concat([bars("S", "2024-01-01 03:45", [10.0, 11.0], interval="5m", step_minutes=5),
                    bars("S", "2024-01-01 20:00", [20.0], interval="5m")])
    out = pandas_jobs.daily_range_stats(df).sort_values("day")
    assert [str(d) for d in out["day"]] == ["2024-01-01", "2024-01-02"]
    assert out["bars"].tolist() == [2, 1] and out["high"].tolist() == pytest.approx([11.1, 20.1])
    assert out["mean_close"].tolist() == pytest.approx([10.5, 20.0])


# ------------------------------------------------------------------ MongoDB pipelines checked against pandas
@pytest.fixture()
def db():
    client = get_client()
    name = f"BharatMarketTest_{uuid.uuid4().hex[:8]}"
    assert name.startswith("BharatMarketTest_")
    database = client[name]
    setup_database(database)
    yield database
    client.drop_database(name)


def put_daily(db, frame: pd.DataFrame):
    db["adjusted_prices"].insert_many([{
        "symbol": r.symbol, "exchange": "NSE", "interval": "1d", "date": r.date.to_pydatetime(), "open": float(r.close),
        "high": float(r.close), "low": float(r.close), "close": float(r.close), "volume": 1000, "adj_factor": 1.0,
        "source": "yfinance", "method": "provider_adjusted", "processed_at": datetime(2024, 1, 1, tzinfo=UTC)}
        for r in frame.itertuples()])


def put_intraday(db, frame: pd.DataFrame):
    db["intraday_prices"].insert_many([{
        "ts": r.ts.to_pydatetime(), "open": float(r.open), "high": float(r.high), "low": float(r.low), "close": float(r.close),
        "volume": int(r.volume), "meta": {"symbol": r.symbol, "exchange": "NSE", "interval": r.interval, "instrument_type": "index",
                                          "source": "kaggle", "is_synthetic": False, "price_basis": "index_level"}}
        for r in frame.itertuples()])


def test_mongodb_returns_and_volatility_equal_pandas(db):
    frame = daily_frame({"A": wiggly(90, 1), "B": wiggly(90, 2, 50.0), "SHORT": wiggly(15, 3)})
    put_daily(db, frame)
    mongo = pd.DataFrame(mongo_jobs.returns_volatility_latest(db)).set_index("symbol").sort_index()
    pdf = pandas_jobs.latest_per_symbol(pandas_jobs.returns_and_volatility(frame), ["ret", "hist_vol_20"]).set_index("symbol").sort_index()
    assert list(mongo.index) == ["A", "B", "SHORT"]
    for sym in ("A", "B"):
        assert mongo.loc[sym, "ret"] == pytest.approx(pdf.loc[sym, "ret"])
        assert mongo.loc[sym, "hist_vol_20"] == pytest.approx(pdf.loc[sym, "hist_vol_20"])
    assert mongo.loc["SHORT", "hist_vol_20"] is None or pd.isna(mongo.loc["SHORT", "hist_vol_20"])   # fewer than 20 returns


def test_mongodb_daily_range_stats_equal_pandas(db):
    frame = pd.concat([bars("S1", "2024-01-01 03:45", wiggly(40, 4), "5m", 5), bars("S1", "2024-01-01 20:00", [30.0, 31.0], "5m", 5),
                       bars("S2", "2024-01-02 03:45", wiggly(25, 5), "15m", 15)])
    put_intraday(db, frame)
    got = mongo_jobs.daily_range_stats(db)
    expected = pandas_jobs.daily_range_stats(frame)
    assert got == {"groups": len(expected), "bars": int(expected["bars"].sum()), "volume": float(expected["volume"].sum())}
    only = mongo_jobs.daily_range_stats(db, interval="15m")
    assert only["bars"] == 25 and only["groups"] == 1
    assert mongo_jobs.daily_range_stats(db, symbols=["NOPE"]) == {"groups": 0, "bars": 0, "volume": 0.0}


def test_mongodb_resample_equals_pandas(db):
    frame = pd.concat([bars("S1", "2024-01-01 03:45", wiggly(73, 6)), bars("S1", "2024-01-02 03:45", wiggly(40, 7)),
                       bars("S2", "2024-01-01 03:45", wiggly(61, 8))])
    put_intraday(db, frame)
    got = mongo_jobs.resample_totals(db, ["S1", "S2"], 5)
    expected = bm.fp_bars(pandas_jobs.resample_bars(frame, 5))
    assert bm.fingerprints_match(got, expected)
    assert mongo_jobs.resample_totals(db, ["S1"], 5)["bars"] == len(pandas_jobs.resample_bars(frame[frame.symbol == "S1"], 5))


# ------------------------------------------------------------------ benchmark harness
def test_fingerprint_comparison_rules():
    a = {"n": 5, "x": 1.0000000, "y": float("nan")}
    assert bm.fingerprints_match(a, {"n": 5, "x": 1.0000001, "y": None})              # tiny float difference, NaN == missing
    assert not bm.fingerprints_match(a, {"n": 6, "x": 1.0, "y": float("nan")})        # whole numbers must match exactly
    assert not bm.fingerprints_match(a, {"n": 5, "x": 1.01, "y": float("nan")})       # a real difference
    assert not bm.fingerprints_match(a, {"n": 5, "x": 1.0})                           # different keys


def test_measure_runs_warmup_then_the_timed_runs_and_reports_the_median():
    calls = []

    def fn():
        calls.append(1)
        time.sleep(0.02)
        return {"rows": 3}

    m = bm.measure(fn, runs=3, warmup=True)
    assert len(calls) == 4 and len(m["runs"]) == 3 and m["first_run_seconds"] is not None
    assert m["min_seconds"] <= m["seconds"] <= m["max_seconds"] and m["seconds"] >= 0.02 and m["fingerprint"] == {"rows": 3}
    assert m["peak_mb"] > 0 and m["cpu_cores_used"] >= 0
    before = len(calls)
    no_warmup = bm.measure(fn, runs=2, warmup=False)
    assert len(calls) == before + 2 and no_warmup["first_run_seconds"] is None


def test_run_tasks_flags_an_engine_that_gives_a_different_answer():
    tasks = [bm.Task("job_a", "demo", 1000, {"pandas": lambda: {"n": 1}, "mongodb_aggregation": lambda: {"n": 1}, "pyspark": lambda: {"n": 2}}),
             bm.Task("other", "demo", 10, {"pandas": lambda: {"n": 1}})]
    out = bm.run_tasks(tasks, runs=1, warmup=False)
    by = {(r["task"], r["engine"]): r for r in out}
    assert by[("job_a", "pandas")]["matches_reference"] and by[("job_a", "mongodb_aggregation")]["matches_reference"]
    assert by[("job_a", "pyspark")]["matches_reference"] is False
    assert [r["engine"] for r in bm.run_tasks(tasks, engines=["pandas"], runs=1, warmup=False)] == ["pandas", "pandas"]
    assert {r["task"] for r in bm.run_tasks(tasks, only=["oth"], runs=1, warmup=False)} == {"other"}
    assert "DIFFERENT" in bm.format_table(out) and "job_a" in bm.format_table(out)


def test_results_are_stored_in_benchmark_results(db):
    tasks = [bm.Task("job_a", "a demo job", 2000, {"pandas": lambda: {"n": 1}, "pyspark": lambda: {"n": 2}})]
    run_id = bm.store_results(db, bm.run_tasks(tasks, runs=1, warmup=False), notes_extra="Spark start-up 5.0s.")
    docs = list(db["benchmark_results"].find({"run_id": run_id}, {"_id": 0}).sort("technology", 1))
    assert [d["technology"] for d in docs] == ["pandas", "pyspark"] and all(d["is_synthetic"] is False for d in docs)
    assert docs[0]["operation"] == "job_a" and docs[0]["record_count"] == 2000 and docs[0]["records_per_second"] > 0
    assert "DIFFERENT ANSWER" in docs[1]["notes"] and "Spark start-up 5.0s" in docs[1]["notes"]


def test_current_universe_picks_symbols_with_recent_data(db):
    put_daily(db, daily_frame({"LIVE": wiggly(100, 1), "STALE": wiggly(50, 2), "ALSO": wiggly(100, 3)}))
    symbols, start, end = bm.current_universe(db)
    assert symbols == ["ALSO", "LIVE"] and end == pd.Timestamp("2023-04-10", tz="UTC")
    assert start == end - pd.DateOffset(years=3)


# ------------------------------------------------------------------ environment checks
def test_spark_status_reports_what_is_missing_instead_of_raising():
    status = spark_session.spark_status()
    assert set(status) == {"pyspark", "pyarrow", "java_home", "java_ok", "problems"}
    if status["pyspark"] is None:
        assert any("pyspark" in p for p in status["problems"])
        with pytest.raises(RuntimeError):
            spark_session.get_spark()


# ------------------------------------------------------------------ Parquet export and Spark (skipped until installed)
def write_parquet_fixture(base, daily: pd.DataFrame, intraday: pd.DataFrame):
    from app.bigdata import export
    export._write(daily, base / "daily" / "part-0.parquet")
    for (sym, ivl), part in intraday.groupby(["symbol", "interval"]):
        export._write(part[["ts", "open", "high", "low", "close", "volume"]],
                      base / "intraday" / f"symbol={sym}" / f"interval={ivl}" / "part-0.parquet")
    return base


def test_export_writes_readable_parquet_with_microsecond_timestamps(db, tmp_path, monkeypatch):
    pytest.importorskip("pyarrow", reason="pyarrow is not installed")
    import pyarrow.parquet as pq

    from app.bigdata import export
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")
    put_daily(db, daily_frame({"TCS": wiggly(30, 1), "NIFTY_50": wiggly(30, 2)}))
    put_intraday(db, bars("NIFTY_50", "2024-01-01 03:45", wiggly(20, 3), "5m", 5))
    db["intraday_coverage"].insert_one({"symbol": "NIFTY_50", "exchange": "NSE", "interval": "5m", "source": "kaggle",
                                        "first_ts": datetime(2024, 1, 1, tzinfo=UTC), "last_ts": datetime(2024, 1, 1, tzinfo=UTC),
                                        "row_count": 20, "updated_at": datetime(2024, 1, 1, tzinfo=UTC)})
    out = tmp_path / "data" / "processed" / "parquet"
    d = export.export_daily(db, out)
    i = export.export_intraday(db, out)
    assert d["rows"] == 60 and i["rows"] == 20 and i["files"] == 1
    daily = pd.read_parquet(out / "daily")
    assert set(daily.columns) >= {"symbol", "date", "close", "sector", "instrument_type"}
    assert daily.loc[daily["symbol"] == "TCS", "sector"].iloc[0] == "Information Technology"      # joined from instruments
    assert daily.loc[daily["symbol"] == "NIFTY_50", "instrument_type"].iloc[0] == "index"
    assert str(pq.read_schema(out / "daily" / "part-0.parquet").field("date").type) == "timestamp[us, tz=UTC]"
    intraday = pd.read_parquet(out / "intraday")
    assert len(intraday) == 20 and (out / "intraday" / "symbol=NIFTY_50" / "interval=5m" / "part-0.parquet").exists()
    export.export_daily(db, out)                                                                  # a second export replaces the first
    assert len(pd.read_parquet(out / "daily")) == 60


def test_export_refuses_to_clear_a_folder_outside_the_data_directory(tmp_path):
    from app.bigdata import export
    with pytest.raises(ValueError):
        export._fresh_dir(tmp_path / "elsewhere")


@pytest.fixture(scope="module")
def spark():
    status = spark_session.spark_status()
    if status["problems"]:
        pytest.skip("PySpark, PyArrow or Java is not available: " + "; ".join(status["problems"]))
    session = spark_session.get_spark("bharatmarket-tests", shuffle_partitions=4, driver_memory="2g")
    yield session
    spark_session.stop_spark()


@pytest.fixture(scope="module")
def spark_data(spark, tmp_path_factory):
    """The same small tables for pandas and Spark: symbols with '&' and '-' (the tricky column names)."""
    base = tmp_path_factory.mktemp("parquet")
    series = {"M&M": wiggly(260, 1), "BAJAJ-AUTO": wiggly(260, 2), "TCS": wiggly(260, 3), "NIFTY_50": wiggly(260, 4)}
    daily = daily_frame(series, sectors={"M&M": "Auto", "BAJAJ-AUTO": "Auto", "TCS": "IT"}, types={"NIFTY_50": "index"})
    intraday = pd.concat([bars("NIFTY_50", "2024-01-01 03:45", wiggly(80, 5), "1m"), bars("NIFTY_50", "2024-01-02 03:45", wiggly(60, 6), "1m"),
                          bars("NIFTY_50", "2024-01-01 03:45", wiggly(16, 7), "5m", 5), bars("NIFTY_50", "2024-01-02 03:45", wiggly(12, 8), "5m", 5)])
    write_parquet_fixture(base, daily, intraday)
    return base, daily, intraday


def test_spark_returns_and_volatility_equal_pandas(spark, spark_data):
    from app.bigdata import spark_jobs as sj
    base, daily, _ = spark_data
    got = sj.latest_per_symbol(sj.with_volatility(sj.with_returns(sj.read_daily(spark, base)))).toPandas().set_index("symbol").sort_index()
    want = pandas_jobs.latest_per_symbol(pandas_jobs.returns_and_volatility(daily), ["ret", "hist_vol_20"]).set_index("symbol").sort_index()
    assert list(got.index) == list(want.index)
    assert got["ret"].tolist() == pytest.approx(want["ret"].tolist()) and got["hist_vol_20"].tolist() == pytest.approx(want["hist_vol_20"].tolist())


def test_spark_sector_returns_equal_pandas(spark, spark_data):
    from app.bigdata import spark_jobs as sj
    base, daily, _ = spark_data
    got = sj.sector_daily_returns(sj.with_returns(sj.read_daily(spark, base))).toPandas().sort_values(["sector", "date"]).reset_index(drop=True)
    want = pandas_jobs.sector_daily_returns(pandas_jobs.returns_and_volatility(daily)).sort_values(["sector", "date"]).reset_index(drop=True)
    assert len(got) == len(want) and got["ret"].tolist() == pytest.approx(want["ret"].tolist()) and got["n"].tolist() == want["n"].tolist()


def test_spark_correlation_equals_pandas_with_special_symbol_names(spark, spark_data):
    from app.bigdata import spark_jobs as sj
    base, daily, _ = spark_data
    symbols = ["BAJAJ-AUTO", "M&M", "NIFTY_50", "TCS"]
    start, end = daily["date"].min(), daily["date"].max()
    got, n = sj.correlation_matrix(sj.with_returns(sj.read_daily(spark, base)), symbols, start, end)
    want, m = pandas_jobs.correlation_matrix(pandas_jobs.returns_and_volatility(daily), symbols, start, end)
    assert n == m and np.allclose(got.values, want.values, atol=1e-9)


def test_spark_market_trend_equals_pandas(spark, spark_data):
    from app.bigdata import spark_jobs as sj
    base, daily, _ = spark_data
    got = sj.market_trend(sj.read_daily(spark, base)).toPandas().sort_values("date").reset_index(drop=True)
    want = pandas_jobs.market_trend(daily).sort_values("date").reset_index(drop=True)
    assert len(got) == len(want) > 0 and got["pct_above_sma200"].tolist() == pytest.approx(want["pct_above_sma200"].tolist())


def test_spark_and_pandas_agree_on_an_exact_tie_with_the_average(spark, tmp_path):
    from app.bigdata import export
    from app.bigdata import spark_jobs as sj
    df = daily_frame({"FLAT": [101.35] * 230, "UP": g(0.003, 230)})
    export._write(df, tmp_path / "daily" / "part-0.parquet")
    got = sj.market_trend(sj.read_daily(spark, tmp_path)).toPandas()
    assert got["stocks"].eq(2).all() and got["above"].eq(1).all()


def test_spark_realised_variance_equals_pandas(spark, spark_data):
    from pyspark.sql import functions as F

    from app.bigdata import spark_jobs as sj
    base, _, intraday = spark_data
    got = sj.intraday_realised_variance(sj.read_intraday(spark, base).filter(F.col("interval") == "5m")).toPandas()
    want = pandas_jobs.intraday_realised_variance(intraday[intraday["interval"] == "5m"])
    got["day"], want["day"] = got["day"].astype(str), want["day"].astype(str)
    got, want = got.sort_values("day").reset_index(drop=True), want.sort_values("day").reset_index(drop=True)
    assert got["rv"].tolist() == pytest.approx(want["rv"].tolist()) and got["returns"].tolist() == want["returns"].tolist()


def test_spark_resample_equals_pandas_and_range_stats_match(spark, spark_data):
    from pyspark.sql import functions as F

    from app.bigdata import spark_jobs as sj
    base, _, intraday = spark_data
    one = intraday[intraday["interval"] == "1m"]
    got = sj.resample_bars(sj.read_intraday(spark, base).filter(F.col("interval") == "1m"), 5).toPandas().sort_values("ts").reset_index(drop=True)
    want = pandas_jobs.resample_bars(one, 5).sort_values("ts").reset_index(drop=True)
    assert len(got) == len(want)
    for col in ("open", "high", "low", "close", "volume", "bars"):
        assert got[col].tolist() == pytest.approx(want[col].tolist())
    stats = sj.daily_range_stats(sj.read_intraday(spark, base)).toPandas()
    ref = pandas_jobs.daily_range_stats(intraday)
    assert len(stats) == len(ref) and stats["bars"].sum() == ref["bars"].sum()


def test_spark_session_uses_utc_and_lenient_division(spark):
    assert spark.conf.get("spark.sql.session.timeZone") == "UTC" and spark.conf.get("spark.sql.ansi.enabled") == "false"
    assert spark.sql("select 1.0 / 0").collect()[0][0] is None                       # NULL, not an error, as in pandas
