"""Shared building blocks for the routes: database handle, pagination, input checks, rate limits and write access.

Security model (see docs/API.md):
  * Reading data is open (the app is meant to run on localhost or behind your own proxy).
  * Anything that CHANGES data needs the `X-API-Key` header when `API_KEY` is set in `.env`. With no key it is open in
    development and DISABLED in production.
  * Every client address is rate limited; heavy endpoints (backtests, imports, processing) have a smaller allowance.
  * The client address is the direct connection address. `X-Forwarded-For` is deliberately NOT trusted (it can be forged);
    behind a reverse proxy, apply the limit there as well.
"""
import hmac
import math
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import Query, Request
from pymongo.collection import Collection
from pymongo.database import Database

from app.api.errors import ApiError
from app.core.config import get_settings
from app.db.mongo import get_db

SYMBOL_RE = re.compile(r"^[A-Z0-9&_-]{1,30}$")
MAX_PAGE_SIZE = 500
INTERVALS = ("1d", "1m", "5m", "15m", "30m", "1h")
EXCHANGES = ("NSE", "BSE")


# ----------------------------------------------------------------------------------------------- database
def get_database() -> Database:
    """FastAPI dependency. Tests replace it with a temporary database via `app.dependency_overrides`."""
    return get_db()


# ------------------------------------------------------------------------------------------- rate limiting
class RateLimiter:
    """A sliding one-minute window per client key, kept in memory (one server process)."""

    def __init__(self, limit: int, window_seconds: float = 60.0):
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, now: Optional[float] = None) -> tuple[bool, int]:
        """(allowed, seconds until a slot frees up)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(1, math.ceil(self.window - (now - hits[0])))
            hits.append(now)
            if len(self._hits) > 5000:                       # forget idle clients so memory cannot grow without bound
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
                    del self._hits[k]
            return True, 0


def make_limiters() -> dict:
    s = get_settings()
    return {"standard": RateLimiter(s.rate_limit_per_minute), "heavy": RateLimiter(s.heavy_rate_limit_per_minute)}


def _limit(request: Request, kind: str) -> None:
    if not get_settings().rate_limit_enabled:
        return
    limiter: RateLimiter = request.app.state.limiters[kind]
    client = request.client.host if request.client else "unknown"
    allowed, retry = limiter.check(f"{kind}:{client}")
    if not allowed:
        raise ApiError(429, "rate_limited", f"Too many requests. Try again in {retry} seconds.",
                       {"limit_per_minute": limiter.limit}, headers={"Retry-After": str(retry)})


def standard_rate_limit(request: Request) -> None:
    _limit(request, "standard")


def heavy_rate_limit(request: Request) -> None:
    _limit(request, "heavy")


# ------------------------------------------------------------------------------------------- write access
def check_write_access(request: Request) -> None:
    settings = get_settings()
    key = settings.api_key.get_secret_value() if settings.api_key else ""
    if not key:
        if settings.environment == "production":
            raise ApiError(403, "write_disabled", "Endpoints that change data are disabled because no API_KEY is configured.")
        return
    supplied = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), key.encode("utf-8")):
        raise ApiError(401, "unauthorized", "A valid X-API-Key header is required for this endpoint.",
                       headers={"WWW-Authenticate": "ApiKey"})


# ---------------------------------------------------------------------------------------------- pagination
@dataclass
class Page:
    page: int
    page_size: int

    @property
    def skip(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(page: int = Query(1, ge=1, le=1_000_000, description="1-based page number"),
                page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE, description=f"items per page, at most {MAX_PAGE_SIZE}")) -> Page:
    return Page(page, page_size)


def paginate(coll: Collection, query: dict, page: Page, *, sort: list, projection: Optional[dict] = None) -> dict:
    """The standard list envelope: {items, page, page_size, total, pages}."""
    total = coll.count_documents(query)
    cursor = coll.find(query, {"_id": 0, **(projection or {})}).sort(sort).skip(page.skip).limit(page.page_size)
    return {"items": list(cursor), "page": page.page, "page_size": page.page_size, "total": total,
            "pages": math.ceil(total / page.page_size) if total else 0}


# ------------------------------------------------------------------------------------------- input checks
def norm_symbol(symbol: str) -> str:
    """Upper-case and validate a symbol (the same rule the database enforces)."""
    s = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(s):
        raise ApiError(422, "invalid_symbol", f"{symbol!r} is not a valid symbol: use 1-30 letters, digits, & _ or -.")
    return s


def utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def time_filter(start: Optional[datetime], end: Optional[datetime]) -> dict:
    """A MongoDB range condition for a time field, after checking start <= end."""
    start, end = utc(start), utc(end)
    if start and end and start > end:
        raise ApiError(422, "invalid_range", "start must not be after end.")
    cond = {}
    if start:
        cond["$gte"] = start
    if end:
        cond["$lte"] = end
    return cond


def csv_list(text: Optional[str], *, name: str, max_items: int, upper: bool = False) -> list[str]:
    if not text:
        return []
    items = [x.strip().upper() if upper else x.strip() for x in text.split(",") if x.strip()]
    if len(items) > max_items:
        raise ApiError(422, "too_many_items", f"{name}: at most {max_items} values, got {len(items)}.")
    return list(dict.fromkeys(items))


def require_instrument(db: Database, symbol: str, exchange: str = "NSE") -> dict:
    inst = db["instruments"].find_one({"symbol": symbol, "exchange": exchange}, {"_id": 0})
    if not inst:
        raise ApiError(404, "not_found", f"No instrument called {symbol} on {exchange}.")
    return inst
