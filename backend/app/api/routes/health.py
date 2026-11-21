"""Health check endpoint: confirms the API and MongoDB are reachable."""
from datetime import datetime, timezone

from fastapi import APIRouter, Response, status

from app.core.config import get_settings
from app.db.mongo import check_connection

router = APIRouter(tags=["System"])


@router.get("/health", summary="API and database health")
def health(response: Response) -> dict:
    settings = get_settings()
    database = check_connection()
    if not database["connected"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if database["connected"] else "degraded",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "database": database,
    }
