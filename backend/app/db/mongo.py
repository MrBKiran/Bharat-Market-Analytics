"""MongoDB connection helpers.

We use the synchronous PyMongo driver on purpose: Pandas, NumPy and PySpark
are all synchronous, and FastAPI runs plain `def` endpoints in a thread pool,
so this keeps the code simple and fast enough.
"""
import logging
from typing import Optional

from pymongo import MongoClient
from pymongo.database import Database

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: Optional[MongoClient] = None


def get_client() -> MongoClient:
    """Return one shared MongoClient (it manages its own connection pool)."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = MongoClient(
            settings.mongo_uri.get_secret_value(),
            serverSelectionTimeoutMS=settings.mongo_timeout_ms,
            appname="bharatmarket-analytics",
            tz_aware=True,  # datetimes read back as UTC-aware, never naive
        )
        logger.info("MongoDB client created (database=%s)", settings.mongo_db_name)
    return _client


def get_db() -> Database:
    return get_client()[get_settings().mongo_db_name]


def check_connection() -> dict:
    """Ping the server. Returns connection info, never raises."""
    settings = get_settings()
    try:
        client = get_client()
        client.admin.command("ping")
        version = client.server_info().get("version")
        return {
            "connected": True,
            "database": settings.mongo_db_name,
            "server_version": version,
        }
    except Exception as exc:  # noqa: BLE001 - we want to report any failure
        logger.error("MongoDB connection failed: %s", type(exc).__name__)
        return {
            "connected": False,
            "database": settings.mongo_db_name,
            "error": type(exc).__name__,
        }


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.info("MongoDB client closed")
