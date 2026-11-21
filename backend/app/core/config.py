"""Application settings.

All configuration comes from environment variables (or the `.env` file in the
project root). Nothing secret is hard-coded here, and the frontend never sees
these values.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[3] is the project root
BASE_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "BharatMarket Analytics"
    app_version: str = "0.1.0"
    environment: Literal["development", "testing", "production"] = "development"
    log_level: str = "INFO"

    # --- MongoDB ---
    # SecretStr so the URI (which may contain a password) is never printed in logs.
    mongo_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    mongo_db_name: str = "BharatMarketDB"
    mongo_timeout_ms: int = 5000

    # --- API ---
    # Comma-separated list of allowed browser origins (the React dev server).
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Endpoints that change data (import, processing, instrument edits, storing backtests) need this key in the
    # `X-API-Key` header. Unset in development: they are open on localhost. Unset in production: they are disabled.
    api_key: Optional[SecretStr] = None
    # Per client IP, per minute. Heavy endpoints (backtests, imports, processing) get the smaller limit.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 300
    heavy_rate_limit_per_minute: int = 20

    # --- Files ---
    data_dir: Path = BASE_DIR / "data"
    log_dir: Path = BASE_DIR / "logs"
    max_upload_mb: int = 200

    # --- Optional external services (leave empty if unused) ---
    java_home: Optional[str] = None  # used to launch Spark in Phase 8
    broker_api_key: Optional[SecretStr] = None
    broker_api_secret: Optional[SecretStr] = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings object, so the .env file is read only once."""
    return Settings()
