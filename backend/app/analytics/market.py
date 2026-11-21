"""Market analytics over the processed daily dataset (`adjusted_prices`).

Each analysis is a plain function over a `Panel` (dates x symbols tables of close and volume), so it can
be tested with small made-up data. `run_analytics` computes everything and stores one document per
analysis in `analytics_results`.

Two snapshots are computed, because the data does not end on the same day for every stock:
  latest          the newest date (only the stocks with current data, today 10 stocks, take part)
  full_universe   the latest date on which at least 90% of all stocks have a price (about 49 stocks)
Instruments whose last price is more than 10 days before the snapshot date are excluded (and listed).

Not investment advice: "bullish" / "bearish" are simple rule-based labels, not predictions.
Sector and breadth figures use today's stock list for all history (survivorship bias).
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd
from pymongo.database import Database

from app.analytics import indicators as ind

STALE_DAYS = 10
TREND_MIN_ROWS = 200
OFFSETS = {"1w": pd.DateOffset(days=7), "1m": pd.DateOffset(months=1), "3m": pd.DateOffset(months=3),
           "6m": pd.DateOffset(months=6), "1y": pd.DateOffset(months=12)}
PERIOD_KEYS = ("1d", "1w", "1m", "3m", "6m", "1y", "ytd")
CORE_INDICES = ("NIFTY_50", "NIFTY_BANK")


@dataclass
class Panel:
    close: pd.DataFrame        # index: date (UTC), columns: symbol
    volume: pd.DataFrame
    instruments: dict          # symbol -> instrument document

    def kind(self, symbol: str) -> str:
        return "index" if self.instruments.get(symbol, {}).get("instrument_type") == "index" else "stock"

    def name(self, symbol: str) -> str:
        return self.instruments.get(symbol, {}).get("name", symbol)

    def sector(self, symbol: str) -> str:
        return self.instruments.get(symbol, {}).get("sector", "Unclassified")


# ----------------------------------------------------------------------------- helpers
def load_panel(db: Database) -> Panel:
    df = pd.DataFrame(list(db["adjusted_prices"].find({}, {"_id": 0, "symbol": 1, "date": 1, "close": 1, "volume": 1})))
    if df.empty:
        raise ValueError("adjusted_prices is empty: run `scripts/process_data.py adjust` first")
    if "volume" not in df:
        df["volume"] = np.nan
    df["date"] = pd.to_datetime(df["date"], utc=True)
    close = df.pivot(index="date", columns="symbol", values="close").sort_index()
    volume = df.pivot(index="date", columns="symbol", values="volume").sort_index()
    return Panel(close, volume, {i["symbol"]: i for i in db["instruments"].find({}, {"_id": 0})})


def iso(ts) -> str:
    return pd.Timestamp(ts).date().isoformat()


def pct(new, old) -> Optional[float]:
    if old is None or new is None or not np.isfinite(old) or not np.isfinite(new) or old == 0:
        return None
    return round((float(new) / float(old) - 1) * 100, 3)


def clean(obj):
    """Make a result safe for MongoDB and JSON: no NumPy types, NaN/inf become None, dates become text."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, float):                      # includes np.float64
        return None if (math.isnan(obj) or math.isinf(obj)) else float(obj)
    if isinstance(obj, np.floating):
        return clean(float(obj))
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    return obj


def latest_as_of(panel: Panel, reference: str = "NIFTY_50") -> pd.Timestamp:
    if reference in panel.close:
        return panel.close[reference].last_valid_index()
    return panel.close.index.max()


def full_universe_as_of(panel: Panel, share: float = 0.9) -> pd.Timestamp:
    stocks = [s for s in panel.close.columns if panel.kind(s) == "stock"]
    counts = panel.close[stocks].notna().sum(axis=1)
    return counts[counts >= share * counts.max()].index.max()


def universe(panel: Panel, as_of: pd.Timestamp) -> tuple[list[str], list[str]]:
    """(symbols with a recent price on `as_of`, symbols too stale to include)."""
    active, stale = [], []
    for sym in panel.close.columns:
        s = panel.close[sym].loc[:as_of].dropna()
        if s.empty:
            continue
        (active if (as_of - s.index[-1]).days <= STALE_DAYS else stale).append(sym)
    return active, stale


def max_drawdown(s: pd.Series) -> tuple[float, str, str]:
    """(worst peak-to-trough fall in %, peak date, trough date)."""
    drawdown = s / s.cummax() - 1
    trough = drawdown.idxmin()
    peak = s.loc[:trough].idxmax()
    return round(float(drawdown.min() * 100), 2), iso(peak), iso(trough)


# ----------------------------------------------------------------------------- the analyses
def performance(panel: Panel, as_of: pd.Timestamp, symbols: list[str]) -> dict:
    """Return (in %) over 1 day, 1 week, 1/3/6 months, 1 year and year-to-date, from the close on `as_of`."""
    out = {}
    for sym in symbols:
        s = panel.close[sym].loc[:as_of].dropna()
        if s.empty:
            continue
        last_date, last = s.index[-1], float(s.iloc[-1])
        row = {"date": iso(last_date), "close": round(last, 4), "1d": pct(last, s.iloc[-2]) if len(s) > 1 else None}
        for key, offset in OFFSETS.items():
            base = s.loc[:last_date - offset]
            row[key] = pct(last, float(base.iloc[-1])) if len(base) else None
        year_end = s.loc[:pd.Timestamp(year=last_date.year - 1, month=12, day=31, tz="UTC")]
        row["ytd"] = pct(last, float(year_end.iloc[-1])) if len(year_end) else None
        out[sym] = row
    return out


def top_movers(perf: dict, panel: Panel, n: int = 5) -> dict:
    stocks = {s: r for s, r in perf.items() if panel.kind(s) == "stock"}
    n = max(1, min(n, len(stocks) // 2)) if stocks else 0
    out = {}
    for key in ("1d", "1w", "1m"):
        ranked = sorted(((s, r[key]) for s, r in stocks.items() if r.get(key) is not None), key=lambda x: x[1])
        row = lambda pairs: [{"symbol": s, "name": panel.name(s), "return_pct": v} for s, v in pairs]  # noqa: E731
        # "gainers" only rose and "losers" only fell: in a small universe the bottom-n can include winners
        out[key] = {"gainers": row(p for p in reversed(ranked[-n:]) if p[1] > 0) if n else [],
                    "losers": row(p for p in ranked[:n] if p[1] < 0)}
    return out


def trend_snapshot(s: pd.Series) -> Optional[dict]:
    """Rule-based trend label from five simple checks (0-5 points): bullish >= 4, bearish <= 1."""
    if len(s) < TREND_MIN_ROWS:
        return None
    close = float(s.iloc[-1])
    sma50, sma200 = float(ind.sma(s, 50).iloc[-1]), float(ind.sma(s, 200).iloc[-1])
    ema20, ema50 = float(ind.ema(s, 20).iloc[-1]), float(ind.ema(s, 50).iloc[-1])
    rsi14, hist = float(ind.rsi(s, 14).iloc[-1]), float(ind.macd(s)["macd_hist"].iloc[-1])
    checks = {"above_sma50": close > sma50, "above_sma200": close > sma200, "ema20_above_ema50": ema20 > ema50,
              "macd_positive": hist > 0, "rsi_above_50": rsi14 > 50}
    score = sum(checks.values())
    return {"close": round(close, 4), "sma_50": round(sma50, 4), "sma_200": round(sma200, 4), "ema_20": round(ema20, 4),
            "ema_50": round(ema50, 4), "rsi_14": round(rsi14, 2), "macd_hist": round(hist, 4), **checks,
            "golden_cross": sma50 > sma200, "score": score,
            "trend": "bullish" if score >= 4 else "bearish" if score <= 1 else "neutral"}


def trend(panel: Panel, as_of: pd.Timestamp, symbols: list[str]) -> dict:
    per_symbol, short = {}, []
    for sym in symbols:
        snap = trend_snapshot(panel.close[sym].loc[:as_of].dropna())
        if snap is None:
            short.append(sym)
        else:
            per_symbol[sym] = snap
    counts = {"bullish": 0, "neutral": 0, "bearish": 0}
    for sym, snap in per_symbol.items():
        if panel.kind(sym) == "stock":
            counts[snap["trend"]] += 1
    return {"symbols": per_symbol, "stock_summary": counts, "insufficient_history": short,
            "note": "rule-based label (close vs 50/200-day averages, EMA20 vs EMA50, MACD sign, RSI vs 50); not a prediction"}


def market_breadth(panel: Panel, as_of: pd.Timestamp, symbols: list[str], perf: dict, trend_res: dict) -> dict:
    stocks = [s for s in symbols if panel.kind(s) == "stock"]
    day = [perf[s]["1d"] for s in stocks if s in perf and perf[s].get("1d") is not None]
    snaps = [trend_res["symbols"][s] for s in stocks if s in trend_res["symbols"]]
    highs = lows = 0
    for s in stocks:
        window = panel.close[s].loc[as_of - pd.Timedelta(days=365):as_of].dropna()
        if len(window) >= 200:
            highs += bool(window.iloc[-1] >= window.max())
            lows += bool(window.iloc[-1] <= window.min())

    returns = panel.close[[s for s in panel.close.columns if panel.kind(s) == "stock"]].loc[:as_of].pct_change(fill_method=None)
    net = ((returns > 0).sum(axis=1) - (returns < 0).sum(axis=1)).cumsum()
    monthly = net.resample("ME").last().tail(120)
    return {
        "stocks": len(stocks), "advancers": sum(v > 0 for v in day), "decliners": sum(v < 0 for v in day),
        "unchanged": sum(v == 0 for v in day),
        "advance_decline_ratio": round(sum(v > 0 for v in day) / max(sum(v < 0 for v in day), 1), 2) if day else None,
        "pct_above_sma50": round(100 * sum(x["above_sma50"] for x in snaps) / len(snaps), 1) if snaps else None,
        "pct_above_sma200": round(100 * sum(x["above_sma200"] for x in snaps) / len(snaps), 1) if snaps else None,
        "new_52w_highs": highs, "new_52w_lows": lows,
        "ad_line_monthly": [[iso(d), int(v)] for d, v in monthly.items()],
    }


def sector_performance(panel: Panel, perf: dict, symbols: list[str]) -> dict:
    groups: dict[str, list[str]] = {}
    for s in symbols:
        if panel.kind(s) == "stock" and s in perf:
            groups.setdefault(panel.sector(s), []).append(s)
    sectors = {}
    for name, members in groups.items():
        row: dict = {"stocks": len(members), "members": sorted(members)}
        for key in PERIOD_KEYS:
            vals = [perf[m][key] for m in members if perf[m].get(key) is not None]
            row[key] = round(sum(vals) / len(vals), 2) if vals else None
        ranked = sorted((m for m in members if perf[m].get("1m") is not None), key=lambda m: perf[m]["1m"])
        row["best_1m"] = ranked[-1] if ranked else None
        row["worst_1m"] = ranked[0] if ranked else None
        sectors[name] = row
    ranking = sorted(sectors, key=lambda n: (sectors[n]["1m"] is None, -(sectors[n]["1m"] or 0)))
    return {"sectors": sectors, "ranking_by_1m": ranking,
            "note": "equal-weighted averages of the stocks listed; today's constituents and sectors for all dates"}


def volatility(panel: Panel, as_of: pd.Timestamp, symbols: list[str]) -> dict:
    per_symbol = {}
    for sym in symbols:
        s = panel.close[sym].loc[:as_of].dropna()
        if len(s) < 60:
            continue
        daily = float(s.pct_change().dropna().tail(252).std() * 100)
        weekly_returns = s.resample("W-FRI").last().dropna().pct_change().dropna().tail(104)
        weekly = float(weekly_returns.std() * 100) if len(weekly_returns) >= 20 else None
        per_symbol[sym] = {
            "daily_vol_pct": round(daily, 3), "annualised_vol_pct": round(daily * math.sqrt(252), 2),
            "weekly_vol_pct": round(weekly, 3) if weekly is not None else None,
            "weekly_annualised_vol_pct": round(weekly * math.sqrt(52), 2) if weekly is not None else None,
            "hist_vol_20_pct": round(float(ind.historical_volatility(s, 20).iloc[-1]), 2)}
    ranked = sorted(per_symbol, key=lambda k: per_symbol[k]["annualised_vol_pct"])
    return {"symbols": per_symbol, "most_volatile": ranked[::-1][:5], "least_volatile": ranked[:5],
            "note": "daily/weekly volatility = standard deviation of returns over the last 252 days / 104 weeks"}


def volume_analysis(panel: Panel, as_of: pd.Timestamp, symbols: list[str]) -> dict:
    per_symbol = {}
    for sym in [s for s in symbols if panel.kind(s) == "stock"]:
        v = panel.volume[sym].loc[:as_of].dropna()
        v = v[v > 0]
        if len(v) < 50:
            continue
        last, prior20, avg50 = float(v.iloc[-1]), float(v.iloc[-21:-1].mean()), float(v.tail(50).mean())
        close = float(panel.close[sym].loc[:as_of].dropna().iloc[-1])
        per_symbol[sym] = {"date": iso(v.index[-1]), "volume": int(last), "avg_prior_20": int(prior20),
                           "avg_50": int(avg50), "ratio_to_prior_20": round(last / prior20, 2),
                           "avg20_vs_avg50_pct": pct(float(v.tail(20).mean()), avg50), "value_traded": round(last * close)}
    spikes = sorted((s for s, r in per_symbol.items() if r["ratio_to_prior_20"] >= 2),
                    key=lambda s: -per_symbol[s]["ratio_to_prior_20"])
    active = sorted(per_symbol, key=lambda s: -per_symbol[s]["value_traded"])[:5]
    return {"symbols": per_symbol, "volume_spikes_2x": spikes, "most_active_by_value": active,
            "note": "volume is split-adjusted; indices have no volume and are not included"}


def correlation(panel: Panel, as_of: pd.Timestamp, symbols: list[str], window: int = 756, min_share: float = 0.8) -> dict:
    rets = panel.close[symbols].loc[:as_of].tail(window + 1).pct_change(fill_method=None).iloc[1:]
    cols = [c for c in rets.columns if rets[c].notna().mean() >= min_share]
    if len(cols) < 2:
        return {"note": "not enough overlapping history", "window_days": window}
    corr = rets[cols].corr(min_periods=int(0.6 * len(rets)))
    stocks = [c for c in cols if panel.kind(c) == "stock"]
    pairs = [(a, b, float(corr.loc[a, b])) for i, a in enumerate(stocks) for b in stocks[i + 1:] if pd.notna(corr.loc[a, b])]
    pairs.sort(key=lambda p: p[2])
    fmt = lambda ps: [{"a": a, "b": b, "correlation": round(c, 3)} for a, b, c in ps]  # noqa: E731
    return {"window_days": window, "observations": int(len(rets)), "symbols": cols,
            "matrix": [[None if pd.isna(x) else round(float(x), 3) for x in row] for row in corr.values],
            "most_correlated_stock_pairs": fmt(reversed(pairs[-5:])), "least_correlated_stock_pairs": fmt(pairs[:5])}


def index_analysis(panel: Panel, as_of: pd.Timestamp, symbols: list[str]) -> dict:
    out: dict = {"indices": {}}
    present = [i for i in CORE_INDICES if i in symbols]
    perf = performance(panel, as_of, present)
    for idx in present:
        s = panel.close[idx].loc[:as_of].dropna()
        r = s.pct_change().dropna() * 100
        year_end = s.groupby(s.index.year).last()
        yearly = (year_end.pct_change() * 100).dropna()
        yearly.loc[year_end.index[0]] = (float(year_end.iloc[0]) / float(s.iloc[0]) - 1) * 100 if len(year_end) else np.nan
        yearly = yearly.sort_index()
        window = s.loc[s.index[-1] - pd.Timedelta(days=365):]
        mdd, peak, trough = max_drawdown(s)
        out["indices"][idx] = {
            "name": panel.name(idx), "first_date": iso(s.index[0]), "last_date": iso(s.index[-1]),
            "close": round(float(s.iloc[-1]), 2), "performance_pct": perf[idx],
            "calendar_year_returns_pct": {str(int(y)): round(float(v), 2) for y, v in yearly.items() if pd.notna(v)},
            "max_drawdown_pct": mdd, "max_drawdown_peak": peak, "max_drawdown_trough": trough,
            "current_drawdown_pct": round(float((s.iloc[-1] / s.cummax().iloc[-1] - 1) * 100), 2),
            "annualised_vol_pct_full_history": round(float(r.std() * math.sqrt(252)), 2),
            "annualised_vol_pct_last_1y": round(float(r.tail(252).std() * math.sqrt(252)), 2),
            "pct_up_days": round(float((r > 0).mean() * 100), 1),
            "best_days": [{"date": iso(d), "return_pct": round(float(v), 2)} for d, v in r.nlargest(5).items()],
            "worst_days": [{"date": iso(d), "return_pct": round(float(v), 2)} for d, v in r.nsmallest(5).items()],
            "high_52w": round(float(window.max()), 2), "low_52w": round(float(window.min()), 2),
            "pct_below_52w_high": round(float((s.iloc[-1] / window.max() - 1) * 100), 2),
            "trend": trend_snapshot(s)}
    if len(present) == 2:
        pair = panel.close[list(present)].loc[:as_of].tail(757).pct_change(fill_method=None).dropna()
        if len(pair) > 60:
            a, b = present
            out["nifty_vs_bank"] = {"window_days": int(len(pair)), "correlation": round(float(pair[a].corr(pair[b])), 3),
                                    "beta_of_bank_on_nifty": round(float(pair[b].cov(pair[a]) / pair[a].var()), 3)}
    return out


def return_distribution(panel: Panel, symbols: list[str]) -> dict:
    edges = list(range(-10, 11))
    out = {}
    for sym in symbols:
        r = panel.close[sym].pct_change(fill_method=None).dropna() * 100
        if len(r) < 250:
            continue
        q = r.quantile([0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
        inside = r[(r >= -10) & (r < 10)]
        out[sym] = {
            "observations": int(len(r)), "mean_pct": round(float(r.mean()), 4), "std_pct": round(float(r.std()), 4),
            "skew": round(float(r.skew()), 3), "excess_kurtosis": round(float(r.kurt()), 2),
            "min_pct": round(float(r.min()), 2), "max_pct": round(float(r.max()), 2),
            "pct_up_days": round(float((r > 0).mean() * 100), 1),
            "percentiles_pct": {f"p{int(k * 100):02d}": round(float(v), 3) for k, v in q.items()},
            "var_95_pct": round(float(-q.loc[0.05]), 3), "var_99_pct": round(float(-q.loc[0.01]), 3),
            "histogram": {"bin_edges_pct": edges, "counts": np.histogram(inside, bins=edges)[0].tolist(),
                          "below_minus_10": int((r < -10).sum()), "at_or_above_10": int((r >= 10).sum())}}
    return {"symbols": out, "note": "daily returns in %; VaR = the loss exceeded on 5% / 1% of days historically"}


def historical_statistics(panel: Panel, symbols: list[str]) -> dict:
    out = {}
    for sym in symbols:
        s = panel.close[sym].dropna()
        if len(s) < 2:
            continue
        years = (s.index[-1] - s.index[0]).days / 365.25
        r = s.pct_change().dropna() * 100
        mdd, peak, trough = max_drawdown(s)
        cagr = ((float(s.iloc[-1]) / float(s.iloc[0])) ** (1 / years) - 1) * 100 if years > 0 else None
        vol = float(r.std() * math.sqrt(252))
        row = {"name": panel.name(sym), "type": panel.kind(sym), "first_date": iso(s.index[0]), "last_date": iso(s.index[-1]),
               "rows": int(len(s)), "years": round(years, 2), "total_return_pct": pct(s.iloc[-1], s.iloc[0]),
               "cagr_pct": round(cagr, 2) if cagr is not None else None, "annualised_vol_pct": round(vol, 2),
               "cagr_per_vol": round(cagr / vol, 3) if cagr is not None and vol else None,
               "max_drawdown_pct": mdd, "max_drawdown_peak": peak, "max_drawdown_trough": trough,
               "best_day": {"date": iso(r.idxmax()), "return_pct": round(float(r.max()), 2)},
               "worst_day": {"date": iso(r.idxmin()), "return_pct": round(float(r.min()), 2)},
               "pct_up_days": round(float((r > 0).mean() * 100), 1),
               # Days with a >30% one-day move. On a stock these are usually an unadjusted split, a demerger or a
               # provider glitch, so a non-zero count means the CAGR / drawdown figures below may be distorted.
               "days_over_30pct_move": int((r.abs() > 30).sum())}
        v = panel.volume[sym].dropna()
        if panel.kind(sym) == "stock" and (v > 0).any():
            row["average_volume"] = int(v[v > 0].mean())
        out[sym] = row
    return {"symbols": out, "note": "prices are split-adjusted; dividends are not included in returns"}


def outliers(panel: Panel, symbols: list[str], top: int = 20) -> dict:
    returns = panel.close[symbols].pct_change(fill_method=None) * 100
    returns.index.name, returns.columns.name = "date", "symbol"      # do not rely on the caller's labels
    rets = returns.reset_index().melt(id_vars="date").dropna()
    rets["abs"] = rets["value"].abs()
    largest = [{"symbol": r.symbol, "date": iso(r.date), "return_pct": round(float(r.value), 2)}
               for r in rets.nlargest(top, "abs").itertuples()]
    robust = {}
    for sym, g in rets.groupby("symbol"):
        median = g["value"].median()
        mad = (g["value"] - median).abs().median()
        if mad > 0:
            count = int(((g["value"] - median).abs() / (1.4826 * mad) > 10).sum())
            if count:
                robust[sym] = count
    return {"largest_moves": largest, "robust_outlier_days": dict(sorted(robust.items(), key=lambda kv: -kv[1])),
            "note": "robust outlier = move more than 10 robust standard deviations (median/MAD) from the median"}


# ----------------------------------------------------------------------------- storing and running
def store_result(db: Database, analysis_type: str, scope: dict, result: dict, *, params: Optional[dict] = None,
                 data_range: Optional[dict] = None) -> None:
    """Replace the stored result for (analysis_type, scope), so re-running never accumulates copies."""
    db["analytics_results"].delete_many({"analysis_type": analysis_type, "scope": scope})
    doc = {"analysis_type": analysis_type, "scope": scope, "interval": "1d", "result": clean(result),
           "is_synthetic": False, "computed_at": datetime.now(timezone.utc)}
    if params:
        doc["params"] = clean(params)
    if data_range:
        doc["data_range"] = clean(data_range)
    db["analytics_results"].insert_one(doc)


def get_result(db: Database, analysis_type: str, scope: dict) -> Optional[dict]:
    doc = db["analytics_results"].find_one({"analysis_type": analysis_type, "scope": scope}, {"_id": 0})
    return doc["result"] if doc else None


def run_analytics(db: Database, *, progress: Optional[Callable[[str], None]] = None) -> dict:
    """Compute and store every analysis for both snapshots, plus the all-history analyses."""
    say = progress or (lambda _m: None)
    panel = load_panel(db)
    first = panel.close.index.min()
    summary: dict = {"snapshots": {}, "stored": 0}

    for name, as_of in (("latest", latest_as_of(panel)), ("full_universe", full_universe_as_of(panel))):
        active, stale = universe(panel, as_of)
        stocks = [s for s in active if panel.kind(s) == "stock"]
        scope = {"snapshot": name, "as_of": iso(as_of)}
        rng = {"from": iso(first), "to": iso(as_of)}
        uni = {"symbols": len(active), "stocks": len(stocks), "indices": len(active) - len(stocks), "stale_excluded": stale}

        perf = performance(panel, as_of, active)
        trend_res = trend(panel, as_of, active)
        analyses = {
            "performance": {"universe": uni, "symbols": perf},
            "top_movers": {"universe": uni, **top_movers(perf, panel)},
            "trend": {"universe": uni, **trend_res},
            "market_breadth": {"universe": uni, **market_breadth(panel, as_of, active, perf, trend_res)},
            "sector_performance": {"universe": uni, **sector_performance(panel, perf, active)},
            "volatility": {"universe": uni, **volatility(panel, as_of, active)},
            "volume_analysis": {"universe": uni, **volume_analysis(panel, as_of, active)},
            "correlation": {"universe": uni, **correlation(panel, as_of, active)},
            "index_analysis": {"universe": uni, **index_analysis(panel, as_of, active)},
        }
        for analysis_type, result in analyses.items():
            store_result(db, analysis_type, scope, result, data_range=rng)
            summary["stored"] += 1
        summary["snapshots"][name] = {"as_of": iso(as_of), "instruments": len(active), "stocks": len(stocks), "stale_excluded": len(stale)}
        say(f"  {name}: as of {iso(as_of)}, {len(stocks)} stocks + {len(active) - len(stocks)} indices, {len(stale)} stale excluded")

    everything = list(panel.close.columns)
    history_scope = {"history": "all"}
    rng = {"from": iso(first), "to": iso(panel.close.index.max())}
    for analysis_type, fn in (("return_distribution", lambda: return_distribution(panel, everything)),
                              ("historical_statistics", lambda: historical_statistics(panel, everything)),
                              ("outliers", lambda: outliers(panel, everything))):
        store_result(db, analysis_type, history_scope, fn(), data_range=rng)
        summary["stored"] += 1
    say(f"  history: all {len(everything)} instruments")
    return summary
