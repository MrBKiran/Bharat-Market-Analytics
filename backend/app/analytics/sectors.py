"""Sector classification for the NIFTY 50 stocks.

The sources give no usable sectors: Yahoo has none and the Kaggle file uses an old NSE taxonomy
(`industry_legacy`, e.g. "SERVICES"). The 10 original instruments already have NSE-style sectors from
the seed data. For the rest we assign the NSE macro-economic sector below.

IMPORTANT: this table comes from general knowledge of how NSE classifies these companies, not from a
downloaded file, so it is recorded as `sector_source = "assigned_by_project"`. Check it against NSE's
current classification before relying on a single stock's sector. Companies move between sectors
over time, and we use today's grouping for all history.
"""
from pymongo.database import Database

SECTOR_SOURCE = "assigned_by_project"

SECTOR_MAP = {
    "Financial Services": ["AXISBANK", "BAJAJFINSV", "BAJFINANCE", "HDFC", "HDFCBANK", "ICICIBANK", "INDUSINDBK",
                           "KOTAKBANK", "SBIN"],
    "Information Technology": ["HCLTECH", "INFY", "TCS", "TECHM", "WIPRO"],
    "Oil, Gas & Consumable Fuels": ["BPCL", "COALINDIA", "GAIL", "IOC", "ONGC", "RELIANCE"],
    "Automobile and Auto Components": ["BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO", "M&M", "MARUTI", "TATAMOTORS"],
    "Fast Moving Consumer Goods": ["BRITANNIA", "HINDUNILVR", "ITC", "NESTLEIND"],
    "Healthcare": ["CIPLA", "DRREDDY", "SUNPHARMA"],
    "Metals & Mining": ["HINDALCO", "JSWSTEEL", "TATASTEEL", "VEDL"],
    "Construction Materials": ["GRASIM", "SHREECEM", "ULTRACEMCO"],
    "Construction": ["LT"],
    "Power": ["NTPC", "POWERGRID"],
    "Telecommunication": ["BHARTIARTL", "INFRATEL"],
    "Consumer Durables": ["ASIANPAINT", "TITAN"],
    "Services": ["ADANIPORTS"],
    "Media, Entertainment & Publication": ["ZEEL"],
    "Chemicals": ["UPL"],
}
SYMBOL_TO_SECTOR = {sym: sector for sector, syms in SECTOR_MAP.items() for sym in syms}


def assign_sectors(db: Database, *, dry_run: bool = False) -> dict:
    """Set `sector` on stocks that have none. Existing sectors are never overwritten."""
    assigned, already, unmapped = [], 0, []
    for inst in db["instruments"].find({"instrument_type": "equity", "active": True}, {"_id": 0, "symbol": 1, "sector": 1}):
        sym = inst["symbol"]
        if inst.get("sector"):
            already += 1
        elif sym in SYMBOL_TO_SECTOR:
            assigned.append(sym)
            if not dry_run:
                db["instruments"].update_one({"symbol": sym, "instrument_type": "equity"},
                                             {"$set": {"sector": SYMBOL_TO_SECTOR[sym], "sector_source": SECTOR_SOURCE}})
        else:
            unmapped.append(sym)
    return {"assigned": len(assigned), "already_had_sector": already, "unmapped": unmapped}
