"""Turn database documents into JSON that any client can parse.

MongoDB returns datetimes, ObjectIds and numpy/pandas values, and analytics can contain NaN or infinity. Python's
JSON encoder would either fail or emit `NaN`, which is not valid JSON and breaks browsers. `to_json` converts everything
once, in one place: datetimes become ISO-8601 UTC strings ending in `Z`, NaN and infinity become `null`, `_id` is dropped.
"""
import functools
import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from bson import ObjectId
from bson.decimal128 import Decimal128
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response


def iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_json(obj: Any) -> Any:
    """Recursively convert `obj` into plain JSON types."""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, (Decimal, Decimal128)):
        value = float(obj if isinstance(obj, Decimal) else obj.to_decimal())
        return value if math.isfinite(value) else None
    if obj is pd.NaT:
        return None
    if isinstance(obj, pd.Timestamp):
        return None if pd.isna(obj) else iso(obj.to_pydatetime())
    if isinstance(obj, datetime):
        return iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): to_json(v) for k, v in obj.items() if k != "_id"}
    if isinstance(obj, (list, tuple, set)):
        return [to_json(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [to_json(v) for v in obj.tolist()]
    if isinstance(obj, pd.DataFrame):
        return to_json(obj.to_dict("records"))
    return str(obj)


class SafeAPIRouter(APIRouter):
    """An APIRouter whose endpoints return `to_json(result)`.

    FastAPI would otherwise run its own encoder first (datetimes as `+00:00`, NaN left in place). Converting inside the
    endpoint wrapper gives every route the same timestamp format and guarantees valid JSON.
    """

    def add_api_route(self, path, endpoint, **kwargs):
        @functools.wraps(endpoint)
        def converted(*args, **kw):
            result = endpoint(*args, **kw)
            return result if isinstance(result, Response) else to_json(result)

        return super().add_api_route(path, converted, **kwargs)


class SafeJSONResponse(JSONResponse):
    """The default response class: always valid JSON, never a 500 because of a NaN or a datetime."""

    def render(self, content: Any) -> bytes:
        return json.dumps(to_json(content), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
