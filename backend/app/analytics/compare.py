"""Compare several instruments over the same dates (pure computation, no database).

Input: a DataFrame of closing prices, one column per symbol, indexed by date. Only dates on which EVERY symbol has a
close are used, so the comparison covers one common period and no series is flattered by a longer or luckier history.

Output per symbol: total return, annualised return (CAGR), annualised volatility, deepest drawdown, best and worst day.
Also a rebased-to-100 price series for charting and the correlation matrix of daily returns.
"""
import math
from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 252
MAX_SERIES_POINTS = 500
MIN_COMMON_BARS = 20


def _num(x, digits: int = 4) -> Optional[float]:
    x = float(x)
    return round(x, digits) if math.isfinite(x) else None


def compare_closes(closes: pd.DataFrame) -> dict:
    """Statistics, correlation and a rebased series for the columns of `closes`."""
    common = closes.dropna(how="any").sort_index()
    if len(common) < MIN_COMMON_BARS:
        raise ValueError(f"only {len(common)} dates have a close for every symbol (need at least {MIN_COMMON_BARS})")
    returns = common.pct_change().dropna()
    years = (common.index[-1] - common.index[0]).days / 365.25
    stats = {}
    for sym in common.columns:
        s, r = common[sym], returns[sym]
        drawdown = (s / s.cummax() - 1) * 100
        total = (s.iloc[-1] / s.iloc[0] - 1) * 100
        stats[sym] = {
            "first_close": _num(s.iloc[0]), "last_close": _num(s.iloc[-1]), "total_return_pct": _num(total),
            "cagr_pct": _num(((s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1) * 100) if years >= 0.25 else None,
            "volatility_pct": _num(np.log(s / s.shift(1)).std(ddof=1) * math.sqrt(TRADING_DAYS) * 100),
            "max_drawdown_pct": _num(drawdown.min()),
            "best_day_pct": _num(r.max() * 100), "worst_day_pct": _num(r.min() * 100),
        }
    corr = returns.corr() if len(common.columns) > 1 else pd.DataFrame()
    rebased = common / common.iloc[0] * 100
    keep = np.unique(np.linspace(0, len(rebased) - 1, min(MAX_SERIES_POINTS, len(rebased))).astype(int))
    series = [{"ts": rebased.index[i].to_pydatetime(), **{c: _num(rebased.iloc[i][c], 3) for c in rebased.columns}} for i in keep]
    return {
        "period": {"start": common.index[0].to_pydatetime(), "end": common.index[-1].to_pydatetime(), "bars": len(common),
                   "years": _num(years, 2)},
        "stats": stats,
        "correlation": {a: {b: _num(corr.loc[a, b], 3) for b in corr.columns} for a in corr.index} if not corr.empty else {},
        "rebased_series": series,
        "note": "only dates on which every symbol has a close are used; prices are split-adjusted, not dividend-adjusted",
    }
