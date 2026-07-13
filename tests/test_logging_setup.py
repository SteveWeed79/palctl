"""Logging must never be the reason the daemon won't start, and calling setup
twice (GUI + daemon in one process, or a reload) must not stack duplicate
handlers that write every line five times."""

import logging
from pathlib import Path

import palctl.logging_setup as ls


def _reset() -> None:
    logging.getLogger("palctl").handlers.clear()


def test_setup_creates_log_dir_and_handlers(tmp_path: Path, monkeypatch):
    _reset()
    monkeypatch.setattr(ls, "config_dir", lambda: tmp_path)

    logger = ls.setup_logging()
    assert (tmp_path / "logs").is_dir()
    assert logger.handlers  # at least the file (or console fallback)
    _reset()


def test_setup_is_idempotent(tmp_path: Path, monkeypatch):
    _reset()
    monkeypatch.setattr(ls, "config_dir", lambda: tmp_path)

    first = ls.setup_logging()
    count = len(first.handlers)
    second = ls.setup_logging()

    assert second is first
    assert len(second.handlers) == count  # no duplicate handlers
    _reset()


def test_setup_survives_unwritable_log_dir(tmp_path: Path, monkeypatch):
    _reset()
    # Point config_dir at a *file*, so creating <file>/logs raises — the daemon
    # must still come up with a console logger rather than crashing.
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ls, "config_dir", lambda: blocker)

    logger = ls.setup_logging()
    assert logger.handlers  # console handler still attached despite mkdir failing
    _reset()
