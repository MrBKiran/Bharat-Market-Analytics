"""Performance metrics for one backtest, computed from its trades and its equity curve.

Definitions (all in the currency of the backtest, normally rupees):
  net_pnl          sum of trade profits AFTER costs and slippage
  win_rate         % of trades with net_pnl > 0
  profit_factor    gross profit / gross loss (None when there is no losing trade: it would be infinite)
  max_drawdown     the largest peak-to-trough fall of the equity curve, in %
  avg_r            average R multiple: each trade's net P&L divided by the money risked at its stop
  payoff_ratio     average winning trade / average losing trade (the realised reward-to-risk ratio)
  sharpe           mean / standard deviation of per-bar equity returns, annualised, risk-free rate 0
                   (an approximation: bars where nothing is held count as zero returns)
  cagr             compound annual growth of the equity curve
  exposure         % of bars with a position open
"""
import math
from typing import Optional

import numpy as np
import pandas as pd

from app.analytics.indicators import PERIODS_PER_YEAR

MAX_CURVE_POINTS = 800


def _num(x) -> Optional[float]:
    """JSON/BSON-safe number: NaN and infinity become None."""
    if x is None:
        return None
    x = float(x)
    return round(x, 4) if math.isfinite(x) else None


def max_drawdown(equity: pd.Series) -> tuple[float, int]:
    """(largest fall from a previous peak in %, longest stretch of bars below a peak)."""
    if equity.empty:
        return 0.0, 0
    peak = equity.cummax()
    dd = (equity / peak - 1) * 100
    below = (equity < peak).to_numpy()
    longest = run = 0
    for b in below:
        run = run + 1 if b else 0
        longest = max(longest, run)
    return float(dd.min()), int(longest)


def downsample_curve(equity: pd.DataFrame, max_points: int = MAX_CURVE_POINTS) -> list[dict]:
    """The equity curve (and its drawdown) thinned to at most `max_points` points for storage and charts."""
    if equity.empty:
        return []
    peak = equity["equity"].cummax()
    drawdown = (equity["equity"] / peak - 1) * 100
    idx = np.unique(np.linspace(0, len(equity) - 1, min(max_points, len(equity))).astype(int))
    return [{"ts": equity["ts"].iloc[i].to_pydatetime(), "equity": round(float(equity["equity"].iloc[i]), 2),
             "drawdown_pct": round(float(drawdown.iloc[i]), 3)} for i in idx]


def compute_metrics(trades: pd.DataFrame, equity: pd.DataFrame, initial_capital: float, interval: str = "1d") -> dict:
    """The metric set stored with every backtest."""
    out: dict = {"initial_capital": initial_capital}
    if equity.empty:
        return {**out, "total_trades": 0, "note": "no bars in the tested range"}
    eq = equity["equity"]
    final = float(eq.iloc[-1])
    years = max((equity["ts"].iloc[-1] - equity["ts"].iloc[0]).days / 365.25, 1e-9)
    dd, dd_bars = max_drawdown(eq)
    per_year = PERIODS_PER_YEAR.get(interval, 252)
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std(ddof=1) * math.sqrt(per_year)) if len(rets) > 2 and rets.std(ddof=1) > 0 else None
    total_return = (final / initial_capital - 1) * 100
    cagr = ((final / initial_capital) ** (1 / years) - 1) * 100 if final > 0 and years >= 0.25 else None
    out.update({"final_equity": _num(final), "total_return_pct": _num(total_return), "cagr_pct": _num(cagr),
                "max_drawdown_pct": _num(dd), "max_drawdown_bars": dd_bars, "sharpe": _num(sharpe),
                "calmar": _num(cagr / abs(dd)) if cagr is not None and dd < 0 else None,
                "exposure_pct": _num(equity["in_position"].mean() * 100), "years": _num(years)})

    n = len(trades)
    out["total_trades"] = n
    if n == 0:
        return {**out, "winners": 0, "losers": 0, "net_pnl": 0.0, "win_rate_pct": None, "profit_factor": None}
    pnl = trades["net_pnl"]
    wins, losses = trades[pnl > 0], trades[pnl <= 0]
    gross_profit, gross_loss = float(wins["net_pnl"].sum()), float(-losses["net_pnl"].sum())
    losing_streak = streak = 0
    for v in pnl:
        streak = streak + 1 if v <= 0 else 0
        losing_streak = max(losing_streak, streak)
    avg_win = float(wins["net_pnl"].mean()) if len(wins) else None
    avg_loss = float(losses["net_pnl"].mean()) if len(losses) else None
    r = trades["r_multiple"].replace([np.inf, -np.inf], np.nan).dropna()
    r_wins, r_losses = r[r > 0], r[r <= 0]
    out.update({
        "winners": int(len(wins)), "losers": int(len(losses)), "win_rate_pct": _num(len(wins) / n * 100),
        "net_pnl": _num(pnl.sum()), "gross_profit": _num(gross_profit), "gross_loss": _num(-gross_loss),
        "profit_factor": _num(gross_profit / gross_loss) if gross_loss > 0 else None,
        "avg_trade": _num(pnl.mean()), "avg_win": _num(avg_win), "avg_loss": _num(avg_loss),
        "best_trade": _num(pnl.max()), "worst_trade": _num(pnl.min()),
        "payoff_ratio": _num(avg_win / abs(avg_loss)) if avg_win is not None and avg_loss not in (None, 0) else None,
        "avg_r": _num(r.mean()) if len(r) else None,
        "avg_r_win": _num(r_wins.mean()) if len(r_wins) else None,
        "avg_r_loss": _num(r_losses.mean()) if len(r_losses) else None,
        "avg_risk_reward": _num(r_wins.mean() / abs(r_losses.mean())) if len(r_wins) and len(r_losses) and r_losses.mean() != 0 else None,
        "expectancy": _num(pnl.mean()),
        "max_consecutive_losses": int(losing_streak),
        "avg_holding_days": _num(trades["holding_days"].mean()),
        "total_costs": _num(trades["costs"].sum()),
        "costs_pct_of_gross_profit": _num(trades["costs"].sum() / gross_profit * 100) if gross_profit > 0 else None,
        "exit_reasons": {k: int(v) for k, v in trades["exit_reason"].value_counts().items()},
    })
    return out
