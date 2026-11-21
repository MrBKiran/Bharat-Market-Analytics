"""Phase 7 tests: indicator formulas, the independent `ta` cross-check, and the storage pipeline."""
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.analytics import indicator_pipeline as pipe
from app.analytics import indicators as ind
from app.db.mongo import get_client
from app.db.setup import setup_database

UTC = timezone.utc


def walk(n=600, seed=7, volume=True) -> pd.DataFrame:
    """A random-walk price series with consistent bars (low <= open, close <= high)."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    open_ = close * (1 + rng.uniform(-0.004, 0.004, n))
    high = np.maximum.reduce([close * (1 + rng.uniform(0, 0.01, n)), open_, close])
    low = np.minimum.reduce([close * (1 - rng.uniform(0, 0.01, n)), open_, close])
    return pd.DataFrame({
        "ts": pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.integers(1000, 5000, n).astype(float) if volume else np.zeros(n)})


# ------------------------------------------------------------------ formulas with known answers
def test_sma_is_exact():
    out = ind.sma(pd.Series(range(1, 11), dtype=float), 3)
    assert out.isna().sum() == 2 and list(out.dropna()) == [2, 3, 4, 5, 6, 7, 8, 9]


def test_ema_follows_the_recursion():
    out = ind.ema(pd.Series([1.0, 2.0, 3.0, 4.0]), 3)          # alpha = 0.5, seeded with the first value
    assert out.isna().sum() == 2 and out.iloc[2] == pytest.approx(2.25) and out.iloc[3] == pytest.approx(3.125)


def test_rsi_extremes():
    up, down, flat = pd.Series(np.arange(1.0, 60)), pd.Series(np.arange(60.0, 1, -1)), pd.Series([5.0] * 60)
    assert ind.rsi(up).iloc[-1] == 100 and ind.rsi(down).iloc[-1] == pytest.approx(0, abs=1e-9)
    assert ind.rsi(flat).iloc[-1] == 50
    assert ind.rsi(walk().close).dropna().between(0, 100).all()


def test_bollinger_on_a_constant_series_has_zero_width():
    b = ind.bollinger(pd.Series([10.0] * 40), 20, 2)
    assert (b["bb_width"].dropna() == 0).all() and (b["bb_upper"].dropna() == 10).all() and b["bb_pct_b"].isna().all()


def test_bollinger_bands_bracket_the_mean_by_k_population_stds():
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    b = ind.bollinger(close, 5, 2)
    std = np.std([1, 2, 3, 4, 5])                                   # ddof=0
    assert b["bb_mid"].iloc[-1] == 3 and b["bb_upper"].iloc[-1] == pytest.approx(3 + 2 * std)


def test_stochastic_reaches_100_at_a_new_high_and_0_at_a_new_low():
    rising = pd.Series(np.arange(1.0, 40))
    assert ind.stochastic(rising, rising, rising)["stoch_k"].iloc[-1] == 100
    falling = pd.Series(np.arange(40.0, 1, -1))
    assert ind.stochastic(falling, falling, falling)["stoch_k"].iloc[-1] == 0


def test_true_range_and_atr_on_a_gap():
    high, low, close = pd.Series([10.0, 12.0]), pd.Series([9.0, 11.0]), pd.Series([9.5, 11.5])
    assert ind.true_range(high, low, close).iloc[1] == 2.5          # |high - previous close| beats high - low = 1
    assert ind.atr(pd.Series([2.0] * 40), pd.Series([1.0] * 40), pd.Series([1.5] * 40), 14).iloc[-1] == pytest.approx(1.0)


def test_rolling_vwap_by_hand():
    h, l, c = pd.Series([10.0, 20.0]), pd.Series([10.0, 20.0]), pd.Series([10.0, 20.0])
    v = pd.Series([1.0, 3.0])
    assert ind.vwap_rolling(h, l, c, v, 2).iloc[1] == pytest.approx((10 * 1 + 20 * 3) / 4)


def test_session_vwap_restarts_each_day():
    ts = pd.Series(pd.to_datetime(["2024-01-01 04:00", "2024-01-01 04:05", "2024-01-02 04:00"], utc=True))
    p = pd.Series([10.0, 20.0, 50.0])
    v = pd.Series([1.0, 1.0, 5.0])
    out = ind.vwap_session(ts, p, p, p, v)
    assert out.iloc[0] == 10 and out.iloc[1] == 15 and out.iloc[2] == 50   # the second day does not carry over


def test_average_range():
    out = ind.average_range(pd.Series([12.0] * 20), pd.Series([10.0] * 20), pd.Series([100.0] * 20), 14)
    assert out["adr_14"].iloc[-1] == 2 and out["adr_pct_14"].iloc[-1] == pytest.approx(2.0)


def test_historical_volatility():
    steady = pd.Series(100 * 1.01 ** np.arange(60))                          # constant growth: no variability
    assert ind.historical_volatility(steady, 20).iloc[-1] == pytest.approx(0, abs=1e-9)
    alternating = pd.Series(100 * np.cumprod(np.where(np.arange(60) % 2, 1.01, 0.99)))
    expected = np.log(alternating / alternating.shift(1)).tail(20).std(ddof=1) * np.sqrt(252) * 100
    assert ind.historical_volatility(alternating, 20).iloc[-1] == pytest.approx(expected)


# ------------------------------------------------------------------ independent cross-check against `ta`
def test_matches_the_ta_library():
    ta_momentum = pytest.importorskip("ta.momentum")
    ta_trend, ta_vol = pytest.importorskip("ta.trend"), pytest.importorskip("ta.volatility")
    d = walk(1500)
    c, h, lo, skip = d["close"], d["high"], d["low"], 300

    def close_enough(a, b):
        diff = (a - b).abs().iloc[skip:]
        assert diff.max() < 1e-6, diff.max()

    close_enough(ind.sma(c, 50), ta_trend.SMAIndicator(c, 50).sma_indicator())
    close_enough(ind.ema(c, 200), ta_trend.EMAIndicator(c, 200).ema_indicator())
    close_enough(ind.rsi(c, 14), ta_momentum.RSIIndicator(c, 14).rsi())
    m, tm = ind.macd(c), ta_trend.MACD(c)
    close_enough(m["macd"], tm.macd())
    close_enough(m["macd_signal"], tm.macd_signal())
    close_enough(m["macd_hist"], tm.macd_diff())
    close_enough(ind.atr(h, lo, c, 14), ta_vol.AverageTrueRange(h, lo, c, 14).average_true_range())
    b, tb = ind.bollinger(c, 20, 2), ta_vol.BollingerBands(c, 20, 2)
    close_enough(b["bb_upper"], tb.bollinger_hband())
    close_enough(b["bb_lower"], tb.bollinger_lband())
    s, ts = ind.stochastic(h, lo, c, 14, 3), ta_momentum.StochasticOscillator(h, lo, c, 14, 3)
    close_enough(s["stoch_k"], ts.stoch())
    close_enough(s["stoch_d"], ts.stoch_signal())


# ------------------------------------------------------------------ the full set
def test_compute_indicators_columns_and_warm_up():
    out = ind.compute_indicators(walk(300), "1d")
    assert list(out.columns) == ["ts", *ind.INDICATOR_COLUMNS]
    assert out["sma_200"].first_valid_index() == 199 and out["sma_20"].first_valid_index() == 19
    assert out["vwap_20"].notna().any() and out["vwap_session"].isna().all()       # daily: rolling VWAP only
    assert out["vol_sma_20"].notna().any()


def test_indices_without_volume_get_no_volume_indicators():
    out = ind.compute_indicators(walk(300, volume=False), "1d")
    assert out[["vwap_20", "vwap_session", "vol_sma_20", "vol_sma_50"]].isna().all().all()
    assert out["rsi_14"].notna().any()


def test_intraday_uses_session_vwap_and_annualises_by_bars_per_day():
    d = walk(300)
    d["ts"] = pd.date_range("2024-01-01 03:45", periods=300, freq="5min", tz="UTC")
    out = ind.compute_indicators(d, "5m")
    assert out["vwap_session"].notna().any() and out["vwap_20"].isna().all()
    assert ind.PERIODS_PER_YEAR["5m"] == 75 * 252 and ind.PERIODS_PER_YEAR["1d"] == 252


def test_unsupported_interval_is_rejected():
    with pytest.raises(ValueError):
        ind.compute_indicators(walk(50), "3m")


# ------------------------------------------------------------------ storage pipeline (database)
@pytest.fixture()
def db():
    client = get_client()
    name = f"BharatMarketTest_{uuid.uuid4().hex[:8]}"
    assert name.startswith("BharatMarketTest_")
    database = client[name]
    setup_database(database)
    yield database
    client.drop_database(name)


def put_daily(db, symbol, frame, *, exchange="NSE"):
    docs = [{"symbol": symbol, "exchange": exchange, "interval": "1d", "date": r.ts.to_pydatetime(),
             "open": float(r.open), "high": float(r.high), "low": float(r.low), "close": float(r.close),
             "adj_factor": 1.0, "source": "yfinance", "method": "provider_adjusted",
             "processed_at": datetime(2024, 1, 1, tzinfo=UTC), **({"volume": int(r.volume)} if r.volume > 0 else {})}
            for r in frame.itertuples()]
    db["adjusted_prices"].insert_many(docs)


def put_intraday(db, symbol, interval, bars, *, exchange="NSE"):
    meta = {"symbol": symbol, "exchange": exchange, "interval": interval, "instrument_type": "index",
            "source": "kaggle", "is_synthetic": False, "price_basis": "index_level"}
    start = datetime(2024, 1, 1, 3, 45, tzinfo=UTC)
    d = walk(bars, volume=False)
    db["intraday_prices"].insert_many([{"ts": start + timedelta(minutes=5 * i), "meta": meta, "open": float(r.open),
                                        "high": float(r.high), "low": float(r.low), "close": float(r.close)}
                                       for i, r in enumerate(d.itertuples())])
    db["intraday_coverage"].insert_one({"symbol": symbol, "exchange": exchange, "interval": interval, "source": "kaggle",
                                        "first_ts": start, "last_ts": start + timedelta(minutes=5 * bars),
                                        "row_count": bars, "updated_at": start})


def test_daily_indicators_are_stored_with_meta_and_without_nan(db):
    put_daily(db, "TCS", walk(300))
    res = pipe.compute_and_store(db, "TCS", "NSE", "1d")
    assert res["status"] == "stored" and res["bars"] == 300 and 0 < res["documents"] < 300
    docs = list(db["indicators"].find({"meta.symbol": "TCS"}).sort("ts", 1))
    assert len(docs) == res["documents"]
    assert docs[0]["meta"] == {"symbol": "TCS", "exchange": "NSE", "interval": "1d", "input": "adjusted_prices"}
    assert all(isinstance(v, float) and np.isfinite(v) for d in docs for k, v in d.items() if k not in ("_id", "ts", "meta"))
    assert "sma_200" not in docs[0] and "sma_200" in docs[-1] and "vwap_20" in docs[-1]
    expected = ind.compute_indicators(pipe.load_series(db, "TCS", "NSE", "1d")[0], "1d")
    assert docs[-1]["rsi_14"] == pytest.approx(round(float(expected["rsi_14"].iloc[-1]), 6))


def test_recomputing_replaces_and_never_touches_other_series(db):
    put_daily(db, "TCS", walk(300, seed=1))
    put_daily(db, "INFY", walk(300, seed=2))
    pipe.compute_and_store(db, "TCS", "NSE", "1d")
    pipe.compute_and_store(db, "INFY", "NSE", "1d")
    before = db["indicators"].count_documents({"meta.symbol": "INFY"})
    pipe.compute_and_store(db, "TCS", "NSE", "1d")
    pipe.compute_and_store(db, "TCS", "NSE", "1d")
    assert db["indicators"].count_documents({"meta.symbol": "TCS"}) == pipe.compute_and_store(db, "TCS", "NSE", "1d", dry_run=True)["documents"]
    assert db["indicators"].count_documents({"meta.symbol": "INFY"}) == before


def test_dry_run_and_missing_data(db):
    put_daily(db, "TCS", walk(100))
    assert pipe.compute_and_store(db, "TCS", "NSE", "1d", dry_run=True)["status"] == "dry_run"
    assert db["indicators"].count_documents({}) == 0
    assert pipe.compute_and_store(db, "NOSUCH", "NSE", "1d")["status"] == "no_data"


def test_index_without_volume_stores_no_volume_indicators(db):
    put_daily(db, "NIFTY_50", walk(300, volume=False))
    pipe.compute_and_store(db, "NIFTY_50", "NSE", "1d")
    last = db["indicators"].find_one({"meta.symbol": "NIFTY_50"}, sort=[("ts", -1)])
    assert "rsi_14" in last and not any(k in last for k in ("vwap_20", "vwap_session", "vol_sma_20"))


def test_intraday_indicators_and_interval_gating(db):
    put_intraday(db, "NIFTY_50", "5m", 250)
    results = pipe.run_indicators(db, ["5m", "15m"], symbols=["NIFTY_50"])
    assert [(r["symbol"], r["interval"], r["status"]) for r in results] == [("NIFTY_50", "5m", "stored")]   # 15m has no data
    doc = db["indicators"].find_one({"meta.interval": "5m"}, sort=[("ts", -1)])
    assert doc["meta"]["input"] == "intraday_prices" and "rsi_14" in doc and "hist_vol_20" in doc


def test_run_indicators_covers_every_instrument_with_data(db):
    put_daily(db, "TCS", walk(100))
    put_daily(db, "RELIANCE", walk(100))
    results = pipe.run_indicators(db, ["1d"])
    assert sorted(r["symbol"] for r in results) == ["RELIANCE", "TCS"]
