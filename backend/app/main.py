"""FastAPI application entry point.

Run from the project root with:
    .venv\\Scripts\\python -m uvicorn app.main:app --app-dir backend --reload
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import make_limiters, standard_rate_limit
from app.api.errors import RequestLogMiddleware, install_error_handlers
from app.api.routes import analytics, backtests, health, imports, indicators, instruments, prices, system
from app.api.serialize import SafeJSONResponse
from app.core.config import get_settings
from app.core.logging_config import setup_logging
from app.db.mongo import close_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    logger.info("Starting %s v%s (%s)", settings.app_name, settings.app_version, settings.environment)
    yield
    close_client()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="MongoDB-based Indian stock market big-data storage, processing and analytics.",
        lifespan=lifespan,
        default_response_class=SafeJSONResponse,
    )
    app.state.limiters = make_limiters()
    install_error_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_middleware(RequestLogMiddleware)       # added last = outermost: every response, errors included, gets a request id

    app.include_router(health.router, prefix="/api")      # never rate limited: monitors poll it
    limited = [Depends(standard_rate_limit)]
    for module in (instruments, prices, indicators, analytics, imports, backtests, system):
        app.include_router(module.router, prefix="/api", dependencies=limited)
    return app


app = create_app()
