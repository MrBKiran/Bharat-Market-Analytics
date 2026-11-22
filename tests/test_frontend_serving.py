"""Phase 12 tests: the API process serves the built dashboard at one address, safely.

Uses a tiny fake build (index.html + assets/) in a temporary folder, so no Node, no database and no real build is needed.
"""
import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.frontend import ASSET_CACHE, content_security_policy
from app.main import create_app

INLINE = "try{document.documentElement.setAttribute('data-theme','dark')}catch(e){}"
INDEX = f"""<!doctype html><html><head><title>BharatMarket</title><script>{INLINE}</script></head>
<body><div id="root"></div><script type="module" src="/assets/app-abc123.js"></script></body></html>"""


def sha(code: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(code.encode()).digest()).decode() + "'"


@pytest.fixture()
def dist(tmp_path):
    folder = tmp_path / "dist"
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text(INDEX, encoding="utf-8")
    (folder / "assets" / "app-abc123.js").write_text("console.log('app')", encoding="utf-8")
    (folder / "assets" / "style-abc123.css").write_text("body{margin:0}", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")          # outside the build: must never be served
    return folder


@pytest.fixture()
def make_client(monkeypatch):
    def factory(frontend_dist, serve=True) -> TestClient:
        settings = Settings(_env_file=None, serve_frontend=serve, frontend_dist=frontend_dist, rate_limit_enabled=False)
        monkeypatch.setattr("app.main.get_settings", lambda: settings)
        monkeypatch.setattr("app.api.deps.get_settings", lambda: settings)
        return TestClient(create_app(), raise_server_exceptions=False)
    return factory


# ------------------------------------------------------------------------------------------------- pages
def test_the_root_serves_the_dashboard_page_with_a_strict_policy(make_client, dist):
    r = make_client(dist).get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert '<div id="root">' in r.text and "/assets/app-abc123.js" in r.text
    assert r.headers["cache-control"] == "no-cache"                                       # a new build shows up at once
    csp = r.headers["content-security-policy"]
    assert f"script-src 'self' {sha(INLINE)}" in csp                                      # the one inline script, allowed by its hash
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]               # ...and no blanket permission for scripts
    for rule in ("default-src 'self'", "connect-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "base-uri 'self'"):
        assert rule in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "same-origin"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-request-id"]


@pytest.mark.parametrize("path", ["/stock", "/stock?symbol=TCS", "/compare", "/backtest", "/data", "/some/deep/page"])
def test_every_page_address_returns_the_dashboard_so_refresh_and_bookmarks_work(make_client, dist, path):
    r = make_client(dist).get(path)
    assert r.status_code == 200 and '<div id="root">' in r.text


def test_a_rebuild_is_picked_up_without_restarting_the_server(make_client, dist):
    c = make_client(dist)
    first = c.get("/")
    new_script = "window.__v = 2;"
    (dist / "index.html").write_text(INDEX.replace(INLINE, new_script), encoding="utf-8")
    second = c.get("/")
    assert sha(new_script) in second.headers["content-security-policy"]
    assert second.headers["content-security-policy"] != first.headers["content-security-policy"]


# ------------------------------------------------------------------------------------------------ assets
def test_hashed_assets_are_cached_for_a_year(make_client, dist):
    c = make_client(dist)
    js, css = c.get("/assets/app-abc123.js"), c.get("/assets/style-abc123.css")
    assert js.status_code == 200 and js.text == "console.log('app')" and js.headers["cache-control"] == ASSET_CACHE
    assert "javascript" in js.headers["content-type"] and css.headers["content-type"].startswith("text/css")
    assert "immutable" in ASSET_CACHE and "max-age=31536000" in ASSET_CACHE


def test_a_missing_asset_is_a_json_404_not_the_page(make_client, dist):
    r = make_client(dist).get("/assets/nope-999.js")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_files_outside_the_build_cannot_be_reached(make_client, dist):
    c = make_client(dist)
    for path in ("/assets/../../secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets//etc/passwd"):
        r = c.get(path)
        assert "TOP SECRET" not in r.text, path
        assert r.status_code in (200, 404), path                                           # 200 only ever means the harmless page


def test_missing_files_with_an_extension_are_404_not_the_page(make_client, dist):
    c = make_client(dist)
    for path in ("/robots.txt", "/app.js.map", "/deep/missing.png"):
        r = c.get(path)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path


def test_the_browsers_favicon_request_is_answered_quietly_and_head_requests_work(make_client, dist):
    c = make_client(dist)
    icon = c.get("/favicon.ico")
    assert icon.status_code == 204 and icon.content == b"" and "max-age" in icon.headers["cache-control"]
    for path in ("/", "/stock"):                                                           # monitors and link checkers send HEAD
        head = c.head(path)
        assert head.status_code == 200 and head.headers["content-type"].startswith("text/html") and head.content == b""
    assert c.head("/api/nope").status_code == 404


# ----------------------------------------------------------------------------------- the API is untouched
def test_the_api_and_its_docs_still_answer_and_unknown_api_paths_stay_json(make_client, dist):
    c = make_client(dist)
    assert c.get("/openapi.json").json()["info"]["title"] == "BharatMarket Analytics"
    docs = c.get("/docs")
    assert docs.status_code == 200 and "swagger" in docs.text.lower() and '<div id="root">' not in docs.text
    for path in ("/api", "/api/", "/api/nope", "/api/instruments/extra/segments/here"):
        r = c.get(path)
        assert r.headers["content-type"].startswith("application/json"), path
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path
    assert c.post("/stock").status_code == 405 and c.post("/stock").json()["error"]["code"] == "method_not_allowed"
    assert c.delete("/").status_code == 405


# ------------------------------------------------------------------------------------ when not to serve
def test_without_a_build_only_the_api_is_served(make_client, tmp_path):
    c = make_client(tmp_path / "does-not-exist")
    r = c.get("/")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert c.get("/openapi.json").status_code == 200


def test_an_api_only_server_can_be_chosen_even_when_a_build_exists(make_client, dist):
    r = make_client(dist, serve=False).get("/")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert make_client(dist, serve=False).get("/assets/app-abc123.js").status_code == 404


def test_a_folder_without_index_html_is_not_served(make_client, dist):
    (dist / "index.html").unlink()
    assert make_client(dist).get("/stock").status_code == 404


# --------------------------------------------------------------------------------------- policy builder
def test_policy_hashes_every_inline_script_and_ignores_external_ones():
    html = "<script>var a=1;</script><script src='/x.js'></script><script type='module'>import './y.js'</script>"
    csp = content_security_policy(html)
    assert sha("var a=1;") in csp and sha("import './y.js'") in csp
    assert "x.js" not in csp
    assert "script-src 'self'" in content_security_policy("<html></html>")                 # no inline scripts: just the origin
