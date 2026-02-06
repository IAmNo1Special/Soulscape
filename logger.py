# logger.py
"""Logging configuration for Soulscape using stdlib logging."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def get_log_dir() -> Path:
    """Get the Soulscape logs directory in %APPDATA%."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        logs_dir = Path(appdata) / "Soulscape" / "logs"
    else:
        logs_dir = Path.cwd() / ".soulscape" / "logs"

    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """
    Set up the Soulscape logger with rotating file handler.

    Args:
        level: Logging level (default: INFO)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger("soulscape")

    # Avoid duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    logger.setLevel(level)

    # File handler with rotation (5MB max, 3 backups)
    log_file = get_log_dir() / "soulscape.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(level)

    # Console handler for development
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)

    # Formatter
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.info(f"Logging initialized. Log file: {log_file}")
    return logger


# Global logger instance
log = setup_logging()
