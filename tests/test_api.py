"""Phase 10 tests: the REST API.

Every test runs the real application against a throwaway MongoDB database (BharatMarketTest_<random>, dropped afterwards)
and a temporary data folder, through FastAPI's TestClient. Real data is never touched. No network is used.
"""
import math
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from app.analytics import market
from app.api.deps import RateLimiter, get_database
from app.api.routes import imports
from app.api.serialize import SafeJSONResponse, to_json
from app.backtesting import runner
from app.core.config import Settings
from app.db.mongo import get_client
from app.db.setup import setup_database
from app.main import create_app

UTC = timezone.utc
YF_SIX_GOOD = "Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits\n" + "".join(
    f"2026-01-{d:02d} 00:00:00+05:30,100,105,99,104,103,1000,0,0\n" for d in range(5, 11))


# ------------------------------------------------------------------------------------------------ fixtures
@pytest.fixture()
def db():
    client = get_client()
    name = f"BharatMarketTest_{uuid.uuid4().hex[:8]}"
    assert name.startswith("BharatMarketTest_")
    database = client[name]
    setup_database(database)
    yield database
    client.drop_database(name)


@pytest.fixture()
def make_client(db, tmp_path, monkeypatch):
    """A factory: make_client(api_key=..., environment=..., rate_limit_per_minute=...) -> TestClient on the temp database."""
    def factory(**overrides) -> TestClient:
        settings = Settings(_env_file=None, data_dir=tmp_path / "data", **overrides)
        monkeypatch.setattr("app.api.deps.get_settings", lambda: settings)
        monkeypatch.setattr("app.api.routes.imports.get_settings", lambda: settings)
        app = create_app()
        app.dependency_overrides[get_database] = lambda: db
        return TestClient(app, raise_server_exceptions=False)
    return factory


@pytest.fixture()
def client(make_client):
    return make_client(rate_limit_enabled=False)


def put_daily(db, symbol, n=60, seed=1, start="2023-01-02", flags_on=None, volume=True):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, n)))
    dates = pd.bdate_range(start, periods=n, tz="UTC")
    docs = []
    for i, (d, c) in enumerate(zip(dates, close)):
        doc = {"symbol": symbol, "exchange": "NSE", "interval": "1d", "date": d.to_pydatetime(), "open": float(c * 0.999),
               "high": float(c * 1.01), "low": float(c * 0.99), "close": float(c), "adj_factor": 1.0, "source": "yfinance",
               "method": "provider_adjusted", "processed_at": datetime(2024, 1, 1, tzinfo=UTC)}
        if volume:
            doc["volume"] = int(rng.integers(1000, 9000))
        if flags_on is not None and i == flags_on:
            doc["quality_flags"] = ["return_outlier"]
        docs.append(doc)
    db["adjusted_prices"].insert_many(docs)
    return close


def wavy_daily(db, symbol, n=900, seed=7):
    rng = np.random.default_rng(seed)
    i = np.arange(n)
    close = np.maximum(100 + 25 * np.sin(i / 40) + 15 * np.sin(i / 11) + np.cumsum(rng.normal(0.05, 1.2, n)), 20)
    open_ = close + rng.normal(0, 0.6, n)
    high, low = np.maximum(open_, close) + rng.uniform(0.1, 1.5, n), np.minimum(open_, close) - rng.uniform(0.1, 1.5, n)
    dates = pd.bdate_range("2015-01-01", periods=n, tz="UTC")
    db["adjusted_prices"].insert_many([
        {"symbol": symbol, "exchange": "NSE", "interval": "1d", "date": d.to_pydatetime(), "open": float(o), "high": float(h), "low": float(l),
         "close": float(c), "volume": 50_000, "adj_factor": 1.0, "source": "yfinance", "method": "provider_adjusted",
         "processed_at": datetime(2024, 1, 1, tzinfo=UTC)} for d, o, h, l, c in zip(dates, open_, high, low, close)])


NEW_INSTRUMENT = {"symbol": "zzz", "name": "Zed Industries", "instrument_type": "equity", "sector": "Testing"}


# ------------------------------------------------------------------------------------------ plumbing
def test_health_and_request_id_headers(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert len(r.headers["x-request-id"]) >= 8 and r.headers["x-content-type-options"] == "nosniff" and "x-response-time-ms" in r.headers
    assert client.get("/api/health", headers={"X-Request-ID": "trace-abc-12345"}).headers["x-request-id"] == "trace-abc-12345"
    forged = client.get("/api/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert forged.headers["x-request-id"] != "bad id\twith spaces"


def test_every_error_has_the_same_envelope(client):
    for response, status, code in (
        (client.get("/api/nope"), 404, "not_found"),
        (client.get("/api/prices/NOPE"), 404, "not_found"),
        (client.get("/api/prices/bad symbol"), 422, "invalid_symbol"),
        (client.get("/api/prices/RELIANCE?interval=2d"), 422, "validation_error"),
        (client.get("/api/instruments?page_size=100000"), 422, "validation_error"),
        (client.delete("/api/health"), 405, "method_not_allowed"),
    ):
        assert response.status_code == status, response.text
        err = response.json()["error"]
        assert err["code"] == code and err["message"] and err["request_id"] == response.headers["x-request-id"]


def test_unexpected_failures_are_500_without_leaking_details(make_client):
    c = make_client(rate_limit_enabled=False)

    def boom():
        raise RuntimeError("secret-internal-detail mongodb://user:pass@host")

    def down():
        raise ServerSelectionTimeoutError("localhost:27017 timed out")

    c.app.add_api_route("/api/_boom", boom)
    c.app.add_api_route("/api/_down", down)
    r = c.get("/api/_boom")
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert "secret-internal-detail" not in r.text and "pass@host" not in r.text and r.json()["error"]["request_id"]
    d = c.get("/api/_down")
    assert d.status_code == 503 and d.json()["error"]["code"] == "database_unavailable" and "27017" not in d.text


def test_json_safety_nan_infinity_datetimes_and_objectids():
    out = to_json({"_id": ObjectId(), "a": float("nan"), "b": float("inf"), "c": np.float64("-inf"), "d": np.int64(5),
                   "e": datetime(2024, 1, 2, 3, 4, tzinfo=UTC), "f": pd.Timestamp("2024-01-02", tz="UTC"), "g": [np.nan, 1.5],
                   "h": np.bool_(True), "i": datetime(2024, 1, 2), "j": pd.NaT})
    assert out == {"a": None, "b": None, "c": None, "d": 5, "e": "2024-01-02T03:04:00Z", "f": "2024-01-02T00:00:00Z",
                   "g": [None, 1.5], "h": True, "i": "2024-01-02T00:00:00Z", "j": None}
    body = SafeJSONResponse({"x": float("nan"), "when": datetime(2024, 1, 1, tzinfo=UTC)}).body
    assert body == b'{"x":null,"when":"2024-01-01T00:00:00Z"}'


# ------------------------------------------------------------------------------------------- instruments
def test_instrument_list_search_filter_and_pagination(client):
    first = client.get("/api/instruments?page_size=5").json()
    assert first["total"] == 16 and len(first["items"]) == 5 and first["pages"] == 4 and first["page"] == 1
    symbols = [i["symbol"] for i in first["items"]]
    assert symbols == sorted(symbols)
    last = client.get("/api/instruments?page_size=5&page=4").json()
    assert len(last["items"]) == 1 and client.get("/api/instruments?page_size=5&page=9").json()["items"] == []
    assert client.get("/api/instruments?instrument_type=index").json()["total"] == 6
    assert [i["symbol"] for i in client.get("/api/instruments?q=rel").json()["items"]] == ["RELIANCE"]
    assert [i["symbol"] for i in client.get("/api/instruments?q=tata").json()["items"]] == ["TCS"]      # matches inside the name
    assert client.get("/api/instruments?q=zzzz").json()["total"] == 0
    assert client.get("/api/instruments?q=.*").json()["total"] == 0          # regex characters are matched literally, never executed
    assert client.get("/api/instruments?instrument_type=equity&active=false").json()["total"] == 0


def test_instrument_detail_includes_data_coverage(client, db):
    put_daily(db, "RELIANCE", 30)
    detail = client.get("/api/instruments/reliance").json()
    assert detail["symbol"] == "RELIANCE" and detail["coverage"]["daily"]["bars"] == 30
    assert client.get("/api/instruments/TCS").json()["coverage"]["daily"] is None


def test_instrument_create_update_and_validation(client, db):
    created = client.post("/api/instruments", json=NEW_INSTRUMENT)
    assert created.status_code == 201 and created.json()["symbol"] == "ZZZ" and created.json()["currency"] == "INR"
    assert db["instruments"].count_documents({"symbol": "ZZZ"}) == 1
    assert client.post("/api/instruments", json=NEW_INSTRUMENT).status_code == 409
    assert client.post("/api/instruments", json={**NEW_INSTRUMENT, "symbol": "bad symbol!"}).json()["error"]["code"] == "invalid_symbol"
    assert client.post("/api/instruments", json={**NEW_INSTRUMENT, "symbol": "QQQ", "surprise": 1}).status_code == 422
    assert client.post("/api/instruments", json={"symbol": "QQQ", "name": "x", "instrument_type": "future"}).status_code == 422
    assert client.post("/api/instruments", json={**NEW_INSTRUMENT, "symbol": "QQQ", "isin": "nope"}).status_code == 422
    changed = client.patch("/api/instruments/zzz", json={"sector": "Retail", "active": False})
    assert changed.status_code == 200 and changed.json()["sector"] == "Retail" and changed.json()["active"] is False
    assert client.patch("/api/instruments/ZZZ", json={"sector": None}).json().get("sector") is None
    assert client.patch("/api/instruments/ZZZ", json={}).status_code == 422
    assert client.patch("/api/instruments/ZZZ", json={"name": None}).json()["error"]["code"] == "invalid_value"
    assert client.patch("/api/instruments/NOSUCH", json={"sector": "x"}).status_code == 404
    assert client.patch("/api/instruments/ZZZ", json={"symbol": "NEW"}).status_code == 422        # the symbol cannot be changed


def test_sectors(client, db):
    sectors = client.get("/api/sectors").json()
    assert sectors["unassigned"] == [] and any(s["sector"] and s["count"] for s in sectors["sectors"])
    assert client.get("/api/sectors/performance").json()["error"]["code"] == "not_computed"
    market.store_result(db, "sector_performance", {"snapshot": "latest", "as_of": "2024-01-02"}, {"ranking_by_1m": ["IT"], "sectors": {}})
    perf = client.get("/api/sectors/performance").json()
    assert perf["ranking_by_1m"] == ["IT"] and perf["snapshot"] == "latest"


# ------------------------------------------------------------------------------------------------ prices
def test_prices_order_range_and_cursor_pagination(client, db):
    put_daily(db, "RELIANCE", 30)
    newest = client.get("/api/prices/reliance?limit=3").json()
    assert newest["count"] == 3 and newest["basis"] == "adjusted" and newest["next_cursor"]
    stamps = [b["ts"] for b in newest["items"]]
    assert stamps == sorted(stamps, reverse=True) and newest["items"][0]["close"] > 0 and "date" not in newest["items"][0]
    for order in ("desc", "asc"):                         # following the cursor visits every bar exactly once
        seen, cursor = [], None
        for _ in range(20):
            page = client.get("/api/prices/RELIANCE", params={"limit": 7, "order": order, **({"cursor": cursor} if cursor else {})}).json()
            seen += [b["ts"] for b in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 30 and seen == sorted(seen, reverse=(order == "desc"))
    window = client.get("/api/prices/RELIANCE?start=2023-01-10&end=2023-01-13&order=asc").json()
    assert [b["ts"][:10] for b in window["items"]] == ["2023-01-10", "2023-01-11", "2023-01-12", "2023-01-13"]
    assert client.get("/api/prices/RELIANCE?start=2023-02-01&end=2023-01-01").json()["error"]["code"] == "invalid_range"
    assert client.get("/api/prices/RELIANCE?limit=0").status_code == 422 and client.get("/api/prices/RELIANCE?limit=5001").status_code == 422
    assert client.get("/api/prices/TCS").json()["items"] == []                           # known instrument, no data yet


def test_raw_prices_require_a_source(client, db):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    db["stock_prices"].insert_one({"symbol": "RELIANCE", "exchange": "NSE", "interval": "1d", "date": datetime(2026, 1, 5, tzinfo=UTC),
                                   "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 10, "source": "kaggle",
                                   "retrieved_at": now, "is_synthetic": False})
    assert client.get("/api/prices/RELIANCE?basis=raw").json()["error"]["code"] == "source_required"
    raw = client.get("/api/prices/RELIANCE?basis=raw&source=kaggle").json()
    assert raw["basis"] == "raw" and raw["count"] == 1 and raw["items"][0]["source"] == "kaggle"
    assert client.get("/api/prices/RELIANCE?basis=raw&source=Robert'); DROP").status_code == 422     # source is pattern-checked


def test_intraday_prices(client, db):
    base = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)
    db["intraday_prices"].insert_many([{"ts": base + timedelta(minutes=5 * i), "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i,
                                        "volume": 0, "meta": {"symbol": "NIFTY_50", "exchange": "NSE", "interval": "5m", "instrument_type": "index",
                                                              "source": "kaggle", "is_synthetic": False, "price_basis": "index_level"}} for i in range(6)])
    r = client.get("/api/prices/NIFTY_50?interval=5m&limit=4&order=asc").json()
    assert r["basis"] == "raw" and r["count"] == 4 and r["items"][0]["ts"] == "2026-01-05T03:45:00Z" and r["items"][0]["source"] == "kaggle"
    nxt = client.get("/api/prices/NIFTY_50", params={"interval": "5m", "limit": 4, "order": "asc", "cursor": r["next_cursor"]}).json()
    assert nxt["count"] == 2 and nxt["next_cursor"] is None
    assert client.get("/api/prices/NIFTY_50?interval=1m").json()["items"] == []


def test_latest_price(client, db):
    close = put_daily(db, "RELIANCE", 30)
    latest = client.get("/api/prices/RELIANCE/latest").json()
    assert latest["close"] == pytest.approx(close[-1]) and latest["previous_close"] == pytest.approx(close[-2])
    assert latest["change_pct"] == pytest.approx((close[-1] / close[-2] - 1) * 100)
    assert client.get("/api/prices/TCS/latest").json()["error"]["code"] == "no_data"


def test_historical_search(client, db):
    put_daily(db, "TCS", 60, seed=1, flags_on=10)
    put_daily(db, "INFY", 60, seed=2)
    everything = client.get("/api/search/prices?page_size=20").json()
    assert everything["total"] == 120 and everything["pages"] == 6
    only = client.get("/api/search/prices?symbols=tcs").json()
    assert only["total"] == 60 and {b["symbol"] for b in only["items"]} == {"TCS"}
    closes = [b["close"] for b in client.get("/api/search/prices?symbols=TCS,INFY&page_size=500").json()["items"]]
    cutoff = float(np.median(closes))
    high = client.get(f"/api/search/prices?min_close={cutoff}&page_size=500").json()
    assert 0 < high["total"] < 120 and all(b["close"] >= cutoff for b in high["items"])
    assert client.get("/api/search/prices?flag=return_outlier").json()["total"] == 1
    ranked = client.get("/api/search/prices?sort_by=close&order=desc&page_size=3").json()["items"]
    assert [b["close"] for b in ranked] == sorted([b["close"] for b in ranked], reverse=True)
    assert client.get("/api/search/prices?min_close=200&max_close=100").json()["error"]["code"] == "invalid_range"
    assert client.get("/api/search/prices?flag=bogus").status_code == 422
    assert client.get("/api/search/prices?symbols=" + ",".join(f"S{i}" for i in range(51))).json()["error"]["code"] == "too_many_items"


# -------------------------------------------------------------------------------------------- indicators
def test_indicators_compute_read_and_validate(client, db):
    put_daily(db, "TCS", 300)
    assert client.get("/api/indicators").json()["indicators"][:2] == ["sma_20", "sma_50"]
    assert client.get("/api/indicators/TCS").json()["items"] == []
    computed = client.post("/api/indicators/TCS/compute")
    assert computed.status_code == 200 and computed.json()["status"] == "stored" and computed.json()["documents"] > 0
    r = client.get("/api/indicators/tcs?names=rsi_14,ema_20&limit=5").json()
    assert r["count"] == 5 and r["indicators"] == ["rsi_14", "ema_20"]
    assert all(set(i) == {"ts", "rsi_14", "ema_20"} and 0 <= i["rsi_14"] <= 100 for i in r["items"])
    again = client.post("/api/indicators/TCS/compute")
    assert again.status_code == 200 and db["indicators"].count_documents({"meta.symbol": "TCS"}) == computed.json()["documents"]   # no duplicates
    assert client.get("/api/indicators/TCS?names=rsi_14,bogus").json()["error"]["code"] == "unknown_indicator"
    assert client.post("/api/indicators/INFY/compute").json()["error"]["code"] == "no_data"
    assert client.post("/api/indicators/NOSUCH/compute").status_code == 404


# ---------------------------------------------------------------------------------------------- analytics
def test_stored_analytics_and_market_overview(client, db):
    assert client.get("/api/market/overview").json()["error"]["code"] == "not_computed"
    scope = {"snapshot": "latest", "as_of": "2026-11-20"}
    market.store_result(db, "trend", scope, {"universe": {"stocks": 10}, "stock_summary": {"bullish": 6, "neutral": 2, "bearish": 2}})
    market.store_result(db, "market_breadth", scope, {"advancers": 7, "decliners": 3})
    market.store_result(db, "outliers", {"history": "all"}, {"rows": []})
    market.store_result(db, "spark_market_trend", {"engine": "spark"}, {"days": 5})
    assert client.get("/api/analytics/trend").json()["result"]["stock_summary"]["bullish"] == 6
    assert client.get("/api/analytics/outliers").json()["scope"] == {"history": "all"}
    assert client.get("/api/analytics/spark_market_trend").json()["result"]["days"] == 5
    listing = client.get("/api/analytics").json()
    assert listing["count"] == 4 and "result" not in listing["items"][0]
    missing = client.get("/api/analytics/correlation").json()["error"]
    assert missing["code"] == "not_found" and "trend" in missing["details"]["available"]
    assert client.get("/api/analytics/Bad-Type").json()["error"]["code"] == "invalid_analysis_type"
    overview = client.get("/api/market/overview").json()
    assert overview["as_of"] == "2026-11-20" and overview["trend"]["bullish"] == 6 and overview["breadth"]["advancers"] == 7
    assert client.get("/api/market/overview?snapshot=full_universe").json()["error"]["code"] == "not_computed"


def test_compare_instruments(client, db):
    a = put_daily(db, "RELIANCE", 120, seed=1)
    put_daily(db, "TCS", 120, seed=2)
    r = client.get("/api/compare?symbols=reliance,tcs").json()
    assert r["symbols"] == ["RELIANCE", "TCS"] and r["period"]["bars"] == 120
    assert r["stats"]["RELIANCE"]["total_return_pct"] == pytest.approx((a[-1] / a[0] - 1) * 100, abs=1e-3)
    assert r["stats"]["TCS"]["max_drawdown_pct"] <= 0 and r["stats"]["TCS"]["volatility_pct"] > 0
    assert r["correlation"]["RELIANCE"]["RELIANCE"] == 1.0 and r["correlation"]["RELIANCE"]["TCS"] == r["correlation"]["TCS"]["RELIANCE"]
    assert r["rebased_series"][0]["RELIANCE"] == 100.0 and r["rebased_series"][0]["TCS"] == 100.0
    narrow = client.get("/api/compare?symbols=RELIANCE,TCS&start=2023-03-01&end=2023-04-28").json()
    assert 20 <= narrow["period"]["bars"] < 120 and narrow["period"]["start"] >= "2023-03-01"
    assert client.get("/api/compare?symbols=RELIANCE").json()["error"]["code"] == "too_few_symbols"
    assert client.get("/api/compare?symbols=RELIANCE,RELIANCE").json()["error"]["code"] == "too_few_symbols"
    assert client.get("/api/compare?symbols=RELIANCE,NOSUCH").status_code == 404
    assert client.get("/api/compare?symbols=" + ",".join(f"S{i}" for i in range(11))).json()["error"]["code"] == "too_many_items"
    put_daily(db, "INFY", 60, seed=3, start="2025-06-02")                                # no dates in common with RELIANCE
    assert client.get("/api/compare?symbols=RELIANCE,INFY").json()["error"]["code"] == "not_enough_common_data"
    assert client.get("/api/compare?symbols=RELIANCE,SBIN").json()["error"]["code"] == "no_data"


# ----------------------------------------------------------------------------------- imports and jobs
def upload(client, name="prices.csv", content=YF_SIX_GOOD, data=None, ctype="text/csv"):
    payload = content.encode() if isinstance(content, str) else content
    return client.post("/api/imports/upload", files={"file": (name, payload, ctype)}, data={"symbol": "TCS", "interval": "1d", "exchange": "NSE", **(data or {})})


def test_upload_imports_a_file_once(client, db, tmp_path):
    r = upload(client, "../../my evil name.csv")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed" and body["counts"]["inserted"] == 6 and "path" not in body and body["file"] == "my evil name.csv"
    assert db["stock_prices"].count_documents({"symbol": "TCS"}) == 6
    uploads = tmp_path / "data" / "uploads"
    folders = list(uploads.iterdir())
    assert len(folders) == 1 and folders[0].parent == uploads                                # a private random folder per upload
    saved = list(folders[0].iterdir())
    assert [p.name for p in saved] == ["my_evil_name.csv"] and uploads in saved[0].resolve().parents   # sanitised name, inside uploads
    assert upload(client).json()["status"] == "already_imported"
    assert db["stock_prices"].count_documents({"symbol": "TCS"}) == 6
    jobs = client.get("/api/imports").json()
    assert jobs["total"] >= 1 and client.get(f"/api/jobs/{jobs['items'][0]['job_id']}").json()["job_type"] == "import"


def test_upload_dry_run_stores_nothing(client, db, tmp_path):
    r = upload(client, data={"dry_run": "true"})
    assert r.status_code == 200 and r.json()["status"] == "dry_run"
    assert db["stock_prices"].count_documents({}) == 0 and list((tmp_path / "data" / "uploads").iterdir()) == []


def test_upload_is_checked_before_anything_is_read(make_client, db, tmp_path):
    c = make_client(rate_limit_enabled=False, max_upload_mb=1)
    uploads = tmp_path / "data" / "uploads"
    cases = [(upload(c, "prices.txt"), 422, "unsupported_file_type"), (upload(c, "prices.exe", b"MZ"), 422, "unsupported_file_type"),
             (upload(c, "book.xlsx", "this is plain text"), 422, "not_an_xlsx"), (upload(c, "empty.csv", ""), 422, "empty_file"),
             (upload(c, "bin.csv", b"Date,Open\n\x00\x01\x02"), 422, "not_a_csv"),
             (upload(c, "big.csv", "Date,Open,High,Low,Close,Volume\n" + "x" * (1024 * 1024 + 10)), 413, "file_too_large"),
             (upload(c, "junk.csv", "a,b,c\n1,2,3\n"), 422, "import_failed"),
             (upload(c, data={"symbol": "ZZZQ"}), 409, "unknown_instrument"),
             (upload(c, data={"symbol": "bad symbol"}), 422, "invalid_symbol"),
             (upload(c, data={"interval": "7d"}), 422, "validation_error")]
    for response, status, code in cases:
        assert response.status_code == status and response.json()["error"]["code"] == code, (code, response.text)
    no_exchange = c.post("/api/imports/upload", files={"file": ("prices.csv", YF_SIX_GOOD.encode(), "text/csv")}, data={"symbol": "TCS", "interval": "1d"})
    assert no_exchange.status_code == 422 and no_exchange.json()["error"]["code"] == "exchange_unknown"      # guessed from the file name: nonsense
    assert not uploads.exists() or list(uploads.iterdir()) == []                            # rejected files are never kept
    assert db["stock_prices"].count_documents({}) == 0
    assert upload(c, data={"symbol": "ZZZQ", "auto_register": "true"}).json()["status"] == "completed"   # opt-in registration works


def test_jobs_listing_and_lookup(client, db):
    now = datetime.now(UTC)
    for i, (jt, st) in enumerate((("clean", "completed"), ("analytics", "failed"), ("import", "running"))):
        db["processing_jobs"].insert_one({"job_id": f"job_{i}", "job_type": jt, "status": st, "progress": 0.0, "created_at": now + timedelta(seconds=i)})
    jobs = client.get("/api/jobs").json()
    assert jobs["total"] == 3 and jobs["items"][0]["job_id"] == "job_2"                      # newest first
    assert client.get("/api/jobs?status=failed").json()["total"] == 1 and client.get("/api/jobs?job_type=clean").json()["total"] == 1
    assert client.get("/api/jobs/job_1").json()["status"] == "failed" and client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/bad%20id").status_code == 422 and client.get("/api/jobs?status=weird").status_code == 422


def test_processing_runs_in_the_background_and_reports_status(client, db):
    put_daily(db, "TCS", 300)
    r = client.post("/api/processing/indicators", json={"symbols": ["tcs"]})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    job = client.get(r.json()["status_url"]).json()
    assert job["status"] == "completed" and job["progress"] == 100.0 and job["counts"]["documents"] > 0 and job["job_type"] == "indicators"
    assert db["indicators"].count_documents({"meta.symbol": "TCS"}) == job["counts"]["documents"]
    assert client.post("/api/processing/adjust", json={"symbols": ["TCS"]}).json()["error"]["code"] == "symbols_not_supported"
    assert client.post("/api/processing/bogus").status_code == 422
    assert client.post("/api/processing/indicators", json={"symbols": ["x"] * 51}).status_code == 422
    assert client.post("/api/processing/indicators", json={"surprise": 1}).status_code == 422


def test_failed_processing_is_recorded_not_hidden(client, db, monkeypatch):
    def broken(db_, job_id, params):
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(imports.STEPS, "analytics", (broken, "analytics"))
    r = client.post("/api/processing/analytics")
    assert r.status_code == 202
    job = client.get(r.json()["status_url"]).json()
    assert job["status"] == "failed" and "disk on fire" in job["error"] and "finished_at" not in job


def test_only_one_run_of_a_step_at_a_time(client, db):
    now = datetime.now(UTC)
    db["processing_jobs"].insert_one({"job_id": "busy_1", "job_type": "analytics", "status": "running", "progress": 5.0, "created_at": now,
                                      "updated_at": now, "params": {"step": "analytics"}})
    r = client.post("/api/processing/analytics")
    assert r.status_code == 409 and r.json()["error"]["details"]["job_id"] == "busy_1"
    db["processing_jobs"].update_one({"job_id": "busy_1"}, {"$set": {"updated_at": now - timedelta(hours=3)}})   # a dead job (server restart)
    assert client.post("/api/processing/analytics").status_code == 202


# ----------------------------------------------------------------------------- write access and limits
def test_write_endpoints_need_the_api_key_when_one_is_set(make_client, db):
    c = make_client(rate_limit_enabled=False, api_key="s3cret-key")
    assert c.get("/api/instruments").status_code == 200                                      # reading stays open
    no_key = c.post("/api/instruments", json=NEW_INSTRUMENT)
    assert no_key.status_code == 401 and no_key.json()["error"]["code"] == "unauthorized" and no_key.headers["www-authenticate"] == "ApiKey"
    assert c.post("/api/instruments", json=NEW_INSTRUMENT, headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.post("/api/instruments", json=NEW_INSTRUMENT, headers={"X-API-Key": "s3cret-key"}).status_code == 201
    for method, url, kw in (("patch", "/api/instruments/ZZZ", {"json": {"sector": "x"}}), ("post", "/api/processing/analytics", {}),
                            ("post", "/api/indicators/TCS/compute", {}), ("post", "/api/imports/upload", {"files": {"file": ("a.csv", b"x")}}),
                            ("delete", "/api/backtests/abc", {})):
        assert getattr(c, method)(url, **kw).status_code == 401, url
    assert "s3cret-key" not in no_key.text


def test_writes_are_disabled_in_production_without_a_key_and_open_in_development(make_client, db):
    prod = make_client(rate_limit_enabled=False, environment="production")
    r = prod.post("/api/instruments", json=NEW_INSTRUMENT)
    assert r.status_code == 403 and r.json()["error"]["code"] == "write_disabled" and prod.get("/api/instruments").status_code == 200
    dev = make_client(rate_limit_enabled=False, environment="development")
    assert dev.post("/api/instruments", json=NEW_INSTRUMENT).status_code == 201


def test_rate_limit_returns_429_with_retry_after(make_client):
    c = make_client(rate_limit_per_minute=3)
    assert [c.get("/api/stats").status_code for _ in range(3)] == [200, 200, 200]
    blocked = c.get("/api/stats")
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(blocked.headers["retry-after"]) <= 60 and blocked.json()["error"]["details"]["limit_per_minute"] == 3
    assert all(c.get("/api/health").status_code == 200 for _ in range(10))                   # monitors are never limited


def test_heavy_endpoints_have_their_own_smaller_limit(make_client):
    c = make_client(rate_limit_per_minute=1000, heavy_rate_limit_per_minute=2)
    codes = [c.post("/api/backtests/run", json={"symbol": "TCS", "strategy": "nope"}).status_code for _ in range(3)]
    assert codes == [422, 422, 429] and c.get("/api/instruments").status_code == 200
    sweep = c.post("/api/backtests/sweep", json={"symbol": "TCS", "strategy": "nope", "grid": {"fast": [1]}})
    assert sweep.status_code == 429                                                          # the allowance is shared by heavy endpoints


def test_rate_limiter_window_and_isolation():
    limiter = RateLimiter(2, window_seconds=60)
    assert limiter.check("a", now=0) == (True, 0) and limiter.check("a", now=10) == (True, 0)
    allowed, retry = limiter.check("a", now=20)
    assert not allowed and retry == 40
    assert limiter.check("b", now=20)[0]                                                    # another client is unaffected
    assert limiter.check("a", now=61)[0]                                                     # the oldest hit has expired


def test_rate_limit_can_be_disabled(make_client):
    c = make_client(rate_limit_enabled=False, rate_limit_per_minute=1)
    assert all(c.get("/api/stats").status_code == 200 for _ in range(5))


# ------------------------------------------------------------------------------------------- backtests
def test_strategies_and_cost_presets(client):
    r = client.get("/api/backtests/strategies").json()
    assert len(r["strategies"]) == 7 and {"ema_crossover", "breakout_retest", "pivot_trendline"} <= {s["name"] for s in r["strategies"]}
    assert set(r["cost_presets"]) == {"delivery", "intraday", "none", "stress"} and r["strategies"][0]["defaults"]


def test_backtest_run_without_storing(client, db):
    wavy_daily(db, "RELIANCE")
    r = client.post("/api/backtests/run", json={"symbol": "reliance", "strategy": "ema_crossover", "params": {"fast": 10, "slow": 30}})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["stored"] is False and doc["params"]["fast"] == 10 and doc["metrics"]["total_trades"] > 0 and doc["trades"]
    assert doc["benchmark"] and doc["periods"]["out_of_sample"] and any("survivorship" in w for w in doc["warnings"])
    assert db["backtest_results"].count_documents({}) == 0 and db["trading_signals"].count_documents({}) == 0
    slim = client.post("/api/backtests/run", json={"symbol": "RELIANCE", "strategy": "rsi_reversal", "include_trades": False,
                                                    "cost_preset": "stress", "slippage_bps": 10, "start": "2017-01-01", "end": "2018-12-31"}).json()
    assert "trades" not in slim and slim["costs"]["model"]["name"] == "stress" and slim["start"].startswith("2017")


def test_backtest_run_validation(client, db):
    wavy_daily(db, "RELIANCE", 400)
    run = lambda **kw: client.post("/api/backtests/run", json={"symbol": "RELIANCE", "strategy": "ema_crossover", **kw})  # noqa: E731
    unknown = run(strategy="moon_phase").json()["error"]
    assert unknown["code"] == "unknown_strategy" and "ema_crossover" in unknown["details"]["available"]
    assert client.post("/api/backtests/run", json={"symbol": "NOSUCH", "strategy": "ema_crossover"}).status_code == 404
    assert run(params={"fast": 60, "slow": 50}).json()["error"]["code"] == "invalid_backtest"
    assert run(params={"bogus": 1}).json()["error"]["code"] == "invalid_backtest"
    assert run(interval="5m").json()["error"]["code"] == "range_required"
    assert run(interval="5m", start="2020-01-01", end="2023-01-01").json()["error"]["code"] == "range_required"
    for bad in ({"initial_capital": 5}, {"slippage_bps": -1}, {"position_pct": 0}, {"position_pct": 101}, {"risk_pct": 0}, {"cost_preset": "free"},
                {"surprise": 1}, {"params": {"fast": "ten"}}):
        assert run(**bad).status_code == 422, bad
    assert client.post("/api/backtests/run", json={"symbol": "RELIANCE"}).status_code == 422
    put_daily(db, "TCS", 30)
    assert client.post("/api/backtests/run", json={"symbol": "TCS", "strategy": "ema_crossover"}).json()["error"]["code"] == "invalid_backtest"


def test_stored_backtests_can_be_listed_read_and_deleted(make_client, db):
    c = make_client(rate_limit_enabled=False, api_key="k")
    wavy_daily(db, "RELIANCE")
    denied = c.post("/api/backtests/run", json={"symbol": "RELIANCE", "strategy": "ema_crossover", "store": True})
    assert denied.status_code == 401                                                         # storing needs the key, previewing does not
    assert c.post("/api/backtests/run", json={"symbol": "RELIANCE", "strategy": "ema_crossover"}).status_code == 200
    key = {"X-API-Key": "k"}
    run = c.post("/api/backtests/run", json={"symbol": "RELIANCE", "strategy": "ema_crossover", "store": True}, headers=key).json()
    other = c.post("/api/backtests/run", json={"symbol": "RELIANCE", "strategy": "ma_trend_following", "store": True, "params": {"ma": 60}}, headers=key).json()
    assert run["stored"] and db["backtest_results"].count_documents({}) == 2
    listing = c.get("/api/backtests").json()
    assert listing["total"] == 2 and "trades" not in listing["items"][0] and "equity_curve" not in listing["items"][0]
    assert listing["items"][0]["metrics"]["total_trades"] is not None and "benchmark" in listing["items"][0]
    assert c.get("/api/backtests?strategy=ema_crossover").json()["total"] == 1 and c.get("/api/backtests?symbol=tcs").json()["total"] == 0
    by_return = c.get("/api/backtests?sort_by=total_return_pct&order=desc").json()["items"]
    assert by_return[0]["metrics"]["total_return_pct"] >= by_return[1]["metrics"]["total_return_pct"]
    detail = c.get(f"/api/backtests/{run['run_id']}").json()
    assert detail["run_id"] == run["run_id"] and "trades" not in detail and detail["equity_curve"]
    assert c.get(f"/api/backtests/{run['run_id']}?include_trades=true").json()["trades"]
    total = run["trades_total"]
    pages = [c.get(f"/api/backtests/{run['run_id']}/trades", params={"page": p, "page_size": 10}).json() for p in range(1, 4)]
    assert pages[0]["total"] == total and len(pages[0]["items"]) == min(10, total) and pages[0]["items"][0]["entry_ts"]
    assert sum(len(p["items"]) for p in pages) == min(total, 30)
    signals = c.get(f"/api/backtests/{run['run_id']}/signals?page_size=5").json()
    assert signals["total"] > 0 and signals["items"][0]["run_id"] == run["run_id"] and signals["items"][0]["signal"] in ("buy", "exit_long")
    assert c.get("/api/backtests/bad%20id").status_code == 422 and c.get("/api/backtests/nope").status_code == 404
    assert c.get("/api/backtests/nope/trades").status_code == 404 and c.get("/api/backtests/nope/signals").status_code == 404
    assert c.delete(f"/api/backtests/{run['run_id']}").status_code == 401
    gone = c.delete(f"/api/backtests/{run['run_id']}", headers=key).json()
    assert gone["deleted"] == run["run_id"] and gone["signals_deleted"] > 0
    assert c.get(f"/api/backtests/{run['run_id']}").status_code == 404 and db["trading_signals"].count_documents({"run_id": run["run_id"]}) == 0
    assert c.delete(f"/api/backtests/{run['run_id']}", headers=key).status_code == 404
    assert db["backtest_results"].count_documents({"run_id": other["run_id"]}) == 1


def test_parameter_sweep(client, db):
    wavy_daily(db, "RELIANCE")
    r = client.post("/api/backtests/sweep", json={"symbol": "RELIANCE", "strategy": "ema_crossover", "grid": {"fast": [5, 10, 20, 60], "slow": [30, 50, 100]}})
    assert r.status_code == 200, r.text
    out = r.json()
    valid = [(f, s) for f in (5, 10, 20, 60) for s in (30, 50, 100) if f < s]                # 60 only fits under 100
    assert out["combinations"] == len(valid) == 10 and len(out["table"]) == 10 and {"p_fast", "oos_objective", "is_objective"} <= set(out["table"][0])
    assert all(row["p_fast"] < row["p_slow"] for row in out["table"]) and out["split_date"] and "buy_hold_oos_return_pct" in out
    big = {"fast": list(range(1, 6)), "slow": list(range(10, 15)), "atr_stop_mult": [1, 2, 3, 4, 5], "target_r": [1, 2, 3, 4, 5]}
    assert client.post("/api/backtests/sweep", json={"symbol": "RELIANCE", "strategy": "ema_crossover", "grid": big}).json()["error"]["code"] == "grid_too_large"
    assert client.post("/api/backtests/sweep", json={"symbol": "RELIANCE", "strategy": "ema_crossover", "grid": {"fast": list(range(21))}}).json()["error"]["code"] == "invalid_grid"
    assert client.post("/api/backtests/sweep", json={"symbol": "RELIANCE", "strategy": "ema_crossover", "grid": {"fast": []}}).json()["error"]["code"] == "invalid_grid"
    assert client.post("/api/backtests/sweep", json={"symbol": "RELIANCE", "strategy": "x", "grid": {"fast": [1]}}).json()["error"]["code"] == "unknown_strategy"
    assert client.post("/api/backtests/sweep", json={"symbol": "RELIANCE", "strategy": "ema_crossover", "grid": {"a": [1], "b": [1], "c": [1], "d": [1], "e": [1]}}).status_code == 422


def test_leaderboard_summarises_a_comparison_batch(client, db):
    assert client.get("/api/backtests/leaderboard").json()["error"]["code"] == "no_runs"
    wavy_daily(db, "RELIANCE", seed=1)
    wavy_daily(db, "TCS", seed=2)
    runner.compare(db, ["RELIANCE", "TCS"], ["ema_crossover", "rsi_reversal"])
    board = client.get("/api/backtests/leaderboard").json()
    assert board["runs"] == 4 and {s["strategy"] for s in board["strategies"]} == {"ema_crossover", "rsi_reversal"}
    first = board["strategies"][0]
    assert first["stocks"] == 2 and 0 <= first["beat_buy_and_hold"] <= 2 and "median_sharpe" in first and "survivorship" in board["warning"]
    assert not math.isnan(first["median_total_return_pct"] or 0)


# ---------------------------------------------------------------------------------- system endpoints
def test_stats_benchmarks_and_quality_reports(client, db):
    put_daily(db, "RELIANCE", 30)
    stats = client.get("/api/stats").json()
    assert stats["collections"]["instruments"] == 16 and stats["collections"]["adjusted_prices"] == 30
    assert stats["daily_prices"]["symbols"] == 1 and stats["size_mb"]["storage"] >= 0
    now = datetime.now(UTC)
    for tech, op in (("pandas", "resample"), ("pyspark", "resample"), ("pandas", "returns")):
        db["benchmark_results"].insert_one({"run_id": "r1", "technology": tech, "operation": op, "record_count": 10, "duration_seconds": 1.5,
                                            "is_synthetic": False, "created_at": now})
    assert client.get("/api/benchmarks").json()["total"] == 3
    assert client.get("/api/benchmarks?technology=pandas").json()["total"] == 2 and client.get("/api/benchmarks?operation=returns").json()["total"] == 1
    assert client.get("/api/benchmarks?technology=excel").status_code == 422
    db["data_quality_reports"].insert_one({"dataset": "processed:audit", "totals": {"rows": 5}, "issues": [{"x": 1}], "created_at": now})
    slim = client.get("/api/quality/reports").json()["items"][0]
    assert slim["totals"]["rows"] == 5 and "issues" not in slim
    assert client.get("/api/quality/reports?include_issues=true").json()["items"][0]["issues"] == [{"x": 1}]
    assert client.get("/api/quality/reports?dataset=nothing").json()["total"] == 0


def test_openapi_documents_every_endpoint(client):
    spec = client.get("/openapi.json").json()
    operations = [(m, p) for p, ops in spec["paths"].items() for m in ops]
    assert len(operations) >= 34
    assert all(spec["paths"][p][m].get("summary") for m, p in operations)
