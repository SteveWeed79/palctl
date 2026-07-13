"""
The one owner of "take the server down and bring it back".

Server-stopping operations start from five places: the memory watchdog, the
daily scheduled restart, SteamCMD updates (manual and scheduled), backup
restores, and crash auto-recovery — plus the user's own Start/Stop buttons.
Before this module each of them drove sc.exe/systemctl directly and only some
of them knew about each other, so a scheduled restart could fire mid-update,
or a watchdog restart could race a restore.

The controller serialises all of that behind one asyncio.Lock, names whatever
operation currently holds it (so the GUI and CLI can say *why* the server is
busy), and owns the one copy of the stop → start → wait-until-alive cycle.

Two acquisition styles, matching two kinds of caller:

  operation(name)      waits its turn.   For things a human asked for —
                       restart, update, restore. Queueing is what they expect.
  try_operation(name)  skips if busy.    For opportunistic automation — the
                       watchdog and auto-recovery re-evaluate every poll
                       anyway, and must never queue up behind a 10-minute
                       countdown only to restart a server that was just fixed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from . import procs
from .api import PalApi
from .config import Config


class ServerController:
    def __init__(self, cfg: Config, api: PalApi) -> None:
        self._cfg = cfg
        self._api = api
        self._lock = asyncio.Lock()
        self._current: str | None = None

    def reconfigure(self, cfg: Config, api: PalApi) -> None:
        self._cfg = cfg
        self._api = api

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def current_op(self) -> str | None:
        """Name of the operation holding the lock, or None."""
        return self._current

    @contextlib.asynccontextmanager
    async def operation(self, name: str) -> AsyncIterator[None]:
        async with self._lock:
            self._current = name
            try:
                yield
            finally:
                self._current = None

    def try_operation(self, name: str):
        """The non-blocking variant: the context manager if the server is free,
        None if another operation holds it. No await between the check and the
        acquisition (callers enter the context immediately), so on a single
        event loop this doesn't race."""
        if self._lock.locked():
            return None
        return self.operation(name)

    # ---------- the primitives every operation is built from ----------
    #
    # These do NOT take the lock — they run inside an operation()/
    # try_operation() block, which is what makes composing them safe.

    async def save_best_effort(self, settle: float = 0.0) -> bool:
        """Flush the world to disk if the API answers; never raises. `settle`
        gives the server a moment to finish writing before we act on the files."""
        try:
            await self._api.save()
        except Exception:
            return False
        if settle:
            await asyncio.sleep(settle)
        return True

    async def stop(self) -> bool:
        return await procs.stop_service(self._cfg.service_name)

    async def start(self) -> bool:
        return await procs.start_service(self._cfg.service_name)

    async def restart_cycle(self, *, stop_delay: float = 3.0, timeout: float = 240.0) -> bool:
        """Stop, breathe, start, and wait for the REST API to answer again.
        Returns whether the server actually came back."""
        await self.stop()
        await asyncio.sleep(stop_delay)
        await self.start()
        return await self._api.wait_until_alive(timeout=timeout)
