"""
Rotating file + console logging.

Palworld's dedicated server ships no log file, and until now the daemon only
printed to whatever NSSM happened to capture. When a server misbehaves at 2am —
a watchdog restart that didn't come back, a SteamCMD update that failed halfway —
you want a trail to read the next morning. This writes one to
``%APPDATA%/palctl/logs/palctl.log`` and rotates it so it can't grow without
bound.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import config_dir

_LOG_NAME = "palctl"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure and return the palctl logger. Idempotent — safe to call twice."""
    logger = logging.getLogger(_LOG_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    log_dir = config_dir() / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / "palctl.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        # A read-only or missing log dir must never stop the daemon starting;
        # fall back to console-only.
        pass

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOG_NAME)
