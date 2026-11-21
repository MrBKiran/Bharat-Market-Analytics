"""Phase 6 tests: corporate actions, adjustment, quality audit and symbol standardisation.

Database tests run against a throwaway database (BharatMarketTest_<random>, dropped afterwards).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.db.mongo import get_client
from app.db.setup import setup_database
from app.processing import adjustment, quality, symbols
from app.processing import corporate_actions as ca
from app.processing.jobs import tracked_job

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)


def D(n: int) -> datetime:
    return START + timedelta(days=n)


def frame(closes, volumes=None, step=1) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({"date": pd.date_range(START, periods=n, freq=f"{step}D", tz="UTC"),
                         "close": closes, "volume": volumes or [1000] * n})


@pytest.fixture()
def db():
    client = get_client()
    name = f"BharatMarketTest_{uuid.uuid4().hex[:8]}"
    assert name.startswith("BharatMarketTest_")
    database = client[name]
    setup_database(database)
    yield database
    client.drop_database(name)


def put(db, symbol, source, items, *, basis="raw", collection="stock_prices", flat=False, exchange="NSE"):
    """items: [(day_number, close, volume)] -> valid bar documents."""
    docs = []
    for day, close, volume in items:
        doc = {"symbol": symbol, "exchange": exchange, "interval": "1d", "date": D(day),
               "open": close if flat else close * 0.995, "high": close if flat else close * 1.01,
               "low": close if flat else close * 0.99, "close": close, "source": source, "price_basis": basis,
               "retrieved_at": D(0), "is_synthetic": False}
        if volume is not None:
            doc["volume"] = volume
        docs.append(doc)
    db[collection].insert_many(docs)


# ------------------------------------------------------------------ split detection (no database)
def test_detects_a_two_for_one_split_with_the_exact_factor():
    events = ca.infer_split_events(frame([100, 101, 50.5, 51, 52], [1000, 1000, 2100, 2000, 2000]))
    assert len(events) == 1
    ev = events[0]
    assert ev["ex_date"] == D(2) and ev["factor"] == 0.5 and 0.49 < ev["observed_ratio"] < 0.51
    assert ev["volume_ratio"] > 2


@pytest.mark.parametrize("closes,factor", [
    ([100, 66.5], 2 / 3),        # 3-for-2 bonus
    ([100, 20.4], 0.2),          # 5-for-1 split with a small market move
    ([100, 35], 1 / 3),
    ([100, 22.7], 0.2),          # 5-for-1 on a day the market rose (BRITANNIA 2010 closed at 0.227)
    ([100, 5.3], 0.05),          # 10-for-1 split plus a 1:1 bonus the same day (TITAN 2011 closed at 0.053)
    ([100, 7.3], 0.1),           # 10-for-1 on a day the market also fell (ITC 2005 closed at 0.073)
    ([10, 20.2], 2.0),           # reverse split (consolidation)
])
def test_other_split_ratios(closes, factor):
    events = ca.infer_split_events(frame(closes))
    assert [e["factor"] for e in events] == [pytest.approx(factor)]


@pytest.mark.parametrize("closes,step", [
    ([100, 80, 81], 1),          # a 20% crash is not a split
    ([100, 90, 95, 99], 1),
    ([100, 50, 51], 10),         # 10 calendar days between rows: could simply be missing data
    ([100, 100.5, 99.8], 1),
])
def test_ordinary_moves_gaps_and_crashes_are_not_splits(closes, step):
    assert ca.infer_split_events(frame(closes, step=step)) == []


def test_describe_factor():
    assert ca.describe_factor(0.5).startswith("2-for-1")
    assert ca.describe_factor(2 / 3).startswith("3-for-2")
    assert "reverse" in ca.describe_factor(2.0)


# ------------------------------------------------------------------ adjustment maths (no database)
def test_cumulative_factors_exclude_the_ex_date_itself():
    dates = pd.Series(pd.date_range(START, periods=5, freq="D", tz="UTC"))
    assert list(adjustment.cumulative_factors(dates, [(D(2), 0.5)])) == [0.5, 0.5, 1, 1, 1]
    assert list(adjustment.cumulative_factors(dates, [(D(1), 0.5), (D(3), 0.2)])) == pytest.approx([0.1, 0.2, 0.2, 1, 1])
    assert list(adjustment.cumulative_factors(dates, [])) == [1, 1, 1, 1, 1]


def test_adjust_bars_makes_the_series_continuous_and_scales_volume_inversely():
    raw = frame([100, 100, 50, 50], [10, 10, 20, 20])
    raw["open"] = raw["high"] = raw["low"] = raw["close"]
    out = adjustment.adjust_bars(raw, [(D(2), 0.5)])
    assert list(out["close"]) == [50, 50, 50, 50] and list(out["raw_close"]) == [100, 100, 50, 50]
    assert list(out["volume"]) == [20, 20, 20, 20] and list(out["adj_factor"]) == [0.5, 0.5, 1, 1]


def test_flags():
    df = frame([100, 140, 141, 141, 142], [1000, 1000, 1000, 0, 1000])
    df["date"] = pd.to_datetime([D(0), D(1), D(2), D(3), D(20)], utc=True)        # last row follows a 17-day gap
    df["open"] = df["high"] = df["low"] = df["close"]                              # every bar is flat
    flags = adjustment.compute_flags(df, "stock")
    assert flags[0] == []                                                          # first row: no previous day
    assert flags[1] == ["return_outlier"]                                         # +40%
    assert flags[3] == ["no_trade"]                                               # flat and zero volume
    assert "gap_before" in flags[4]
    assert "no_trade" not in adjustment.compute_flags(df, "index")[3]            # zero volume is normal for indices


# ------------------------------------------------------------------ Yahoo actions
YAHOO_CSV = """Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits
2020-01-03 00:00:00+05:30,1,1,1,1,1,100,0,0
2020-01-04 00:00:00+05:30,1,1,1,1,1,100,0,2.0
2020-01-05 00:00:00+05:30,1,1,1,1,1,100,5.5,0
2020-01-06 00:00:00+05:30,1,1,1,1,1,100,0,1.5
2020-01-07 00:00:00+05:30,1,1,1,1,1,100,0,0.5
"""


def test_yahoo_actions_parse_splits_and_dividends(tmp_path):
    f = tmp_path / "RELIANCE_NSE.csv"
    f.write_text(YAHOO_CSV)
    docs = ca.yahoo_actions(f)
    splits = {d["ex_date"].day: d["factor"] for d in docs if d["action_type"] == "split"}
    assert splits == {4: 0.5, 6: pytest.approx(1 / 1.5), 7: 2.0}                  # 0.5 = reverse split
    div = next(d for d in docs if d["action_type"] == "dividend")
    assert div["amount"] == 5.5 and div["ex_date"] == datetime(2020, 1, 5, tzinfo=UTC)
    assert {d["symbol"] for d in docs} == {"RELIANCE"} and all(d["confidence"] == "provider" for d in docs)


def test_index_files_have_no_actions(tmp_path):
    f = tmp_path / "NIFTY_50_NSE.csv"
    f.write_text(YAHOO_CSV)
    (tmp_path / "NIFTY_50_NSE.csv.meta.json").write_text('{"instrument_type": "index", "symbol": "NIFTY_50", "exchange": "NSE"}')
    assert ca.yahoo_actions(f) == []


def test_storing_yahoo_actions_twice_does_not_duplicate(db, tmp_path):
    f = tmp_path / "RELIANCE_NSE.csv"
    f.write_text(YAHOO_CSV)
    docs = ca.yahoo_actions(f)
    assert ca.store_yahoo_actions(db, docs) == {"inserted": 4, "updated": 0}
    assert ca.store_yahoo_actions(db, docs) == {"inserted": 0, "updated": 4}
    assert db["corporate_actions"].count_documents({}) == 4


# ------------------------------------------------------------------ inferred actions and reconciliation
RAW_SPLIT = [(0, 100, 1000), (1, 101, 1000), (2, 102, 1000), (3, 103, 1000),
             (4, 52, 2100), (5, 52.5, 2000), (6, 53, 2000), (7, 54, 2000)]     # 2-for-1 on day 4


def test_inferred_split_confirms_a_yahoo_split_instead_of_duplicating(db):
    put(db, "RELIANCE", "kaggle", RAW_SPLIT)
    ca.store_yahoo_actions(db, [{"symbol": "RELIANCE", "exchange": "NSE", "action_type": "split", "ex_date": D(4),
                                 "factor": 0.5, "source": "yfinance", "confidence": "provider", "retrieved_at": D(0)}])
    events = ca.infer_split_events(ca.load_raw_bars(db, "RELIANCE", "NSE", "kaggle"))
    result = ca.store_inferred_actions(db, "RELIANCE", "NSE", events)
    assert result == {"events": 1, "confirmed_yahoo": 1, "new_inferred": 0}
    doc = db["corporate_actions"].find_one({"action_type": "split"})
    assert doc["confirmed_by"] == ["derived_nse_prices"] and db["corporate_actions"].count_documents({}) == 1


def test_inferred_only_split_is_stored_and_regenerated_exactly(db):
    put(db, "BPCL", "kaggle", RAW_SPLIT)
    events = ca.infer_split_events(ca.load_raw_bars(db, "BPCL", "NSE", "kaggle"))
    ca.store_inferred_actions(db, "BPCL", "NSE", events)
    ca.store_inferred_actions(db, "BPCL", "NSE", events)                          # re-running replaces, never accumulates
    docs = list(db["corporate_actions"].find({"symbol": "BPCL"}))
    assert len(docs) == 1 and docs[0]["confidence"] == "inferred" and docs[0]["factor"] == 0.5
    assert docs[0]["source"] == "derived_nse_prices" and "inferred" in docs[0]["details"]


def test_build_corporate_actions_and_reconcile(db, tmp_path):
    yahoo_dir = tmp_path / "1d"
    yahoo_dir.mkdir()
    (yahoo_dir / "RELIANCE_NSE.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits\n"
        "2024-01-05 00:00:00+05:30,1,1,1,1,1,100,0,2.0\n"                          # day 4 = 2024-01-05
        "2024-01-02 00:00:00+05:30,1,1,1,1,1,100,0,2.0\n")                         # a split the raw data has no jump for
    put(db, "RELIANCE", "kaggle", RAW_SPLIT)
    put(db, "BPCL", "kaggle", RAW_SPLIT)
    dry = ca.build_corporate_actions(db, yahoo_dir, dry_run=True)
    assert dry["yahoo_splits"] == 2 and dry["inferred_events"] == 2 and db["corporate_actions"].count_documents({}) == 0

    summary = ca.build_corporate_actions(db, yahoo_dir)
    assert summary["stocks_scanned"] == 2 and summary["inferred_new"] == 1 and summary["inferred_confirmed_yahoo"] == 1
    report = {r["symbol"]: r for r in ca.reconcile_with_yahoo(db)}
    assert report["RELIANCE"]["yahoo_splits_in_range"] == 2 and report["RELIANCE"]["found_by_detector"] == 1
    assert len(report["RELIANCE"]["missed"]) == 1 and "2024-01-02" in report["RELIANCE"]["missed"][0]


# ------------------------------------------------------------------ building adjusted_prices
def test_yahoo_is_preferred_and_not_adjusted_again(db):
    put(db, "RELIANCE", "yfinance", [(0, 50, 2000), (1, 50.5, 2000), (2, 51, 2000)], basis="split_adjusted")
    put(db, "RELIANCE", "kaggle", RAW_SPLIT)
    summary = adjustment.build_adjusted_prices(db)
    docs = list(db["adjusted_prices"].find({"symbol": "RELIANCE"}).sort("date", 1))
    assert len(docs) == 3 and {d["source"] for d in docs} == {"yfinance"}
    assert all(d["method"] == "provider_adjusted" and d["adj_factor"] == 1.0 and "raw_close" not in d for d in docs)
    assert docs[0]["close"] == 50 and summary["by_method"]["provider_adjusted"] == 3


def test_raw_only_stock_is_adjusted_with_inferred_splits(db):
    put(db, "TCS", "kaggle", RAW_SPLIT)
    ca.store_inferred_actions(db, "TCS", "NSE", ca.infer_split_events(ca.load_raw_bars(db, "TCS", "NSE", "kaggle")))
    adjustment.build_adjusted_prices(db)
    docs = list(db["adjusted_prices"].find({"symbol": "TCS"}).sort("date", 1))
    assert len(docs) == 8 and all(d["method"] == "derived_adjustment" for d in docs)
    assert docs[0]["close"] == 50 and docs[0]["raw_close"] == 100 and docs[0]["adj_factor"] == 0.5
    assert docs[4]["close"] == 52 and docs[4]["adj_factor"] == 1.0                       # ex-date is not adjusted
    assert docs[0]["volume"] == 2000                                                     # volume scaled the other way
    closes = [d["close"] for d in docs]
    assert max(abs(b / a - 1) for a, b in zip(closes, closes[1:])) < 0.05                # no jump left


def test_index_merges_sources_with_yahoo_first(db):
    put(db, "NIFTY_50", "kaggle", [(0, 100, None), (1, 101, None), (2, 102, None)], basis="index_level", collection="market_indices")
    put(db, "NIFTY_50", "yfinance", [(2, 150, None), (3, 151, None)], basis="split_adjusted", collection="market_indices")
    adjustment.build_adjusted_prices(db)
    docs = {d["date"]: d for d in db["adjusted_prices"].find({"symbol": "NIFTY_50"})}
    assert len(docs) == 4 and all(d["method"] == "index_level" for d in docs.values())
    assert docs[D(2)]["source"] == "yfinance" and docs[D(2)]["close"] == 150 and docs[D(1)]["source"] == "kaggle"
    assert "volume" not in docs[D(0)]


def test_rebuild_is_idempotent_and_dry_run_writes_nothing(db):
    put(db, "TCS", "kaggle", RAW_SPLIT)
    assert adjustment.build_adjusted_prices(db, dry_run=True)["rows"] == 8
    assert db["adjusted_prices"].count_documents({}) == 0
    adjustment.build_adjusted_prices(db)
    adjustment.build_adjusted_prices(db)
    assert db["adjusted_prices"].count_documents({"symbol": "TCS"}) == 8


def test_instruments_without_data_are_reported_not_invented(db):
    put(db, "TCS", "kaggle", RAW_SPLIT)
    summary = adjustment.build_adjusted_prices(db)
    assert "INFY" in summary["skipped"] and db["adjusted_prices"].count_documents({"symbol": "INFY"}) == 0


def test_pre_listing_placeholder_rows_are_dropped_but_midseries_no_trade_days_are_kept(db):
    """TCS-style data: flat zero-volume rows BEFORE the first real trade are fake; one in the middle is real."""
    put(db, "TCS", "yfinance", [(0, 100, 0), (1, 100, 0), (2, 100, 0)], basis="split_adjusted", flat=True)   # pre-listing
    put(db, "TCS", "yfinance", [(3, 110, 500), (4, 111, 600), (6, 112, 700)], basis="split_adjusted")
    put(db, "TCS", "yfinance", [(5, 111, 0)], basis="split_adjusted", flat=True)                              # real no-trade day
    summary = adjustment.build_adjusted_prices(db)
    docs = list(db["adjusted_prices"].find({"symbol": "TCS"}).sort("date", 1))
    assert summary["placeholder_rows_dropped"] == 3 and [d["date"] for d in docs] == [D(i) for i in range(3, 7)]
    assert docs[0].get("quality_flags") is None                                    # the first real day is not flagged
    assert docs[2]["quality_flags"] == ["no_trade"]                                # day 5 stays, flagged


def test_phantom_rows_are_dropped_but_real_big_moves_are_kept(db):
    """RELIANCE/TCS 2005-07-28 at Yahoo: zero volume and a 4x price on a day with no trading. A +47% move WITH volume is real."""
    put(db, "RELIANCE", "yfinance", [(0, 100, 10), (1, 101, 10), (2, 400, 0), (3, 102, 10), (4, 150, 500), (5, 151, 500)],
        basis="split_adjusted")
    summary = adjustment.build_adjusted_prices(db)
    docs = {d["date"]: d for d in db["adjusted_prices"].find({"symbol": "RELIANCE"})}
    assert D(2) not in docs and D(4) in docs and summary["phantom_rows_dropped"] == 1
    assert docs[D(3)].get("quality_flags") is None                     # day 3 is compared with day 1, so no false outlier
    assert docs[D(4)]["quality_flags"] == ["return_outlier"]           # the real +47% move is kept and flagged


def test_rows_before_a_confirmed_listing_date_are_dropped(db):
    docs = [{"symbol": "TCS", "exchange": "NSE", "interval": "1d", "date": datetime(2004, 8, day, tzinfo=UTC), "open": c * 0.995,
             "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 100, "source": "yfinance", "price_basis": "split_adjusted",
             "retrieved_at": D(0), "is_synthetic": False} for day, c in [(23, 38.0), (24, 39.0), (25, 100.0), (26, 101.0)]]
    db["stock_prices"].insert_many(docs)
    put(db, "INFY", "yfinance", [(0, 100, 10), (1, 101, 10)], basis="split_adjusted")      # no listing override: untouched
    summary = adjustment.build_adjusted_prices(db)
    dates = [d["date"] for d in db["adjusted_prices"].find({"symbol": "TCS"}).sort("date", 1)]
    assert dates == [datetime(2004, 8, 25, tzinfo=UTC), datetime(2004, 8, 26, tzinfo=UTC)]
    assert summary["pre_listing_rows_dropped"] == 2 and db["adjusted_prices"].count_documents({"symbol": "INFY"}) == 2


def test_series_that_is_all_placeholders_is_skipped(db):
    put(db, "TCS", "yfinance", [(0, 100, 0), (1, 100, 0)], basis="split_adjusted", flat=True)
    summary = adjustment.build_adjusted_prices(db)
    assert "TCS" in summary["skipped"] and db["adjusted_prices"].count_documents({"symbol": "TCS"}) == 0


def test_flags_are_stored_on_documents(db):
    put(db, "TCS", "yfinance", [(0, 100, 10), (1, 150, 10), (2, 151, 10), (30, 152, 10)], basis="split_adjusted")
    adjustment.build_adjusted_prices(db)
    flags = {d["date"]: d.get("quality_flags", []) for d in db["adjusted_prices"].find({"symbol": "TCS"})}
    assert flags[D(0)] == [] and flags[D(1)] == ["return_outlier"] and flags[D(30)] == ["gap_before"]


# ------------------------------------------------------------------ audit
def test_calendar_gaps_found_against_nifty(db):
    put(db, "NIFTY_50", "kaggle", [(i, 100 + i, None) for i in range(6)], basis="index_level", collection="market_indices")
    put(db, "TCS", "yfinance", [(i, 100 + i, 10) for i in (0, 1, 3, 4, 5)], basis="split_adjusted")     # day 2 missing
    adjustment.build_adjusted_prices(db)
    out = quality.calendar_gaps(db)
    assert out["worst"][0]["symbol"] == "TCS" and out["worst"][0]["missing_days"] == 1 and out["total_missing_days"] == 1


def test_intraday_completeness_reports_short_days(db):
    def bar(day, hour_utc, minute):
        return {"ts": D(day).replace(hour=hour_utc, minute=minute), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                "meta": {"symbol": "NIFTY_50", "exchange": "NSE", "interval": "1h", "instrument_type": "index",
                         "source": "kaggle", "is_synthetic": False, "price_basis": "index_level"}}
    full = [bar(0, 3 + h, 45) for h in range(7)]            # 09:15 .. 15:15 IST
    short = [bar(1, 3 + h, 45) for h in range(3)]           # only 3 bars
    db["intraday_prices"].insert_many(full + short)
    out = quality.intraday_completeness(db)["per_interval"]["1h"]
    assert out["series_days"] == 2 and out["short_days"] == 1 and out["full_sessions_pct"] == 50.0


def test_duplicate_audit_is_clean_and_detects_coverage_mismatch(db):
    put(db, "TCS", "kaggle", RAW_SPLIT)
    assert quality.duplicate_audit(db)["stock_prices"] == 0
    db["intraday_prices"].insert_one({"ts": D(0), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                                      "meta": {"symbol": "NIFTY_50", "exchange": "NSE", "interval": "1m",
                                               "instrument_type": "index", "source": "kaggle", "is_synthetic": False,
                                               "price_basis": "index_level"}})
    assert quality.duplicate_audit(db)["intraday_series_count_mismatches_vs_coverage"] == 1   # no coverage row yet


def test_cross_source_returns_agree_when_adjustment_is_right_and_differ_when_not(db):
    put(db, "RELIANCE", "kaggle", RAW_SPLIT)
    ca.store_inferred_actions(db, "RELIANCE", "NSE", ca.infer_split_events(ca.load_raw_bars(db, "RELIANCE", "NSE", "kaggle")))
    ours = [(d, c * (0.5 if d < 4 else 1), v) for d, c, v in RAW_SPLIT]
    put(db, "RELIANCE", "yfinance", ours, basis="split_adjusted")
    agree = quality.cross_source_returns(db)[0]
    assert agree["overlap_days"] == 8 and agree["mismatch_days"] == 0

    db["stock_prices"].update_one({"symbol": "RELIANCE", "source": "yfinance", "date": D(2)}, {"$mul": {"close": 1.05, "high": 1.05}})
    assert quality.cross_source_returns(db)[0]["mismatch_days"] == 2                  # the bumped day and the day after


def test_run_audit_returns_every_section(db):
    put(db, "TCS", "kaggle", RAW_SPLIT)
    adjustment.build_adjusted_prices(db)
    audit = quality.run_audit(db)
    assert set(audit) == {"duplicates", "adjusted", "calendar_gaps", "intraday", "corporate_actions",
                          "cross_source_returns", "series_starts"}
    assert audit["adjusted"]["rows"] == 8 and audit["duplicates"]["adjusted_prices"] == 0


def test_series_starts_lists_yahoo_history_that_predates_the_nse_record(db):
    put(db, "TCS", "yfinance", [(i, 100 + i, 10) for i in range(0, 120, 10)], basis="split_adjusted")   # from day 0
    put(db, "TCS", "kaggle", [(i, 200 + i, 10) for i in range(90, 120, 10)])                            # NSE from day 90
    put(db, "INFY", "yfinance", [(0, 100, 10), (10, 101, 10)], basis="split_adjusted")
    put(db, "INFY", "kaggle", [(0, 100, 10), (10, 101, 10)])                                            # same start: not listed
    out = quality.series_starts(db)
    assert out == [{"symbol": "TCS", "yahoo_first": "2024-01-01", "nse_first": "2024-03-31",
                    "yahoo_rows_before_nse_start": 9}]


# ------------------------------------------------------------------ symbols
def test_normalise_query():
    assert symbols.normalise_query("  nse:Reliance.NS ") == "RELIANCE"
    assert symbols.normalise_query("Nifty 50") == "NIFTY_50"
    assert symbols.normalise_query("^NSEI") == "NSEI" and symbols.normalise_query("TCS.BO") == "TCS"


def test_resolve_symbol_variants_aliases_and_history(db):
    symbols.store_index_aliases(db)
    assert symbols.resolve_symbol(db, "reliance.ns")["symbol"] == "RELIANCE"
    assert symbols.resolve_symbol(db, "NSE:TCS")["symbol"] == "TCS"
    assert symbols.resolve_symbol(db, "banknifty")["symbol"] == "NIFTY_BANK"
    assert symbols.resolve_symbol(db, "nifty 50")["symbol"] == "NIFTY_50"
    assert symbols.resolve_symbol(db, "^NSEI")["symbol"] == "NIFTY_50"            # a Yahoo ticker
    assert symbols.resolve_symbol(db, "NOSUCHCO") is None and symbols.resolve_symbol(db, "  ") is None
    db["instruments"].update_one({"symbol": "TCS"}, {"$set": {"active": False}})
    assert symbols.resolve_symbol(db, "TCS") is None                                # inactive instruments are not resolved


def test_symbol_history_is_built_from_files_and_resolves_old_names(db, tmp_path):
    (tmp_path / "AXISBANK.csv").write_text("Date,Symbol\n2000-01-03,UTIBANK\n2007-08-14,UTIBANK\n2007-08-16,AXISBANK\n")
    (tmp_path / "HINDALCO.csv").write_text("Date,Symbol\n2000-01-03,HINDALC0\n2007-08-27,HINDALCO\n")
    (tmp_path / "MM.csv").write_text("Date,Symbol\n2000-01-03,M&M\n")
    (tmp_path / "NIFTY50_all.csv").write_text("Date,Symbol\n2000-01-03,ZZZ\n")          # must be skipped
    history = symbols.build_symbol_history(tmp_path)
    assert set(history) == {"AXISBANK", "HINDALCO", "M&M"}
    assert history["AXISBANK"]["symbol_history"][0]["symbol"] == "UTIBANK"
    assert "digit zero" in history["HINDALCO"]["symbol_history"][0]["note"]
    assert history["M&M"]["symbol_history"] == [] and history["M&M"]["aliases"] == ["MM"]

    stored = symbols.store_symbol_history(db, history)
    assert stored == {"instruments_updated": 1, "not_in_instruments": 2}               # only AXISBANK is seeded here
    assert symbols.resolve_symbol(db, "UTIBANK")["symbol"] == "AXISBANK"
    entry = db["instruments"].find_one({"symbol": "AXISBANK"})["symbol_history"][0]
    assert entry["first_date"] == datetime(2000, 1, 3, tzinfo=UTC) and entry["last_date"] == datetime(2007, 8, 14, tzinfo=UTC)


# ------------------------------------------------------------------ job tracking
def test_tracked_job_records_success_and_failure(db):
    with tracked_job(db, "demo") as job:
        job["counts"] = {"rows": 5}
    done = db["processing_jobs"].find_one({"job_id": job["job_id"]})
    assert done["status"] == "completed" and done["progress"] == 100.0 and done["counts"] == {"rows": 5}
    assert done["job_type"] == "clean" and done["params"]["step"] == "demo"

    with pytest.raises(ValueError):
        with tracked_job(db, "demo") as bad:
            raise ValueError("boom")
    failed = db["processing_jobs"].find_one({"job_id": bad["job_id"]})
    assert failed["status"] == "failed" and "boom" in failed["error"]
