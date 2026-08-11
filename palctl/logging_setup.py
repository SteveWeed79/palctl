"""
Rotating file + console logging.

Palworld's dedicated server ships no log file, and until now the daemon only
printed to whatever the service wrapper happened to capture. When a server misbehaves at 2am —
a watchdog restart that didn't come back, a SteamCMD update that failed halfway —
you want a trail to read the next morning. This writes one to
``%APPDATA%/palctl/logs/palctl.log`` and rotates it so it can't grow without
bound.

The handlers go on the ROOT logger, not on ``palctl``. Under a service wrapper
stdio is discarded, so the file is the only trail that survives — and the
libraries the daemon is built out of report their failures through their own
loggers. aiohttp logs an unhandled error in a request handler (a 500 the client
sees as a bare status line), discord.py logs a rejected token and every
reconnect, httpx logs transport trouble. Attached to ``palctl`` alone, all of
that went to logging's last-resort stderr handler and straight into the void,
so the 2am trail this module exists to write was missing exactly the entries
that explain an unresponsive daemon. Root keeps ``WARNING`` so libraries
contribute problems and not chatter, while ``palctl`` itself stays at INFO.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import config_dir

_LOG_NAME = "palctl"

# Everything noisier than this from third-party loggers is dropped; palctl's own
# logger is set to `level` separately below. A propagated record is filtered by
# the logger it was created on and then by each handler — never by the levels of
# the ancestors it passes through — so this gates libraries without touching
# palctl's INFO records on their way to the same handlers.
_LIBRARY_LEVEL = logging.WARNING


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure logging and return the palctl logger. Idempotent — safe to call
    twice (the second call finds the handlers already installed and returns)."""
    logger = logging.getLogger(_LOG_NAME)
    root = logging.getLogger()
    if any(getattr(h, "_palctl", False) for h in root.handlers) or logger.handlers:
        return logger

    logger.setLevel(level)
    root.setLevel(_LIBRARY_LEVEL)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    handlers: list[logging.Handler] = []
    log_dir = config_dir() / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / "palctl.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
            )
        )
    except OSError:
        # A read-only or missing log dir must never stop the daemon starting;
        # fall back to console-only.
        pass

    handlers.append(logging.StreamHandler())

    for h in handlers:
        h.setFormatter(fmt)
        # Marks the handler as ours, so a second call is a no-op even though the
        # root logger may carry handlers somebody else installed.
        h._palctl = True  # type: ignore[attr-defined]
        root.addHandler(h)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOG_NAME)
