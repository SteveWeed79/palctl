"""Fixtures shared across the suite.

`sim` lives here rather than being imported into each scenario module: pytest
resolves fixtures by name from conftest, so importing it explicitly would work
but reads as an unused name and trips F811 in every module that defines a test
taking `sim`.
"""

import pytest

from tests.sim.harness import sim  # noqa: F401  (re-exported as a fixture)


@pytest.fixture(autouse=True)
def _instant_save_settle(monkeypatch):
    """Every backup now waits for the world's files to stop changing before it
    copies them (backups.wait_for_quiet) — seconds in production, and pure
    dead time in a test whose fake world never changes. Make the wait instant
    everywhere; the tests of the wait itself pass their own timings."""
    from palctl import scheduler

    monkeypatch.setattr(scheduler, "_SETTLE_QUIET_SECONDS", 0.0)
    monkeypatch.setattr(scheduler, "_SETTLE_TIMEOUT", 0.0)
    monkeypatch.setattr(scheduler, "_SETTLE_POLL", 0.001)
