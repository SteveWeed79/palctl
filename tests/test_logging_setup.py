"""Logging must never be the reason the daemon won't start, and calling setup
twice (GUI + daemon in one process, or a reload) must not stack duplicate
handlers that write every line five times.

The handlers live on the ROOT logger so the libraries the daemon is built out of
(aiohttp, discord.py, httpx) write into the same rotating file — under a service
wrapper that file is the only trail that survives, and their failures are
exactly what explains an unresponsive daemon."""

import logging
from pathlib import Path

import palctl.logging_setup as ls


def _reset() -> None:
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_palctl", False)]:
        root.removeHandler(h)
    logging.getLogger("palctl").handlers.clear()


def _our_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, "_palctl", False)]


def test_setup_creates_log_dir_and_handlers(tmp_path: Path, monkeypatch):
    _reset()
    monkeypatch.setattr(ls, "config_dir", lambda: tmp_path)

    ls.setup_logging()
    assert (tmp_path / "logs").is_dir()
    assert _our_handlers()  # at least the file (or console fallback)
    _reset()


def test_setup_is_idempotent(tmp_path: Path, monkeypatch):
    _reset()
    monkeypatch.setattr(ls, "config_dir", lambda: tmp_path)

    first = ls.setup_logging()
    count = len(_our_handlers())
    second = ls.setup_logging()

    assert second is first
    assert len(_our_handlers()) == count  # no duplicate handlers
    _reset()


def test_setup_survives_unwritable_log_dir(tmp_path: Path, monkeypatch):
    _reset()
    # Point config_dir at a *file*, so creating <file>/logs raises — the daemon
    # must still come up with a console logger rather than crashing.
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ls, "config_dir", lambda: blocker)

    ls.setup_logging()
    assert _our_handlers()  # console handler still attached despite mkdir failing
    _reset()


def test_library_warnings_reach_the_log_file(tmp_path: Path, monkeypatch):
    """aiohttp's 'Error handling request' traceback and discord.py's rejected
    token are the entries that explain a daemon nobody can talk to. Attached to
    the palctl logger alone they went to stderr, which the service wrapper
    discards — so the 2am trail was missing precisely them."""
    _reset()
    monkeypatch.setattr(ls, "config_dir", lambda: tmp_path)
    ls.setup_logging()

    logging.getLogger("aiohttp.server").error("Error handling request")
    logging.getLogger("discord.client").warning("Improper token has been passed")
    logging.getLogger("httpx").warning("transport trouble")

    written = (tmp_path / "logs" / "palctl.log").read_text(encoding="utf-8")
    assert "Error handling request" in written
    assert "Improper token has been passed" in written
    assert "transport trouble" in written
    _reset()


def test_library_chatter_is_filtered_out(tmp_path: Path, monkeypatch):
    """Libraries contribute problems, not chatter: discord.py logs every gateway
    heartbeat at DEBUG/INFO and would otherwise rotate the useful history out of
    a 2 MB file. palctl's own INFO still gets through."""
    _reset()
    monkeypatch.setattr(ls, "config_dir", lambda: tmp_path)
    logger = ls.setup_logging()

    logging.getLogger("discord.gateway").info("keeping websocket alive")
    logger.info("palctl says hello")

    written = (tmp_path / "logs" / "palctl.log").read_text(encoding="utf-8")
    assert "keeping websocket alive" not in written
    assert "palctl says hello" in written
    _reset()


def test_palctl_lines_are_not_duplicated(tmp_path: Path, monkeypatch):
    """Handlers on root plus handlers on `palctl` would write every palctl line
    twice. Only root carries them."""
    _reset()
    monkeypatch.setattr(ls, "config_dir", lambda: tmp_path)
    logger = ls.setup_logging()

    logger.warning("only once please")
    logging.getLogger("palctl.daemon").warning("child only once too")

    written = (tmp_path / "logs" / "palctl.log").read_text(encoding="utf-8")
    assert written.count("only once please") == 1
    assert written.count("child only once too") == 1
    _reset()
