"""GUI thread lifecycle.

Qt aborts the process — "QThread: Destroyed while thread is still running" —
when a QThread is destroyed while its run() is still going. The tray's Quit
action goes straight to QApplication.quit, and the Poller loops forever by
design, so at quit time it is *always* running: quitting the GUI reliably ended
in a crash instead of a clean exit. On Windows that is a "palctl-gui.exe has
stopped working" dialog as the last thing the user sees.

These need a real QApplication, so they run under the offscreen Qt platform in
the import-smoke CI job (which already installs the Qt runtime libs) and skip
anywhere PySide6 isn't installed.
"""

import pytest

# QtWidgets, not just PySide6: the wheel can be installed on a box that still
# can't load it (missing libEGL/libGL is an ImportError at submodule import).
# Skipping on the submodule covers both "not installed" and "can't initialise".
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

import palctl.gui.main as gui  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_workers_register_themselves(app):
    """The registry is the whole mechanism — a worker that isn't in it is one
    that can still abort the app on exit."""
    poller = gui.Poller()
    worker = gui.CallWorker("/state", {}, "ok")
    try:
        assert poller in gui._LIVE_WORKERS
        assert worker in gui._LIVE_WORKERS
    finally:
        gui.stop_all_workers(grace_ms=2000)


def test_every_worker_class_is_drainable():
    """Guards against a future worker inheriting QThread directly and slipping
    out of the drain."""
    from PySide6.QtCore import QThread

    for cls in (gui.Poller, gui.CallWorker, gui._MirrorTestWorker):
        assert issubclass(cls, gui._Worker), f"{cls.__name__} must inherit _Worker"
        assert issubclass(cls, QThread)


def test_stop_all_workers_joins_a_running_poller(app):
    """The regression: without the drain this thread is still running when Qt
    destroys it, and the process aborts."""
    poller = gui.Poller()
    poller.start()
    try:
        assert poller.isRunning()
        gui.stop_all_workers(grace_ms=3000)
        assert not poller.isRunning(), "the poller must be joined, not abandoned"
    finally:
        if poller.isRunning():  # never leave a live thread behind for Qt to free
            poller.terminate()
            poller.wait(1000)


def test_the_poller_stops_promptly_not_after_a_full_interval(app):
    """A single msleep(POLL_MS) would make quit sit through the rest of the poll
    interval; the sliced sleep is what keeps exit snappy."""
    import time

    poller = gui.Poller()
    poller.start()
    time.sleep(0.3)  # let it get into its sleep between polls
    t0 = time.monotonic()
    gui.stop_all_workers(grace_ms=5000)
    elapsed = time.monotonic() - t0
    assert not poller.isRunning()
    # Comfortably under the 2s poll interval, without being tight enough to
    # flake on a loaded CI box.
    assert elapsed < 1.5, f"took {elapsed:.2f}s to stop"


def test_a_stuck_worker_cannot_hold_up_quit(app):
    """/action/stop can legitimately be in a 180-second call. Quit terminates it
    rather than waiting — still far better than the abort we'd get otherwise."""
    import time

    class _Stuck(gui._Worker):
        def run(self):
            time.sleep(60)  # ignores interruption, like a blocked socket read

    stuck = _Stuck()
    stuck.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    gui.stop_all_workers(grace_ms=300)
    elapsed = time.monotonic() - t0
    assert not stuck.isRunning()
    assert elapsed < 5, f"quit waited {elapsed:.2f}s on a stuck worker"


# ---------------- settings editor rendering ----------------


def test_group_headings_show_their_ampersand(app, tmp_path):
    """Qt reads "&" in a title as a mnemonic marker — it swallows the "&" and
    underlines the next character, so "Difficulty & rates" rendered on screen as
    "Difficulty _rates"."""
    from PySide6.QtWidgets import QGroupBox

    from palctl.gui.settings_editor import GROUPS, SettingsEditor

    assert any("&" in title for title in GROUPS), "test assumes an & heading exists"

    live = tmp_path / "PalWorldSettings.ini"
    live.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        "OptionSettings=(ExpRate=1.000000,BaseCampMaxNum=4)\n",
        encoding="utf-8",
    )
    editor = SettingsEditor(live, tmp_path / "DefaultPalWorldSettings.ini")
    editor.reload()

    titles = [b.title() for b in editor.findChildren(QGroupBox)]
    assert titles, "the editor should have built some groups"
    for title in titles:
        # A lone "&" is the mnemonic form; a literal ampersand must be doubled.
        assert "&&" in title or "&" not in title, f"{title!r} would render mangled"
    assert any("Difficulty && rates" == t for t in titles)


def test_the_settings_editor_opens_an_ini_with_a_trailing_comment(app, tmp_path):
    """End to end through the widget: this file used to render as 'Couldn't
    parse', with no way forward but re-seeding (which destroys the settings)."""
    from palctl.gui.settings_editor import SettingsEditor

    live = tmp_path / "PalWorldSettings.ini"
    live.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(ExpRate=1.000000,ServerName="mine")\n'
        "; a note\n",
        encoding="utf-8",
    )
    editor = SettingsEditor(live, tmp_path / "DefaultPalWorldSettings.ini")
    editor.reload()

    assert editor._settings is not None, "the ini must parse"
    assert editor._settings.get("ServerName") == "mine"
    editor._settings.set("ExpRate", 2.0)
    editor._settings.save(live)
    assert "; a note" in live.read_text(encoding="utf-8")
