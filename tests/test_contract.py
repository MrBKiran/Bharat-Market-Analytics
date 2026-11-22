"""Phase 12: the API and the dashboard agree on the shape of the data.

`frontend/src/test/contract.json` lists, for each endpoint, the fields the dashboard reads. This test runs the REAL API (real
analytics, a real backtest, a real import) on a throwaway database and checks every response against that list. The frontend's
own test checks its fixtures against the same file, so a change to either side that the other does not expect fails a test.
"""
import json
from pathlib import Path

import pytest

from app.analytics import market
from app.analytics.indicator_pipeline import compute_and_store
from app.backtesting import runner
from datetime import datetime, timezone

from test_api import YF_SIX_GOOD, db, make_client, wavy_daily  # noqa: F401  (db and make_client are pytest fixtures)

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / "frontend" / "src" / "test" / "contract.json").read_text(encoding="utf-8"))
NAMES = [k for k in CONTRACT if not k.startswith("_")]
TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}
# Paths that may legitimately match nothing on random test data (a list that happens to be empty).
MAY_BE_EMPTY = {"top_movers.1d.losers[].symbol"}


def is_type(value, kind: str) -> bool:
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, TYPES[kind])


def leaves(node, parts, trail, errors):
    """Every value a path reaches; problems (a missing key, the wrong container) go into `errors`."""
    if not parts:
        yield node
        return
    part, rest = parts[0], parts[1:]
    if part == "*":
        if not isinstance(node, dict):
            errors.append(f"{trail}: expected an object")
            return
        for key, value in node.items():
            yield from leaves(value, rest, f"{trail}.{key}", errors)
    elif part.endswith("[]"):
        key = part[:-2]
        if not isinstance(node, dict) or key not in node:
            errors.append(f"{trail}.{key}: missing")
        elif not isinstance(node[key], list):
            errors.append(f"{trail}.{key}: expected a list")
        else:
            for i, element in enumerate(node[key]):
                yield from leaves(element, rest, f"{trail}.{key}[{i}]", errors)
    else:
        if not isinstance(node, dict) or part not in node:
            errors.append(f"{trail}.{part}: missing")
        else:
            yield from leaves(node[part], rest, f"{trail}.{part}", errors)


def check(data, fields) -> list[str]:
    errors: list[str] = []
    for spec in fields:
        path, _, kind = spec.partition(":")
        before = len(errors)
        found = list(leaves(data, path.split("."), "$", errors))
        if not found and len(errors) == before and ("[]" in path or "*" in path) and path not in MAY_BE_EMPTY:
            errors.append(f"{path}: matched nothing, so it proved nothing (the test data should have produced some)")
        if kind:
            for value in found:
                if value is not None and not is_type(value, kind):
                    errors.append(f"{path}: expected {kind}, got {type(value).__name__} ({value!r:.40})")
    return errors


# -------------------------------------------------------------------------------------- the checker itself
def test_checker_finds_missing_keys_wrong_types_and_vacuous_paths():
    data = {"a": {"b": 1, "c": None}, "items": [{"x": 1}, {"x": "no"}], "empty": [], "by": {"k1": {"v": 1}, "k2": {}}}
    assert check(data, ["a.b:number", "a.c", "a.c:string"]) == []                          # present, null is allowed for any type
    assert check(data, ["a.zzz"]) == ["$.a.zzz: missing"]
    assert check(data, ["a.b:string"])[0].startswith("a.b: expected string")
    assert [e for e in check(data, ["items[].x:number"])] == ["items[].x: expected number, got str ('no')"]
    assert check(data, ["by.*.v"]) == ["$.by.k2.v: missing"]
    assert any("matched nothing" in e for e in check(data, ["empty[].y"]))
    assert check({"n": True}, ["n:number"])                                                  # a boolean is not a number


# ------------------------------------------------------------------------------------------ the real API
@pytest.fixture()
def world(db, make_client):
    """A small database filled by the real code paths, and a client for it."""
    for k, instrument in enumerate(db["instruments"].find({}, {"symbol": 1}).sort("symbol", 1)):
        wavy_daily(db, instrument["symbol"], 900, seed=k + 1)
    market.run_analytics(db)
    compute_and_store(db, "RELIANCE", "NSE", "1d")
    runner.compare(db, ["RELIANCE", "TCS"], ["ema_crossover", "rsi_reversal"])              # the "compare" batch behind the leaderboard
    db["benchmark_results"].insert_one({"run_id": "r1", "technology": "pandas", "operation": "resample", "record_count": 1000, "duration_seconds": 0.5,
                                        "is_synthetic": False, "created_at": datetime.now(timezone.utc)})
    return db, make_client(rate_limit_enabled=False)


def real_responses(db, client) -> dict:
    out = {}
    start = client.post("/api/processing/indicators", json={"symbols": ["TCS"]})
    out["processing_start"] = (start, start.json())
    upload = client.post("/api/imports/upload", files={"file": ("TCS_NSE.csv", YF_SIX_GOOD.encode(), "text/csv")},
                         data={"symbol": "TCS", "exchange": "NSE", "interval": "1d", "dry_run": "true"})
    out["upload_result"] = (upload, upload.json())
    run = client.post("/api/backtests/run", json={"symbol": "RELIANCE", "strategy": "ema_crossover", "params": {"fast": 10, "slow": 30}, "store": True})
    out["backtest_run"] = (run, run.json())
    gets = {
        "health": "/api/health", "instruments_list": "/api/instruments?page_size=5", "instrument_detail": "/api/instruments/RELIANCE",
        "prices": "/api/prices/RELIANCE?limit=5", "latest": "/api/prices/RELIANCE/latest", "indicators": "/api/indicators/RELIANCE?names=rsi_14&limit=5",
        "search": "/api/search/prices?symbols=RELIANCE&page_size=5", "sectors": "/api/sectors", "overview": "/api/market/overview",
        "compare": "/api/compare?symbols=RELIANCE,TCS,NIFTY_50", "stats": "/api/stats", "strategies": "/api/backtests/strategies",
        "backtests_list": "/api/backtests?page_size=5", "leaderboard": "/api/backtests/leaderboard", "jobs": "/api/jobs",
        "benchmarks": "/api/benchmarks", "error": "/api/prices/NOPE", "reports_catalog": "/api/reports",
    }
    for name, url in gets.items():
        r = client.get(url)
        out[name] = (r, r.json())
    return out


def test_every_endpoint_the_dashboard_uses_has_the_shape_it_expects(world):
    db, client = world
    responses = real_responses(db, client)
    assert set(responses) == set(NAMES), "contract.json and this test must cover the same endpoints"
    problems = []
    for name in NAMES:
        response, body = responses[name]
        expected = 404 if name == "error" else 202 if name == "processing_start" else 200
        if response.status_code != expected:
            problems.append(f"{name}: HTTP {response.status_code}, expected {expected}: {str(body)[:150]}")
            continue
        problems += [f"{name}: {e}" for e in check(body, CONTRACT[name])]
    assert not problems, "\n" + "\n".join(problems)


def test_the_backtest_and_leaderboard_shapes_hold_with_real_trades(world):
    """A guard against vacuous passes: the checks above only mean something if real data reached the lists."""
    db, client = world
    run = client.post("/api/backtests/run", json={"symbol": "RELIANCE", "strategy": "ema_crossover", "params": {"fast": 10, "slow": 30}}).json()
    assert run["trades"] and run["equity_curve"] and run["periods"]
    board = client.get("/api/backtests/leaderboard").json()
    assert {s["strategy"] for s in board["strategies"]} == {"ema_crossover", "rsi_reversal"}
    overview = client.get("/api/market/overview").json()
    assert overview["top_movers"]["1m"]["gainers"] and overview["sectors"]["sectors"] and overview["indices"]["indices"]
