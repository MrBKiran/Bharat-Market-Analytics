"""Collection definitions for BharatMarketDB: validators, indexes, time-series options.

Design rules (verified against MongoDB 8.2):
  * Time-series collections do NOT support validators or unique indexes, so
    `intraday_prices` and `indicators` are validated and de-duplicated by the
    importer (Phase 5), backed by the `import_files` and `intraday_coverage`
    registries.
  * Everything else is a regular collection with a strict $jsonSchema
    validator and unique indexes, so bad or duplicate rows are rejected by
    the database itself.

Timestamp conventions (all BSON Date, always UTC):
  * Daily/weekly/monthly bars: `date` = 00:00:00 UTC of the exchange trading
    date (a calendar date, not an instant).
  * Intraday bars: `ts` = the real UTC instant the bar opened
    (09:15 IST = 03:45 UTC).
"""
from dataclasses import dataclass
from typing import Optional

from pymongo import ASCENDING as ASC
from pymongo import DESCENDING as DESC

# ----------------------------------------------------------------------------
# Controlled vocabularies (shared with importers and the API)
# ----------------------------------------------------------------------------
EXCHANGES = ("NSE", "BSE")
INSTRUMENT_TYPES = ("equity", "index", "etf", "future", "option")
DAILY_INTERVALS = ("1d", "1w", "1M")
INTRADAY_INTERVALS = ("1m", "5m", "15m", "30m", "1h")
DATA_CLASSES = ("historical", "delayed", "real_time", "synthetic")
JOB_TYPES = ("import", "clean", "indicators", "backtest", "spark", "benchmark", "report")
JOB_STATUSES = ("queued", "running", "paused", "completed", "failed", "cancelled")
ACTION_TYPES = ("split", "bonus", "dividend", "rights", "merger", "demerger")
BENCHMARK_TECHNOLOGIES = ("pandas", "mongodb_aggregation", "pyspark", "ingestion")

# Time-series options shared by the two time-series collections.
TIMESERIES_OPTIONS = {"timeField": "ts", "metaField": "meta", "granularity": "minutes"}
# Fields every intraday document must carry (checked by the importer, not by MongoDB).
INTRADAY_META_FIELDS = ("symbol", "exchange", "interval", "instrument_type", "source", "is_synthetic",
                        "price_basis")
INTRADAY_VALUE_FIELDS = ("ts", "open", "high", "low", "close", "volume")

# ----------------------------------------------------------------------------
# $jsonSchema building blocks
# ----------------------------------------------------------------------------
_NUM = ["double", "int", "long", "decimal"]  # accept ints as well as floats
STR = {"bsonType": "string"}
DATE = {"bsonType": "date"}
BOOL = {"bsonType": "bool"}
OBJ = {"bsonType": "object"}
ARR = {"bsonType": "array"}
NUMBER = {"bsonType": _NUM}
POSITIVE = {"bsonType": _NUM, "minimum": 0, "exclusiveMinimum": True}
NON_NEG = {"bsonType": _NUM, "minimum": 0}
INT_NON_NEG = {"bsonType": ["int", "long"], "minimum": 0}
# Canonical symbol: upper-case, no spaces, no exchange suffix (e.g. RELIANCE, M&M, NIFTY_50).
SYMBOL = {"bsonType": "string", "pattern": r"^[A-Z0-9&_-]{1,30}$"}
SHA256 = {"bsonType": "string", "pattern": r"^[a-f0-9]{64}$"}


def enum(*values: str) -> dict:
    return {"bsonType": "string", "enum": list(values)}


def schema(required: list[str], properties: dict) -> dict:
    return {"$jsonSchema": {"bsonType": "object", "required": required, "properties": properties}}


# OHLC sanity rules: high is the max and low is the min of the bar.
_OHLC_RULES = {
    "$expr": {
        "$and": [
            {"$gte": ["$high", "$low"]},
            {"$gte": ["$high", "$open"]},
            {"$gte": ["$high", "$close"]},
            {"$lte": ["$low", "$open"]},
            {"$lte": ["$low", "$close"]},
        ]
    }
}


def with_ohlc(json_schema: dict) -> dict:
    return {"$and": [json_schema, _OHLC_RULES]}


# ----------------------------------------------------------------------------
# Spec containers
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class IndexSpec:
    name: str
    keys: tuple[tuple[str, int], ...]
    unique: bool = False


@dataclass(frozen=True)
class CollectionSpec:
    name: str
    description: str
    indexes: tuple[IndexSpec, ...]
    validator: Optional[dict] = None       # None for time-series collections
    timeseries: Optional[dict] = None


def _bar_properties(extra: Optional[dict] = None) -> dict:
    props = {
        "symbol": SYMBOL,
        "exchange": enum(*EXCHANGES),
        "interval": enum(*DAILY_INTERVALS),
        "date": DATE,
        "open": POSITIVE,
        "high": POSITIVE,
        "low": POSITIVE,
        "close": POSITIVE,
        "volume": NON_NEG,
        "source": STR,
        "retrieved_at": DATE,
        "is_synthetic": BOOL,
        # raw = as traded; split_adjusted = provider already adjusted prices for splits/bonuses (and
        # possibly other events such as demergers; dividends excluded) - do NOT adjust again;
        # index_level = an index value (no adjustment applies)
        "price_basis": enum("raw", "split_adjusted", "index_level"),
        "import_job_id": STR,
        "quality_flags": ARR,
    }
    props.update(extra or {})
    return props


_BAR_REQUIRED = ["symbol", "exchange", "interval", "date", "open", "high", "low", "close",
                 "source", "retrieved_at", "is_synthetic"]

# ----------------------------------------------------------------------------
# The collections
# ----------------------------------------------------------------------------
STOCK_PRICES = CollectionSpec(
    name="stock_prices",
    description="Daily/weekly/monthly OHLCV bars for equities and ETFs (regular collection: "
                "needs exact de-duplication and later updates for corporate-action adjustment).",
    validator=with_ohlc(schema(
        _BAR_REQUIRED + ["volume"],
        _bar_properties({
            "instrument_type": enum("equity", "etf"),
            "adj_close": POSITIVE,
            "prev_close": POSITIVE,
            "turnover": NON_NEG,
            "trades": INT_NON_NEG,
            "isin": STR,
            "series": STR,
        }),
    )),
    indexes=(
        # `source` is part of the key so the same bar from two providers (e.g. Yahoo split-adjusted
        # and NSE raw) can coexist; queries pick a source or compare them.
        IndexSpec("uq_symbol_exchange_interval_source_date",
                  (("symbol", ASC), ("exchange", ASC), ("interval", ASC), ("source", ASC), ("date", ASC)),
                  unique=True),
        IndexSpec("idx_date_interval", (("date", DESC), ("interval", ASC))),
    ),
)

MARKET_INDICES = CollectionSpec(
    name="market_indices",
    description="Daily/weekly/monthly OHLC bars for indices (NIFTY 50, SENSEX, ...). Volume is optional.",
    validator=with_ohlc(schema(_BAR_REQUIRED, _bar_properties())),
    indexes=(
        IndexSpec("uq_symbol_exchange_interval_source_date",
                  (("symbol", ASC), ("exchange", ASC), ("interval", ASC), ("source", ASC), ("date", ASC)),
                  unique=True),
        IndexSpec("idx_date_interval", (("date", DESC), ("interval", ASC))),
    ),
)

INTRADAY_PRICES = CollectionSpec(
    name="intraday_prices",
    description="Intraday OHLCV bars (1m-1h) for equities and indices. TIME-SERIES collection: "
                "no validator or unique index possible, so the importer validates and de-duplicates.",
    timeseries=TIMESERIES_OPTIONS,
    indexes=(
        IndexSpec("idx_symbol_exchange_interval_ts",
                  (("meta.symbol", ASC), ("meta.exchange", ASC), ("meta.interval", ASC), ("ts", DESC))),
    ),
)

INDICATORS = CollectionSpec(
    name="indicators",
    description="Computed technical indicators per bar (RSI, MACD, EMAs, ATR, ...). TIME-SERIES; "
                "recompute = delete by meta, then re-insert.",
    timeseries=TIMESERIES_OPTIONS,
    indexes=(
        IndexSpec("idx_symbol_exchange_interval_ts",
                  (("meta.symbol", ASC), ("meta.exchange", ASC), ("meta.interval", ASC), ("ts", DESC))),
    ),
)

INSTRUMENTS = CollectionSpec(
    name="instruments",
    description="Master list of supported instruments (equities, indices, ETFs, derivatives).",
    validator=schema(
        ["symbol", "exchange", "name", "instrument_type", "active", "created_at"],
        {
            "symbol": SYMBOL,
            "exchange": enum(*EXCHANGES),
            "name": STR,
            "instrument_type": enum(*INSTRUMENT_TYPES),
            "sector": STR,
            "industry": STR,
            "isin": STR,
            "yfinance_symbol": STR,
            "currency": enum("INR"),
            "active": BOOL,
            "expiry": DATE,
            "strike": POSITIVE,
            "option_type": enum("CE", "PE"),
            "lot_size": {"bsonType": ["int", "long"], "minimum": 1},
            "created_at": DATE,
        },
    ),
    indexes=(
        IndexSpec("uq_instrument",
                  (("exchange", ASC), ("symbol", ASC), ("instrument_type", ASC),
                   ("expiry", ASC), ("strike", ASC), ("option_type", ASC)), unique=True),
        IndexSpec("idx_sector", (("sector", ASC),)),
    ),
)

CORPORATE_ACTIONS = CollectionSpec(
    name="corporate_actions",
    description="Splits, bonuses and dividends used to adjust historical prices.",
    validator=schema(
        ["symbol", "exchange", "action_type", "ex_date", "source", "retrieved_at"],
        {
            "symbol": SYMBOL,
            "exchange": enum(*EXCHANGES),
            "action_type": enum(*ACTION_TYPES),
            "ex_date": DATE,
            "factor": POSITIVE,        # price adjustment factor, e.g. 0.5 for a 1:2 split
            "amount": NON_NEG,         # cash dividend per share (INR)
            "details": STR,
            "source": STR,
            "retrieved_at": DATE,
        },
    ),
    indexes=(
        IndexSpec("uq_symbol_exchange_type_exdate",
                  (("symbol", ASC), ("exchange", ASC), ("action_type", ASC), ("ex_date", ASC)), unique=True),
    ),
)

MARKET_SOURCES = CollectionSpec(
    name="market_sources",
    description="Registry of data providers, their access rules and whether data is historical, "
                "delayed, real-time or synthetic.",
    validator=schema(
        ["source_id", "name", "data_class", "requires_auth", "active", "created_at"],
        {
            "source_id": {"bsonType": "string", "pattern": r"^[a-z0-9_]{2,40}$"},
            "name": STR,
            "url": STR,
            "data_class": enum(*DATA_CLASSES),
            "requires_auth": BOOL,
            "license_notes": STR,
            "active": BOOL,
            "last_success_at": DATE,
            "created_at": DATE,
        },
    ),
    indexes=(IndexSpec("uq_source_id", (("source_id", ASC),), unique=True),),
)

TRADING_SIGNALS = CollectionSpec(
    name="trading_signals",
    description="Buy/sell/exit signals generated by strategies.",
    validator=schema(
        ["run_id", "symbol", "exchange", "interval", "ts", "strategy", "signal", "created_at"],
        {
            "run_id": STR,
            "symbol": SYMBOL,
            "exchange": enum(*EXCHANGES),
            "interval": STR,
            "ts": DATE,
            "strategy": STR,
            "signal": enum("buy", "sell", "exit_long", "exit_short"),
            "price": POSITIVE,
            "params": OBJ,
            "created_at": DATE,
        },
    ),
    indexes=(
        IndexSpec("idx_symbol_interval_ts", (("symbol", ASC), ("interval", ASC), ("ts", DESC))),
        IndexSpec("idx_run_id", (("run_id", ASC),)),
    ),
)

BACKTEST_RESULTS = CollectionSpec(
    name="backtest_results",
    description="One document per backtest run: parameters, cost assumptions, metrics, equity curve.",
    validator=schema(
        ["run_id", "strategy", "symbol", "exchange", "interval", "params", "metrics", "created_at"],
        {
            "run_id": STR,
            "strategy": STR,
            "symbol": SYMBOL,
            "exchange": enum(*EXCHANGES),
            "interval": STR,
            "start": DATE,
            "end": DATE,
            "params": OBJ,
            "costs": OBJ,
            "metrics": OBJ,
            "equity_curve": ARR,
            "trades": ARR,
            "created_at": DATE,
        },
    ),
    indexes=(
        IndexSpec("uq_run_id", (("run_id", ASC),), unique=True),
        IndexSpec("idx_strategy_symbol_created", (("strategy", ASC), ("symbol", ASC), ("created_at", DESC))),
    ),
)

PROCESSING_JOBS = CollectionSpec(
    name="processing_jobs",
    description="Imports, cleaning, indicator, Spark and report jobs: status, progress and resume checkpoint.",
    validator=schema(
        ["job_id", "job_type", "status", "progress", "created_at"],
        {
            "job_id": STR,
            "job_type": enum(*JOB_TYPES),
            "status": enum(*JOB_STATUSES),
            "progress": {"bsonType": _NUM, "minimum": 0, "maximum": 100},
            "params": OBJ,
            "source": STR,
            "checkpoint": OBJ,          # e.g. {"file": "...", "row_offset": 120000} for resuming
            "counts": OBJ,              # read / inserted / duplicates / rejected
            "error": STR,
            "created_at": DATE,
            "started_at": DATE,
            "finished_at": DATE,
            "updated_at": DATE,
        },
    ),
    indexes=(
        IndexSpec("uq_job_id", (("job_id", ASC),), unique=True),
        IndexSpec("idx_status_created", (("status", ASC), ("created_at", DESC))),
        IndexSpec("idx_type_created", (("job_type", ASC), ("created_at", DESC))),
    ),
)

ANALYTICS_RESULTS = CollectionSpec(
    name="analytics_results",
    description="Cached analytics outputs (trend, breadth, correlation, sector performance, ...).",
    validator=schema(
        ["analysis_type", "scope", "result", "computed_at"],
        {
            "analysis_type": STR,
            "scope": OBJ,
            "interval": STR,
            "params": OBJ,
            "result": OBJ,
            "data_range": OBJ,
            "is_synthetic": BOOL,
            "computed_at": DATE,
        },
    ),
    indexes=(IndexSpec("idx_type_computed", (("analysis_type", ASC), ("computed_at", DESC))),),
)

ECONOMIC_DATA = CollectionSpec(
    name="economic_data",
    description="Macro indicators from RBI, MOSPI and other official sources.",
    validator=schema(
        ["series_id", "name", "source", "frequency", "date", "value", "retrieved_at"],
        {
            "series_id": {"bsonType": "string", "pattern": r"^[A-Za-z0-9_.-]{1,60}$"},
            "name": STR,
            "source": STR,
            "frequency": enum("daily", "weekly", "monthly", "quarterly", "annual"),
            "date": DATE,
            "value": NUMBER,
            "unit": STR,
            "retrieved_at": DATE,
        },
    ),
    indexes=(IndexSpec("uq_series_date", (("series_id", ASC), ("date", ASC)), unique=True),),
)

# ---- Supporting collections (added beyond the original 12) ------------------
IMPORT_FILES = CollectionSpec(
    name="import_files",
    description="One document per imported file, keyed by SHA-256: prevents importing the same "
                "file twice and records source and retrieval time.",
    validator=schema(
        ["sha256", "filename", "size_bytes", "source", "status", "created_at"],
        {
            "sha256": SHA256,
            "filename": STR,
            "size_bytes": INT_NON_NEG,
            "source": STR,
            "dataset": STR,
            "status": enum("processing", "completed", "failed"),
            "rows_read": INT_NON_NEG,
            "rows_inserted": INT_NON_NEG,
            "rows_duplicate": INT_NON_NEG,
            "rows_rejected": INT_NON_NEG,
            "retrieved_at": DATE,
            "job_id": STR,
            "is_synthetic": BOOL,
            "error": STR,
            "created_at": DATE,
            "completed_at": DATE,
        },
    ),
    indexes=(
        IndexSpec("uq_sha256", (("sha256", ASC),), unique=True),
        IndexSpec("idx_source_created", (("source", ASC), ("created_at", DESC))),
    ),
)

INTRADAY_COVERAGE = CollectionSpec(
    name="intraday_coverage",
    description="Per (symbol, exchange, interval, source): first/last timestamp and row count already "
                "stored in intraday_prices. The importer uses it to skip already-loaded bars.",
    validator=schema(
        ["symbol", "exchange", "interval", "source", "first_ts", "last_ts", "row_count", "updated_at"],
        {
            "symbol": SYMBOL,
            "exchange": enum(*EXCHANGES),
            "interval": enum(*INTRADAY_INTERVALS),
            "source": STR,
            "first_ts": DATE,
            "last_ts": DATE,
            "row_count": INT_NON_NEG,
            "updated_at": DATE,
        },
    ),
    indexes=(
        IndexSpec("uq_coverage", (("exchange", ASC), ("symbol", ASC), ("interval", ASC), ("source", ASC)),
                  unique=True),
    ),
)

DATA_QUALITY_REPORTS = CollectionSpec(
    name="data_quality_reports",
    description="Cleaning results per dataset: counts of valid, rejected, duplicate and repaired rows.",
    validator=schema(
        ["dataset", "totals", "created_at"],
        {
            "dataset": STR,
            "job_id": STR,
            "totals": OBJ,
            "issues": ARR,
            "created_at": DATE,
        },
    ),
    indexes=(
        IndexSpec("idx_dataset_created", (("dataset", ASC), ("created_at", DESC))),
        IndexSpec("idx_job_id", (("job_id", ASC),)),
    ),
)

BENCHMARK_RESULTS = CollectionSpec(
    name="benchmark_results",
    description="Performance test results. `is_synthetic` separates synthetic benchmark data from real market data.",
    validator=schema(
        ["run_id", "technology", "operation", "record_count", "duration_seconds", "is_synthetic", "created_at"],
        {
            "run_id": STR,
            "technology": enum(*BENCHMARK_TECHNOLOGIES),
            "operation": STR,
            "record_count": INT_NON_NEG,
            "duration_seconds": NON_NEG,
            "records_per_second": NON_NEG,
            "cpu_percent": NON_NEG,
            "memory_mb": NON_NEG,
            "storage_mb": NON_NEG,
            "is_synthetic": BOOL,
            "notes": STR,
            "created_at": DATE,
        },
    ),
    indexes=(
        IndexSpec("idx_tech_op_count", (("technology", ASC), ("operation", ASC), ("record_count", ASC))),
        IndexSpec("idx_run_id", (("run_id", ASC),)),
    ),
)

# Indexes from earlier versions of the schema that setup_database() must drop
# (their replacement has a different key, so MongoDB cannot update them in place).
OBSOLETE_INDEXES = (
    ("stock_prices", "uq_symbol_exchange_interval_date"),
    ("market_indices", "uq_symbol_exchange_interval_date"),
)

# The 12 collections required by the project brief, then the supporting ones.
REQUIRED_COLLECTIONS = (
    "stock_prices", "intraday_prices", "market_indices", "instruments", "corporate_actions",
    "market_sources", "indicators", "trading_signals", "backtest_results", "processing_jobs",
    "analytics_results", "economic_data",
)

COLLECTIONS: tuple[CollectionSpec, ...] = (
    STOCK_PRICES, INTRADAY_PRICES, MARKET_INDICES, INSTRUMENTS, CORPORATE_ACTIONS, MARKET_SOURCES,
    INDICATORS, TRADING_SIGNALS, BACKTEST_RESULTS, PROCESSING_JOBS, ANALYTICS_RESULTS, ECONOMIC_DATA,
    IMPORT_FILES, INTRADAY_COVERAGE, DATA_QUALITY_REPORTS, BENCHMARK_RESULTS,
)
