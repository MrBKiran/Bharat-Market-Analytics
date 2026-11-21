"""Phase 9 tests: costs, the seven strategies (above all: no look-ahead), the engine's execution rules,
metrics, and storing results in MongoDB.

The engine tests use tiny hand-built price frames whose correct answer can be worked out on paper.
"""
import math
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.backtesting import costs, metrics, runner
from app.backtesting.engine import BacktestConfig, buy_and_hold, run_backtest
from app.backtesting.strategies import STRATEGIES, Signals, cross_up, generate_signals, resolve_params
from app.db.mongo import get_client
from app.db.setup import setup_database

UTC = timezone.utc
NO_COST = BacktestConfig(cost_model=costs.preset("none"), slippage_bps=0.0)


# ------------------------------------------------------------------------------------------- test data
def wavy(n=900, seed=7) -> pd.DataFrame:
    """A cyclical random walk with trends, dips and breakouts, so every strategy has something to trade."""
    rng = np.random.default_rng(seed)
    i = np.arange(n)
    close = 100 + 25 * np.sin(i / 40) + 15 * np.sin(i / 11) + np.cumsum(rng.normal(0.05, 1.2, n))
    close = np.maximum(close, 20)
    open_ = close + rng.normal(0, 0.6, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 1.5, n)
    return pd.DataFrame({"ts": pd.bdate_range("2015-01-01", periods=n, tz="UTC"), "open": open_, "high": high,
                         "low": low, "close": close, "volume": rng.integers(10_000, 90_000, n).astype(float)})


def frame(o, h=None, l=None, c=None, start="2024-01-01") -> pd.DataFrame:
    """Hand-made daily bars. Unless given, high/low hug the open and close."""
    o = [float(x) for x in o]
    c = [float(x) for x in (c if c is not None else o)]
    h = [float(x) for x in h] if h is not None else [max(a, b) + 0.1 for a, b in zip(o, c)]
    l = [float(x) for x in l] if l is not None else [min(a, b) - 0.1 for a, b in zip(o, c)]
    return pd.DataFrame({"ts": pd.bdate_range(start, periods=len(o), tz="UTC"), "open": o, "high": h, "low": l,
                         "close": c, "volume": 1000.0})


def signals(n, entry=(), exit_from=None, exit_at=(), stop=None, target_r=None) -> Signals:
    entry_s = pd.Series(False, index=range(n))
    for i in entry:
        entry_s.iloc[i] = True
    exit_s = pd.Series(False, index=range(n))
    if exit_from is not None:
        exit_s.iloc[exit_from:] = True
    for i in exit_at:
        exit_s.iloc[i] = True
    return Signals(entry=entry_s, exit=exit_s, stop=pd.Series(stop if stop is not None else np.nan, index=range(n), dtype=float),
                   target_r=target_r)


# ------------------------------------------------------------------------------------------------ costs
def test_delivery_costs_match_hand_calculation():
    buy = costs.DELIVERY.leg("buy", 100_000)
    assert buy["stt"] == pytest.approx(100.0) and buy["stamp"] == pytest.approx(15.0) and buy["dp"] == 0
    assert buy["exchange"] == pytest.approx(100_000 * 0.00297 / 100) and buy["sebi"] == pytest.approx(0.10)     # Rs 10 per crore
    assert buy["gst"] == pytest.approx((0 + buy["exchange"] + buy["sebi"]) * 0.18)
    sell = costs.DELIVERY.leg("sell", 100_000)
    assert sell["stamp"] == 0 and sell["dp"] == 13.5 and sell["stt"] == pytest.approx(100.0)
    assert sell["total"] == pytest.approx(sell["stt"] + sell["exchange"] + sell["sebi"] + 13.5 + sell["gst"])


def test_intraday_brokerage_is_capped_and_stt_is_sell_side_only():
    big = costs.INTRADAY.leg("buy", 10_000_000)
    assert big["brokerage"] == 20.0 and big["stt"] == 0.0
    assert costs.INTRADAY.leg("sell", 10_000_000)["stt"] == pytest.approx(10_000_000 * 0.025 / 100)
    assert costs.INTRADAY.leg("buy", 10_000)["brokerage"] == pytest.approx(3.0)       # 0.03% is below the cap


def test_stress_preset_doubles_every_charge_and_none_is_free():
    for side in ("buy", "sell"):
        assert costs.preset("stress").leg(side, 250_000)["total"] == pytest.approx(2 * costs.DELIVERY.leg(side, 250_000)["total"])
        assert costs.preset("none").leg(side, 250_000)["total"] == 0.0


def test_cost_model_rejects_bad_input():
    with pytest.raises(ValueError):
        costs.DELIVERY.leg("short", 100)
    with pytest.raises(ValueError):
        costs.DELIVERY.leg("buy", -1)
    with pytest.raises(ValueError):
        costs.preset("nonsense")
    assert costs.preset("delivery", dp_charge_sell=0.0).leg("sell", 1000)["dp"] == 0.0


def test_financial_year_starts_in_april():
    assert costs.financial_year("2023-03-31") == 2022 and costs.financial_year("2023-04-01") == 2023


def trades_frame(rows):
    return pd.DataFrame(rows, columns=["exit_ts", "holding_days", "net_pnl"])


def test_tax_short_term_long_term_exemption_and_loss_offset():
    t = costs.estimate_capital_gains_tax(trades_frame([("2023-06-01", 30, 100_000.0), ("2023-07-01", 400, 225_000.0)]))
    fy = t["by_financial_year"]["FY2023-24"]
    assert fy["tax"] == pytest.approx(100_000 * 0.20 + (225_000 - 125_000) * 0.125)
    offset = costs.estimate_capital_gains_tax(trades_frame([("2023-06-01", 30, -50_000.0), ("2023-07-01", 400, 225_000.0)]))
    assert offset["total_tax"] == pytest.approx((175_000 - 125_000) * 0.125)               # the short-term loss reduced the long-term gain
    assert costs.estimate_capital_gains_tax(trades_frame([]))["total_tax"] == 0.0
    assert costs.estimate_capital_gains_tax(trades_frame([("2023-06-01", 10, -1000.0)]))["total_tax"] == 0.0


# ------------------------------------------------------------------------------------------- strategies
def test_cross_up_needs_a_real_previous_bar():
    a, b = pd.Series([np.nan, 2.0, 3.0, 1.0, 3.0]), pd.Series([1.0, 1.0, 1.0, 2.0, 2.0])
    assert cross_up(a, b).tolist() == [False, False, False, False, True]     # no cross is invented out of the warm-up NaN


# The test series is short (900 bars), so the 200-bar moving average of the default would never trade on it.
TEST_PARAMS = {"ma_trend_following": {"ma": 60, "band_pct": 0.5, "slope_bars": 10}}


def sigs(name, df):
    return generate_signals(name, df, TEST_PARAMS.get(name))


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_signals_are_causal_nothing_peeks_ahead_or_repaints(name):
    """The heart of Phase 9: signals computed on data cut off at bar t equal the full-data signals for bars up to t."""
    df = wavy()
    full = sigs(name, df)
    assert full.entry.sum() > 0 and full.exit.sum() > 0, f"{name} produced no signals on the test data, so this test would prove nothing"
    for cut in (260, 333, 480, 611, 777, 899):
        part = sigs(name, df.iloc[:cut + 1].copy())
        for field in ("entry", "exit"):
            got, want = getattr(part, field).to_numpy(), getattr(full, field).to_numpy()[:cut + 1]
            assert (got == want).all(), f"{name}.{field} changed at bars {np.flatnonzero(got != want)[:5]} when future data was removed (cut {cut})"
        np.testing.assert_allclose(part.stop.to_numpy(), full.stop.to_numpy()[:cut + 1], equal_nan=True, err_msg=f"{name} stop")


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_a_future_shock_does_not_change_earlier_signals(name):
    """Corrupt every bar after t wildly: nothing at or before t may move."""
    df = wavy()
    t = 500
    base = sigs(name, df)
    shocked = df.copy()
    shocked.loc[t + 1:, ["open", "high", "low", "close"]] *= 3.0
    after = sigs(name, shocked)
    assert (after.entry.to_numpy()[:t + 1] == base.entry.to_numpy()[:t + 1]).all()
    assert (after.exit.to_numpy()[:t + 1] == base.exit.to_numpy()[:t + 1]).all()


def test_pivot_strategy_waits_for_confirmation():
    """A swing low is only usable `pivot_bars` bars after it formed."""
    n, k = 60, 5
    low = np.full(n, 100.0)
    low[10], low[30] = 90.0, 95.0                                           # two swing lows, the second one higher: rising support
    df = pd.DataFrame({"ts": pd.bdate_range("2024-01-01", periods=n, tz="UTC"), "open": low + 5, "high": low + 6, "low": low,
                       "close": low + 5, "volume": 1.0})
    sig = generate_signals("pivot_trendline", df, {"pivot_bars": k})
    first_possible = 30 + k                                                 # the second pivot is confirmed at bar 35
    assert not sig.entry.iloc[:first_possible].any() and not sig.exit.iloc[:first_possible].any()


def test_vwap_strategy_never_trades_without_volume():
    df = wavy()
    df["volume"] = 0.0
    sig = generate_signals("vwap_trend", df)
    assert not sig.entry.any()


def test_parameters_are_validated():
    assert resolve_params("ema_crossover", {"fast": 10})["fast"] == 10
    with pytest.raises(ValueError):
        resolve_params("ema_crossover", {"bogus": 1})
    with pytest.raises(ValueError):
        resolve_params("ema_crossover", {"fast": 60, "slow": 50})
    with pytest.raises(ValueError):
        resolve_params("rsi_reversal", {"oversold": 80, "overbought": 70})
    with pytest.raises(ValueError):
        resolve_params("ema_crossover", {"fast": 2.5})
    with pytest.raises(ValueError):
        resolve_params("ema_crossover", {"slow": -5})
    with pytest.raises(ValueError):
        resolve_params("unknown", {})


# ------------------------------------------------------------------------------ engine: execution rules
def test_signal_at_the_close_is_filled_at_the_next_open_not_the_close():
    df = frame(o=[100, 100, 102, 104, 106, 108, 110, 112], c=[101, 101, 103, 105, 107, 109, 111, 113])
    res = run_backtest(df, signals(8, entry=[1], exit_from=4, stop=90.0), NO_COST)
    t = res.trades.iloc[0]
    assert t.entry_price == 102 and t.exit_price == 108                      # opens of bars 2 and 5, not the closes (101, 107)
    assert t.entry_ts == df.ts[2] and t.exit_ts == df.ts[5] and t.exit_reason == "signal"
    assert t.qty == 1_000_000 // 102 and t.net_pnl == pytest.approx(6 * t.qty)


def test_stop_fills_at_the_stop_but_a_gap_fills_at_the_open():
    base = dict(o=[100, 100, 100, 99, 99, 99], c=[100, 100, 100, 96, 96, 96])
    intrabar = frame(**base, l=[99.9, 99.9, 99.9, 94.0, 95, 95])           # bar 3 trades down through the stop of 95
    t = run_backtest(intrabar, signals(6, entry=[1], stop=95.0), NO_COST).trades.iloc[0]
    assert t.exit_price == 95.0 and t.exit_reason == "stop"
    gap = frame(o=[100, 100, 100, 92, 92, 92], c=[100, 100, 100, 91, 91, 91], l=[99.9, 99.9, 99.9, 90, 90, 90])
    g = run_backtest(gap, signals(6, entry=[1], stop=95.0), NO_COST).trades.iloc[0]
    assert g.exit_price == 92.0 and g.exit_reason == "gap_stop" and g.net_pnl < t.net_pnl     # the gap loses more than the stop


def test_when_stop_and_target_are_both_possible_the_stop_wins():
    df = frame(o=[100, 100, 100, 100, 100], h=[100.1, 100.1, 100.1, 107, 100.1], l=[99.9, 99.9, 99.9, 94, 99.9], c=[100] * 5)
    t = run_backtest(df, signals(5, entry=[1], stop=95.0, target_r=1.0), NO_COST).trades.iloc[0]   # target would be 105
    assert t.exit_reason == "stop" and t.exit_price == 95.0


def test_target_fills_at_the_target_or_at_the_open_if_it_gapped_through():
    hit = frame(o=[100, 100, 100, 100, 100], h=[100.1, 100.1, 100.1, 106, 100.1], l=[99.9, 99.9, 99.9, 99.5, 99.9], c=[100] * 5)
    t = run_backtest(hit, signals(5, entry=[1], stop=95.0, target_r=1.0), NO_COST).trades.iloc[0]
    assert t.exit_reason == "target" and t.exit_price == 105.0
    gap = frame(o=[100, 100, 100, 108, 108], h=[100.1, 100.1, 100.1, 109, 109], l=[99.9, 99.9, 99.9, 107, 107], c=[100, 100, 100, 108, 108])
    g = run_backtest(gap, signals(5, entry=[1], stop=95.0, target_r=1.0), NO_COST).trades.iloc[0]
    assert g.exit_reason == "target" and g.exit_price == 108.0


def test_entry_is_skipped_when_the_open_is_already_past_the_stop():
    df = frame(o=[100, 100, 97, 97, 97], c=[100, 100, 97, 97, 97])
    res = run_backtest(df, signals(5, entry=[1], stop=98.0), NO_COST)
    assert res.trades.empty and res.signal_log[0]["action"] == "skipped_gap_through_stop"


def test_signal_without_a_stop_price_is_not_traded():
    res = run_backtest(frame(o=[100] * 5), signals(5, entry=[1]), NO_COST)
    assert res.trades.empty and res.signal_log[0]["action"] == "skipped_no_stop"


def test_costs_and_slippage_are_charged_on_both_sides():
    df = frame(o=[100, 100, 100, 100, 100, 110], c=[100, 100, 100, 100, 100, 110])
    cfg = BacktestConfig(cost_model=costs.DELIVERY, slippage_bps=100.0)                # 1% slippage
    t = run_backtest(df, signals(6, entry=[1], exit_from=3, stop=90.0), cfg).trades.iloc[0]
    assert t.entry_price == pytest.approx(101.0) and t.exit_price == pytest.approx(99.0)
    expected = costs.DELIVERY.leg("buy", t.qty * t.entry_price)["total"] + costs.DELIVERY.leg("sell", t.qty * t.exit_price)["total"]
    assert t.costs == pytest.approx(expected) and t.net_pnl == pytest.approx(t.gross_pnl - t.costs)
    assert t.net_pnl < 0                                                                # flat prices: only friction, so a loss


def test_whole_shares_and_cash_never_go_negative():
    df = frame(o=[3000.0] * 6)
    t = run_backtest(df, signals(6, entry=[1], exit_from=3, stop=2000.0), BacktestConfig(initial_capital=10_000, cost_model=costs.preset("none"), slippage_bps=0)).trades.iloc[0]
    assert t.qty == 3
    wavy_df = wavy()
    cfg = BacktestConfig(initial_capital=50_000.0, cost_model=costs.DELIVERY)
    res = run_backtest(wavy_df, generate_signals("ema_crossover", wavy_df), cfg)
    assert (res.equity["equity"] > 0).all() and not res.trades.empty


def test_risk_sizing_caps_the_loss_at_the_stop():
    df = frame(o=[100.0] * 6)
    cfg = BacktestConfig(risk_pct=1.0, cost_model=costs.preset("none"), slippage_bps=0)
    t = run_backtest(df, signals(6, entry=[1], exit_from=3, stop=90.0), cfg).trades.iloc[0]
    assert t.qty == 1000 and t.initial_risk == pytest.approx(10_000)                       # 1% of 1,000,000 at Rs 10 a share


def test_open_position_is_closed_at_the_end_of_the_range():
    df = frame(o=[100, 100, 100, 101, 102], c=[100, 100, 100, 101, 105])
    t = run_backtest(df, signals(5, entry=[1], stop=90.0), NO_COST).trades.iloc[0]
    assert t.exit_reason == "end_of_data" and t.exit_price == 105 and t.exit_ts == df.ts.iloc[-1]


def test_date_range_only_trades_inside_the_range():
    df = wavy()
    sig = generate_signals("ema_crossover", df)
    start, end = df.ts[300], df.ts[700]
    res = run_backtest(df, sig, BacktestConfig(start=start, end=end, cost_model=costs.preset("none")))
    assert not res.trades.empty
    assert (res.trades.entry_ts >= start).all() and (res.trades.exit_ts <= end).all()
    assert res.equity.ts.iloc[0] == start and res.equity.ts.iloc[-1] == end


def test_a_backtest_of_the_first_part_matches_the_start_of_the_full_backtest():
    """No look-ahead, end to end: cutting the data off later must not change trades that had already closed."""
    df = wavy()
    cfg = BacktestConfig(cost_model=costs.DELIVERY)
    for name in sorted(STRATEGIES):
        full = run_backtest(df, sigs(name, df), cfg).trades
        part_df = df.iloc[:600].copy()
        part = run_backtest(part_df, sigs(name, part_df), cfg).trades
        part = part[part.exit_reason != "end_of_data"]
        merged = full.head(len(part))
        assert len(part) > 0, name
        cols = ["entry_ts", "exit_ts", "entry_price", "exit_price", "qty", "net_pnl", "exit_reason"]
        pd.testing.assert_frame_equal(part[cols].reset_index(drop=True), merged[cols].reset_index(drop=True), obj=name)


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_equity_reconciles_with_the_trades(name):
    df = wavy()
    cfg = BacktestConfig(cost_model=costs.DELIVERY)
    res = run_backtest(df, sigs(name, df), cfg)
    assert res.equity["equity"].iloc[-1] == pytest.approx(cfg.initial_capital + res.trades["net_pnl"].sum(), rel=1e-9)
    assert not res.equity["in_position"].iloc[-1]
    assert (res.trades["costs"] > 0).all() and (res.trades["exit_ts"] >= res.trades["entry_ts"]).all()


def test_intraday_runs_are_flat_at_the_end_of_every_day():
    ts = [pd.Timestamp("2024-01-02 03:45", tz="UTC") + pd.Timedelta(minutes=5 * i) for i in range(6)] + \
         [pd.Timestamp("2024-01-03 03:45", tz="UTC") + pd.Timedelta(minutes=5 * i) for i in range(6)]
    px = [100.0 + i for i in range(12)]
    df = pd.DataFrame({"ts": ts, "open": px, "high": [p + 0.2 for p in px], "low": [p - 0.2 for p in px], "close": px, "volume": 1.0})
    cfg = BacktestConfig(cost_model=costs.preset("none"), slippage_bps=0, force_eod=True)
    res = run_backtest(df, signals(12, entry=[1], stop=50.0), cfg)
    t = res.trades.iloc[0]
    assert t.exit_reason == "forced_eod" and t.exit_ts == ts[5]                          # closed at the open of day one's last bar
    late = run_backtest(df, signals(12, entry=[4], stop=50.0), cfg)                       # would fill on the day's last bar
    assert late.trades.empty and late.signal_log[0]["action"] == "skipped_end_of_day"


def test_buy_and_hold_benchmark():
    df = frame(o=[100, 100, 110, 120, 130], c=[100, 100, 110, 120, 140])
    bh = buy_and_hold(df, NO_COST)
    t = bh.trades.iloc[0]
    assert len(bh.trades) == 1 and t.entry_price == 100 and t.exit_price == 140 and t.exit_reason == "end_of_data"
    costly = buy_and_hold(df, BacktestConfig(cost_model=costs.DELIVERY, slippage_bps=10)).trades.iloc[0]
    assert costly.net_pnl < t.net_pnl


# ----------------------------------------------------------------------------------------------- metrics
def hand_trades():
    pnl = [100.0, -50.0, 200.0, -50.0, -50.0]
    return pd.DataFrame({"net_pnl": pnl, "r_multiple": [2.0, -1.0, 4.0, -1.0, -1.0], "costs": 1.0, "holding_days": 5,
                         "exit_reason": ["signal", "stop", "target", "stop", "stop"]})


def hand_equity(values):
    return pd.DataFrame({"ts": pd.bdate_range("2024-01-01", periods=len(values), tz="UTC"), "equity": values,
                         "in_position": [True] * len(values)})


def test_metrics_match_hand_calculation():
    m = metrics.compute_metrics(hand_trades(), hand_equity([1000, 1100, 1050, 1250, 1200, 1150]), 1000.0)
    assert m["total_trades"] == 5 and m["winners"] == 2 and m["losers"] == 3 and m["win_rate_pct"] == 40.0
    assert m["net_pnl"] == 150.0 and m["profit_factor"] == 2.0                           # 300 won, 150 lost
    assert m["payoff_ratio"] == 3.0                                                        # avg win 150 / avg loss 50
    assert m["avg_risk_reward"] == 3.0 and m["avg_r"] == pytest.approx(0.6) and m["max_consecutive_losses"] == 2
    assert m["max_drawdown_pct"] == pytest.approx((1150 / 1250 - 1) * 100, abs=1e-3)
    assert m["total_return_pct"] == 15.0 and m["exposure_pct"] == 100.0


def test_metrics_handle_no_trades_and_no_losers():
    empty = metrics.compute_metrics(hand_trades().iloc[0:0], hand_equity([1000, 1000, 1000]), 1000.0)
    assert empty["total_trades"] == 0 and empty["profit_factor"] is None and empty["win_rate_pct"] is None
    only_wins = hand_trades().iloc[[0, 2]]
    assert metrics.compute_metrics(only_wins, hand_equity([1000, 1100, 1300]), 1000.0)["profit_factor"] is None   # infinite, so None
    assert metrics.compute_metrics(hand_trades(), hand_equity([]), 1000.0)["total_trades"] == 0


def test_max_drawdown_function_and_curve_downsampling():
    dd, bars = metrics.max_drawdown(pd.Series([100, 120, 90, 95, 130, 100]))
    assert dd == pytest.approx(-25.0) and bars == 2 and metrics.max_drawdown(pd.Series(dtype=float)) == (0.0, 0)
    curve = metrics.downsample_curve(hand_equity(list(np.linspace(100, 200, 5000))), max_points=300)
    assert len(curve) <= 300 and curve[-1]["equity"] == 200.0 and curve[0]["equity"] == 100.0


# ------------------------------------------------------------------------------- database and runner
@pytest.fixture()
def db():
    client = get_client()
    name = f"BharatMarketTest_{uuid.uuid4().hex[:8]}"
    assert name.startswith("BharatMarketTest_")
    database = client[name]
    setup_database(database)
    yield database
    client.drop_database(name)


def put_prices(db, symbol, df: pd.DataFrame, volume=True):
    db["adjusted_prices"].insert_many([{
        "symbol": symbol, "exchange": "NSE", "interval": "1d", "date": r.ts.to_pydatetime(), "open": float(r.open), "high": float(r.high),
        "low": float(r.low), "close": float(r.close), "adj_factor": 1.0, "source": "yfinance", "method": "provider_adjusted",
        "processed_at": datetime(2024, 1, 1, tzinfo=UTC), **({"volume": int(r.volume)} if volume else {})} for r in df.itertuples()])


def has_bad_numbers(x) -> bool:
    if isinstance(x, dict):
        return any(has_bad_numbers(v) for v in x.values())
    if isinstance(x, list):
        return any(has_bad_numbers(v) for v in x)
    return isinstance(x, float) and not math.isfinite(x)


def test_backtest_is_stored_in_a_valid_document_with_its_signals(db):
    put_prices(db, "RELIANCE", wavy())
    d = runner.backtest(db, "RELIANCE", "ema_crossover")
    stored = db["backtest_results"].find_one({"run_id": d["run_id"]})
    assert stored and stored["strategy"] == "ema_crossover" and stored["symbol"] == "RELIANCE"
    assert not has_bad_numbers(stored)
    for key in ("metrics", "benchmark", "periods", "costs", "equity_curve", "trades", "warnings", "params"):
        assert key in stored
    assert stored["periods"]["in_sample"] and stored["periods"]["out_of_sample"]
    assert any("survivorship" in w for w in stored["warnings"])
    assert stored["costs"]["model"]["name"] == "delivery" and stored["costs"]["slippage_bps"] == 5.0
    assert stored["equity_curve"][0]["benchmark_equity"] > 0
    sigs = list(db["trading_signals"].find({"run_id": d["run_id"]}))
    assert sigs and {s["signal"] for s in sigs} <= {"buy", "exit_long"} and all(s["price"] > 0 for s in sigs)
    assert stored["metrics"]["total_trades"] == len(stored["trades"]) == stored["trades_total"]
    assert "tax_estimate" in stored


def test_backtest_respects_dates_params_and_cost_choices(db):
    put_prices(db, "RELIANCE", wavy())
    cfg = runner.default_config("1d", start="2017-01-01", end="2018-06-30", cost_preset="stress")
    d = runner.backtest(db, "RELIANCE", "ema_crossover", params={"fast": 10, "slow": 30}, cfg=cfg, store=False)
    assert d["params"]["fast"] == 10 and d["costs"]["model"]["name"] == "stress"
    assert d["start"].year == 2017 and d["end"] <= datetime(2018, 6, 30, tzinfo=UTC)
    assert db["backtest_results"].count_documents({}) == 0                               # store=False writes nothing


def test_unknown_or_short_series_is_an_error_not_a_silent_empty_result(db):
    with pytest.raises(ValueError):
        runner.backtest(db, "NOPE", "ema_crossover")
    put_prices(db, "RELIANCE", wavy(30))
    with pytest.raises(ValueError):
        runner.backtest(db, "RELIANCE", "ema_crossover")


def test_index_results_are_labelled_as_a_proxy_and_vwap_warns_without_volume(db):
    put_prices(db, "NIFTY_50", wavy(), volume=False)
    d = runner.backtest(db, "NIFTY_50", "vwap_trend", store=False)
    text = " ".join(d["warnings"])
    assert "INDEX" in text and "needs traded volume" in text and d["metrics"]["total_trades"] == 0


def test_compare_replaces_its_earlier_batch_and_summarises(db):
    put_prices(db, "RELIANCE", wavy(seed=1))
    put_prices(db, "TCS", wavy(seed=2))
    docs = runner.compare(db, ["RELIANCE", "TCS"], ["ema_crossover", "rsi_reversal"])
    assert len(docs) == 4 and db["backtest_results"].count_documents({"batch": "compare"}) == 4
    runner.compare(db, ["RELIANCE", "TCS"], ["ema_crossover", "rsi_reversal"])
    assert db["backtest_results"].count_documents({"batch": "compare"}) == 4             # replaced, not doubled
    run_ids = {d["run_id"] for d in db["backtest_results"].find({}, {"run_id": 1})}
    assert {s["run_id"] for s in db["trading_signals"].find({}, {"run_id": 1})} <= run_ids   # no orphaned signals from the replaced runs
    table = runner.summarise(docs)
    assert set(table.symbol) == {"RELIANCE", "TCS"} and {"return_pct", "buy_hold_pct", "oos_return_pct"} <= set(table.columns)


def test_sweep_skips_invalid_combinations_and_reports_out_of_sample(db):
    put_prices(db, "RELIANCE", wavy())
    out = runner.sweep(db, "RELIANCE", "ema_crossover", {"fast": [5, 10, 20, 60], "slow": [30, 50, 100]})
    valid = [(f, s) for f in (5, 10, 20, 60) for s in (30, 50, 100) if f < s]
    assert out["combinations"] == len(valid)
    t = out["table"]
    assert (t["p_fast"] < t["p_slow"]).all() and {"is_objective", "oos_objective", "oos_return_pct"} <= set(t.columns)
    enough = ((t["is_trades"] >= runner.MIN_TRADES_FOR_RANKING) & t["is_objective"].notna()).sum() >= 3
    assert ("rank_correlation" in out) == enough                                         # only ranked when there are enough trades to rank
    assert out["buy_hold_oos_return_pct"] is not None and out["split_date"]


def test_tradable_universe_excludes_indices_and_short_histories(db):
    put_prices(db, "RELIANCE", wavy(800))
    put_prices(db, "TCS", wavy(100))
    put_prices(db, "NIFTY_50", wavy(800), volume=False)
    assert runner.tradable_universe(db) == ["RELIANCE"]


def test_list_and_get_runs(db):
    put_prices(db, "RELIANCE", wavy())
    d = runner.backtest(db, "RELIANCE", "breakout_retest")
    rows = runner.list_runs(db, symbol="RELIANCE")
    assert [r["run_id"] for r in rows] == [d["run_id"]] and "trades" not in rows[0]
    assert runner.get_run(db, d["run_id"])["strategy"] == "breakout_retest" and runner.get_run(db, "nope") is None
