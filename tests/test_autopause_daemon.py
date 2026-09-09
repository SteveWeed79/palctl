"""Auto-pause as the daemon drives it.

`autopause.decide` was pure and tested; what shipped was the decision with
nothing acting on it — no loop built an Observation, nothing listened on the
game port, and the config switch did nothing at all. These tests drive the
daemon's tick with the real controller, the real knock listener on a real UDP
port, and the real state file, so the two transitions and the port hand-back
are exercised rather than described. Skips cleanly where aiohttp/discord
aren't installed (palctl.daemon imports both)."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("discord")

import palctl.daemon as daemon_mod  # noqa: E402
from palctl import autopause  # noqa: E402
from palctl import control as control_mod  # noqa: E402
from palctl.api import PalApiUnreachable  # noqa: E402
from palctl.control import ServerController  # noqa: E402
from palctl.decisions import DecisionLog  # noqa: E402


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class FakeApi:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.saved = 0

    async def save(self):
        if self.fail:
            raise PalApiUnreachable("down")
        self.saved += 1

    async def metrics(self):
        raise PalApiUnreachable("down")

    async def players(self):
        return []

    async def wait_until_alive(self, timeout=240):
        return True


def _daemon(
    tmp_path: Path,
    monkeypatch,
    *,
    port: int,
    enabled: bool = True,
    alive: bool = True,
    online: int = 0,
    service: str = "RUNNING",
    desired: bool = True,
    paused: bool = False,
    api: FakeApi | None = None,
):
    """A Daemon with just enough of __init__ for the auto-pause path: the
    real controller (with the service manager faked), the real listener, the
    real state file, and stubs for the rest."""
    monkeypatch.setattr(daemon_mod, "_STATE_PATH", tmp_path / "daemon_state.json")
    calls: list[str] = []

    async def stop(name):
        calls.append("stop")
        return True

    async def start(name):
        calls.append("start")
        return True

    monkeypatch.setattr(control_mod.procs, "stop_service", stop)
    monkeypatch.setattr(control_mod.procs, "start_service", start)

    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d.cfg = daemon_mod.Config()
    d.cfg.autopause_enabled = enabled
    d.cfg.autopause_idle_minutes = 1
    d.cfg.game_port = port
    d.log = logging.getLogger("test-autopause")
    d.decisions = DecisionLog()
    d._alive = alive
    d.__dict__["_Daemon__desired_running"] = desired
    d._paused = paused
    d._empty_since = None
    d._woke_at = 0.0
    d._knock = autopause.KnockListener()
    d.tracker = types.SimpleNamespace(online=[object()] * online)
    d.api = api or FakeApi()
    d.control = ServerController(d.cfg, d.api)
    d.control.before_start = d._release_knock_port
    d.calls = calls
    d.emitted = []

    class _Bus:
        @staticmethod
        async def emit(e):
            d.emitted.append(e)

    d.bus = _Bus()

    async def _state(ttl=2.0):
        return service

    d._service_state_cached = _state
    return d


def _idle_for(d, seconds: float) -> None:
    d._empty_since = time.monotonic() - seconds


def _actions(d) -> list[str]:
    return [e.data.get("action") for e in d.emitted if e.data.get("action")]


async def _teardown(d) -> None:
    await d._knock.stop()


# ---------------- putting it away ----------------


def test_an_idle_empty_server_is_saved_stopped_and_listened_for(tmp_path, monkeypatch):
    port = free_udp_port()
    d = _daemon(tmp_path, monkeypatch, port=port)
    _idle_for(d, 120)

    async def go():
        try:
            await d._autopause_tick()
            return d._knock.listening
        finally:
            await _teardown(d)

    assert asyncio.run(go()) is True
    assert d.calls == ["stop"]
    assert d.api.saved == 1, "the world is flushed before the stop"
    assert d._paused is True
    assert daemon_mod._load_paused() is True, "the pause survives a daemon restart"
    assert "autopause" in _actions(d)
    assert d.decisions.latest().action == "autopause_sleep"
    assert not d.control.busy, "the operation lock is released afterwards"


def test_a_briefly_empty_server_is_left_alone(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch, port=free_udp_port())
    _idle_for(d, 20)
    asyncio.run(d._autopause_tick())
    assert d.calls == [] and d._paused is False


def test_players_online_keep_the_server_up(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch, port=free_udp_port(), online=2)
    _idle_for(d, 9999)  # a stale idle clock must be reset by the players
    asyncio.run(d._autopause_tick())
    assert d.calls == [] and d._paused is False
    assert d._empty_since is None


def test_nothing_happens_while_switched_off(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch, port=free_udp_port(), enabled=False)
    _idle_for(d, 9999)
    asyncio.run(d._autopause_tick())
    assert d.calls == [] and d._paused is False


def test_a_pause_that_cannot_listen_starts_the_server_again(tmp_path, monkeypatch):
    """A server put away with no way to wake it is a server nobody can reach:
    if the game port can't be taken, the stop is undone."""
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind(("0.0.0.0", 0))
    port = held.getsockname()[1]
    d = _daemon(tmp_path, monkeypatch, port=port)
    _idle_for(d, 120)
    try:
        asyncio.run(d._autopause_tick())
    finally:
        held.close()

    assert d.calls == ["stop", "start"]
    assert d._paused is False
    assert daemon_mod._load_paused() is False
    assert "autopause_failed" in _actions(d)
    assert any(str(port) in e.message for e in d.emitted if e.kind == "error")


def test_a_server_that_will_not_stop_is_not_recorded_as_asleep(tmp_path, monkeypatch):
    async def stop_fails(name):
        return False

    d = _daemon(tmp_path, monkeypatch, port=free_udp_port())
    monkeypatch.setattr(control_mod.procs, "stop_service", stop_fails)
    _idle_for(d, 120)
    asyncio.run(d._autopause_tick())
    assert d._paused is False
    assert not d._knock.listening
    assert "autopause_failed" in _actions(d)


# ---------------- waking it ----------------


def test_a_knock_wakes_the_server_and_hands_the_port_back(tmp_path, monkeypatch):
    port = free_udp_port()
    d = _daemon(tmp_path, monkeypatch, port=port, paused=True, alive=False,
                service="STOPPED")
    daemon_mod._save_paused(True)

    async def go():
        assert await d._knock.start(daemon_mod.KNOCK_HOST, port)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"hello", ("127.0.0.1", port))
        sender.close()
        for _ in range(50):
            if d._knock.knocked:
                break
            await asyncio.sleep(0.02)
        assert d._knock.knocked
        await d._autopause_tick()
        # The port is really free again — the server could bind it now.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False
        finally:
            probe.close()
            await _teardown(d)

    assert asyncio.run(go()) is True
    assert d.calls == ["start"]
    assert d._paused is False
    assert daemon_mod._load_paused() is False
    assert d._woke_at > 0
    assert "autowake" in _actions(d)


def test_a_sleeping_server_with_no_knock_keeps_sleeping(tmp_path, monkeypatch):
    port = free_udp_port()
    d = _daemon(tmp_path, monkeypatch, port=port, paused=True, alive=False,
                service="STOPPED")

    async def go():
        await d._knock.start(daemon_mod.KNOCK_HOST, port)
        try:
            await d._autopause_tick()
        finally:
            await _teardown(d)

    asyncio.run(go())
    assert d.calls == [] and d._paused is True


def test_every_start_path_releases_the_port_first(tmp_path, monkeypatch):
    """Start, restart, update, restore, recovery and the boot-time start all
    go through the controller; a server started while palctl still holds its
    port dies at bind. The hook on the controller is what makes them safe."""
    port = free_udp_port()
    d = _daemon(tmp_path, monkeypatch, port=port, paused=True, alive=False,
                service="STOPPED")
    daemon_mod._save_paused(True)

    async def go():
        await d._knock.start(daemon_mod.KNOCK_HOST, port)
        return await d.control.start()  # an admin's Start, say

    assert asyncio.run(go()) is True
    assert d.calls == ["start"]
    assert not d._knock.listening
    assert d._paused is False and daemon_mod._load_paused() is False


def test_turning_auto_pause_off_wakes_a_sleeping_server(tmp_path, monkeypatch):
    """Fail safe toward running: a server asleep when the feature is switched
    off is not left asleep with nothing to wake it."""
    port = free_udp_port()
    d = _daemon(tmp_path, monkeypatch, port=port, paused=True, alive=False,
                service="STOPPED", enabled=False)

    async def go():
        await d._knock.start(daemon_mod.KNOCK_HOST, port)
        await d._autopause_tick()
        await _teardown(d)

    asyncio.run(go())
    assert d.calls == ["start"]
    assert d._paused is False


def test_turning_auto_pause_off_never_restarts_a_server_stopped_on_purpose(
    tmp_path, monkeypatch
):
    port = free_udp_port()
    d = _daemon(tmp_path, monkeypatch, port=port, paused=True, alive=False,
                service="STOPPED", enabled=False, desired=False)

    async def go():
        await d._knock.start(daemon_mod.KNOCK_HOST, port)
        await d._autopause_tick()
        listening = d._knock.listening
        await _teardown(d)
        return listening

    assert asyncio.run(go()) is False  # the port is handed back...
    assert d.calls == []  # ...and the admin's Stop stands
    assert d._paused is False


# ---------------- across a daemon restart ----------------


def test_a_daemon_that_restarts_mid_sleep_listens_again(tmp_path, monkeypatch):
    port = free_udp_port()
    d = _daemon(tmp_path, monkeypatch, port=port, paused=True, alive=False,
                service="STOPPED")

    async def go():
        await d._resume_listening()
        try:
            return d._knock.listening
        finally:
            await _teardown(d)

    assert asyncio.run(go()) is True
    assert d._paused is True


def test_a_port_already_in_use_at_startup_means_the_server_is_awake(tmp_path, monkeypatch):
    """Somebody started the server by hand while the daemon was down."""
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind(("0.0.0.0", 0))
    port = held.getsockname()[1]
    d = _daemon(tmp_path, monkeypatch, port=port, paused=True)
    daemon_mod._save_paused(True)
    try:
        asyncio.run(d._resume_listening())
    finally:
        held.close()
    assert d._paused is False
    assert daemon_mod._load_paused() is False


def test_a_pause_recorded_for_a_server_stopped_on_purpose_is_dropped(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch, port=free_udp_port(), paused=True, desired=False)
    asyncio.run(d._resume_listening())
    assert d._paused is False and not d._knock.listening


# ---------------- what the rest of the daemon makes of a pause ----------------


def test_the_poll_loop_does_not_announce_an_outage_for_a_pause(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch, port=free_udp_port(), paused=True, alive=True)
    d.cfg.watchdog.crash_confirm_polls = 1
    d._api_fail_streak = 0
    d._last_metrics = object()

    async def closed():
        d.emitted.append("sessions-closed")

    d.tracker = types.SimpleNamespace(handle_server_down=closed, online=[])

    async def no_recovery():
        pass

    d._maybe_autorecover = no_recovery
    asyncio.run(d._poll())

    assert d._alive is False and d._last_metrics is None
    assert "sessions-closed" in d.emitted
    assert not any(
        getattr(e, "kind", "") == "server_down" for e in d.emitted
    ), "a pause is not an outage"


def test_the_supervisor_is_told_about_the_pause(tmp_path, monkeypatch):
    """The whole reason the flag exists: without it the external-stop
    detector adopted palctl's own pause as an admin's Stop."""
    from palctl import supervisor

    d = _daemon(tmp_path, monkeypatch, port=free_udp_port(), paused=True,
                alive=False, service="STOPPED")
    d.cfg.watchdog.auto_restart_on_crash = True
    d.__dict__["_Daemon__ever_alive"] = True
    d._service_seen_up = True
    d._boot_intent_pending = False
    d._external_stop_polls = supervisor.EXTERNAL_STOP_CONFIRM_POLLS
    d._down_polls = 0
    d._autorestart_times = []
    d.watchdog = types.SimpleNamespace(is_restarting=False)
    seen: list = []
    real_decide = supervisor.decide

    def spy(obs):
        seen.append(obs)
        return real_decide(obs)

    monkeypatch.setattr(supervisor, "decide", spy)
    for _ in range(3):
        asyncio.run(d._maybe_autorecover())

    assert seen and all(o.paused for o in seen)
    assert d._desired_running is True, "the pause must not be adopted as a Stop"
    assert d.calls == []
    assert d.decisions.latest().action == "stand_down"
