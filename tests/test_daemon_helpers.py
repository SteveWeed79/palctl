"""The crash auto-recovery rate-limiter is what stops a genuinely broken server
from being restarted in a tight loop. Its windowing is trivial but load-bearing,
so it's pinned. The daemon imports aiohttp, which the minimal-deps CI test job
doesn't install, so skip cleanly there rather than erroring at collection."""

import pytest

pytest.importorskip("aiohttp")

from palctl.daemon import _within_window  # noqa: E402  (after importorskip guard)


def test_within_window_keeps_recent_drops_old():
    now = 10_000.0
    times = [now - 4000, now - 3599, now - 100, now - 1]
    kept = _within_window(times, now, window=3600)
    assert kept == [now - 3599, now - 100, now - 1]  # the 4000s-old one is dropped


def test_within_window_empty():
    assert _within_window([], 123.0) == []


def test_within_window_all_recent():
    now = 500.0
    times = [now - 10, now - 20, now - 30]
    assert _within_window(times, now, window=3600) == times
