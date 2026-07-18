"""The ServerController is the one lock between every operation that stops the
server (watchdog, scheduled restart, update, restore, auto-recover, the user's
own buttons). If the serialisation is wrong, two of them interleave and the
server gets stopped mid-update or started mid-restore — so the lock semantics
are pinned here. Skips cleanly on the minimal-deps CI job."""

import asyncio
import types

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


def test_reserve_makes_the_server_busy_synchronously():
    # The daemon reserves before spawning a background op. A reservation must read
    # as busy immediately (same turn), so a second request can't slip a duplicate
    # op past a busy check while the first task hasn't taken the lock yet.
    ctl = ServerController(Config(), FakeApi())
    assert ctl.reserve("restart") is True
    assert ctl.busy and ctl.current_op == "restart"
    assert ctl.reserve("backup") is False          # a second claim is refused
    assert ctl.try_operation("watchdog-restart") is None  # opportunistic callers skip too
    ctl.clear_reservation("restart")
    assert not ctl.busy and ctl.current_op is None


def test_operation_clears_a_reservation_when_it_takes_the_lock():
    ctl = ServerController(Config(), FakeApi())
    assert ctl.reserve("restart") is True

    async def main():
        async with ctl.operation("restart"):
            # Inside the real lock now: the reservation has been converted, and a
            # stale clear_reservation from a wrapper's finally must not free it.
            assert ctl.busy and ctl.current_op == "restart"
            ctl.clear_reservation("restart")
            assert ctl.busy  # still held by the lock, not the reservation

    asyncio.run(main())
    assert not ctl.busy


def test_restart_cycle_order_and_result(monkeypatch):
    calls = []
    _patch_service(monkeypatch, calls)
    ctl = ServerController(Config(), FakeApi(alive=True))
    assert asyncio.run(ctl.restart_cycle(stop_delay=0)) is True
    assert calls == ["stop", "start"]


def test_restart_cycle_reports_failure_when_stop_never_took(monkeypatch):
    # Even the force-kill couldn't stop the server. start() would then no-op on the
    # still-RUNNING service and the stale process would answer wait_until_alive —
    # so restart_cycle must report False, not a phantom success, and never start().
    started = []

    async def never_stops(self, *, escalate=False, on_escalate=None):
        return False

    async def start(self):
        started.append(True)
        return True

    monkeypatch.setattr(ServerController, "stop", never_stops)
    monkeypatch.setattr(ServerController, "start", start)
    ctl = ServerController(Config(), FakeApi(alive=True))
    assert asyncio.run(ctl.restart_cycle(stop_delay=0, escalate=True)) is False
    assert started == []  # a failed stop must not be followed by a start


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


# ---------- force-kill escalation for a hung server ----------


def _hung_stop_service(monkeypatch):
    """Patch the service stop to time out (never reaches STOPPED)."""
    async def stop_service(name):
        return False

    monkeypatch.setattr(control_mod.procs, "stop_service", stop_service)


def test_plain_stop_reports_failure_and_never_kills(monkeypatch):
    # The user's own Stop button: a hung server honestly returns False so a
    # human can step in. No PID gets terminated behind their back.
    _hung_stop_service(monkeypatch)
    touched = []
    monkeypatch.setattr(control_mod.procs, "find_process", lambda: touched.append("find"))
    ctl = ServerController(Config(), FakeApi())
    assert asyncio.run(ctl.stop()) is False
    assert touched == []  # escalation never even looked for the process


def test_stop_escalates_to_terminate_when_service_stop_times_out(monkeypatch):
    _hung_stop_service(monkeypatch)
    proc = types.SimpleNamespace(pid=4321)
    monkeypatch.setattr(control_mod.procs, "find_process", lambda: proc)
    steps = []

    async def terminate(p, timeout=10.0):
        steps.append(("terminate", p.pid))
        return True  # terminate was enough

    async def kill(p, timeout=10.0):
        steps.append(("kill", p.pid))
        return True

    async def wait_stopped(name, timeout=60.0):
        steps.append("wait_stopped")
        return True

    monkeypatch.setattr(control_mod.procs, "terminate_process", terminate)
    monkeypatch.setattr(control_mod.procs, "kill_process", kill)
    monkeypatch.setattr(control_mod.procs, "wait_stopped", wait_stopped)

    msgs = []

    async def notify(m):
        msgs.append(m)

    ctl = ServerController(Config(), FakeApi())
    assert asyncio.run(ctl.stop(escalate=True, on_escalate=notify)) is True
    assert steps == [("terminate", 4321), "wait_stopped"]  # no hard kill needed
    assert len(msgs) == 1  # only the terminate notice was surfaced


def test_stop_hard_kills_when_terminate_is_ignored(monkeypatch):
    _hung_stop_service(monkeypatch)
    proc = types.SimpleNamespace(pid=99)
    monkeypatch.setattr(control_mod.procs, "find_process", lambda: proc)
    steps = []

    async def terminate(p, timeout=10.0):
        steps.append("terminate")
        return False  # survived — must escalate to a hard kill

    async def kill(p, timeout=10.0):
        steps.append("kill")
        return True

    async def wait_stopped(name, timeout=60.0):
        steps.append("wait_stopped")
        return True

    monkeypatch.setattr(control_mod.procs, "terminate_process", terminate)
    monkeypatch.setattr(control_mod.procs, "kill_process", kill)
    monkeypatch.setattr(control_mod.procs, "wait_stopped", wait_stopped)

    msgs = []

    async def notify(m):
        msgs.append(m)

    ctl = ServerController(Config(), FakeApi())
    assert asyncio.run(ctl.stop(escalate=True, on_escalate=notify)) is True
    assert steps == ["terminate", "kill", "wait_stopped"]
    assert len(msgs) == 2  # terminate notice + hard-kill notice


def test_stop_escalation_with_no_process_just_waits(monkeypatch):
    # Wedged service with nothing behind it (or the process died in the gap):
    # there's nothing to signal, so confirm the service state instead of crashing.
    _hung_stop_service(monkeypatch)
    monkeypatch.setattr(control_mod.procs, "find_process", lambda: None)
    waited = []

    async def wait_stopped(name, timeout=60.0):
        waited.append(name)
        return False

    monkeypatch.setattr(control_mod.procs, "wait_stopped", wait_stopped)
    ctl = ServerController(Config(), FakeApi())
    assert asyncio.run(ctl.stop(escalate=True)) is False
    assert waited == [Config().service_name]


def test_restart_cycle_forwards_escalation(monkeypatch):
    seen = {}

    async def fake_stop(self, *, escalate=False, on_escalate=None):
        seen["escalate"] = escalate
        return True

    async def fake_start(self):
        return True

    monkeypatch.setattr(ServerController, "stop", fake_stop)
    monkeypatch.setattr(ServerController, "start", fake_start)
    ctl = ServerController(Config(), FakeApi(alive=True))
    assert asyncio.run(ctl.restart_cycle(stop_delay=0, escalate=True)) is True
    assert seen["escalate"] is True
