"""Turn database content into `Report` objects: one builder per report type.

Builders read from MongoDB and from the analyses stored by Phases 6-9; they never write. Every builder checks its parameters first and
raises `ReportError` (with an HTTP-style status) for anything the caller can fix: a missing symbol, an unknown run, a range with no data, or
a range so long the file would be unreasonably large.
"""
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pandas as pd
from pymongo.database import Database

from app.analytics.compare import MIN_COMMON_BARS, compare_closes
from app.analytics.indicators import INDICATOR_COLUMNS
from app.backtesting import runner
from app.backtesting.strategies import STRATEGIES
from app.reports.model import MAX_ROWS, Chart, Column, Report, ReportError, Series, Table

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL_RE = re.compile(r"^[A-Z0-9&_-]{1,30}$")
INTERVALS = ("1d", "1m", "5m", "15m", "30m", "1h")
PERIODS = [("1d", "1D"), ("1w", "1W"), ("1m", "1M"), ("3m", "3M"), ("6m", "6M"), ("1y", "1Y"), ("ytd", "YTD")]
CHART_POINTS = 500
DIVIDEND_NOTE = "Prices are split-adjusted; dividends are not included in any return."
SECTOR_NOTE = "Sector labels were assigned by this project, not taken from NSE: verify one before relying on a single stock."


# ------------------------------------------------------------------------------------------------ helpers
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _symbol(value: Any, name: str = "symbol") -> str:
    text = (value or "").strip().upper() if isinstance(value, str) else ""
    if not text:
        raise ReportError(422, "missing_parameter", f"This report needs a {name}.")
    if not SYMBOL_RE.match(text):
        raise ReportError(422, "invalid_symbol", f"{value!r} is not a valid symbol: use 1-30 letters, digits, & _ or -.")
    return text


def _symbols(value: Any, low: int, high: int) -> list[str]:
    items = [s.strip().upper() for s in (value or "").split(",") if s.strip()] if isinstance(value, str) else []
    items = list(dict.fromkeys(items))
    if not items:
        raise ReportError(422, "missing_parameter", "This report needs symbols, for example symbols=RELIANCE,TCS.")
    for s in items:
        _symbol(s)
    if not low <= len(items) <= high:
        raise ReportError(422, "invalid_symbols", f"Give between {low} and {high} different symbols, got {len(items)}.")
    return items


def _instrument(db: Database, symbol: str, exchange: str) -> dict:
    inst = db["instruments"].find_one({"symbol": symbol, "exchange": exchange}, {"_id": 0})
    if not inst:
        raise ReportError(404, "not_found", f"No instrument called {symbol} on {exchange}.")
    return inst


def _range(start: Optional[datetime], end: Optional[datetime]) -> dict:
    if start and end and start > end:
        raise ReportError(422, "invalid_range", "start must not be after end.")
    cond = {}
    if start:
        cond["$gte"] = start
    if end:
        cond["$lte"] = end
    return cond


def _range_text(start: Optional[datetime], end: Optional[datetime]) -> str:
    if not start and not end:
        return "all available dates"
    return f"{start.strftime('%d %b %Y') if start else 'the beginning'} to {end.strftime('%d %b %Y') if end else 'the latest bar'}"


def _thin(points: list, limit: int = CHART_POINTS) -> list:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    picked = [points[int(i * step)] for i in range(limit)]
    return picked[:-1] + [points[-1]]


def _check_size(count: int, what: str) -> None:
    if count > MAX_ROWS:
        raise ReportError(422, "too_many_rows", f"{what} would have more than {MAX_ROWS:,} rows. Choose a shorter date range or a coarser timeframe.")


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a in (None, 0) or b is None else (b / a - 1) * 100


def _stamp(ts: datetime, intraday: bool):
    return ts.astimezone(IST).replace(tzinfo=None) if intraday else ts.date()


def _stem(*parts: Any) -> str:
    return "BharatMarket_" + "_".join(str(p) for p in parts if p not in (None, ""))


def _stale_note(last: datetime) -> Optional[str]:
    return f"Price history for this instrument ends on {last.strftime('%d %b %Y')}; nothing after that date is included." if (_now() - last).days > 60 else None


# ----------------------------------------------------------------------------------------------- prices
def build_prices(db: Database, p: dict) -> Report:
    sym, ex = _symbol(p.get("symbol")), p.get("exchange") or "NSE"
    interval, basis, source = p.get("interval") or "1d", p.get("basis") or "adjusted", p.get("source")
    if interval not in INTERVALS:
        raise ReportError(422, "invalid_interval", f"interval must be one of {', '.join(INTERVALS)}.")
    if basis not in ("adjusted", "raw"):
        raise ReportError(422, "invalid_basis", "basis must be adjusted or raw.")
    inst = _instrument(db, sym, ex)
    intraday = interval != "1d"
    start, end = p.get("start"), p.get("end")
    cond = _range(start, end)
    if not intraday and basis == "adjusted":
        coll, field, query = db["adjusted_prices"], "date", {"symbol": sym, "exchange": ex, "interval": "1d"}
        proj = {"_id": 0, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "quality_flags": 1, "source": 1}
        if source:
            query["source"] = source
    elif not intraday:
        if not source:
            raise ReportError(422, "source_required", "Raw daily prices need a source (for example yfinance or kaggle): the same bar can exist from two providers.")
        coll = db["market_indices" if inst.get("instrument_type") == "index" else "stock_prices"]
        field, query = "date", {"symbol": sym, "exchange": ex, "interval": "1d", "source": source}
        proj = {"_id": 0, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "source": 1}
    else:
        coll, field = db["intraday_prices"], "ts"
        query = {"meta.symbol": sym, "meta.exchange": ex, "meta.interval": interval}
        if source:
            query["meta.source"] = source
        proj = {"_id": 0, "ts": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "meta.source": 1}
    if cond:
        query[field] = cond
    docs = list(coll.find(query, proj).sort(field, 1).limit(MAX_ROWS + 1))
    _check_size(len(docs), "This price history")
    if not docs:
        raise ReportError(404, "no_data", f"No {interval} prices for {sym} in that range.")

    rows, flagged, closes = [], 0, []
    for d in docs:
        ts = d[field]
        flags = ", ".join(d.get("quality_flags") or [])
        flagged += 1 if flags else 0
        closes.append(d["close"])
        core = [d["open"], d["high"], d["low"], d["close"], d.get("volume")]
        if intraday:
            rows.append([ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), _stamp(ts, True), *core])
        else:
            rows.append([_stamp(ts, False), *core, flags])
    if intraday:
        cols = [Column("time_utc", "Time (UTC)", "text", 21), Column("time_ist", "Time (IST)", "datetime", 18)]
    else:
        cols = [Column("date", "Date", "date", 13)]
    cols += [Column("open", "Open (Rs)", "inr"), Column("high", "High (Rs)", "inr"), Column("low", "Low (Rs)", "inr"),
             Column("close", "Close (Rs)", "inr"), Column("volume", "Volume", "int")]
    if not intraday:
        cols.append(Column("flags", "Data flags", "text", 22))
    pdf_cols = ["time_ist" if intraday else "date", "open", "high", "low", "close", "volume"]

    first_ts, last_ts = docs[0][field], docs[-1][field]
    volumes = [v for v in (d.get("volume") for d in docs) if v]
    daily_moves = [_pct(a, b) for a, b in zip(closes, closes[1:])] if not intraday else []
    daily_moves = [m for m in daily_moves if m is not None]
    summary = [("Bars", len(docs), "int"), ("First bar", _stamp(first_ts, intraday), "datetime" if intraday else "date"),
               ("Last bar", _stamp(last_ts, intraday), "datetime" if intraday else "date"),
               ("Return over the period", _pct(docs[0]["open"], closes[-1]), "pct"), ("Highest high", max(d["high"] for d in docs), "inr"),
               ("Lowest low", min(d["low"] for d in docs), "inr"), ("Average volume", statistics.fmean(volumes) if volumes else None, "int")]   # (intraday volumes of an index are all zero: shown as blank)
    if daily_moves:
        summary += [("Best day", max(daily_moves), "pct"), ("Worst day", min(daily_moves), "pct")]
    warnings = [DIVIDEND_NOTE] if basis == "adjusted" or intraday else ["Raw prices are shown as stored by the provider and are NOT adjusted for splits or bonuses."]
    if (note := _stale_note(last_ts)):
        warnings.append(note)
    if flagged:
        warnings.append(f"{flagged:,} {'bar carries' if flagged == 1 else 'bars carry'} data-quality flags (see the Data flags column): treat {'that day' if flagged == 1 else 'those days'} with care.")
    pts = _thin([(_stamp(d[field], intraday), d["close"]) for d in docs])
    label = {"1d": "Daily"}.get(interval, interval)
    return Report(
        id="prices", title=f"{sym} historical prices", subtitle=f"{inst['name']} - {label} bars, {_range_text(start, end)}", generated_at=_now(),
        filename_stem=_stem("prices", sym, interval, _stamp(first_ts, False), "to", _stamp(last_ts, False)),
        meta=[("Instrument", f"{sym} ({inst['name']})"), ("Exchange", ex), ("Type", str(inst.get("instrument_type", "")).title() + (f", {inst['sector']}" if inst.get("sector") else "")),
              ("Timeframe", label), ("Price basis", "split-adjusted (processed series)" if basis == "adjusted" and not intraday else "as stored" + (f" ({source})" if source else "")),
              ("Date range", _range_text(start, end))],
        summary=summary, warnings=warnings, charts=[Chart(f"{sym} close", [Series("Close", pts)], "inr")],
        tables=[Table("prices", "Price bars", cols, rows, pdf_columns=pdf_cols, pdf_rows=40, pdf_tail=True,
                      note="Times in IST are for reading; the UTC column is the exact instant." if intraday else None)])


# -------------------------------------------------------------------------------------------- indicators
def build_indicators(db: Database, p: dict) -> Report:
    sym, ex, interval = _symbol(p.get("symbol")), p.get("exchange") or "NSE", p.get("interval") or "1d"
    if interval not in INTERVALS:
        raise ReportError(422, "invalid_interval", f"interval must be one of {', '.join(INTERVALS)}.")
    names = [n.strip() for n in (p.get("names") or "").split(",") if n.strip()] or list(INDICATOR_COLUMNS)
    unknown = [n for n in names if n not in INDICATOR_COLUMNS]
    if unknown:
        raise ReportError(422, "unknown_indicator", f"Unknown indicator(s): {', '.join(unknown)}. Available: {', '.join(INDICATOR_COLUMNS)}.")
    inst = _instrument(db, sym, ex)
    intraday = interval != "1d"
    start, end = p.get("start"), p.get("end")
    query: dict = {"meta.symbol": sym, "meta.exchange": ex, "meta.interval": interval}
    if (cond := _range(start, end)):
        query["ts"] = cond
    docs = list(db["indicators"].find(query, {"_id": 0, "ts": 1, **{n: 1 for n in names}}).sort("ts", 1).limit(MAX_ROWS + 1))
    _check_size(len(docs), "This indicator history")
    if not docs:
        raise ReportError(404, "no_indicators", f"No {interval} indicators stored for {sym} in that range. Compute them first (POST /api/indicators/{sym}/compute).")
    labels = {n: n.upper().replace("_", " ") for n in names}
    cols = ([Column("time_utc", "Time (UTC)", "text", 21), Column("time_ist", "Time (IST)", "datetime", 18)] if intraday else [Column("date", "Date", "date", 13)])
    cols += [Column(n, labels[n], "float") for n in names]
    rows = []
    for d in docs:
        head = [d["ts"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), _stamp(d["ts"], True)] if intraday else [_stamp(d["ts"], False)]
        rows.append(head + [d.get(n) for n in names])
    priority = [n for n in ("rsi_14", "ema_20", "ema_50", "sma_200", "macd", "atr_14", "bb_upper", "bb_lower") if n in names] or names
    shown = priority[:6]
    last = docs[-1]
    summary = [("Rows", len(docs), "int"), ("First bar", _stamp(docs[0]["ts"], intraday), "datetime" if intraday else "date"),
               ("Last bar", _stamp(last["ts"], intraday), "datetime" if intraday else "date")]
    summary += [(f"Latest {labels[n]}", last.get(n), "float") for n in shown]
    chart_names = [n for n in ("ema_20", "ema_50", "sma_200") if n in names][:3] or shown[:2]
    charts = [Chart(f"{sym}: {', '.join(labels[n] for n in chart_names)}",
                    [Series(labels[n], _thin([(_stamp(d["ts"], intraday), d[n]) for d in docs if d.get(n) is not None])) for n in chart_names])]
    return Report(
        id="indicators", title=f"{sym} technical indicators", subtitle=f"{inst['name']} - {'Daily' if not intraday else interval}, {_range_text(start, end)}", generated_at=_now(),
        filename_stem=_stem("indicators", sym, interval, _stamp(docs[0]["ts"], False), "to", _stamp(last["ts"], False)),
        meta=[("Instrument", f"{sym} ({inst['name']})"), ("Exchange", ex), ("Timeframe", interval), ("Indicators", ", ".join(labels[n] for n in names)), ("Date range", _range_text(start, end))],
        summary=summary, charts=charts, landscape=True,
        warnings=["Early values are blank on purpose: an indicator that needs 200 bars has no value for the first 199 (nothing is back-filled).",
                  "Volume-based indicators (VWAP, volume averages) exist only for instruments that have traded volume: an index has none."],
        notes=["Indicators were computed from the split-adjusted daily prices (or the raw intraday bars) in Phase 7: Wilder smoothing for RSI and ATR, "
               "exponential averages with span n, Bollinger bands with the population standard deviation."],
        tables=[Table("indicators", "Indicator values", cols, rows, pdf_columns=[("time_ist" if intraday else "date")] + shown, pdf_rows=30, pdf_tail=True)])


# ------------------------------------------------------------------------------------------- performance
def _latest(db: Database, kind: str) -> Optional[dict]:
    return db["analytics_results"].find_one({"analysis_type": kind, "scope.snapshot": "latest"}, {"_id": 0})


def build_performance(db: Database, p: dict) -> Report:
    perf, vol, trend = _latest(db, "performance"), _latest(db, "volatility"), _latest(db, "trend")
    if not perf:
        raise ReportError(404, "not_computed", "Market analytics have not been computed yet. Run POST /api/processing/analytics first.")
    wanted = set(_symbols(p["symbols"], 1, 50)) if p.get("symbols") else None
    info = {i["symbol"]: i for i in db["instruments"].find({"exchange": "NSE"}, {"_id": 0, "symbol": 1, "name": 1, "sector": 1, "instrument_type": 1})}
    rows = []
    for sym, r in perf["result"]["symbols"].items():
        if wanted and sym not in wanted:
            continue
        i, v, t = info.get(sym, {}), (vol or {}).get("result", {}).get("symbols", {}).get(sym, {}), (trend or {}).get("result", {}).get("symbols", {}).get(sym, {})
        sma200, close = t.get("sma_200"), r.get("close")
        rows.append([sym, i.get("name", ""), i.get("sector", ""), str(i.get("instrument_type", "")).title(), close, *[r.get(k) for k, _ in PERIODS],
                     v.get("annualised_vol_pct"), t.get("rsi_14"), _pct(sma200, close)])
    if not rows:
        raise ReportError(404, "no_data", "None of those symbols is in the stored performance analysis.")
    rows.sort(key=lambda r: (r[7] is None, -(r[7] or 0)))                       # best 1-month first
    cols = [Column("symbol", "Symbol", "text", 14), Column("name", "Name", "text", 30), Column("sector", "Sector", "text", 26), Column("type", "Type", "text", 9),
            Column("close", "Close (Rs)", "inr"), *[Column(k, f"{label} (%)", "pct") for k, label in PERIODS],
            Column("vol", "Volatility (% a year)", "float"), Column("rsi", "RSI 14", "float"), Column("sma200", "Vs 200-day average (%)", "pct")]
    stocks = [r for r in rows if r[3] == "Equity"]
    month = [(r[0], r[7]) for r in stocks if r[7] is not None]
    as_of = perf.get("scope", {}).get("as_of")
    uni = perf["result"].get("universe", {})
    summary = [("As of", as_of, "text"), ("Stocks", len(stocks), "int"), ("Indices", len(rows) - len(stocks), "int")]
    if month:
        best, worst = max(month, key=lambda x: x[1]), min(month, key=lambda x: x[1])
        summary += [("Best 1-month stock", f"{best[0]} {best[1]:+.2f}%", "text"), ("Worst 1-month stock", f"{worst[0]} {worst[1]:+.2f}%", "text"),
                    ("Average 1-month stock return", statistics.fmean(m for _, m in month), "pct")]
    warnings = [DIVIDEND_NOTE]
    stale = uni.get("stale_excluded") or []
    if stale:
        warnings.append(f"{len(stale)} further stocks are left out because their price history ends before {as_of} (the free Kaggle data stops in April 2021).")
    return Report(
        id="performance", title="Stock performance analysis", subtitle=f"Returns, volatility and trend as of {as_of}", generated_at=_now(),
        filename_stem=_stem("performance", as_of), landscape=True,
        meta=[("As of", str(as_of)), ("Universe", f"{uni.get('stocks', len(stocks))} stocks and {uni.get('indices', 0)} indices with current data"),
              ("Symbols", ", ".join(sorted(wanted)) if wanted else "all in the stored analysis")],
        summary=summary, warnings=warnings,
        notes=["Returns are price returns to the as-of date for the last day, week, month, 3 and 6 months, year and calendar year to date. "
               "Volatility is the annualised standard deviation of daily returns. 'Vs 200-day average' is how far the close is above (+) or below (-) the 200-day average."],
        tables=[Table("performance", "Performance by instrument", cols, rows, pdf_columns=["symbol", "close", "1d", "1w", "1m", "3m", "1y", "ytd", "vol"], pdf_rows=60)])


# -------------------------------------------------------------------------------------------- comparison
def build_comparison(db: Database, p: dict) -> Report:
    syms, ex = _symbols(p.get("symbols"), 2, 10), p.get("exchange") or "NSE"
    for s in syms:
        _instrument(db, s, ex)
    start, end = p.get("start"), p.get("end")
    query: dict = {"symbol": {"$in": syms}, "exchange": ex, "interval": "1d"}
    if (cond := _range(start, end)):
        query["date"] = cond
    docs = list(db["adjusted_prices"].find(query, {"_id": 0, "symbol": 1, "date": 1, "close": 1}))
    if not docs:
        raise ReportError(404, "no_data", "None of these symbols has daily prices in that range.")
    frame = pd.DataFrame(docs).pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    missing = [s for s in syms if s not in frame.columns]
    if missing:
        raise ReportError(404, "no_data", f"No daily prices in that range for: {', '.join(missing)}.")
    try:
        result = compare_closes(frame[syms])
    except ValueError as exc:
        raise ReportError(422, "not_enough_common_data", f"{exc}. Widen the date range or choose symbols with overlapping history (minimum {MIN_COMMON_BARS} common days).")
    stats = result["stats"]
    stat_rows = [[s, st["first_close"], st["last_close"], st["total_return_pct"], st["cagr_pct"], st["volatility_pct"], st["max_drawdown_pct"], st["best_day_pct"], st["worst_day_pct"]] for s, st in stats.items()]
    stat_cols = [Column("symbol", "Symbol", "text", 14), Column("first", "First close (Rs)", "inr"), Column("last", "Last close (Rs)", "inr"), Column("ret", "Total return (%)", "pct"),
                 Column("cagr", "CAGR (%)", "pct"), Column("vol", "Volatility (% a year)", "float"), Column("dd", "Max drawdown (%)", "pct"), Column("best", "Best day (%)", "pct"), Column("worst", "Worst day (%)", "pct")]
    corr = result["correlation"]
    corr_rows = [[a, *[corr[a][b] for b in syms]] for a in syms]
    series_rows = [[pt["ts"].date() if isinstance(pt["ts"], datetime) else pt["ts"], *[pt[s] for s in syms]] for pt in result["rebased_series"]]
    period = result["period"]
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["total_return_pct"], reverse=True)
    return Report(
        id="comparison", title="Stock comparison", subtitle=f"{', '.join(syms)} over {period['start'].strftime('%d %b %Y')} to {period['end'].strftime('%d %b %Y')}", generated_at=_now(),
        filename_stem=_stem("comparison", "-".join(syms[:4]), period["start"].date(), "to", period["end"].date()),
        meta=[("Symbols", ", ".join(syms)), ("Exchange", ex), ("Requested range", _range_text(start, end)), ("Common period", f"{period['bars']:,} trading days, {period['years']} years")],
        summary=[("Common trading days", period["bars"], "int"), ("Years", period["years"], "ratio"), ("Best performer", f"{ranked[0][0]} {ranked[0][1]['total_return_pct']:+.2f}%", "text"),
                 ("Worst performer", f"{ranked[-1][0]} {ranked[-1][1]['total_return_pct']:+.2f}%", "text")],
        warnings=[DIVIDEND_NOTE, "Only dates on which every symbol has a close are used, so the period may be shorter than any single stock's history."],
        charts=[Chart("Growth of 100 (rebased to the first common day)", [Series(s, [(r[0], r[1 + i]) for r in series_rows]) for i, s in enumerate(syms)])],
        tables=[Table("statistics", "Statistics", stat_cols, stat_rows),
                Table("correlation", "Correlation of daily returns", [Column("symbol", "Symbol", "text", 14), *[Column(s, s, "ratio") for s in syms]], corr_rows),
                Table("rebased_series", "Rebased prices (100 at the start)", [Column("date", "Date", "date", 13), *[Column(s, s, "float") for s in syms]], series_rows, pdf_rows=0)])


# ----------------------------------------------------------------------------------------------- sectors
def build_sectors(db: Database, p: dict) -> Report:
    doc = _latest(db, "sector_performance")
    if not doc:
        raise ReportError(404, "not_computed", "Sector analytics have not been computed yet. Run POST /api/processing/analytics first.")
    sectors = doc["result"]["sectors"]
    rows = [[name, s.get("stocks"), *[s.get(k) for k, _ in PERIODS], s.get("best_1m"), s.get("worst_1m")] for name, s in sectors.items()]
    rows.sort(key=lambda r: (r[4] is None, -(r[4] or 0)))                      # best 1-month first
    cols = [Column("sector", "Sector", "text", 30), Column("stocks", "Stocks", "int"), *[Column(k, f"{label} (%)", "pct") for k, label in PERIODS],
            Column("best", "Best 1M stock", "text", 14), Column("worst", "Worst 1M stock", "text", 14)]
    members = [[i.get("sector") or "(none)", i["symbol"], i["name"]] for i in db["instruments"].find(
        {"instrument_type": {"$in": ["equity", "etf"]}, "active": {"$ne": False}}, {"_id": 0, "symbol": 1, "name": 1, "sector": 1})]
    members.sort(key=lambda m: (m[0], m[1]))
    as_of = doc.get("scope", {}).get("as_of")
    one_month = [(r[0], r[4]) for r in rows if r[4] is not None]
    summary = [("As of", as_of, "text"), ("Sectors", len(rows), "int")]
    if one_month:
        summary += [("Best sector (1 month)", f"{one_month[0][0]} {one_month[0][1]:+.2f}%", "text"), ("Worst sector (1 month)", f"{one_month[-1][0]} {one_month[-1][1]:+.2f}%", "text")]
    return Report(
        id="sectors", title="Sector analytics", subtitle=f"Sector performance as of {as_of}", generated_at=_now(), filename_stem=_stem("sectors", as_of),
        meta=[("As of", str(as_of)), ("Method", "equal-weighted average of each sector's stocks")], summary=summary,
        warnings=[SECTOR_NOTE, doc["result"].get("note", "Equal-weighted averages of today's constituents applied to all dates."), DIVIDEND_NOTE],
        tables=[Table("sectors", "Sector performance", cols, rows, pdf_columns=["sector", "stocks", "1d", "1w", "1m", "3m", "6m", "1y", "ytd"]),
                Table("members", "Stocks in each sector", [Column("sector", "Sector", "text", 30), Column("symbol", "Symbol", "text", 14), Column("name", "Name", "text", 36)], members, pdf_rows=60)])


# ---------------------------------------------------------------------------------------------- backtest
def build_backtest(db: Database, p: dict) -> Report:
    run_id = p.get("run_id")
    if not run_id or not re.match(r"^[A-Za-z0-9_.-]{1,80}$", str(run_id)):
        raise ReportError(422, "missing_parameter", "This report needs a run_id (a stored backtest, for example run_id=bt_20261122_101500_ab12cd).")
    d = db["backtest_results"].find_one({"run_id": run_id}, {"_id": 0})
    if not d:
        raise ReportError(404, "not_found", f"No backtest run {run_id}.")
    intraday = d["interval"] != "1d"
    m, b = d.get("metrics", {}), d.get("benchmark", {})
    title = STRATEGIES[d["strategy"]].title if d["strategy"] in STRATEGIES else d["strategy"]
    trades = [[_stamp(t["entry_ts"], intraday), _stamp(t["exit_ts"], intraday), t["entry_price"], t["exit_price"], t["qty"], t["net_pnl"], t["return_pct"], t.get("r_multiple"), t["holding_days"], t["exit_reason"]] for t in d.get("trades", [])]
    stamp_kind = "datetime" if intraday else "date"
    trade_cols = [Column("entry", "Entry", stamp_kind, 18), Column("exit", "Exit", stamp_kind, 18), Column("buy", "Buy (Rs)", "inr"), Column("sell", "Sell (Rs)", "inr"), Column("qty", "Quantity", "int"),
                  Column("pnl", "Net P&L (Rs)", "inr"), Column("ret", "Return (%)", "pct"), Column("r", "R multiple", "ratio"), Column("days", "Days held", "int"), Column("reason", "Exit reason", "text", 22)]
    curve = d.get("equity_curve", [])
    curve_rows = [[c["ts"].date(), c["equity"], c["drawdown_pct"], c.get("benchmark_equity")] for c in curve]
    tables = [Table("trades", "Trades", trade_cols, trades, pdf_rows=30, pdf_tail=False,
                    note=("The stored run keeps at most 1,500 trades." if d.get("trades_truncated") else None)),
              Table("equity_curve", "Equity curve (thinned to at most 800 points)", [Column("date", "Date", "date", 13), Column("equity", "Strategy equity (Rs)", "inr"), Column("dd", "Drawdown (%)", "pct"), Column("bh", "Buy and hold equity (Rs)", "inr")], curve_rows, pdf_rows=0)]
    periods = d.get("periods")
    if periods:
        pr = [[name, v.get("total_trades"), v.get("total_return_pct"), v.get("buy_hold_return_pct"), v.get("win_rate_pct"), v.get("profit_factor")] for name, v in (("In-sample", periods["in_sample"]), ("Out-of-sample", periods["out_of_sample"]))]
        tables.append(Table("periods", f"In-sample vs out-of-sample (split at {periods['split_date'].strftime('%d %b %Y')})",
                            [Column("period", "Period", "text", 16), Column("trades", "Trades", "int"), Column("ret", "Strategy return (%)", "pct"), Column("bh", "Buy and hold (%)", "pct"), Column("win", "Win rate (%)", "percent"), Column("pf", "Profit factor", "ratio")], pr))
    summary = [("Net profit after costs", m.get("net_pnl"), "inr"), ("Total return", m.get("total_return_pct"), "pct"), ("Buy and hold, same dates", b.get("total_return_pct"), "pct"),
               ("Strategy minus buy and hold", d.get("excess_return_pct"), "pct"), ("Maximum drawdown", m.get("max_drawdown_pct"), "pct"), ("Buy and hold drawdown", b.get("max_drawdown_pct"), "pct"),
               ("Win rate", m.get("win_rate_pct"), "percent"), ("Winners", m.get("winners"), "int"), ("Losers", m.get("losers"), "int"), ("Profit factor", m.get("profit_factor"), "ratio"),
               ("Total trades", m.get("total_trades"), "int"), ("Average risk-to-reward", m.get("avg_risk_reward"), "ratio"), ("Average R multiple", m.get("avg_r"), "ratio"),
               ("Sharpe ratio (approx.)", m.get("sharpe"), "ratio"), ("Time invested", m.get("exposure_pct"), "percent"), ("Costs paid", m.get("total_costs"), "inr")]
    tax = d.get("tax_estimate")
    if tax:
        summary += [("Estimated capital-gains tax", tax.get("total_tax"), "inr"), ("Net profit after tax", tax.get("net_pnl_after_tax"), "inr")]
    costs = d.get("costs", {})
    params_text = ", ".join(f"{k} {v}" for k, v in d.get("params", {}).items() if v is not None) + (f", target {d['target_r']}R" if d.get("target_r") else ", no profit target")
    return Report(
        id="backtest", title=f"Backtest: {title} on {d['symbol']}", subtitle=f"{d['interval']} bars, {d['start'].strftime('%d %b %Y')} to {d['end'].strftime('%d %b %Y')}", generated_at=_now(),
        filename_stem=_stem("backtest", d["strategy"], d["symbol"], d["interval"], run_id[-8:]),
        meta=[("Run", run_id), ("Strategy", title), ("Instrument", f"{d['symbol']} ({d['exchange']})"), ("Timeframe", d["interval"]), ("Period", f"{d['start'].strftime('%d %b %Y')} to {d['end'].strftime('%d %b %Y')}"),
              ("Parameters", params_text), ("Costs", f"{costs.get('model', {}).get('name', '')} preset, slippage {costs.get('slippage_bps', '')} bps per fill, starting capital Rs {costs.get('initial_capital', 0):,.0f}"),
              ("Execution", costs.get("execution", "signals act at the next bar's open"))],
        summary=summary, warnings=list(d.get("warnings", [])) + ["A backtest is not a forecast: transaction costs and slippage are assumptions, and parameters tuned on past data often fail on new data."],
        charts=[Chart("Equity curve: strategy against buy and hold", [Series("Strategy", [(c["ts"].date(), c["equity"]) for c in curve]), Series("Buy and hold", [(c["ts"].date(), c["benchmark_equity"]) for c in curve if c.get("benchmark_equity") is not None])], "inr")] if curve else [],
        notes=[tax["assumptions"]] if tax else [], tables=tables)


# -------------------------------------------------------------------------------------------- strategies
def build_strategies(db: Database, p: dict) -> Report:
    batch, interval = p.get("batch") or "compare", p.get("interval") or "1d"
    board = runner.leaderboard(db, batch, interval)
    if board is None:
        raise ReportError(404, "no_runs", f"No stored runs in batch {batch!r}. Run `scripts/backtest.py compare` first.")
    rows = [[s["strategy"], s["stocks"], s["beat_buy_and_hold"], s.get("median_total_trades"), s.get("median_win_rate_pct"), s.get("median_profit_factor"), s.get("median_total_return_pct"),
             s.get("median_buy_hold_return_pct"), s.get("median_max_drawdown_pct"), s.get("median_sharpe"), s.get("median_buy_hold_sharpe"), s.get("median_oos_return_pct")] for s in board["strategies"]]
    cols = [Column("strategy", "Strategy", "text", 24), Column("stocks", "Stocks", "int"), Column("beat", "Beat buy and hold", "int"), Column("trades", "Median trades", "int"), Column("win", "Median win rate (%)", "float"),
            Column("pf", "Median profit factor", "ratio"), Column("ret", "Median return (%)", "pct"), Column("bh", "Median buy and hold (%)", "pct"), Column("dd", "Median max drawdown (%)", "pct"),
            Column("sharpe", "Median Sharpe", "ratio"), Column("bh_sharpe", "Buy and hold Sharpe", "ratio"), Column("oos", "Median out-of-sample return (%)", "pct")]
    best = board["strategies"][0]
    return Report(
        id="strategies", title="Strategy comparison", subtitle=f"Medians across {board['strategies'][0]['stocks']} stocks, {board['runs']} stored runs", generated_at=_now(),
        filename_stem=_stem("strategies", batch, interval), landscape=True,
        meta=[("Batch", batch), ("Timeframe", interval), ("Runs", f"{board['runs']:,}")],
        summary=[("Runs", board["runs"], "int"), ("Strategies", len(board["strategies"]), "int"), ("Best median Sharpe", f"{best['strategy']} ({best.get('median_sharpe')})", "text")],
        warnings=[board["warning"], "Buy and hold beat most strategies on most stocks: read the Beat buy and hold column before the returns."],
        tables=[Table("strategies", "Median results per strategy", cols, rows, pdf_columns=["strategy", "stocks", "beat", "trades", "win", "pf", "ret", "bh", "dd", "sharpe", "oos"])])


# ------------------------------------------------------------------------------------------- benchmarks
def build_benchmarks(db: Database, p: dict) -> Report:
    query = {k: p[k] for k in ("technology", "operation") if p.get(k)}
    docs = list(db["benchmark_results"].find(query, {"_id": 0}).sort([("created_at", -1), ("operation", 1), ("technology", 1)]).limit(MAX_ROWS))
    if not docs:
        raise ReportError(404, "no_data", "No benchmark results are stored yet. Run `scripts/bigdata.py benchmark` first.")
    rows = [[d["operation"], d["technology"], d["record_count"], d["duration_seconds"], d.get("records_per_second"), d.get("cpu_percent"), d.get("memory_mb"), d.get("storage_mb"),
             "Synthetic" if d.get("is_synthetic") else "Real", d.get("notes", ""), d["created_at"].date()] for d in docs]
    cols = [Column("operation", "Operation", "text", 30), Column("technology", "Technology", "text", 20), Column("records", "Records", "int"), Column("seconds", "Seconds", "float"), Column("rps", "Records per second", "int"),
            Column("cpu", "CPU (%)", "float"), Column("memory", "Memory (MB)", "float"), Column("storage", "Storage (MB)", "float"), Column("data", "Data", "text", 10), Column("notes", "Notes", "text", 36), Column("date", "Run date", "date", 13)]
    groups: dict = {}
    for d in docs:
        groups.setdefault((d["operation"], d["record_count"]), {}).setdefault(d["technology"], []).append(d["duration_seconds"])
    fastest = []
    for (op, n), techs in sorted(groups.items()):
        if len(techs) < 2:
            continue
        median = {t: statistics.median(v) for t, v in techs.items()}
        winner = min(median, key=median.get)
        fastest.append([op, n, winner, median[winner], *[median.get(t) for t in ("pandas", "pyspark", "mongodb_aggregation")]])
    stats = db.command("dbstats", scale=1024 * 1024)
    names = sorted(n for n in db.list_collection_names() if not n.startswith("system."))
    counts = [[n, db[n].estimated_document_count()] for n in names]
    synthetic = sum(1 for d in docs if d.get("is_synthetic"))
    tables = [Table("benchmarks", "Stored timings", cols, rows, pdf_columns=["operation", "technology", "records", "seconds", "rps", "data"], pdf_rows=45)]
    if fastest:
        tables.append(Table("fastest", "Fastest technology per job (median of the stored runs)",
                            [Column("operation", "Operation", "text", 30), Column("records", "Records", "int"), Column("winner", "Fastest", "text", 20), Column("seconds", "Seconds", "float"),
                             Column("pandas", "pandas (s)", "float"), Column("pyspark", "pyspark (s)", "float"), Column("mongo", "MongoDB (s)", "float")], fastest))
    tables.append(Table("collections", "Documents per collection", [Column("collection", "Collection", "text", 28), Column("documents", "Documents", "int")], counts, pdf_rows=30))
    return Report(
        id="benchmarks", title="Database performance report", subtitle=f"{len(docs):,} stored timings and the database's size", generated_at=_now(), filename_stem=_stem("benchmarks"),
        meta=[("Filters", ", ".join(f"{k}={v}" for k, v in query.items()) or "none"), ("Database", db.name)],
        summary=[("Stored timings", len(docs), "int"), ("Technologies", len({d["technology"] for d in docs}), "int"), ("Operations", len({d["operation"] for d in docs}), "int"),
                 ("Documents in the database", sum(c for _, c in counts), "int"), ("Data size (MB)", stats.get("dataSize"), "float"), ("Storage on disk (MB)", stats.get("storageSize"), "float")],
        warnings=["Timings come from one machine and one run each: compare technologies with each other on the same job, not with other computers.",
                  f"{synthetic:,} of these timings use synthetic data." if synthetic else "These timings use real market data."],
        notes=["A timing counts only when every engine produced the same answer (see docs/BIGDATA.md). Spark's start-up time is not included."], tables=tables, landscape=True)


# -------------------------------------------------------------------------------------------- registry
BUILDERS: dict[str, Callable[[Database, dict], Report]] = {
    "prices": build_prices, "indicators": build_indicators, "performance": build_performance, "comparison": build_comparison,
    "sectors": build_sectors, "backtest": build_backtest, "strategies": build_strategies, "benchmarks": build_benchmarks,
}

_RANGE = [{"name": "start", "description": "first date or datetime (UTC)"}, {"name": "end", "description": "last date or datetime (UTC)"}]
CATALOG = [
    {"id": "prices", "title": "Historical prices", "description": "Price bars (open, high, low, close, volume) for one instrument over a date range, with a summary and chart.",
     "params": [{"name": "symbol", "required": True, "description": "e.g. RELIANCE"}, {"name": "exchange", "default": "NSE"}, {"name": "interval", "default": "1d", "description": "1d, 1h, 15m, 5m, 1m"},
                {"name": "basis", "default": "adjusted", "description": "adjusted or raw"}, {"name": "source", "description": "required for raw prices"}, *_RANGE], "tables": ["prices"]},
    {"id": "indicators", "title": "Technical indicators", "description": "Stored indicator values (RSI, MACD, moving averages, Bollinger bands ...) for one instrument.",
     "params": [{"name": "symbol", "required": True}, {"name": "exchange", "default": "NSE"}, {"name": "interval", "default": "1d"}, {"name": "names", "description": "comma-separated indicators; default all"}, *_RANGE], "tables": ["indicators"]},
    {"id": "performance", "title": "Stock performance analysis", "description": "Returns over 1 day to 1 year, volatility and trend for every instrument in the stored analysis.",
     "params": [{"name": "symbols", "description": "optional comma-separated filter"}], "tables": ["performance"]},
    {"id": "comparison", "title": "Stock comparison", "description": "Two to ten instruments over the same dates: returns, volatility, drawdown, correlation, rebased prices.",
     "params": [{"name": "symbols", "required": True, "description": "2 to 10, comma-separated"}, {"name": "exchange", "default": "NSE"}, *_RANGE], "tables": ["statistics", "correlation", "rebased_series"]},
    {"id": "sectors", "title": "Sector analytics", "description": "Sector performance over 1 day to year-to-date, with each sector's stocks.", "params": [], "tables": ["sectors", "members"]},
    {"id": "backtest", "title": "Backtest result", "description": "A stored backtest: key figures, trades, equity curve against buy and hold, in-sample vs out-of-sample.",
     "params": [{"name": "run_id", "required": True, "description": "from the Backtesting page's saved runs"}], "tables": ["trades", "equity_curve", "periods"]},
    {"id": "strategies", "title": "Strategy comparison", "description": "Median results of every strategy across the stored comparison run, next to buy and hold.",
     "params": [{"name": "batch", "default": "compare"}, {"name": "interval", "default": "1d"}], "tables": ["strategies"]},
    {"id": "benchmarks", "title": "Database performance report", "description": "Stored pandas / MongoDB / Spark timings, the fastest technology per job, and the database's size.",
     "params": [{"name": "technology", "description": "pandas, pyspark, mongodb_aggregation or ingestion"}, {"name": "operation"}], "tables": ["benchmarks", "fastest", "collections"]},
]
for _entry in CATALOG:
    _entry["formats"] = ["csv", "xlsx", "pdf"]


def build_report(db: Database, report_id: str, params: dict) -> Report:
    builder = BUILDERS.get(report_id)
    if builder is None:
        raise ReportError(404, "unknown_report", f"Unknown report {report_id!r}. Available: {', '.join(BUILDERS)}.")
    report = builder(db, params)
    for table in report.tables:
        _check_size(len(table.rows), table.title)
    return report
