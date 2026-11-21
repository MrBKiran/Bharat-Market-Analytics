"""Phase 7 tests: market analytics over small made-up panels, sector assignment, and an end-to-end run."""
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.analytics import market as m
from app.analytics import sectors
from app.db.mongo import get_client
from app.db.setup import setup_database

UTC = timezone.utc


def g(rate: float, n: int, start: float = 100.0) -> list[float]:
    """Geometric series: start * (1 + rate) ** i."""
    return list(start * (1 + rate) ** np.arange(n))


def make_panel(series: dict, volumes: dict = None, kinds: dict = None, sector_of: dict = None) -> m.Panel:
    """Daily panel; every series ends on the same last date (shorter ones start later)."""
    n = max(len(v) for v in series.values())
    idx = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")

    def frame(data):
        return pd.DataFrame({k: pd.Series(list(v), index=idx[n - len(v):]) for k, v in data.items()}, index=idx)

    close = frame(series)
    volume = frame(volumes).reindex(columns=close.columns) if volumes else close * np.nan
    instruments = {k: {"symbol": k, "name": f"{k} Ltd", "instrument_type": (kinds or {}).get(k, "equity"),
                       **({"sector": sector_of[k]} if sector_of and k in sector_of else {})} for k in series}
    return m.Panel(close, volume, instruments)


def last(panel):
    return panel.close.index[-1]


# ------------------------------------------------------------------ performance and movers
def test_performance_matches_hand_calculation():
    panel = make_panel({"A": g(0.01, 500)})
    p = m.performance(panel, last(panel), ["A"])["A"]
    end = last(panel)
    days = lambda offset: (end - (end - offset)).days  # noqa: E731
    assert p["1d"] == pytest.approx(1.0, abs=1e-3)
    assert p["1w"] == pytest.approx((1.01 ** 7 - 1) * 100, abs=1e-3)
    assert p["1m"] == pytest.approx((1.01 ** days(pd.DateOffset(months=1)) - 1) * 100, abs=1e-3)
    assert p["1y"] == pytest.approx((1.01 ** days(pd.DateOffset(months=12)) - 1) * 100, abs=1e-2)
    ytd_days = (end - pd.Timestamp(year=end.year - 1, month=12, day=31, tz="UTC")).days
    assert p["ytd"] == pytest.approx((1.01 ** ytd_days - 1) * 100, abs=1e-2) and p["date"] == end.date().isoformat()


def test_periods_without_enough_history_are_none_not_wrong():
    panel = make_panel({"A": g(0.01, 50)})
    p = m.performance(panel, last(panel), ["A"])["A"]
    assert p["1w"] is not None and p["1y"] is None and p["6m"] is None


def flat_then(change_pct: dict, n=40) -> dict:
    return {k: [100.0] * (n - 1) + [100.0 * (1 + v / 100)] for k, v in change_pct.items()}


def test_top_movers_rank_stocks_only():
    panel = make_panel(flat_then({"A": 10, "B": 5, "C": -5, "D": -10, "IDX": 50}), kinds={"IDX": "index"})
    perf = m.performance(panel, last(panel), list(panel.close.columns))
    out = m.top_movers(perf, panel, n=5)["1d"]
    assert [x["symbol"] for x in out["gainers"]] == ["A", "B"] and [x["symbol"] for x in out["losers"]] == ["D", "C"]
    assert "IDX" not in [x["symbol"] for x in out["gainers"]]                     # indices are not ranked as stocks
    assert out["gainers"][0]["return_pct"] == pytest.approx(10.0)


# ------------------------------------------------------------------ trend and breadth
def test_gainers_only_rose_and_losers_only_fell():
    """In a small universe the bottom-n can include winners; they must not be listed as 'losers'."""
    panel = make_panel(flat_then({"A": 10, "B": 5, "C": 2, "D": 1}))
    perf = m.performance(panel, last(panel), list(panel.close.columns))
    out = m.top_movers(perf, panel, n=5)["1d"]
    assert [x["symbol"] for x in out["gainers"]] == ["A", "B"] and out["losers"] == []
    falling = make_panel(flat_then({"A": -1, "B": -5, "C": 3, "D": 4}))
    out2 = m.top_movers(m.performance(falling, last(falling), list(falling.close.columns)), falling, n=5)["1d"]
    assert [x["symbol"] for x in out2["losers"]] == ["B", "A"] and [x["symbol"] for x in out2["gainers"]] == ["D", "C"]


def test_trend_labels_and_summary():
    panel = make_panel({"UP": g(0.002, 300), "DOWN": g(-0.002, 300), "SHORT": g(0.01, 100), "IDX": g(0.002, 300)},
                       kinds={"IDX": "index"})
    t = m.trend(panel, last(panel), list(panel.close.columns))
    assert t["symbols"]["UP"]["trend"] == "bullish" and t["symbols"]["UP"]["score"] == 5 and t["symbols"]["UP"]["golden_cross"]
    down = t["symbols"]["DOWN"]
    assert down["trend"] == "bearish" and down["score"] <= 1                        # a decaying decline can flip MACD positive
    assert not down["above_sma50"] and not down["above_sma200"] and down["rsi_14"] < 1
    assert t["insufficient_history"] == ["SHORT"]
    assert t["stock_summary"] == {"bullish": 1, "neutral": 0, "bearish": 1}          # the index is not counted


def test_market_breadth_counts():
    panel = make_panel({"A": g(0.002, 300), "B": g(0.003, 300), "C": g(-0.002, 300), "D": [100.0] * 298 + [101.0, 101.0]})
    end, symbols = last(panel), list(panel.close.columns)
    perf, trend = m.performance(panel, end, symbols), m.trend(panel, end, symbols)
    b = m.market_breadth(panel, end, symbols, perf, trend)
    assert (b["advancers"], b["decliners"], b["unchanged"]) == (2, 1, 1)
    assert b["pct_above_sma50"] == 75.0 and b["pct_above_sma200"] == 75.0          # A, B and D (101 vs an average of 100) above; C below
    assert b["new_52w_highs"] == 3 and b["new_52w_lows"] == 1                       # A, B, D at their highs; C at its low
    returns = panel.close.pct_change(fill_method=None)
    expected_last = int(((returns > 0).sum(axis=1) - (returns < 0).sum(axis=1)).cumsum().iloc[-1])
    assert b["ad_line_monthly"][-1][1] == expected_last and b["advance_decline_ratio"] == 2.0


def test_sector_performance_groups_and_averages():
    panel = make_panel(flat_then({"A": 10, "B": 20, "C": -10, "D": 4, "IDX": 99}), kinds={"IDX": "index"},
                       sector_of={"A": "IT", "B": "IT", "C": "Banks"})
    perf = m.performance(panel, last(panel), list(panel.close.columns))
    out = m.sector_performance(panel, perf, list(panel.close.columns))
    assert out["sectors"]["IT"]["1d"] == pytest.approx(15.0) and out["sectors"]["Banks"]["1d"] == pytest.approx(-10.0)
    assert out["sectors"]["Unclassified"]["members"] == ["D"] and "IDX" not in str(out["sectors"])
    assert out["ranking_by_1m"][0] == "IT" and out["sectors"]["IT"]["best_1m"] == "B" and out["sectors"]["IT"]["worst_1m"] == "A"


# ------------------------------------------------------------------ volatility, volume, correlation
def test_volatility_ranks_the_jumpier_series_first():
    wild = list(100 * np.cumprod(np.where(np.arange(400) % 2, 1.01, 0.99)))
    calm = list(100 * np.cumprod(np.where(np.arange(400) % 2, 1.001, 0.999)))
    panel = make_panel({"WILD": wild, "CALM": calm})
    v = m.volatility(panel, last(panel), ["WILD", "CALM"])
    assert v["most_volatile"][0] == "WILD" and v["least_volatile"][0] == "CALM"
    w = v["symbols"]["WILD"]
    assert 0.95 < w["daily_vol_pct"] < 1.05 and w["annualised_vol_pct"] == pytest.approx(w["daily_vol_pct"] * 252 ** 0.5, abs=0.02)
    assert w["weekly_vol_pct"] is not None and w["hist_vol_20_pct"] > 10


def test_volume_spike_detection():
    panel = make_panel({"S": [100.0] * 60, "IDX": [100.0] * 60}, kinds={"IDX": "index"},
                       volumes={"S": [1000.0] * 59 + [3000.0]})
    v = m.volume_analysis(panel, last(panel), ["S", "IDX"])
    assert list(v["symbols"]) == ["S"]                                              # the index has no volume
    assert v["symbols"]["S"]["ratio_to_prior_20"] == 3.0 and v["volume_spikes_2x"] == ["S"]
    assert v["symbols"]["S"]["value_traded"] == 300_000 and v["most_active_by_value"] == ["S"]


def test_correlation_matrix_and_pairs():
    rng = np.random.default_rng(3)
    a = list(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400))))
    panel = make_panel({"A": a, "B": [2 * x for x in a], "C": [1 / x for x in a], "NEW": a[-60:]})
    c = m.correlation(panel, last(panel), list(panel.close.columns), window=300)
    assert "NEW" not in c["symbols"]                                                # too little overlapping history
    i = {s: k for k, s in enumerate(c["symbols"])}
    assert c["matrix"][i["A"]][i["B"]] == pytest.approx(1.0, abs=1e-3) and c["matrix"][i["A"]][i["C"]] < -0.95
    assert {c["most_correlated_stock_pairs"][0]["a"], c["most_correlated_stock_pairs"][0]["b"]} == {"A", "B"}
    assert "C" in (c["least_correlated_stock_pairs"][0]["a"], c["least_correlated_stock_pairs"][0]["b"])
    assert "note" in m.correlation(make_panel({"A": a}), last(make_panel({"A": a})), ["A"])


# ------------------------------------------------------------------ indices, distribution, history, outliers
def test_max_drawdown():
    s = pd.Series([100.0, 120.0, 60.0, 90.0], index=pd.date_range("2024-01-01", periods=4, tz="UTC"))
    assert m.max_drawdown(s) == (-50.0, "2024-01-02", "2024-01-03")


def test_index_analysis_drawdown_years_and_pair_statistics():
    path = np.concatenate([np.linspace(100, 200, 200), np.linspace(200, 100, 200)[1:], np.linspace(100, 150, 200)[1:]])
    panel = make_panel({"NIFTY_50": list(path), "NIFTY_BANK": list(path * 2)}, kinds={"NIFTY_50": "index", "NIFTY_BANK": "index"})
    out = m.index_analysis(panel, last(panel), ["NIFTY_50", "NIFTY_BANK"])
    n50 = out["indices"]["NIFTY_50"]
    assert n50["max_drawdown_pct"] == -50.0 and n50["max_drawdown_peak"] == "2023-07-19" and n50["max_drawdown_trough"] == "2024-02-03"
    assert n50["current_drawdown_pct"] == pytest.approx((150 / 200 - 1) * 100, abs=0.01)
    assert len(n50["best_days"]) == 5 and n50["calendar_year_returns_pct"] and n50["trend"] is not None
    assert n50["pct_below_52w_high"] <= 0 and n50["high_52w"] >= n50["low_52w"]
    assert out["nifty_vs_bank"]["correlation"] == pytest.approx(1.0) and out["nifty_vs_bank"]["beta_of_bank_on_nifty"] == pytest.approx(1.0)


def test_return_distribution_is_complete_and_skips_short_series():
    rng = np.random.default_rng(5)
    panel = make_panel({"N": list(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 1200)))), "SHORT": g(0.001, 100)})
    d = m.return_distribution(panel, ["N", "SHORT"])["symbols"]
    assert list(d) == ["N"]
    h = d["N"]["histogram"]
    assert sum(h["counts"]) + h["below_minus_10"] + h["at_or_above_10"] == d["N"]["observations"] == 1199
    assert 0 < d["N"]["var_95_pct"] < d["N"]["var_99_pct"] and abs(d["N"]["skew"]) < 0.4


def test_historical_statistics_cagr_and_drawdown():
    rate = 2 ** (1 / 365.25) - 1                                                    # doubles every year
    panel = make_panel({"S": g(rate, 731), "IDX": g(rate, 731)}, kinds={"IDX": "index"}, volumes={"S": [1000.0] * 731})
    out = m.historical_statistics(panel, ["S", "IDX"])["symbols"]
    assert out["S"]["cagr_pct"] == pytest.approx(100.0, abs=1.0) and out["S"]["max_drawdown_pct"] == 0
    assert out["S"]["average_volume"] == 1000 and "average_volume" not in out["IDX"] and out["IDX"]["type"] == "index"
    assert out["S"]["pct_up_days"] == 100.0 and out["S"]["days_over_30pct_move"] == 0
    jumpy = make_panel({"J": [100.0] * 300 + [180.0] + [180.0] * 50})                  # one +80% day
    assert m.historical_statistics(jumpy, ["J"])["symbols"]["J"]["days_over_30pct_move"] == 1


def test_outliers_finds_the_injected_move():
    rng = np.random.default_rng(1)
    calm = list(100 * np.exp(np.cumsum(rng.normal(0, 0.005, 300))))
    spiked = list(calm)
    spiked[150:] = [x * 1.6 for x in spiked[150:]]                                   # one +60% day
    panel = make_panel({"CALM": calm, "SPIKE": spiked})
    out = m.outliers(panel, ["CALM", "SPIKE"])
    assert out["largest_moves"][0]["symbol"] == "SPIKE" and out["largest_moves"][0]["return_pct"] == pytest.approx(60.0, abs=3)
    assert out["robust_outlier_days"].get("SPIKE", 0) >= 1


# ------------------------------------------------------------------ snapshots and helpers
def test_stale_instruments_are_excluded_and_snapshots_found():
    panel = make_panel({"A": g(0.001, 200), "B": g(0.001, 200), "S": g(0.001, 200), "NIFTY_50": g(0.001, 200)},
                       kinds={"NIFTY_50": "index"})
    panel.close.loc[panel.close.index[-30:], "S"] = np.nan                           # S stopped 30 days ago
    active, stale = m.universe(panel, last(panel))
    assert stale == ["S"] and set(active) == {"A", "B", "NIFTY_50"}
    assert m.latest_as_of(panel) == last(panel)
    assert m.full_universe_as_of(panel) == panel.close.index[-31]                    # the last day S still traded


def test_clean_makes_results_safe_for_mongodb():
    out = m.clean({"a": np.int64(3), "b": np.float64("nan"), "c": [np.float32(1.5), pd.Timestamp("2024-01-02", tz="UTC")],
                   "d": np.bool_(True), 5: float("inf"), "e": {"x": np.float64(2.5)}})
    assert out == {"a": 3, "b": None, "c": [1.5, "2024-01-02T00:00:00+00:00"], "d": True, "5": None, "e": {"x": 2.5}}
    assert type(out["a"]) is int and type(out["d"]) is bool


def test_pct_handles_bad_inputs():
    assert m.pct(110, 100) == 10.0 and m.pct(1, 0) is None and m.pct(None, 5) is None and m.pct(float("nan"), 5) is None


# ------------------------------------------------------------------ database: storing results, sectors, end to end
@pytest.fixture()
def db():
    client = get_client()
    name = f"BharatMarketTest_{uuid.uuid4().hex[:8]}"
    assert name.startswith("BharatMarketTest_")
    database = client[name]
    setup_database(database)
    yield database
    client.drop_database(name)


def put_adjusted(db, symbol, n, seed, *, volume=True, start="2023-01-01"):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n)))
    dates = pd.date_range(start, periods=n, freq="D", tz="UTC")
    db["adjusted_prices"].insert_many([{
        "symbol": symbol, "exchange": "NSE", "interval": "1d", "date": d.to_pydatetime(), "open": float(c), "high": float(c * 1.01),
        "low": float(c * 0.99), "close": float(c), "adj_factor": 1.0, "source": "yfinance", "method": "provider_adjusted",
        "processed_at": datetime(2024, 1, 1, tzinfo=UTC), **({"volume": int(rng.integers(1000, 9000))} if volume else {})}
        for d, c in zip(dates, close)])


def test_store_result_replaces_instead_of_accumulating(db):
    scope = {"snapshot": "latest", "as_of": "2024-01-01"}
    m.store_result(db, "demo", scope, {"x": np.float64(1.5)})
    m.store_result(db, "demo", scope, {"x": np.float64(2.5)})
    m.store_result(db, "demo", {"snapshot": "full_universe", "as_of": "2024-01-01"}, {"x": 9})
    assert db["analytics_results"].count_documents({"analysis_type": "demo"}) == 2
    assert m.get_result(db, "demo", scope) == {"x": 2.5} and m.get_result(db, "demo", {"nope": 1}) is None


def test_run_analytics_end_to_end(db):
    for i, sym in enumerate(["TCS", "INFY", "RELIANCE", "NIFTY_50", "NIFTY_BANK"]):
        put_adjusted(db, sym, 320, seed=i, volume=sym not in ("NIFTY_50", "NIFTY_BANK"))
    summary = m.run_analytics(db)
    assert summary["stored"] == 21 and db["analytics_results"].count_documents({}) == 21     # 9 x 2 snapshots + 3 history
    assert set(summary["snapshots"]) == {"latest", "full_universe"}
    as_of = summary["snapshots"]["latest"]["as_of"]
    scope = {"snapshot": "latest", "as_of": as_of}
    trend = m.get_result(db, "trend", scope)
    assert trend["universe"] == {"symbols": 5, "stocks": 3, "indices": 2, "stale_excluded": []}
    assert sum(trend["stock_summary"].values()) == 3
    assert set(m.get_result(db, "sector_performance", scope)["sectors"]) == {"Information Technology", "Oil, Gas & Consumable Fuels"}
    assert m.get_result(db, "index_analysis", scope)["nifty_vs_bank"]["window_days"] > 60
    assert m.get_result(db, "historical_statistics", {"history": "all"})["symbols"]["TCS"]["rows"] == 320
    m.run_analytics(db)
    assert db["analytics_results"].count_documents({}) == 21                                   # re-running replaces


def test_run_analytics_needs_processed_data(db):
    with pytest.raises(ValueError):
        m.run_analytics(db)


def test_assign_sectors_fills_gaps_and_never_overwrites(db):
    now = datetime(2024, 1, 1, tzinfo=UTC)
    for sym in ("BPCL", "UPL", "FOOBAR"):
        db["instruments"].insert_one({"symbol": sym, "exchange": "NSE", "name": sym, "instrument_type": "equity",
                                      "active": True, "created_at": now})
    db["instruments"].update_one({"symbol": "TCS"}, {"$set": {"sector": "My Own Sector"}})
    dry = sectors.assign_sectors(db, dry_run=True)
    assert dry["assigned"] == 2 and db["instruments"].find_one({"symbol": "BPCL"}).get("sector") is None
    res = sectors.assign_sectors(db)
    assert res["assigned"] == 2 and res["unmapped"] == ["FOOBAR"] and res["already_had_sector"] == 10
    bpcl = db["instruments"].find_one({"symbol": "BPCL"})
    assert bpcl["sector"] == "Oil, Gas & Consumable Fuels" and bpcl["sector_source"] == "assigned_by_project"
    assert db["instruments"].find_one({"symbol": "TCS"})["sector"] == "My Own Sector"           # not overwritten
    assert sectors.assign_sectors(db)["assigned"] == 0                                           # idempotent


def test_sector_table_covers_each_stock_once():
    flat = [s for syms in sectors.SECTOR_MAP.values() for s in syms]
    assert len(flat) == len(set(flat)) == 50 and "M&M" in flat
