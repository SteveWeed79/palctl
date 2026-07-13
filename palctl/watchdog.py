"""
Memory-leak watchdog.

Palworld's dedicated server leaks. It's the single most common cause of a server
that was fine yesterday and is unplayable today, and the community answer is
"just restart it on a timer" — which either restarts too often (kicking people
for no reason) or too rarely (the server is already a slideshow by then).

A timer restarts on the clock. This restarts on the *symptom*: actual resident
memory of PalServer-Win64-Shipping.exe, read from the OS.

Guard rails, because an auto-restarter that misfires is worse than no
auto-restarter:

  * N consecutive samples over the line, not one spike
  * won't restart out from under people mid-session, unless memory crosses a
    hard limit where the server is going to die anyway
  * announces in-game, counts down, saves the world, then restarts
  * cooldown, so a server that comes back up still bloated doesn't loop
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from . import procs
from .api import PalApi
from .config import Config
from .events import Event, EventBus

COOLDOWN = timedelta(minutes=20)


class Watchdog:
    def __init__(self, cfg: Config, api: PalApi, bus: EventBus) -> None:
        self._cfg = cfg
        self._api = api
        self._bus = bus
        self._over = 0
        self._last_restart: datetime | None = None
        self._restarting = False
        self._hold_notified = False

    @property
    def is_restarting(self) -> bool:
        return self._restarting

    def reconfigure(self, cfg: Config, api: PalApi) -> None:
        self._cfg = cfg
        self._api = api

    async def run(self) -> None:
        while True:
            # Re-read every cycle so a config reload takes effect immediately.
            wd = self._cfg.watchdog
            try:
                if wd.enabled and not self._restarting:
                    await self._tick()
            except Exception as e:
                await self._bus.emit(Event("error", f"Watchdog tick failed: {e}"))
            await asyncio.sleep(wd.poll_seconds)

    async def _tick(self) -> None:
        wd = self._cfg.watchdog

        stats = procs.proc_stats()
        if stats is None:
            self._over = 0
            return

        if stats.memory_mb < wd.memory_limit_mb:
            self._over = 0
            self._hold_notified = False
            return

        self._over += 1
        if self._over < wd.consecutive_samples:
            return  # could be a transient spike; wait for confirmation

        if self._last_restart and datetime.now(UTC) - self._last_restart < COOLDOWN:
            return  # already restarted recently; don't loop

        hard = stats.memory_mb >= wd.hard_limit_mb

        # None = the REST API didn't answer, which is different from "nobody
        # online": we can't warn anyone in-game and we can't know the server
        # is empty.
        players: list | None = None
        try:
            players = await self._api.players()
        except Exception:
            pass

        if players is None and wd.skip_if_players_online and not hard:
            # Below the hard limit, an unknown player list gets the same
            # benefit of the doubt as a populated one. A hung API is crash
            # auto-recovery's job; _over stays as-is so the restart fires on
            # the first tick where we can see again.
            if not self._hold_notified:
                await self._bus.emit(
                    Event(
                        "watchdog",
                        f"⚠️ Memory at **{stats.memory_mb:,.0f} MB** (limit "
                        f"{wd.memory_limit_mb:,}) but the player list is "
                        f"unreachable — holding off in case anyone is online. "
                        f"Will force a restart above {wd.hard_limit_mb:,} MB.",
                        {"memory_mb": stats.memory_mb, "players": None,
                         "action": "deferred"},
                    )
                )
                self._hold_notified = True
            return

        if players and wd.skip_if_players_online and not hard:
            # Keep _over as-is: the moment the last player leaves (or memory
            # crosses the hard limit) the restart fires without re-confirming.
            if not self._hold_notified:
                await self._bus.emit(
                    Event(
                        "watchdog",
                        f"⚠️ Memory at **{stats.memory_mb:,.0f} MB** (limit "
                        f"{wd.memory_limit_mb:,}) but {len(players)} player(s) online — "
                        f"holding off. Will force a restart above "
                        f"{wd.hard_limit_mb:,} MB.",
                        {"memory_mb": stats.memory_mb, "players": len(players),
                         "action": "deferred"},
                    )
                )
                self._hold_notified = True
            return

        await self._restart(stats.memory_mb, len(players or []), hard)

    async def _restart(self, memory_mb: float, player_count: int, hard: bool) -> None:
        self._restarting = True
        wd = self._cfg.watchdog
        warn = wd.warn_seconds if player_count else 5

        try:
            await self._bus.emit(
                Event(
                    "watchdog",
                    f"🔁 Memory at **{memory_mb:,.0f} MB**"
                    + (" (**hard limit**)" if hard else "")
                    + f" — restarting in {warn}s. {player_count} player(s) online.",
                    {"memory_mb": memory_mb, "players": player_count,
                     "hard": hard, "action": "restarting"},
                )
            )

            try:
                await self._api.save()
            except Exception:
                pass

            # Graceful: the game warns players and counts down, then exits cleanly.
            try:
                await self._api.shutdown(
                    warn, f"Automatic restart (memory {memory_mb:,.0f}MB)"
                )
            except Exception:
                pass

            await asyncio.sleep(warn + 15)
            await procs.stop_service(self._cfg.service_name)
            await asyncio.sleep(3)
            await procs.start_service(self._cfg.service_name)

            came_back = await self._api.wait_until_alive(timeout=240)
            self._last_restart = datetime.now(UTC)
            self._over = 0
            self._hold_notified = False

            await self._bus.emit(
                Event(
                    "watchdog",
                    "✅ Server back up after memory restart."
                    if came_back
                    else "❌ Server did **not** come back after the memory restart. "
                         "Needs a look.",
                    {"recovered": came_back, "action": "restarted"},
                )
            )
        finally:
            self._restarting = False
