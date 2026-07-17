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
from collections.abc import AsyncIterator, Awaitable, Callable

from . import procs
from .api import PalApi
from .config import Config

# Awaited once per escalation step with a human-readable message, so the caller
# (which owns an event bus; the controller does not) can surface a hard kill to
# the operator. None means "nobody's listening" — the escalation still happens.
EscalateNotify = Callable[[str], Awaitable[None]]


async def _notify(cb: EscalateNotify | None, message: str) -> None:
    if cb is not None:
        await cb(message)


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

    async def stop(
        self, *, escalate: bool = False, on_escalate: EscalateNotify | None = None
    ) -> bool:
        """Stop the service and confirm it reached STOPPED.

        escalate=False (default): a plain service stop. A server hung in
        STOP_PENDING honestly returns False and a human deals with it — the
        right behaviour for the user's own Stop button.

        escalate=True: for unattended callers (watchdog, auto-recovery,
        scheduled restart) where honest failure fixes nothing — the watchdog
        would just retry the same ineffective stop every cooldown. If the
        service stop times out with the process still alive, terminate() then
        kill() the PID find_process() knows — the only thing that can actually
        clear a PalServer that ignores the SCM stop. `on_escalate` is notified
        at each step so the operator learns a hard kill happened (which can lose
        the last unsaved interval — callers run save_best_effort first)."""
        if await procs.stop_service(self._cfg.service_name):
            return True
        if not escalate:
            return False
        return await self._force_stop(on_escalate)

    async def _force_stop(self, on_escalate: EscalateNotify | None) -> bool:
        """The terminate → kill ladder, reached only when stop(escalate=True)
        times out. Confirms the service manager reaches STOPPED at the end."""
        proc = await asyncio.to_thread(procs.find_process)
        if proc is None:
            # Nothing to signal — the service is wedged with no process behind
            # it, or the process died between the stop timeout and now. Give the
            # service manager a moment to settle on STOPPED.
            return await procs.wait_stopped(self._cfg.service_name)

        pid = proc.pid
        await _notify(
            on_escalate,
            f"Server ignored the stop (PID {pid} still alive after the timeout) "
            "— terminating it. The last unsaved interval may be lost; a save was "
            "attempted first.",
        )
        if await procs.terminate_process(proc):
            return await procs.wait_stopped(self._cfg.service_name)

        await _notify(
            on_escalate,
            f"PID {pid} survived terminate — hard-killing it.",
        )
        await procs.kill_process(proc)
        return await procs.wait_stopped(self._cfg.service_name)

    async def start(self) -> bool:
        return await procs.start_service(self._cfg.service_name)

    async def restart_cycle(
        self,
        *,
        stop_delay: float = 3.0,
        timeout: float = 240.0,
        escalate: bool = False,
        on_escalate: EscalateNotify | None = None,
    ) -> bool:
        """Stop, breathe, start, and wait for the REST API to answer again.
        Returns whether the server actually came back. `escalate`/`on_escalate`
        are forwarded to stop() — see there for the hung-server force-kill."""
        await self.stop(escalate=escalate, on_escalate=on_escalate)
        await asyncio.sleep(stop_delay)
        await self.start()
        return await self._api.wait_until_alive(timeout=timeout)
