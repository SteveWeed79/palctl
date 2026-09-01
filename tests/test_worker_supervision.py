"""A crashed worker loop is restarted, not silently retired.

`_supervised` used to catch an escaped exception, log it, emit one event, and
stop. The daemon stayed up, the control API kept answering and /healthz kept
saying "ok" — with the memory watchdog, or the scheduler, simply gone. A
supervisor that outlives the thing it supervises and still reports healthy is
the silent failure this codebase exists to avoid.
"""

from __future__ import annotations

import asyncio

import pytest

from palctl.daemon import _WORKER_RESTART_BUDGET, Daemon
from palctl.events import Event, EventBus


class Stub:
    """Just enough Daemon to exercise _supervised, without booting one."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.degraded: dict[str, str] = {}
        self.events: list[Event] = []
        self.logged: list[str] = []

        async def handler(event: Event) -> None:
            self.events.append(event)

        self.bus.on_any(handler)

        class Log:
            def error(_self, msg, *args, **kw):
                self.logged.append(msg % args if args else msg)

        self.log = Log()

    _supervised = Daemon._supervised


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """The backoff ladder is real seconds; the tests are about the count."""
    async def instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)


def test_a_crashing_loop_is_restarted_until_the_budget_runs_out():
    app = Stub()
    attempts = 0

    async def always_fails():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    asyncio.run(app._supervised("watchdog", always_fails))

    assert attempts == _WORKER_RESTART_BUDGET + 1


def test_a_loop_that_recovers_keeps_running_and_leaves_no_damage():
    app = Stub()
    attempts = 0

    async def fails_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        return None

    asyncio.run(app._supervised("scheduler", fails_once))

    assert attempts == 2
    assert app.degraded == {}  # recovered, so not degraded
    assert any("restarting" in e.message for e in app.events)


def test_giving_up_marks_the_daemon_degraded():
    """The part that makes /healthz stop lying."""
    app = Stub()

    async def always_fails():
        raise RuntimeError("bad config")

    asyncio.run(app._supervised("watchdog", always_fails))

    assert "watchdog" in app.degraded
    assert "bad config" in app.degraded["watchdog"]
    assert any("stopped for good" in e.message for e in app.events)


def test_a_loop_that_returns_normally_is_not_a_crash():
    app = Stub()
    calls = 0

    async def finishes():
        nonlocal calls
        calls += 1

    asyncio.run(app._supervised("boot intent", finishes))

    assert calls == 1  # not re-run
    assert app.degraded == {}
    assert app.events == []


def test_a_one_shot_can_opt_out_of_restarts():
    """Startup diagnostics must not be re-run — retrying just shells out again."""
    app = Stub()
    attempts = 0

    async def always_fails():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("netsh missing")

    asyncio.run(app._supervised("startup checks", always_fails, restarts=0))

    assert attempts == 1
    assert "startup checks" in app.degraded


def test_cancellation_is_not_a_crash():
    """A daemon shutting down cancels its workers; that must not restart them
    or mark anything degraded."""
    app = Stub()
    attempts = 0

    async def cancelled():
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app._supervised("poll loop", cancelled))

    assert attempts == 1
    assert app.degraded == {}
