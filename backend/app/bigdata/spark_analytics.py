"""Run the Spark jobs on the real data and store the results in `analytics_results` (scope {"engine": "spark"}).

Besides producing results, two of them CHECK the pipeline:
  spark_returns_volatility  compares Spark's 20-day volatility with the Phase 7 `indicators` collection
  spark_resample_validation rebuilds the 5-minute bars from the 1-minute bars and compares them with the
                            5-minute bars the data source provides
"""
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from pymongo.database import Database

from app.analytics.market import iso, store_result
from app.bigdata import pandas_jobs, spark_jobs as sj
from app.bigdata.benchmark import INDEX_SYMBOLS, current_universe

SCOPE = {"engine": "spark"}


def _returns_volatility(db: Database, latest: pd.DataFrame) -> dict:
    symbols, diffs = {}, []
    for r in latest.itertuples():
        symbols[r.symbol] = {"date": iso(r.date), "ret_pct": None if pd.isna(r.ret) else round(float(r.ret) * 100, 4),
                             "hist_vol_20": None if pd.isna(r.hist_vol_20) else round(float(r.hist_vol_20), 4)}
        stored = db["indicators"].find_one({"meta.symbol": r.symbol, "meta.interval": "1d", "ts": r.date.to_pydatetime()},
                                           {"hist_vol_20": 1})
        if stored and "hist_vol_20" in stored and not pd.isna(r.hist_vol_20):
            diffs.append(abs(float(r.hist_vol_20) - stored["hist_vol_20"]))
    return {"symbols": symbols, "cross_check_vs_indicators": {
        "compared": len(diffs), "max_abs_difference": max(diffs) if diffs else None,
        "note": "Spark's 20-day volatility against the pandas indicator stored in Phase 7 (same formula, rounded to 6 decimals)"}}


def _market_trend(series: pd.DataFrame) -> dict:
    s = series.sort_values("date").reset_index(drop=True)
    monthly = s.set_index("date")["pct_above_sma200"].resample("ME").last().dropna().tail(120)
    return {"days": int(len(s)), "first_date": iso(s["date"].min()), "last_date": iso(s["date"].max()),
            "latest_pct_above_sma200": round(float(s["pct_above_sma200"].iloc[-1]), 1), "latest_stocks": int(s["stocks"].iloc[-1]),
            "mean_pct": round(float(s["pct_above_sma200"].mean()), 1),
            "lowest": {"date": iso(s.loc[s["pct_above_sma200"].idxmin(), "date"]), "pct": round(float(s["pct_above_sma200"].min()), 1)},
            "highest": {"date": iso(s.loc[s["pct_above_sma200"].idxmax(), "date"]), "pct": round(float(s["pct_above_sma200"].max()), 1)},
            "monthly_pct_above_sma200": [[iso(d), round(float(v), 1)] for d, v in monthly.items()],
            "note": "stocks only; each day counts the stocks that already have 200 days of history"}


def _resample_validation(spark, base: Path, symbol: str = "NIFTY_50", tol: float = 1e-6) -> dict:
    from pyspark.sql import functions as F
    one = sj.read_intraday(spark, base, interval="1m", symbols=[symbol])
    rebuilt = sj.resample_bars(one, 5).select("ts", "open", "high", "low", "close", "bars")
    stored = (sj.read_intraday(spark, base, interval="5m", symbols=[symbol])
              .select("ts", F.col("open").alias("s_open"), F.col("high").alias("s_high"),
                      F.col("low").alias("s_low"), F.col("close").alias("s_close")))
    joined = rebuilt.join(stored, "ts", "full_outer")
    both = F.col("open").isNotNull() & F.col("s_open").isNotNull()
    exact = (F.abs(F.col("open") - F.col("s_open")) < tol) & (F.abs(F.col("high") - F.col("s_high")) < tol) & \
            (F.abs(F.col("low") - F.col("s_low")) < tol) & (F.abs(F.col("close") - F.col("s_close")) < tol)
    row = joined.agg(
        F.sum(F.col("open").isNotNull().cast("int")).alias("rebuilt"), F.sum(F.col("s_open").isNotNull().cast("int")).alias("stored"),
        F.sum(both.cast("int")).alias("matched_ts"),
        F.sum((F.col("open").isNotNull() & F.col("s_open").isNull()).cast("int")).alias("only_rebuilt"),
        F.sum((F.col("open").isNull() & F.col("s_open").isNotNull()).cast("int")).alias("only_stored"),
        F.sum((both & exact).cast("int")).alias("exact"),
        F.sum((both & (F.abs(F.col("close") - F.col("s_close")) < tol)).cast("int")).alias("close_equal"),
        F.max(F.when(both, F.abs(F.col("close") - F.col("s_close")))).alias("max_close_diff")).collect()[0]
    matched = int(row["matched_ts"] or 0)
    return {"symbol": symbol, "bars_rebuilt_from_1m": int(row["rebuilt"] or 0), "bars_in_source_5m": int(row["stored"] or 0),
            "timestamps_in_both": matched, "only_in_rebuilt": int(row["only_rebuilt"] or 0), "only_in_source": int(row["only_stored"] or 0),
            "ohlc_identical_pct": round(100.0 * int(row["exact"] or 0) / matched, 2) if matched else None,
            "close_identical_pct": round(100.0 * int(row["close_equal"] or 0) / matched, 2) if matched else None,
            "max_abs_close_difference": float(row["max_close_diff"]) if row["max_close_diff"] is not None else None}


def run_spark_analytics(db: Database, spark, base: Path, *, progress: Optional[Callable[[str], None]] = None) -> dict:
    say = progress or (lambda _m: None)
    daily = sj.read_daily(spark, base)
    with_vol = sj.with_volatility(sj.with_returns(daily)).persist()
    stored = {}

    latest = sj.latest_per_symbol(with_vol).toPandas()
    stored["spark_returns_volatility"] = _returns_volatility(db, latest)
    say(f"  returns and volatility: {len(latest)} symbols")

    sector = sj.sector_daily_returns(with_vol).toPandas()
    stored["spark_sector_returns"] = {"sectors": pandas_jobs.summarise_sector_returns(sector),
                                      "note": "equal-weighted daily returns of the stocks in each sector; the stock list changes over time"}
    say(f"  sector returns: {sector['sector'].nunique()} sectors")

    symbols, start, end = current_universe(db)
    corr, n = sj.correlation_matrix(with_vol, symbols, start, end)
    stored["spark_correlation"] = {"symbols": symbols, "matrix": corr.round(4).values.tolist(), "observations": n,
                                   "window": {"from": iso(start), "to": iso(end)}}
    say(f"  correlation: {len(symbols)} symbols, {n} days")

    stored["spark_market_trend"] = _market_trend(sj.market_trend(daily).toPandas())
    say("  market trend: done")

    five = sj.read_intraday(spark, base, interval="5m", symbols=INDEX_SYMBOLS)
    stored["spark_intraday_realised_vol"] = {
        "annualised_realised_vol_pct_by_year": pandas_jobs.summarise_realised_variance(sj.intraday_realised_variance(five).toPandas()),
        "note": "from 5-minute bars, returns within each day only (overnight gaps excluded); sqrt(daily realised variance x 252)"}
    say("  intraday realised volatility: done")

    stored["spark_resample_validation"] = _resample_validation(spark, base)
    say(f"  resample validation: {stored['spark_resample_validation']['timestamps_in_both']:,} bars compared")
    with_vol.unpersist()

    for analysis_type, result in stored.items():
        store_result(db, analysis_type, SCOPE, result)             # store_result converts NumPy types and NaN itself
    return {"stored": len(stored), "types": list(stored), "results": stored}
