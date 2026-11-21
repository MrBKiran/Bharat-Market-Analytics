"""The same jobs on Apache Spark (PySpark DataFrames).

Every function takes and returns Spark DataFrames (or small pandas results), reads the Parquet columns
documented in pandas_jobs.py, and follows the same conventions, so the three engines give the same answers.
PySpark is imported inside the functions, so importing this module never needs Spark installed.
"""
import math
from pathlib import Path

import pandas as pd

TRADING_DAYS = 252
IST = "Asia/Kolkata"
TIE_TOLERANCE = 1e-9          # keep identical to pandas_jobs.TIE_TOLERANCE


def read_daily(spark, base: Path):
    """Daily bars as a Spark DataFrame.

    On Windows, Spark's own Parquet reader needs the native files winutils.exe and hadoop.dll (Hadoop's
    local file layer) and fails without them. So PyArrow reads the files and hands them to Spark in memory
    (Arrow transfer); every job below still runs inside Spark. On Linux or a cluster, replace this with
    `spark.read.parquet(...)` and the transfer cost disappears.
    """
    import pyarrow.parquet as pq
    return spark.createDataFrame(pq.read_table(Path(base) / "daily"))


def read_intraday(spark, base: Path, interval: str = None, symbols: list = None):
    """Intraday bars (Hive-partitioned: symbol and interval come from the folder names).

    `interval` and `symbols` prune whole partitions before anything is loaded, the same way the pandas
    engine does with `read_parquet(filters=...)`.
    """
    import pyarrow as pa
    import pyarrow.dataset as ds
    partitioning = ds.partitioning(pa.schema([("symbol", pa.string()), ("interval", pa.string())]), flavor="hive")
    dataset = ds.dataset(Path(base) / "intraday", format="parquet", partitioning=partitioning)
    expression = None
    if interval:
        expression = ds.field("interval") == interval
    if symbols:
        in_symbols = ds.field("symbol").isin(symbols)
        expression = in_symbols if expression is None else (expression & in_symbols)
    return spark.createDataFrame(dataset.to_table(filter=expression))


def with_returns(daily):
    from pyspark.sql import Window
    from pyspark.sql import functions as F
    w = Window.partitionBy("symbol").orderBy("date")
    prev = F.lag("close").over(w)
    return (daily.withColumn("prev_close", prev)
            .withColumn("ret", F.col("close") / F.col("prev_close") - 1)
            .withColumn("log_ret", F.log(F.col("close") / F.col("prev_close"))))


def with_volatility(df, window: int = 20):
    """20-day historical volatility (%), defined only once 20 log returns exist."""
    from pyspark.sql import Window
    from pyspark.sql import functions as F
    w = Window.partitionBy("symbol").orderBy("date").rowsBetween(-(window - 1), 0)
    return (df.withColumn("_n", F.count("log_ret").over(w))
            .withColumn("hist_vol_20", F.when(F.col("_n") >= window,
                                              F.stddev_samp("log_ret").over(w) * F.lit(math.sqrt(TRADING_DAYS)) * 100))
            .drop("_n"))


def latest_per_symbol(df):
    from pyspark.sql import functions as F
    return df.groupBy("symbol").agg(F.max("date").alias("date"), F.max_by("ret", "date").alias("ret"),
                                    F.max_by("hist_vol_20", "date").alias("hist_vol_20"))


def sector_daily_returns(df):
    from pyspark.sql import functions as F
    return (df.filter((F.col("instrument_type") != "index") & F.col("sector").isNotNull() & F.col("ret").isNotNull())
            .groupBy("sector", "date").agg(F.avg("ret").alias("ret"), F.count("ret").alias("n")))


def correlation_matrix(df, symbols: list[str], start, end) -> tuple[pd.DataFrame, int]:
    """Pearson correlation with Spark ML, over the days on which every listed symbol has a return."""
    from pyspark.ml.feature import VectorAssembler
    from pyspark.ml.stat import Correlation
    from pyspark.sql import functions as F
    lo, hi = pd.Timestamp(start).to_pydatetime(), pd.Timestamp(end).to_pydatetime()      # Spark wants plain datetimes
    sub = df.filter(F.col("symbol").isin(symbols) & (F.col("date") >= F.lit(lo)) & (F.col("date") <= F.lit(hi)))
    wide = sub.groupBy("date").pivot("symbol", symbols).agg(F.first("ret"))
    vectors = VectorAssembler(inputCols=symbols, outputCol="features", handleInvalid="skip").transform(wide).select("features")
    matrix = Correlation.corr(vectors, "features", "pearson").head()[0].toArray()
    return pd.DataFrame(matrix, index=symbols, columns=symbols), int(vectors.count())


def market_trend(daily, window: int = 200):
    from pyspark.sql import Window
    from pyspark.sql import functions as F
    w = Window.partitionBy("symbol").orderBy("date").rowsBetween(-(window - 1), 0)
    df = (daily.filter(F.col("instrument_type") != "index")
          .withColumn("_n", F.count("close").over(w))
          .withColumn("sma", F.when(F.col("_n") >= window, F.avg("close").over(w))))
    return (df.filter(F.col("sma").isNotNull()).groupBy("date")
            # same tie rule as pandas_jobs.market_trend: above by more than rounding noise, never on an exact tie
            .agg(F.count("*").alias("stocks"),
                 F.sum(F.when(F.col("close") > F.col("sma") * F.lit(1 + TIE_TOLERANCE), 1).otherwise(0)).alias("above"))
            .withColumn("pct_above_sma200", 100.0 * F.col("above") / F.col("stocks")))


def intraday_realised_variance(intraday):
    from pyspark.sql import Window
    from pyspark.sql import functions as F
    day = F.to_date(F.from_utc_timestamp("ts", IST))
    w = Window.partitionBy("symbol", "interval", "day").orderBy("ts")
    df = intraday.withColumn("day", day).withColumn("r", F.log(F.col("close") / F.lag("close").over(w)))
    return (df.filter(F.col("r").isNotNull()).groupBy("symbol", "interval", "day")
            .agg(F.sum(F.col("r") * F.col("r")).alias("rv"), F.count("r").alias("returns")))


def resample_bars(intraday_1m, minutes: int = 5):
    """Combine bars into larger bars with a time window: open of the first, close of the last."""
    from pyspark.sql import functions as F
    bars = (intraday_1m.groupBy("symbol", F.window("ts", f"{minutes} minutes").alias("w"))
            .agg(F.min_by("open", "ts").alias("open"), F.max("high").alias("high"), F.min("low").alias("low"),
                 F.max_by("close", "ts").alias("close"), F.sum("volume").alias("volume"), F.count("*").alias("bars")))
    return bars.select("symbol", F.col("w.start").alias("ts"), "open", "high", "low", "close", "volume", "bars")


def daily_range_stats(intraday):
    from pyspark.sql import functions as F
    day = F.to_date(F.from_utc_timestamp("ts", IST))
    return (intraday.withColumn("day", day).groupBy("symbol", "interval", "day")
            .agg(F.count("*").alias("bars"), F.max("high").alias("high"), F.min("low").alias("low"),
                 F.sum("volume").alias("volume"), F.avg("close").alias("mean_close")))
