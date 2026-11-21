"""Phase 3 tests: collections, validators, unique indexes and seeding.

Every test runs against a throwaway database named BharatMarketTest_<random>
that is dropped afterwards, so the real BharatMarketDB is never touched.
"""
import uuid
from datetime import datetime, timezone

import pytest
from pymongo.errors import DuplicateKeyError, WriteError

from app.db.mongo import get_client
from app.db.schemas import COLLECTIONS, REQUIRED_COLLECTIONS
from app.db.setup import setup_database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)


def good_bar(**overrides) -> dict:
    doc = {
        "symbol": "RELIANCE", "exchange": "NSE", "interval": "1d", "date": NOW,
        "open": 100, "high": 105.5, "low": 99.0, "close": 104.0, "volume": 1000,
        "source": "manual_upload", "retrieved_at": NOW, "is_synthetic": False,
    }
    doc.update(overrides)
    return doc


@pytest.fixture()
def db():
    client = get_client()
    name = f"BharatMarketTest_{uuid.uuid4().hex[:8]}"
    assert name.startswith("BharatMarketTest_")  # safety: never drop a real database
    database = client[name]
    setup_database(database)
    yield database
    client.drop_database(name)


def test_all_required_collections_exist(db):
    names = set(db.list_collection_names())
    assert set(REQUIRED_COLLECTIONS) <= names
    assert {c.name for c in COLLECTIONS} <= names


def test_setup_is_idempotent(db):
    second = setup_database(db)
    assert second["created"] == []
    assert second["warnings"] == []
    assert db["market_sources"].count_documents({}) == 9


def test_time_series_collections(db):
    for name in ("intraday_prices", "indicators"):
        opts = list(db.list_collections(filter={"name": name}))[0]["options"]
        assert opts["timeseries"]["timeField"] == "ts"
        assert opts["timeseries"]["metaField"] == "meta"


def test_valid_bar_accepts_int_and_float_prices(db):
    db["stock_prices"].insert_one(good_bar())


def test_duplicate_bar_rejected_by_unique_index(db):
    db["stock_prices"].insert_one(good_bar())
    with pytest.raises(DuplicateKeyError):
        db["stock_prices"].insert_one(good_bar(close=101))


def test_same_bar_from_two_sources_can_coexist(db):
    """Yahoo (split-adjusted) and NSE (raw) may both hold the same day; the same source may not."""
    db["stock_prices"].insert_one(good_bar(source="yfinance", price_basis="split_adjusted"))
    db["stock_prices"].insert_one(good_bar(source="kaggle", price_basis="raw", close=104.5))
    assert db["stock_prices"].count_documents({"symbol": "RELIANCE"}) == 2
    with pytest.raises(DuplicateKeyError):
        db["stock_prices"].insert_one(good_bar(source="kaggle", price_basis="raw"))


def test_obsolete_index_is_dropped_on_upgrade(db):
    """A database created before `source` joined the key is migrated by re-running setup."""
    db["stock_prices"].create_index([("symbol", 1), ("exchange", 1), ("interval", 1), ("date", 1)],
                                    name="uq_symbol_exchange_interval_date", unique=True)
    report = setup_database(db)
    assert "stock_prices.uq_symbol_exchange_interval_date" in report["indexes_dropped"]
    assert "uq_symbol_exchange_interval_date" not in db["stock_prices"].index_information()


@pytest.mark.parametrize("bad", [
    {"high": 90.0},                  # high below low
    {"low": 120.0},                  # low above high
    {"open": 0},                     # non-positive price
    {"volume": -5},                  # negative volume
    {"exchange": "NYSE"},            # not NSE/BSE
    {"interval": "2d"},              # unsupported interval
    {"symbol": "reliance.ns"},       # not canonical (lower case, suffix)
    {"is_synthetic": "no"},          # wrong type
])
def test_invalid_bars_rejected(db, bad):
    with pytest.raises(WriteError):
        db["stock_prices"].insert_one(good_bar(**bad))


def test_missing_required_field_rejected(db):
    doc = good_bar()
    del doc["retrieved_at"]
    with pytest.raises(WriteError):
        db["stock_prices"].insert_one(doc)


def test_index_bar_without_volume_is_allowed(db):
    doc = good_bar(symbol="NIFTY_50")
    del doc["volume"]
    db["market_indices"].insert_one(doc)


def test_seed_data(db):
    assert db["instruments"].count_documents({"instrument_type": "equity"}) == 10
    assert db["instruments"].count_documents({"instrument_type": "index"}) == 6
    assert db["market_sources"].find_one({"source_id": "synthetic_benchmark"})["data_class"] == "synthetic"


def test_reseed_does_not_overwrite_user_changes(db):
    db["instruments"].update_one({"symbol": "TCS"}, {"$set": {"active": False}})
    setup_database(db)
    assert db["instruments"].find_one({"symbol": "TCS"})["active"] is False
    assert db["instruments"].count_documents({}) == 16


def test_datetimes_read_back_as_utc_aware(db):
    db["stock_prices"].insert_one(good_bar())
    got = db["stock_prices"].find_one({"symbol": "RELIANCE"})
    assert got["date"].tzinfo is not None
    assert got["date"] == NOW


def test_unique_import_file_hash(db):
    doc = {"sha256": "a" * 64, "filename": "x.csv", "size_bytes": 10, "source": "kaggle",
           "status": "processing", "created_at": NOW}
    db["import_files"].insert_one(dict(doc))
    with pytest.raises(DuplicateKeyError):
        db["import_files"].insert_one(dict(doc))
