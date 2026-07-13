"""The ServerController is the one lock between every operation that stops the
server (watchdog, scheduled restart, update, restore, auto-recover, the user's
own buttons). If the serialisation is wrong, two of them interleave and the
server gets stopped mid-update or started mid-restore — so the lock semantics
are pinned here. Skips cleanly on the minimal-deps CI job."""

import asyncio

import pytest

pytest.importorskip("httpx")
pytest.importorskip("psutil")
pytest.importorskip("keyring")

from palctl import control as control_mod  # noqa: E402  (after importorskip)
from palctl.config import Config  # noqa: E402
from palctl.control import ServerController  # noqa: E402


class FakeApi:
    def __init__(self, alive=True):
        self._alive = alive

    async def save(self):
        pass

    async def wait_until_alive(self, timeout=240):
        return self._alive


def _patch_service(monkeypatch, calls):
    async def stop(name):
        calls.append("stop")
        return True

    async def start(name):
        calls.append("start")
        return True

    monkeypatch.setattr(control_mod.procs, "stop_service", stop)
    monkeypatch.setattr(control_mod.procs, "start_service", start)


def test_operations_serialise():
    ctl = ServerController(Config(), FakeApi())
    order = []

    async def op(name, hold):
        async with ctl.operation(name):
            order.append(f"{name}-in")
            await asyncio.sleep(hold)
            order.append(f"{name}-out")

    async def main():
        await asyncio.wait_for(asyncio.gather(op("a", 0.02), op("b", 0)), 5)

    asyncio.run(main())
    # b started after a fully finished, even though b had nothing to wait on.
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_try_operation_skips_when_busy_and_names_the_holder():
    ctl = ServerController(Config(), FakeApi())
    seen = {}

    async def main():
        async with ctl.operation("update"):
            seen["while_busy"] = ctl.try_operation("watchdog-restart")
            seen["holder"] = ctl.current_op
            assert ctl.busy
        free = ctl.try_operation("watchdog-restart")
        assert free is not None
        async with free:
            seen["after"] = ctl.current_op
        assert ctl.current_op is None and not ctl.busy

    asyncio.run(main())
    assert seen["while_busy"] is None  # opportunistic callers skip, never queue
    assert seen["holder"] == "update"
    assert seen["after"] == "watchdog-restart"


def test_restart_cycle_order_and_result(monkeypatch):
    calls = []
    _patch_service(monkeypatch, calls)
    ctl = ServerController(Config(), FakeApi(alive=True))
    assert asyncio.run(ctl.restart_cycle(stop_delay=0)) is True
    assert calls == ["stop", "start"]


def test_restart_cycle_reports_server_that_stayed_down(monkeypatch):
    _patch_service(monkeypatch, [])
    ctl = ServerController(Config(), FakeApi(alive=False))
    assert asyncio.run(ctl.restart_cycle(stop_delay=0)) is False


def test_save_best_effort_never_raises():
    class BadApi:
        async def save(self):
            raise RuntimeError("API down")

    ctl = ServerController(Config(), BadApi())
    assert asyncio.run(ctl.save_best_effort()) is False
