"""The local Spark session used by the big-data jobs.

PySpark is an optional dependency: nothing else in the project imports it, and `spark_status()` reports
what is missing instead of raising. Spark runs in local mode (all CPU cores, one machine); the same jobs
would run unchanged on a cluster by changing `master`.

Settings chosen on purpose:
  * ANSI mode OFF   Spark 4 defaults to ANSI mode, where a division by zero or a bad cast raises an
                    error. Price data has zero prices and zero volumes, so we want NULL instead,
                    like pandas gives NaN.
  * time zone UTC   every timestamp in the database is UTC.
  * Arrow ON        fast transfer of results to pandas.
"""
import logging
import os
import sys
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def spark_status() -> dict:
    """What is installed and reachable. Never raises."""
    problems = []
    version = None
    try:
        import pyspark
        version = pyspark.__version__
    except ImportError:
        problems.append("pyspark is not installed (pip install pyspark)")
    try:
        import pyarrow
        arrow = pyarrow.__version__
    except ImportError:
        arrow = None
        problems.append("pyarrow is not installed (pip install pyarrow)")

    java_home = get_settings().java_home or os.environ.get("JAVA_HOME")
    java_ok = False
    if not java_home:
        problems.append("JAVA_HOME is not set (put it in .env; Spark needs JDK 17)")
    elif not any((Path(java_home) / "bin" / exe).exists() for exe in ("java.exe", "java")):
        problems.append(f"no java executable under JAVA_HOME={java_home}")
    else:
        java_ok = True
    return {"pyspark": version, "pyarrow": arrow, "java_home": java_home, "java_ok": java_ok, "problems": problems}


def get_spark(app_name: str = "bharatmarket", *, master: str = "local[*]", driver_memory: str = "4g",
              shuffle_partitions: int = 24):
    """Create (or reuse) the Spark session. Raises RuntimeError with a clear message if Spark cannot start."""
    status = spark_status()
    if status["problems"]:
        raise RuntimeError("; ".join(status["problems"]))
    os.environ["JAVA_HOME"] = status["java_home"]
    os.environ["PYSPARK_PYTHON"] = sys.executable            # workers use the project's own Python
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable

    from pyspark.sql import SparkSession
    spark = (SparkSession.builder.appName(app_name).master(master)
             .config("spark.driver.memory", driver_memory)
             .config("spark.sql.shuffle.partitions", str(shuffle_partitions))
             .config("spark.sql.session.timeZone", "UTC")
             .config("spark.sql.ansi.enabled", "false")
             .config("spark.sql.execution.arrow.pyspark.enabled", "true")
             .config("spark.driver.host", "127.0.0.1")
             .config("spark.ui.enabled", "false")
             .config("spark.ui.showConsoleProgress", "false")
             .getOrCreate())
    spark.sparkContext.setLogLevel("ERROR")
    return spark


def stop_spark() -> None:
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        return
    active = SparkSession.getActiveSession()
    if active is not None:
        active.stop()
