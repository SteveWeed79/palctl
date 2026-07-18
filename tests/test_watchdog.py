"""The memory watchdog's hold-off decision is where a mistake kicks players
for no reason. The subtle case: the REST API not answering means the player
list is *unknown*, which must get the same benefit of the doubt as players
being online — not be treated as an empty server. The watchdog pulls in httpx
(via the API client) and psutil (via procs), which the minimal-deps CI job
doesn't install, so skip cleanly there."""

import asyncio
import types

import pytest

pytest.importorskip("httpx")
pytest.importorskip("psutil")
pytest.importorskip("keyring")

from palctl import watchdog as watchdog_mod  # noqa: E402  (after importorskip)
from palctl.config import Config  # noqa: E402
from palctl.watchdog import Watchdog  # noqa: E402


class FakeBus:
    def __init__(self):
        self.events = []

    async def emit(self, e):
        self.events.append(e)


class FakeApi:
    """players() either answers with a fixed list or raises (API down)."""

    def __init__(self, players=None, down=False):
        self._players = players or []
        self._down = down

    async def players(self):
        if self._down:
            raise RuntimeError("REST API down")
        return self._players


def make_watchdog(monkeypatch, *, memory_mb, api, skip_if_players_online=True, samples=1):
    cfg = Config()  # defaults: limit 12000 MB, hard limit 16000 MB
    cfg.watchdog.consecutive_samples = samples
    cfg.watchdog.skip_if_players_online = skip_if_players_online
    bus = FakeBus()
    wd = Watchdog(cfg, api, bus)

    monkeypatch.setattr(
        watchdog_mod.procs,
        "proc_stats",
        lambda: types.SimpleNamespace(memory_mb=memory_mb),
    )

    restarts = []

    async def record_restart(memory_mb, player_count, hard):
        restarts.append((memory_mb, player_count, hard))

    monkeypatch.setattr(wd, "_restart", record_restart)
    return wd, bus, restarts


def test_players_online_holds_below_hard_limit(monkeypatch):
    api = FakeApi(players=[types.SimpleNamespace(name="p1")])
    wd, bus, restarts = make_watchdog(monkeypatch, memory_mb=13_000, api=api)
    asyncio.run(wd._tick())
    assert restarts == []
    assert any(e.kind == "watchdog" for e in bus.events)  # the hold-off notice


def test_empty_server_restarts(monkeypatch):
    wd, _, restarts = make_watchdog(monkeypatch, memory_mb=13_000, api=FakeApi(players=[]))
    asyncio.run(wd._tick())
    assert restarts == [(13_000, 0, False)]


def test_api_down_below_hard_limit_holds_off(monkeypatch):
    # Unknown player list != empty server: don't restart blind, and don't
    # re-announce the hold on every poll.
    wd, bus, restarts = make_watchdog(monkeypatch, memory_mb=13_000, api=FakeApi(down=True))
    asyncio.run(wd._tick())
    asyncio.run(wd._tick())
    assert restarts == []
    assert len([e for e in bus.events if e.kind == "watchdog"]) == 1


def test_api_down_hard_limit_still_restarts(monkeypatch):
    # Above the hard limit the server is dying anyway — a dead API can't veto.
    wd, _, restarts = make_watchdog(monkeypatch, memory_mb=17_000, api=FakeApi(down=True))
    asyncio.run(wd._tick())
    assert restarts == [(17_000, 0, True)]


def test_api_down_without_skip_setting_restarts(monkeypatch):
    # skip_if_players_online off means the admin accepts mid-session restarts,
    # so an unknown player list doesn't hold anything back.
    wd, _, restarts = make_watchdog(
        monkeypatch, memory_mb=13_000, api=FakeApi(down=True), skip_if_players_online=False
    )
    asyncio.run(wd._tick())
    assert restarts == [(13_000, 0, False)]


def test_hold_keeps_confirmation_so_restart_fires_on_recovery(monkeypatch):
    # Confirmation built up while the API was dark isn't thrown away: the
    # first poll with visibility (and an empty server) restarts immediately.
    wd, _, restarts = make_watchdog(
        monkeypatch, memory_mb=13_000, api=FakeApi(down=True), samples=2
    )
    asyncio.run(wd._tick())  # over-limit sample 1 of 2
    asyncio.run(wd._tick())  # confirmed, but player list unknown -> hold
    assert restarts == []
    wd._api = FakeApi(players=[])
    asyncio.run(wd._tick())
    assert restarts == [(13_000, 0, False)]


# ---------------- frame-time / FPS watchdog ----------------


class FpsApi:
    """metrics() reports a fixed FPS/player count; players() for the memory path."""

    def __init__(self, fps, players=0, down=False):
        self._fps, self._players, self._down = fps, players, down

    async def metrics(self):
        if self._down:
            raise RuntimeError("REST API down")
        return types.SimpleNamespace(server_fps=self._fps, current_players=self._players)

    async def players(self):
        return [types.SimpleNamespace(name=f"p{i}") for i in range(self._players)]


def make_fps_watchdog(monkeypatch, *, fps_api, min_fps=8, samples=2, memory_mb=1_000):
    cfg = Config()
    cfg.watchdog.fps_restart = True
    cfg.watchdog.min_server_fps = min_fps
    cfg.watchdog.fps_consecutive_samples = samples
    bus = FakeBus()
    wd = Watchdog(cfg, fps_api, bus)
    monkeypatch.setattr(
        watchdog_mod.procs, "proc_stats",
        lambda: types.SimpleNamespace(memory_mb=memory_mb),  # memory path stays quiet
    )
    restarts = []

    async def record(fps, players):
        restarts.append((fps, players))

    monkeypatch.setattr(wd, "_fps_restart", record)
    return wd, bus, restarts


def test_fps_restart_needs_consecutive_samples(monkeypatch):
    wd, _, restarts = make_fps_watchdog(monkeypatch, fps_api=FpsApi(fps=3), samples=3)
    asyncio.run(wd._tick())
    asyncio.run(wd._tick())
    assert restarts == []          # 2 of 3 — not confirmed yet
    asyncio.run(wd._tick())
    assert restarts == [(3, 0)]    # confirmed on the third


def test_fps_recovery_resets_the_streak(monkeypatch):
    api = FpsApi(fps=3)
    wd, _, restarts = make_fps_watchdog(monkeypatch, fps_api=api, samples=2)
    asyncio.run(wd._tick())        # low sample 1
    wd._api = FpsApi(fps=30)
    asyncio.run(wd._tick())        # healthy again — streak resets
    wd._api = api
    asyncio.run(wd._tick())        # low sample 1 (again)
    assert restarts == []


def test_fps_zero_is_a_blip_not_a_collapse(monkeypatch):
    wd, _, restarts = make_fps_watchdog(monkeypatch, fps_api=FpsApi(fps=0), samples=1)
    asyncio.run(wd._tick())
    assert restarts == []  # booting / API hiccup readings never trigger


def test_fps_holds_off_with_players_online(monkeypatch):
    wd, bus, restarts = make_fps_watchdog(
        monkeypatch, fps_api=FpsApi(fps=3, players=4), samples=1
    )
    asyncio.run(wd._tick())
    asyncio.run(wd._tick())
    assert restarts == []
    holds = [e for e in bus.events if e.data.get("action") == "deferred"]
    assert len(holds) == 1  # announced once, not every poll


def test_fps_watchdog_off_by_default(monkeypatch):
    # Config defaults leave fps_restart False — the tick must not even call
    # metrics() (the memory-path FakeApi has no metrics(), which is the point).
    api = FakeApi(players=[])
    wd, bus, restarts = make_watchdog(monkeypatch, memory_mb=1_000, api=api)
    asyncio.run(wd._tick())
    assert restarts == []
