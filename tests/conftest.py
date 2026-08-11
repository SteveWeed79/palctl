"""Fixtures shared across the suite, and the guard that stops a job from
passing by not running anything.

`sim` lives here rather than being imported into each scenario module: pytest
resolves fixtures by name from conftest, so importing it explicitly would work
but reads as an unused name and trips F811 in every module that defines a test
taking `sim`.
"""

from __future__ import annotations

import os

from tests.sim.harness import sim  # noqa: F401  (re-exported as a fixture)

# ---------------------------------------------------------------------------
# PALCTL_NO_SKIPS=1 turns a skipped test into a failed run.
#
# pytest exits 0 when the tests it selected all skip — "nothing ran" and
# "everything passed" come out as the same green check. That is correct for the
# version matrix, where the Windows-only and Linux-only tests are *meant* to
# skip on the other OS. It is not correct for the scenarios job, which is twenty
# minutes reserved for the layer that catches what the rest of CI misses: if its
# twelve end-to-end scenarios ever start skipping, the job reports success
# having verified nothing.
#
# The narrow shape matters. A module-level `importorskip` yields no tests at
# all, so if nothing else is selected pytest exits 5 ("no tests ran") and the
# job is already red — that case needs no help, which is why only *selected*
# tests are counted here. Counting collection skips too would fail the scenarios
# job for test_gui_lifecycle.py, which is supposed to skip there: that job has
# no PySide6, and no `-m sim` test lives in that file anyway.
# ---------------------------------------------------------------------------

_NO_SKIPS = os.environ.get("PALCTL_NO_SKIPS") == "1"

_skipped: list[tuple[str, str]] = []

if _NO_SKIPS:

    def pytest_runtest_logreport(report) -> None:
        if not report.skipped:
            return
        # longrepr for a skip is (path, lineno, reason).
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        _skipped.append((report.nodeid, reason))

    def pytest_sessionfinish(session, exitstatus) -> None:
        """Fail the run itself.

        Done here rather than by rewriting each report: by the time a report
        reaches a plugin it has already been counted, so flipping its outcome
        changes the letter printed and not the exit code. The session's exit
        status is the part that decides whether the job goes green.
        """
        if _skipped and session.exitstatus == 0:
            session.exitstatus = 1

    def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
        if not _skipped:
            return
        w = terminalreporter.write_line
        w("")
        w("PALCTL_NO_SKIPS=1 is set: this job exists to RUN these tests, and a")
        w("skip here is a green check for work that never happened.")
        for nodeid, reason in _skipped:
            w(f"  SKIPPED {nodeid}: {reason}")
        w(f"{len(_skipped)} skip(s) -> failing the run.")
