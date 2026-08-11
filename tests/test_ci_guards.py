"""The guard that stops a CI job from passing by not running anything.

An unverified guard is the same failure one level up: PALCTL_NO_SKIPS is only
worth setting on the scenarios job if it actually turns a skipped run red, and
nothing else in the suite would notice if a pytest upgrade moved the hooks out
from under it. So this drives the real conftest in a subprocess and checks the
exit code both ways.

The pairing is the point. Without the variable a fully-skipped run must stay
green — that behaviour is load-bearing for the version matrix, where the
Windows-only and Linux-only tests are meant to skip on the other OS.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Forces every collected test to skip: "the environment didn't come up".
_SKIP_EVERYTHING = '''
import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.skip(reason="simulated environment failure"))
'''

# Pure parser tests: fast, no optional dependencies, so the only thing that can
# make this run skip is the plugin above.
_TARGET = "tests/test_inifile.py"


def _run_all_skipped(tmp_path: Path, *, no_skips: bool) -> subprocess.CompletedProcess:
    (tmp_path / "skip_everything.py").write_text(_SKIP_EVERYTHING)
    env = dict(os.environ)
    # Set explicitly both ways: this test file itself runs under the scenarios
    # job, where the variable is already 1 and would otherwise leak in.
    if no_skips:
        env["PALCTL_NO_SKIPS"] = "1"
    else:
        env.pop("PALCTL_NO_SKIPS", None)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(REPO)])
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", _TARGET, "-p", "skip_everything"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_fully_skipped_run_is_green_without_the_variable(tmp_path):
    """Unset, pytest's own behaviour is preserved: skips are not failures."""
    proc = _run_all_skipped(tmp_path, no_skips=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skipped" in proc.stdout


def test_a_fully_skipped_run_fails_with_the_variable(tmp_path):
    """Set, "nothing ran" stops being indistinguishable from "everything passed"."""
    proc = _run_all_skipped(tmp_path, no_skips=True)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    # The reason has to reach the log, or a red job is just a puzzle.
    assert "skip(s) -> failing the run" in proc.stdout
    assert "simulated environment failure" in proc.stdout


def test_passing_tests_stay_green_with_the_variable(tmp_path):
    """The guard must only fire on skips — a normal run is untouched by it."""
    env = dict(os.environ)
    env["PALCTL_NO_SKIPS"] = "1"
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", _TARGET],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
