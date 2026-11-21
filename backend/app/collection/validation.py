"""Row-level validation for price bars, done with vectorised pandas (fast on millions of rows).

Used by the importer BEFORE data reaches MongoDB. Rejected rows are kept (with the reason)
so nothing is silently lost. Deeper cleaning (gaps, outliers, corporate actions) is Phase 6.
"""
import re

import pandas as pd

SYMBOL_PATTERN = r"^[A-Z0-9&_-]{1,30}$"
PRICE_COLUMNS = ["open", "high", "low", "close"]

# Reason codes written to the rejected-rows file and the data quality report.
REASONS = (
    "bad_timestamp", "invalid_symbol", "missing_price", "non_positive_price",
    "negative_volume", "missing_volume", "ohlc_inconsistent", "duplicate_in_file",
)


def validate_bars(df: pd.DataFrame, *, volume_required: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split `df` into (good, bad). `bad` gets a `reject_reason` column.

    Expects columns: ts, symbol, open, high, low, close, volume.
    Each row gets the FIRST rule it breaks, in the order below.
    """
    reason = pd.Series("", index=df.index, dtype=object)

    def mark(mask: pd.Series, text: str) -> None:
        reason[mask & (reason == "")] = text

    mark(df["ts"].isna(), "bad_timestamp")
    mark(~df["symbol"].astype(str).str.match(SYMBOL_PATTERN), "invalid_symbol")
    mark(df[PRICE_COLUMNS].isna().any(axis=1), "missing_price")
    mark((df[PRICE_COLUMNS] <= 0).any(axis=1), "non_positive_price")
    mark(df["volume"] < 0, "negative_volume")
    if volume_required:
        mark(df["volume"].isna(), "missing_volume")
    mark(
        (df["high"] < df["low"]) | (df["high"] < df["open"]) | (df["high"] < df["close"])
        | (df["low"] > df["open"]) | (df["low"] > df["close"]),
        "ohlc_inconsistent",
    )
    # Same bar twice in this chunk: keep the first, reject the rest.
    still_ok = reason == ""
    repeated = df[still_ok].duplicated(subset=["symbol", "ts"], keep="first")
    reason[repeated[repeated].index] = "duplicate_in_file"

    ok = reason == ""
    good = df[ok]
    bad = df[~ok].assign(reject_reason=reason[~ok])
    return good, bad


def is_valid_symbol(symbol: str) -> bool:
    return bool(re.match(SYMBOL_PATTERN, symbol or ""))
