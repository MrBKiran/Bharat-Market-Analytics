"""Logging setup: console output plus a rotating file in `logs/`."""
import logging
from logging.handlers import RotatingFileHandler

from app.core.config import get_settings

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return

    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    file_handler = RotatingFileHandler(
        settings.log_dir / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    root.addHandler(console)
    root.addHandler(file_handler)

    # PyMongo's driver is very chatty at DEBUG level.
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    _configured = True
