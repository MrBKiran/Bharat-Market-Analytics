"""Phase 5 tests: adapters, validation and the bulk importer.

Runs against a throwaway MongoDB database (BharatMarketTest_<random>, dropped afterwards) and a
temporary data folder, so real data is never touched. No network is used.
"""
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.collection import importer
from app.collection.adapters import canonical_symbol, detect_plan, expand_paths
from app.collection.validation import validate_bars
from app.core.config import get_settings
from app.db.mongo import get_client
from app.db.setup import setup_database

pytestmark = pytest.mark.integration

YF = """Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits
2026-01-05 00:00:00+05:30,100,105,99,104,103,1000,0,0
2026-01-06 00:00:00+05:30,104,110,103,109,108,2000,0,0
2026-01-07 00:00:00+05:30,109,90,95,100,99,1500,0,0
2026-01-08 00:00:00+05:30,0,10,5,7,7,100,0,0
2026-01-09 00:00:00+05:30,100,105,99,104,103,-5,0,0
"""
YF_SIX_GOOD = "Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits\n" + "".join(
    f"2026-01-{d:02d} 00:00:00+05:30,100,105,99,104,103,1000,0,0\n" for d in range(5, 11))
MINUTE_HEAD = "date,open,high,low,close,volume\n"
MINUTE_A = MINUTE_HEAD + ("2015-01-09 09:15:00,8285.45,8295.9,8285.45,8292.1,0\n"
                          "2015-01-09 09:16:00,8292.6,8293.6,8287.2,8288.15,0\n"
                          "2015-01-09 09:17:00,8287.4,8293.9,8287.4,8293.9,0\n")
MINUTE_B = MINUTE_HEAD + ("2015-01-09 09:17:00,8287.4,8293.9,8287.4,8293.9,0\n"      # already stored
                          "2015-01-09 09:18:00,8293.0,8294.0,8290.0,8291.0,0\n"
                          "2015-01-09 09:19:00,8291.0,8292.0,8289.0,8290.0,0\n")
MINUTE_SIX = MINUTE_HEAD + "".join(f"2015-01-09 09:{15 + i}:00,100,101,99,100,0\n" for i in range(6))
STOCK_HEAD = "Date,Symbol,Series,Prev Close,Open,High,Low,Last,Close,VWAP,Volume,Turnover,Trades,Deliverable Volume,%Deliverble\n"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")     # rejected-rows files go here
    client = get_client()
    name = f"BharatMarketTest_{uuid.uuid4().hex[:8]}"
    assert name.startswith("BharatMarketTest_")
    database = client[name]
    setup_database(database)
    yield database
    client.drop_database(name)


def write(tmp_path: Path, relative: str, text: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


# ------------------------------------------------------------------ validation
def frame(**cols) -> pd.DataFrame:
    n = len(next(iter(cols.values())))
    base = {"ts": pd.date_range("2026-01-05", periods=n, tz="UTC"), "symbol": ["TCS"] * n,
            "open": [100.0] * n, "high": [105.0] * n, "low": [99.0] * n, "close": [104.0] * n, "volume": [10.0] * n}
    base.update(cols)
    return pd.DataFrame(base)


def test_validation_assigns_first_broken_rule():
    df = frame(
        ts=[pd.NaT, pd.Timestamp("2026-01-06", tz="UTC"), pd.Timestamp("2026-01-07", tz="UTC"),
            pd.Timestamp("2026-01-08", tz="UTC"), pd.Timestamp("2026-01-09", tz="UTC"), pd.Timestamp("2026-01-10", tz="UTC")],
        open=[100, float("nan"), 0, 100, 100, 100], high=[105, 105, 105, 90, 105, 105],
        low=[99, 99, 99, 95, 99, 99], close=[104, 104, 104, 100, 104, 104], volume=[10, 10, 10, 10, -1, 10])
    good, bad = validate_bars(df, volume_required=True)
    assert list(bad["reject_reason"]) == ["bad_timestamp", "missing_price", "non_positive_price",
                                          "ohlc_inconsistent", "negative_volume"]
    assert len(good) == 1


def test_validation_flags_repeated_bars_and_volume_rules():
    df = frame(ts=[pd.Timestamp("2026-01-05", tz="UTC")] * 2 + [pd.Timestamp("2026-01-06", tz="UTC")],
               volume=[10.0, 10.0, float("nan")])
    good, bad = validate_bars(df, volume_required=True)
    assert sorted(bad["reject_reason"]) == ["duplicate_in_file", "missing_volume"]
    good_idx, bad_idx = validate_bars(df.iloc[[0, 2]], volume_required=False)   # indices may lack volume
    assert len(good_idx) == 2 and bad_idx.empty


# ------------------------------------------------------------------ adapters
def test_format_detection_and_symbol_standardisation(tmp_path):
    f1 = write(tmp_path, "yfinance/1d/RELIANCE_NSE.csv", YF)
    f2 = write(tmp_path, "k/NIFTY FIN SERVICE_60minute.csv", MINUTE_A)
    f3 = write(tmp_path, "s/MM.csv", STOCK_HEAD)
    f4 = write(tmp_path, "mine.csv", "Date,Open,High,Low,Close,Volume\n")
    p1, p2, p3 = detect_plan(f1), detect_plan(f2), detect_plan(f3)
    assert (p1.adapter, p1.symbol, p1.exchange, p1.interval, p1.price_basis) == ("yfinance", "RELIANCE", "NSE", "1d", "split_adjusted")
    assert (p2.adapter, p2.symbol, p2.interval, p2.instrument_type) == ("kaggle_index", "NIFTY_FIN_SERVICE", "1h", "index")
    assert (p3.adapter, p3.symbol, p3.price_basis) == ("kaggle_stock", "M&M", "raw")   # alias MM -> M&M
    with pytest.raises(ValueError):
        detect_plan(f4)                                # generic needs a symbol
    assert detect_plan(f4, {"symbol": "tcs"}).symbol == "TCS"
    assert canonical_symbol(" nifty  bank ") == "NIFTY_BANK"


def test_expand_paths_skips_metadata_and_union_files(tmp_path):
    for name in ("RELIANCE.csv", "NIFTY50_all.csv", "stock_metadata.csv", "notes.txt"):
        write(tmp_path, f"k/{name}", "x")
    assert [p.name for p in expand_paths([tmp_path / "k"])] == ["RELIANCE.csv"]


# ------------------------------------------------------------------ importing daily data
def test_import_daily_file_validates_and_converts(db, tmp_path):
    f = write(tmp_path, "yfinance/1d/RELIANCE_NSE.csv", YF)
    res = importer.import_file(db, f)
    assert res["status"] == "completed" and res["collection"] == "stock_prices"
    assert res["counts"] == {"read": 5, "inserted": 2, "duplicates": 0, "rejected": 3, "optional_fields_dropped": 0}
    assert res["reasons"] == {"ohlc_inconsistent": 1, "non_positive_price": 1, "negative_volume": 1}

    docs = list(db["stock_prices"].find({}, {"_id": 0}).sort("date", 1))
    first = docs[0]
    assert first["date"] == datetime(2026, 1, 5, tzinfo=timezone.utc)         # IST midnight -> 00:00 UTC of that date
    assert (first["source"], first["price_basis"], first["is_synthetic"]) == ("yfinance", "split_adjusted", False)
    assert first["adj_close"] == 103 and first["retrieved_at"].tzinfo is not None
    assert type(first["volume"]) is int and type(first["open"]) is float       # plain Python types, not NumPy

    report = db["data_quality_reports"].find_one({"job_id": res["job_id"]})
    assert report["totals"]["rejected"] == 3 and {i["reason"] for i in report["issues"]} == set(res["reasons"])
    rejected = list((get_settings().data_dir / "rejected").glob("*.rejected.csv"))
    assert len(rejected) == 1 and len(pd.read_csv(rejected[0])) == 3           # nothing silently lost


def test_job_and_file_records(db, tmp_path):
    f = write(tmp_path, "yfinance/1d/RELIANCE_NSE.csv", YF)
    res = importer.import_file(db, f)
    job = db["processing_jobs"].find_one({"job_id": res["job_id"]})
    assert job["status"] == "completed" and job["progress"] == 100.0 and job["job_type"] == "import"
    rec = db["import_files"].find_one({"job_id": res["job_id"]})
    assert rec["status"] == "completed" and rec["rows_inserted"] == 2 and rec["rows_rejected"] == 3
    assert len(rec["sha256"]) == 64 and rec["source"] == "yfinance"


def test_same_file_is_not_imported_twice(db, tmp_path):
    f = write(tmp_path, "yfinance/1d/RELIANCE_NSE.csv", YF)
    importer.import_file(db, f)
    again = importer.import_file(db, f)
    assert again["status"] == "already_imported" and db["stock_prices"].count_documents({}) == 2


def test_dry_run_writes_nothing(db, tmp_path):
    f = write(tmp_path, "yfinance/1d/RELIANCE_NSE.csv", YF)
    res = importer.import_file(db, f, dry_run=True)
    assert res["status"] == "dry_run" and res["counts"]["inserted"] == 2 and res["counts"]["rejected"] == 3
    assert db["stock_prices"].count_documents({}) == 0 and db["processing_jobs"].count_documents({}) == 0
    assert not (get_settings().data_dir / "rejected").exists()


def test_same_day_from_two_sources_is_kept(db, tmp_path):
    importer.import_file(db, write(tmp_path, "yfinance/1d/RELIANCE_NSE.csv", YF))
    kaggle = STOCK_HEAD + "2026-01-05,RELIANCE,EQ,100,100,105,99,104,104,101,1000,1.0E14,50.0,500,0.5\n"
    res = importer.import_file(db, write(tmp_path, "k/RELIANCE.csv", kaggle))
    assert res["status"] == "completed" and res["counts"]["inserted"] == 1 and res["counts"]["duplicates"] == 0
    docs = list(db["stock_prices"].find({"date": datetime(2026, 1, 5, tzinfo=timezone.utc)}, {"_id": 0}))
    assert {d["source"] for d in docs} == {"yfinance", "kaggle"}
    raw = next(d for d in docs if d["source"] == "kaggle")
    assert raw["price_basis"] == "raw" and raw["series"] == "EQ" and type(raw["trades"]) is int


def test_same_source_overlap_is_counted_as_duplicates(db, tmp_path):
    importer.import_file(db, write(tmp_path, "a/yfinance/1d/RELIANCE_NSE.csv", YF))
    res = importer.import_file(db, write(tmp_path, "b/yfinance/1d/RELIANCE_NSE.csv", YF.replace("1000,0,0", "1001,0,0", 1)))
    assert res["counts"]["duplicates"] == 2 and res["counts"]["inserted"] == 0
    assert db["stock_prices"].count_documents({}) == 2


def test_unknown_instrument_is_skipped_unless_auto_registered(db, tmp_path):
    f = write(tmp_path, "yfinance/1d/FOOBAR_NSE.csv", YF)
    skipped = importer.import_file(db, f)
    assert skipped["status"] == "skipped" and "--auto-register" in skipped["reason"]
    done = importer.import_file(db, f, auto_register=True)
    assert done["status"] == "completed"
    inst = db["instruments"].find_one({"symbol": "FOOBAR"})
    assert inst["auto_registered"] is True and inst["instrument_type"] == "equity" and inst["active"] is True


def test_kaggle_stock_alias_and_metadata_registration(db, tmp_path):
    write(tmp_path, "k/stock_metadata.csv",
          "Company Name,Industry,Symbol,Series,ISIN Code\nMahindra & Mahindra Ltd.,AUTOMOBILE,MM,EQ,INE101A01026\n")
    f = write(tmp_path, "k/MM.csv", STOCK_HEAD + "2026-01-05,MM,EQ,100,100,105,99,104,104,101,1000,1.0E14,,,\n")
    res = importer.import_file(db, f, auto_register=True)
    assert res["status"] == "completed"
    inst = db["instruments"].find_one({"symbol": "M&M"})
    assert (inst["name"], inst["isin"], inst["industry_legacy"]) == ("Mahindra & Mahindra Ltd.", "INE101A01026", "AUTOMOBILE")
    assert "sector" not in inst                                     # sector taxonomy is decided in Phase 7
    doc = db["stock_prices"].find_one({"symbol": "M&M"})
    assert "trades" not in doc                                      # empty values are omitted, not stored as NaN


def test_listing_day_with_zero_prev_close_keeps_the_bar(db, tmp_path):
    """Real case (BHARTIARTL 2002-02-18): Prev Close is 0 on the first trading day. Only that field is dropped."""
    rows = (STOCK_HEAD + "2002-02-18,BHARTIARTL,EQ,0,51.9,51.9,43.5,44.35,44.35,45,10381190,4.7E13,,,\n"
                         "2002-02-19,BHARTIARTL,EQ,44.35,44,45,43,44.5,44.5,44,1000,1.0E10,10,500,0.5\n")
    res = importer.import_file(db, write(tmp_path, "k/BHARTIARTL.csv", rows))
    assert res["counts"]["inserted"] == 2 and res["counts"]["rejected"] == 0
    assert res["counts"]["optional_fields_dropped"] == 1
    listing_day = db["stock_prices"].find_one({"date": datetime(2002, 2, 18, tzinfo=timezone.utc)})
    assert listing_day["close"] == 44.35 and "prev_close" not in listing_day
    assert db["stock_prices"].find_one({"date": datetime(2002, 2, 19, tzinfo=timezone.utc)})["prev_close"] == 44.35


def test_force_reprocesses_a_finished_file_without_duplicating(db, tmp_path):
    f = write(tmp_path, "yfinance/1d/RELIANCE_NSE.csv", YF)
    importer.import_file(db, f)
    db["stock_prices"].delete_one({"date": datetime(2026, 1, 6, tzinfo=timezone.utc)})   # a bar goes missing
    assert importer.import_file(db, f)["status"] == "already_imported"                  # normally skipped
    res = importer.import_file(db, f, force=True)
    assert res["status"] == "completed" and res["counts"]["inserted"] == 1 and res["counts"]["duplicates"] == 1
    assert db["stock_prices"].count_documents({}) == 2
    assert db["import_files"].count_documents({}) == 1                                   # same record, new job
    assert db["import_files"].find_one({})["job_id"] == res["job_id"]


def test_index_daily_goes_to_market_indices_with_volume_optional(db, tmp_path):
    f = write(tmp_path, "k/NIFTY 50_day.csv", MINUTE_HEAD + "2020-03-02 00:00:00,11500,11600,11400,11550,0\n")
    res = importer.import_file(db, f)
    assert res["collection"] == "market_indices" and res["counts"]["inserted"] == 1
    doc = db["market_indices"].find_one({"symbol": "NIFTY_50"})
    assert doc["date"] == datetime(2020, 3, 2, tzinfo=timezone.utc) and doc["price_basis"] == "index_level"


def test_generic_csv_and_excel(db, tmp_path):
    csv = write(tmp_path, "mine.csv", "Date,Open,High,Low,Close,Volume\n2026-02-02,100,105,99,104,500\n")
    r1 = importer.import_file(db, csv, overrides={"symbol": "tcs", "interval": "1d"})
    assert r1["status"] == "completed" and db["stock_prices"].find_one({"symbol": "TCS"})["source"] == "manual_upload"
    xlsx = tmp_path / "mine.xlsx"
    pd.DataFrame({"Date": ["2026-02-03"], "Open": [100], "High": [105], "Low": [99], "Close": [104], "Volume": [600]}
                 ).to_excel(xlsx, index=False)
    r2 = importer.import_file(db, xlsx, overrides={"symbol": "tcs", "interval": "1d"})
    assert r2["status"] == "completed" and db["stock_prices"].count_documents({"symbol": "TCS"}) == 2


def test_bad_inputs_return_errors_not_crashes(db, tmp_path):
    assert importer.import_file(db, tmp_path / "missing.csv")["status"] == "error"
    assert importer.import_file(db, write(tmp_path, "x.txt", "hello"))["status"] == "error"


# ------------------------------------------------------------------ importing intraday (time-series) data
def test_intraday_import_converts_ist_to_utc_and_tracks_coverage(db, tmp_path):
    res = importer.import_file(db, write(tmp_path, "k/NIFTY 50_minute.csv", MINUTE_A))
    assert res["status"] == "completed" and res["collection"] == "intraday_prices"
    first = db["intraday_prices"].find_one({}, sort=[("ts", 1)])
    assert first["ts"] == datetime(2015, 1, 9, 3, 45, tzinfo=timezone.utc)      # 09:15 IST = 03:45 UTC
    assert first["meta"] == {"symbol": "NIFTY_50", "exchange": "NSE", "interval": "1m", "instrument_type": "index",
                             "source": "kaggle", "is_synthetic": False, "price_basis": "index_level"}
    cov = db["intraday_coverage"].find_one({"symbol": "NIFTY_50"})
    assert cov["row_count"] == 3 and cov["first_ts"] == first["ts"] and cov["interval"] == "1m"


def test_intraday_overlap_is_removed_exactly(db, tmp_path):
    importer.import_file(db, write(tmp_path, "a/NIFTY 50_minute.csv", MINUTE_A))
    res = importer.import_file(db, write(tmp_path, "b/NIFTY 50_minute.csv", MINUTE_B))
    assert res["counts"]["inserted"] == 2 and res["counts"]["duplicates"] == 1
    assert db["intraday_prices"].count_documents({}) == 5
    assert db["intraday_coverage"].find_one({"symbol": "NIFTY_50"})["row_count"] == 5


# ------------------------------------------------------------------ resume after a crash
def _crash_on_second_call(monkeypatch, name: str, *, after_insert: bool):
    original = getattr(importer, name)
    calls = {"n": 0}

    def flaky(coll, docs):
        calls["n"] += 1
        if calls["n"] == 2:
            if after_insert:
                original(coll, docs)         # the data is stored, but the checkpoint is not saved
            raise RuntimeError("simulated crash")
        return original(coll, docs)

    monkeypatch.setattr(importer, name, flaky)


@pytest.mark.parametrize("after_insert", [False, True])
def test_daily_import_resumes_exactly_once(db, tmp_path, monkeypatch, after_insert):
    f = write(tmp_path, "yfinance/1d/RELIANCE_NSE.csv", YF_SIX_GOOD)
    _crash_on_second_call(monkeypatch, "_insert_regular", after_insert=after_insert)
    first = importer.import_file(db, f, chunk_size=2)
    assert first["status"] == "failed" and "simulated crash" in first["error"]
    assert db["processing_jobs"].find_one({"job_id": first["job_id"]})["status"] == "failed"
    assert db["import_files"].find_one({"job_id": first["job_id"]})["status"] == "failed"

    monkeypatch.undo()
    second = importer.import_file(db, f, chunk_size=2)
    assert second["status"] == "completed" and second["job_id"] == first["job_id"]       # same job, resumed
    assert db["stock_prices"].count_documents({}) == 6                                     # exactly once
    assert db["processing_jobs"].find_one({"job_id": first["job_id"]})["status"] == "completed"


@pytest.mark.parametrize("after_insert", [False, True])
def test_intraday_import_resumes_exactly_once(db, tmp_path, monkeypatch, after_insert):
    f = write(tmp_path, "k/NIFTY 50_minute.csv", MINUTE_SIX)
    _crash_on_second_call(monkeypatch, "_insert_timeseries", after_insert=after_insert)
    assert importer.import_file(db, f, chunk_size=2)["status"] == "failed"
    monkeypatch.undo()
    assert importer.import_file(db, f, chunk_size=2)["status"] == "completed"
    assert db["intraday_prices"].count_documents({}) == 6      # no duplicates even if the crash came after the insert


def test_import_paths_continues_after_a_bad_file(db, tmp_path):
    write(tmp_path, "d/yfinance/1d/FOOBAR_NSE.csv", YF)                      # unknown instrument -> skipped
    write(tmp_path, "d/yfinance/1d/RELIANCE_NSE.csv", YF)
    results = importer.import_paths(db, [tmp_path / "d"])
    assert sorted(r["status"] for r in results) == ["completed", "skipped"]
    assert [j["status"] for j in importer.recent_jobs(db)] == ["completed"]
