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

from . import procs  # noqa: F401  (tests patch service control through this module)
from .api import PalApi
from .config import Config
from .control import ServerController
from .events import Event, EventBus

COOLDOWN = timedelta(minutes=20)


class Watchdog:
    def __init__(
        self,
        cfg: Config,
        api: PalApi,
        bus: EventBus,
        control: ServerController | None = None,
    ) -> None:
        self._cfg = cfg
        self._api = api
        self._bus = bus
        self._control = control or ServerController(cfg, api)
        self._over = 0
        self._last_restart: datetime | None = None
        self._restarting = False
        self._hold_notified = False
        # Frame-time/FPS watchdog state, independent of the memory counters.
        self._fps_low = 0
        self._fps_hold_notified = False

    @property
    def is_restarting(self) -> bool:
        return self._restarting

    def reconfigure(self, cfg: Config, api: PalApi) -> None:
        self._cfg = cfg
        self._api = api
        self._control.reconfigure(cfg, api)

    async def run(self) -> None:
        while True:
            # Re-read every cycle so a config reload takes effect immediately.
            wd = self._cfg.watchdog
            try:
                if wd.enabled and not self._restarting:
                    await self._tick()
            except Exception as e:
                await self._bus.emit(Event("error", f"Watchdog tick failed: {e}"))
            await asyncio.sleep(max(1, wd.poll_seconds))

    async def _tick(self) -> None:
        wd = self._cfg.watchdog

        # Frame-rate collapse can make a server unplayable while memory is still
        # under the limit, so the memory watchdog never fires. Checked first and
        # only when opted in (default off), so the memory path — and its tests,
        # whose fake API has no metrics() — is untouched.
        if wd.fps_restart and wd.min_server_fps > 0 and await self._fps_tick():
            return

        stats = await asyncio.to_thread(procs.proc_stats)  # psutil off the event loop
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
        op = self._control.try_operation("watchdog-restart")
        if op is None:
            # Another operation (update, restore, scheduled restart) owns the
            # server. Skip — memory is still over the line, so the next tick
            # re-evaluates once the server is free.
            return

        self._restarting = True
        wd = self._cfg.watchdog
        warn = wd.warn_seconds if player_count else 5

        try:
            await self._locked_restart(op, memory_mb, player_count, hard, warn)
        finally:
            self._restarting = False

    async def _locked_restart(
        self, op, memory_mb: float, player_count: int, hard: bool, warn: int
    ) -> None:
        async with op:
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

            await self._control.save_best_effort()

            # Graceful: the game warns players and counts down, then exits cleanly.
            try:
                await self._api.shutdown(
                    warn, f"Automatic restart (memory {memory_mb:,.0f}MB)"
                )
            except Exception:
                pass

            await asyncio.sleep(warn + 15)
            came_back = await self._control.restart_cycle(
                escalate=True,
                on_escalate=lambda m: self._bus.emit(
                    Event("watchdog", f"🔨 {m}", {"action": "force_stop"})
                ),
            )
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

    # ---------- frame-time / FPS watchdog ----------

    async def _fps_tick(self) -> bool:
        """Restart on a sustained frame-rate collapse. Mirrors the memory logic's
        guard rails — N consecutive bad samples, the shared cooldown, and the same
        hold-off-if-players-online courtesy (no hard-limit override: unlike memory
        there's no 'the server is going to die anyway' threshold, so a slideshow
        with players waits for the server to empty rather than kicking everyone).
        Returns True only when it actually restarts."""
        wd = self._cfg.watchdog
        try:
            m = await self._api.metrics()
        except Exception:
            # Can't read FPS (API down). That's the crash/hang path's job, not
            # this one — don't count it as a low-FPS sample.
            self._fps_low = 0
            return False

        fps = m.server_fps
        # A reported 0 means booting / an API blip, not a real reading.
        if fps <= 0 or fps >= wd.min_server_fps:
            self._fps_low = 0
            self._fps_hold_notified = False
            return False

        self._fps_low += 1
        if self._fps_low < max(1, wd.fps_consecutive_samples):
            return False
        if self._last_restart and datetime.now(UTC) - self._last_restart < COOLDOWN:
            return False

        players = m.current_players
        if players and wd.skip_if_players_online:
            if not self._fps_hold_notified:
                self._fps_hold_notified = True
                await self._bus.emit(
                    Event(
                        "watchdog",
                        f"🐌 Server FPS at **{fps}** (below {wd.min_server_fps}) but "
                        f"{players} player(s) online — holding off the restart until "
                        "the server empties.",
                        {"server_fps": fps, "players": players,
                         "action": "deferred", "trigger": "fps"},
                    )
                )
            return False

        await self._fps_restart(fps, players)
        return True

    async def _fps_restart(self, fps: int, player_count: int) -> None:
        op = self._control.try_operation("watchdog-restart")
        if op is None:
            return
        self._restarting = True
        wd = self._cfg.watchdog
        warn = wd.warn_seconds if player_count else 5
        try:
            async with op:
                await self._bus.emit(
                    Event(
                        "watchdog",
                        f"🐌 Server FPS collapsed to **{fps}** (below "
                        f"{wd.min_server_fps}) — restarting in {warn}s. "
                        f"{player_count} player(s) online.",
                        {"server_fps": fps, "players": player_count,
                         "action": "restarting", "trigger": "fps"},
                    )
                )
                await self._control.save_best_effort()
                try:
                    await self._api.shutdown(warn, f"Automatic restart (low FPS: {fps})")
                except Exception:
                    pass
                await asyncio.sleep(warn + 15)
                came_back = await self._control.restart_cycle(
                    escalate=True,
                    on_escalate=lambda msg: self._bus.emit(
                        Event("watchdog", f"🔨 {msg}", {"action": "force_stop"})
                    ),
                )
                self._last_restart = datetime.now(UTC)
                self._fps_low = 0
                self._fps_hold_notified = False
                await self._bus.emit(
                    Event(
                        "watchdog",
                        "✅ Server back up after FPS restart."
                        if came_back
                        else "❌ Server did **not** come back after the FPS restart. "
                             "Needs a look.",
                        {"recovered": came_back, "action": "restarted", "trigger": "fps"},
                    )
                )
        finally:
            self._restarting = False
