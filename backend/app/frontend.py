"""Serve the built dashboard (frontend/dist) from the API process, so the whole app lives at ONE address.

    npm run build                      (in frontend/)
    uvicorn app.main:app --app-dir backend     ->  http://127.0.0.1:8000  (dashboard)  and  /api/...  (API)

What this adds, and why:
  * `/assets/*`   the hashed JavaScript and CSS files, cached for a year (their names change when their content does).
  * every other   page address returns index.html, so refreshing or bookmarking /stock?symbol=TCS works (the router in the
                  browser decides what to show). index.html itself is never cached, so a new build shows up at once.
  * `/api/*`      is never answered with HTML: an unknown API address is a JSON 404 like every other error.
  * Content-Security-Policy on the page: scripts may come only from this origin plus the ONE small inline script in index.html
                  (allowed by its SHA-256 hash, not by a blanket 'unsafe-inline'); nothing can be framed, no plug-ins, and the
                  page may only talk to its own origin.
Because the page and the API share an origin there is no CORS involved in this mode.
"""
import base64
import hashlib
import logging
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from starlette.staticfiles import StaticFiles

from app.api.errors import ApiError

logger = logging.getLogger(__name__)

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
ASSET_CACHE = "public, max-age=31536000, immutable"


def content_security_policy(index_html: str) -> str:
    """A strict policy for index.html: the inline scripts it contains are allowed by hash, nothing else inline."""
    hashes = " ".join(f"'sha256-{base64.b64encode(hashlib.sha256(code.encode('utf-8')).digest()).decode()}'" for code in INLINE_SCRIPT.findall(index_html))
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' {hashes}".strip(),
        "style-src 'self' 'unsafe-inline'",           # React writes style="..." attributes; scripts stay locked down
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


class ImmutableAssets(StaticFiles):
    """Static files whose names contain a content hash: safe to cache forever."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = ASSET_CACHE
        return response


def mount_frontend(app: FastAPI, dist: Path) -> bool:
    """Serve `dist` if it holds a build. Returns False (and serves nothing) when there is no build yet."""
    index = dist / "index.html"
    if not index.is_file():
        logger.info("No dashboard build at %s: serving the API only (run `npm run build` in frontend/ to add the dashboard)", dist)
        return False
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", ImmutableAssets(directory=assets), name="assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)     # HEAD: uptime monitors and link checkers use it
    def dashboard(path: str):
        last = path.rsplit("/", 1)[-1]
        if path == "api" or path.startswith("api/"):
            raise ApiError(404, "not_found", "No such API endpoint.")
        if path == "favicon.ico":                                  # browsers always ask; the page names its own icon, so say "none here"
            return Response(status_code=204, headers={"Cache-Control": "public, max-age=86400"})
        if "." in last:                                            # a file that does not exist (map files ...)
            raise ApiError(404, "not_found", f"No such file: /{path}")
        html = index.read_text(encoding="utf-8")                   # re-read each time: a rebuild shows up without a restart
        return Response(html, media_type="text/html", headers={
            "Cache-Control": "no-cache",
            "Content-Security-Policy": content_security_policy(html),
            "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY",
        })

    logger.info("Serving the dashboard from %s", dist)
    return True
