"""File + stdout logging with timestamps."""

from __future__ import annotations

import logging
from pathlib import Path

from config import LOG_PATH, ensure_dirs


def setup_logging() -> logging.Logger:
    ensure_dirs()
    logger = logging.getLogger("things_reminders")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


def log_path() -> Path:
    return LOG_PATH
