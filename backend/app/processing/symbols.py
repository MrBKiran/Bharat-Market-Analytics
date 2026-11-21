"""Symbol standardisation.

Canonical symbols are upper case with no spaces or exchange suffix (RELIANCE, M&M, NIFTY_50).
Real life is messier: users type `reliance.ns` or `NSE:TCS`, and many NSE companies changed their
ticker over time (UTIBANK became AXISBANK in 2007). This module
  * resolves any of those spellings to the instrument (`resolve_symbol`), and
  * records each instrument's earlier tickers, read from the Symbol column of the raw NSE files.
"""
import re
from pathlib import Path
from typing import Optional

import pandas as pd
from pymongo.database import Database

from app.collection.adapters import SKIP_FILE_NAMES, canonical_symbol

# Friendly names people use for the indices.
INDEX_ALIASES = {
    "NIFTY_50": ["NIFTY", "NIFTY50", "CNXNIFTY", "NSEI"],
    "NIFTY_BANK": ["BANKNIFTY", "NIFTYBANK", "BANK_NIFTY", "NSEBANK"],
    "NIFTY_IT": ["NIFTYIT", "CNXIT"],
    "NIFTY_FIN_SERVICE": ["FINNIFTY", "NIFTYFINSERVICE", "NIFTY_FINANCIAL_SERVICES"],
    "SENSEX": ["BSESN", "BSE_SENSEX"],
    "BSE_500": ["BSE500"],
}


def normalise_query(text: str) -> str:
    """'  nse:Reliance.NS ' -> 'RELIANCE'; 'Nifty 50' -> 'NIFTY_50'."""
    s = str(text).strip().upper()
    s = re.sub(r"^(NSE|BSE)[:_ -]+", "", s)
    s = re.sub(r"\.(NS|BO|NSE|BSE)$", "", s)
    s = s.lstrip("^")
    return re.sub(r"\s+", "_", s)


def resolve_symbol(db: Database, text: str) -> Optional[dict]:
    """Find an instrument by canonical symbol, alias, earlier ticker or Yahoo ticker. None if unknown."""
    if not text or not str(text).strip():
        return None
    query = normalise_query(text)
    projection = {"_id": 0}
    for condition in ({"symbol": query}, {"aliases": query}, {"symbol_history.symbol": query},
                      {"yfinance_symbol": str(text).strip()}):
        found = db["instruments"].find_one({**condition, "active": True}, projection)
        if found:
            return found
    return None


def _looks_like_typo(old: str, current: str) -> bool:
    return old != current and old.replace("0", "O") == current


def build_symbol_history(folder: Path) -> dict:
    """From the Symbol column of the raw NSE stock files: {canonical: {symbol_history, aliases}}."""
    out: dict = {}
    for f in sorted(Path(folder).glob("*.csv")):
        if f.name in SKIP_FILE_NAMES:
            continue
        current = canonical_symbol(f.stem)
        df = pd.read_csv(f, usecols=["Date", "Symbol"], parse_dates=["Date"])
        if df.empty:
            continue
        history = []
        for symbol, group in df.groupby("Symbol"):
            old = canonical_symbol(symbol)
            if old == current:
                continue
            entry = {"symbol": old, "first_date": group["Date"].min().to_pydatetime().replace(tzinfo=None),
                     "last_date": group["Date"].max().to_pydatetime().replace(tzinfo=None)}
            if _looks_like_typo(old, current):
                entry["note"] = "digit zero used for letter O in the source data"
            history.append(entry)
        history.sort(key=lambda e: e["first_date"])
        aliases = sorted({f.stem.upper()} - {current})            # e.g. the file name 'MM' for 'M&M'
        if history or aliases:
            out[current] = {"symbol_history": history, "aliases": aliases}
    return out


def store_symbol_history(db: Database, history: dict) -> dict:
    """Write history and aliases onto the instruments. Returns counts."""
    from datetime import timezone
    updated = missing = 0
    for symbol, info in history.items():
        entries = [{**e, "first_date": e["first_date"].replace(tzinfo=timezone.utc),
                    "last_date": e["last_date"].replace(tzinfo=timezone.utc)} for e in info["symbol_history"]]
        update: dict = {"$set": {"symbol_history": entries}}
        if info["aliases"]:
            update["$addToSet"] = {"aliases": {"$each": info["aliases"]}}
        res = db["instruments"].update_one({"symbol": symbol, "instrument_type": "equity"}, update)
        updated += res.matched_count
        missing += 0 if res.matched_count else 1
    return {"instruments_updated": updated, "not_in_instruments": missing}


def store_index_aliases(db: Database) -> int:
    n = 0
    for symbol, aliases in INDEX_ALIASES.items():
        n += db["instruments"].update_one({"symbol": symbol, "instrument_type": "index"},
                                          {"$addToSet": {"aliases": {"$each": aliases}}}).matched_count
    return n
