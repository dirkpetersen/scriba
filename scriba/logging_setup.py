"""Rotating file + console logging (DESIGN.md §7.11)."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import logs_dir

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3


def setup_logging(debug: bool | None = None) -> Path:
    """Configure the root logger. `debug=None` reads the SCRIBA_DEBUG env var."""
    if debug is None:
        debug = os.environ.get("SCRIBA_DEBUG", "").lower() in {"1", "true", "yes"}
    level = logging.DEBUG if debug else logging.INFO

    log_dir = logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "scriba.log"

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = RotatingFileHandler(
        log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    if sys.stderr is not None and sys.stderr.isatty():
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(console_handler)

    return log_path
