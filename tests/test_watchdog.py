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
