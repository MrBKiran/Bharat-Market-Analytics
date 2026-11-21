"""The backtest engine: turns signals into trades, one bar at a time, with realistic execution.

Execution rules (every one of them exists to keep the result honest):

  1. A signal is known at the CLOSE of bar t and is filled at the OPEN of bar t+1 (never at the close it was
     computed from: that would use a price you could not have traded).
  2. Every fill is moved against you by `slippage_bps` (buys higher, sells lower), and every order pays the
     itemised costs of the `CostModel`.
  3. A protective stop is set when the trade is entered (the strategy proposes it) and never moved. It is
     checked against each bar's low. If the bar OPENS beyond the stop (a gap) the fill is the open, not the
     stop price: gaps hurt, and the backtest says so. A take-profit target (optional, `target_r` times the
     initial risk) fills at the target, or at the open if the bar gapped through it.
  4. If a bar could have hit both the stop and the target, the STOP wins (we cannot know the order inside a bar).
  5. If the next bar opens at or beyond the proposed stop, the setup is dead and the entry is skipped.
  6. One position at a time, long only, whole shares, sized from the cash actually available.
  7. A position still open at the end of the tested range is closed at the last bar's close ("end_of_data").
  8. Intraday runs (`force_eod`): no position is carried overnight; it is closed at the open of the last bar of
     the day, and no entry may be filled on that bar.

Not modelled (state it when you quote a result): partial fills, order-book impact (fine for large caps, wrong
for illiquid stocks), circuit limits and trading halts, dividends (prices are split-adjusted only), and the
survivorship bias of the stock list.
"""
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from app.analytics.indicators import IST
from app.backtesting.costs import CostModel, preset
from app.backtesting.strategies import Signals


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 1_000_000.0
    position_pct: float = 100.0              # % of cash committed to a trade
    risk_pct: Optional[float] = None         # also cap the loss at the stop to this % of equity (None = off)
    slippage_bps: float = 5.0                # adverse price move per fill, in basis points
    cost_model: CostModel = field(default_factory=lambda: preset("delivery"))
    start: Optional[pd.Timestamp] = None     # first bar traded (earlier bars only warm the indicators up)
    end: Optional[pd.Timestamp] = None       # last bar traded
    force_eod: bool = False                  # intraday: flat at the end of every day
    max_hold_bars: Optional[int] = None      # time stop

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = None if self.start is None else pd.Timestamp(self.start).isoformat()
        d["end"] = None if self.end is None else pd.Timestamp(self.end).isoformat()
        return d


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.DataFrame                     # ts, equity, in_position
    signal_log: list
    config: BacktestConfig


TRADE_COLUMNS = ["entry_ts", "exit_ts", "entry_price", "exit_price", "qty", "gross_pnl", "costs", "net_pnl",
                 "return_pct", "holding_bars", "holding_days", "stop", "target", "initial_risk", "r_multiple",
                 "mae_pct", "mfe_pct", "exit_reason"]


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _bounds(ts: pd.Series, cfg: BacktestConfig) -> tuple[int, int]:
    """Index range [lo, hi] of the bars that may be traded."""
    mask = np.ones(len(ts), bool)
    if cfg.start is not None:
        mask &= (ts >= _utc(cfg.start)).to_numpy()
    if cfg.end is not None:
        mask &= (ts <= _utc(cfg.end)).to_numpy()
    where = np.flatnonzero(mask)
    return (int(where[0]), int(where[-1])) if len(where) else (0, -1)


def _buy_quantity(cfg: BacktestConfig, cash: float, fill: float, stop: float) -> int:
    qty = int((cash * cfg.position_pct / 100) // fill)
    if cfg.risk_pct is not None and fill > stop:
        qty = min(qty, int((cash * cfg.risk_pct / 100) // (fill - stop)))
    if qty <= 0:
        return 0
    rate = cfg.cost_model.leg("buy", 1_000_000.0)["total"] / 1_000_000.0        # costs per rupee, for a first guess
    qty = min(qty, int(cash / (fill * (1 + rate * 1.05))))
    while qty > 0 and qty * fill + cfg.cost_model.leg("buy", qty * fill)["total"] > cash:
        qty -= 1
    return max(qty, 0)


def run_backtest(df: pd.DataFrame, signals: Signals, cfg: Optional[BacktestConfig] = None) -> BacktestResult:
    """Simulate one strategy on one price frame. `df` needs ts, open, high, low, close (reset index)."""
    cfg = cfg or BacktestConfig()
    df = df.reset_index(drop=True)
    n = len(df)
    ts = pd.to_datetime(df["ts"], utc=True)
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    entry_sig = signals.entry.reset_index(drop=True).to_numpy(bool)
    exit_sig = signals.exit.reset_index(drop=True).to_numpy(bool)
    stop_sig = signals.stop.reset_index(drop=True).to_numpy(float)
    slip, cost = cfg.slippage_bps / 10_000.0, cfg.cost_model
    lo, hi = _bounds(ts, cfg)

    day = ts.dt.tz_convert(IST).dt.date.to_numpy()
    last_of_day = np.zeros(n, bool)
    if n:
        last_of_day[:-1] = day[:-1] != day[1:]
        last_of_day[-1] = True

    cash = float(cfg.initial_capital)
    in_pos = False
    qty, entry_fill, entry_cost, stop, target, entry_i = 0, 0.0, 0.0, np.nan, np.nan, -1
    low_since, high_since = np.inf, -np.inf
    pending_entry, pending_stop, pending_exit, pending_log = False, np.nan, False, None
    trades, signal_log = [], []
    equity = np.full(max(hi - lo + 1, 0), np.nan)
    held = np.zeros(len(equity), bool)

    def close_position(i: int, price: float, reason: str) -> None:
        nonlocal cash, in_pos, qty, low_since, high_since
        proceeds = qty * price
        exit_cost = cost.leg("sell", proceeds)["total"]
        cash += proceeds - exit_cost
        gross = (price - entry_fill) * qty
        costs = entry_cost + exit_cost
        risk = (entry_fill - stop) * qty
        net = gross - costs
        trades.append({
            "entry_ts": ts.iloc[entry_i], "exit_ts": ts.iloc[i], "entry_price": entry_fill, "exit_price": price,
            "qty": qty, "gross_pnl": gross, "costs": costs, "net_pnl": net,
            "return_pct": net / (entry_fill * qty + entry_cost) * 100, "holding_bars": i - entry_i,
            "holding_days": (ts.iloc[i] - ts.iloc[entry_i]).days, "stop": stop, "target": target,
            "initial_risk": risk, "r_multiple": net / risk if risk > 0 else np.nan,
            "mae_pct": (low_since / entry_fill - 1) * 100, "mfe_pct": (high_since / entry_fill - 1) * 100,
            "exit_reason": reason})
        in_pos, qty, low_since, high_since = False, 0, np.inf, -np.inf

    for i in range(lo, hi + 1):
        # 1) orders decided at the previous close are filled at this bar's open
        if pending_exit and in_pos:
            close_position(i, o[i] * (1 - slip), "signal")
        pending_exit = False
        if pending_entry and not in_pos:
            fill = o[i] * (1 + slip)
            planned_stop = pending_stop
            if last_of_day[i] and cfg.force_eod:
                pending_log["action"] = "skipped_end_of_day"
            elif not planned_stop < o[i]:
                pending_log["action"] = "skipped_gap_through_stop"
            else:
                q = _buy_quantity(cfg, cash, fill, planned_stop)
                if q < 1:
                    pending_log["action"] = "skipped_too_small"
                else:
                    buy_cost = cost.leg("buy", q * fill)["total"]
                    cash -= q * fill + buy_cost
                    in_pos, qty, entry_fill, entry_cost, entry_i = True, q, fill, buy_cost, i
                    stop = planned_stop
                    risk_per_share = fill - stop
                    target = fill + signals.target_r * risk_per_share if signals.target_r else np.nan
                    low_since, high_since = fill, fill
                    pending_log["action"] = "executed"
                    pending_log["fill_price"] = fill
        if pending_log is not None:
            signal_log.append(pending_log)
        pending_entry, pending_log = False, None

        # 2) manage the open position inside this bar
        if in_pos:
            if cfg.force_eod and last_of_day[i] and entry_i != i:
                close_position(i, o[i] * (1 - slip), "forced_eod")
            else:
                low_since, high_since = min(low_since, l[i]), max(high_since, h[i])
                if o[i] <= stop:
                    close_position(i, o[i] * (1 - slip), "gap_stop")
                elif l[i] <= stop:
                    close_position(i, stop * (1 - slip), "stop")
                elif target == target and o[i] >= target:
                    close_position(i, o[i], "target")
                elif target == target and h[i] >= target:
                    close_position(i, target, "target")
                elif cfg.max_hold_bars is not None and i - entry_i >= cfg.max_hold_bars:
                    close_position(i, c[i] * (1 - slip), "time_stop")

        # 3) the close: mark to market, then read this bar's signals (they act at the next open)
        if i == hi and in_pos:
            close_position(i, c[i] * (1 - slip), "end_of_data")
        equity[i - lo] = cash + (qty * c[i] if in_pos else 0.0)
        held[i - lo] = in_pos
        if i == hi:
            break
        if in_pos:
            pending_exit = bool(exit_sig[i])
            if pending_exit:
                signal_log.append({"ts": ts.iloc[i], "side": "sell", "price": c[i], "action": "executed"})
        elif entry_sig[i]:
            pending_entry, pending_stop = True, stop_sig[i]
            pending_log = {"ts": ts.iloc[i], "side": "buy", "price": c[i], "stop": float(stop_sig[i]),
                           "action": "pending"}
            if stop_sig[i] != stop_sig[i]:
                pending_entry, pending_log["action"] = False, "skipped_no_stop"
        if entry_sig[i] and in_pos:
            signal_log.append({"ts": ts.iloc[i], "side": "buy", "price": c[i], "stop": float(stop_sig[i]),
                               "action": "ignored_already_in_position"})

    trades_df = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    equity_df = pd.DataFrame({"ts": ts.iloc[lo:hi + 1].to_numpy() if hi >= lo else [], "equity": equity,
                              "in_position": held})
    if len(equity_df):
        equity_df["ts"] = pd.to_datetime(equity_df["ts"], utc=True)
    return BacktestResult(trades=trades_df, equity=equity_df, signal_log=signal_log, config=cfg)


def buy_and_hold(df: pd.DataFrame, cfg: Optional[BacktestConfig] = None) -> BacktestResult:
    """The benchmark every strategy must beat: buy at the first tradable open, hold, sell at the last close.

    Same capital, same slippage and the same costs (including the sale at the end), so the comparison is fair.
    """
    cfg = cfg or BacktestConfig()
    n = len(df)
    flat = pd.Series(False, index=range(n))
    entry = flat.copy()
    lo, hi = _bounds(pd.to_datetime(df["ts"], utc=True).reset_index(drop=True), cfg)
    if hi - lo < 1:
        return run_backtest(df, Signals(entry=flat, exit=flat, stop=pd.Series(np.nan, index=range(n))), cfg)
    entry.iloc[lo] = True                                  # signal on the first bar, fill at the second bar's open
    stop = pd.Series(np.nan, index=range(n))
    stop.iloc[lo] = 0.01                                   # an effectively unreachable stop: we never sell
    one = BacktestConfig(**{**cfg.__dict__, "risk_pct": None, "max_hold_bars": None, "force_eod": False})
    return run_backtest(df, Signals(entry=entry, exit=flat, stop=stop), one)
