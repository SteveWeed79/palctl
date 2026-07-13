"""
The daemon. Headless, wrapped in NSSM, always running.

This is the part that matters. It runs whether or not you're at the PC, whether
or not the GUI is open. It polls, it diffs, it watches memory, it schedules, and
it talks to Discord.

The GUI is a *view* onto this. It can be closed and the server is still managed.

Also exposes a tiny localhost HTTP API so the GUI (a separate process) can read
state and issue commands without duplicating any of this logic.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict

from aiohttp import web

from . import backups, procs
from .api import PalApi, PalApiError
from .bot import run_bot
from .config import Config, get_admin_password
from .events import Event, EventBus, PlayerTracker, SessionStore
from .scheduler import Scheduler
from .watchdog import Watchdog

DAEMON_PORT = 8830  # localhost only


class Daemon:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self.bus = EventBus()
        self.store = SessionStore()
        self.api = PalApi(
            self.cfg.api_host, self.cfg.api_port, get_admin_password()
        )
        self.tracker = PlayerTracker(self.bus, self.store)
        self.scheduler = Scheduler(self.cfg, self.api, self.bus)
        self.watchdog = Watchdog(self.cfg, self.api, self.bus)

        # None = haven't polled yet; don't announce "up" just because the
        # daemon (not the server) was restarted.
        self._alive: bool | None = None
        self._last_metrics = None
        self._history: list[dict] = []  # rolling metrics, for the GUI graphs
        self._tasks: set[asyncio.Task] = set()

        self.bus.on_any(self._persist)

    def _spawn(self, coro) -> None:
        # asyncio holds only weak refs to tasks; keep one or it can be GC'd mid-run.
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _persist(self, e: Event) -> None:
        await asyncio.to_thread(self.store.log_event, e)

    # ---------- polling ----------

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll()
            except Exception as e:
                await self.bus.emit(Event("error", f"Poll failed: {e}"))
            await asyncio.sleep(self.cfg.poll_seconds)

    async def _poll(self) -> None:
        try:
            metrics = await self.api.metrics()
            players = await self.api.players()
        except PalApiError:
            if self._alive:
                await self.tracker.handle_server_down()
                await self.bus.emit(Event("server_down", "🔴 Server is **down**."))
            self._alive = False
            return

        if self._alive is False:
            await self.bus.emit(Event("server_up", "🟢 Server is **up**."))
        self._alive = True

        await self.tracker.update(players)

        stats = procs.proc_stats()
        self._last_metrics = metrics
        self._history.append(
            {
                "fps": metrics.server_fps,
                "frame_time": metrics.server_frame_time,
                "players": metrics.current_players,
                "memory_mb": stats.memory_mb if stats else 0.0,
                "cpu": stats.cpu_percent if stats else 0.0,
            }
        )
        del self._history[:-720]  # ~2h at 10s polling

    # ---------- localhost API for the GUI ----------

    def _routes(self) -> web.Application:
        app = web.Application()

        async def state(_: web.Request) -> web.Response:
            stats = procs.proc_stats()
            return web.json_response(
                {
                    "service": procs.service_state(self.cfg.service_name),
                    "alive": self._alive,
                    "restarting": self.watchdog.is_restarting,
                    "metrics": asdict(self._last_metrics) if self._last_metrics else None,
                    "process": asdict(stats) if stats else None,
                    "players": [asdict(p) for p in self.tracker.online],
                    "history": self._history[-360:],
                    "events": [
                        {"kind": e.kind, "message": e.message, "at": e.at.isoformat()}
                        for e in self.bus.recent(60)
                    ],
                }
            )

        async def action(request: web.Request) -> web.Response:
            body = await request.json() if request.can_read_body else {}
            what = request.match_info["what"]

            try:
                if what == "start":
                    await procs.start_service(self.cfg.service_name)
                elif what == "stop":
                    with contextlib.suppress(PalApiError):
                        await self.api.save()
                    await procs.stop_service(self.cfg.service_name)
                elif what == "restart":
                    self._spawn(
                        self.scheduler.restart_with_countdown(
                            body.get("reason", "Admin restart")
                        )
                    )
                elif what == "announce":
                    await self.api.announce(body["message"])
                elif what == "save":
                    await self.api.save()
                elif what == "backup":
                    self._spawn(self.scheduler.backup_now("gui"))
                elif what == "kick":
                    await self.api.kick(body["user_id"], body.get("reason", ""))
                elif what == "ban":
                    await self.api.ban(body["user_id"], body.get("reason", ""))
                elif what == "reload-config":
                    self.cfg = Config.load()
                    self.api = PalApi(
                        self.cfg.api_host, self.cfg.api_port, get_admin_password()
                    )
                    # The workers hold their own cfg/api references; swap them
                    # too or the reload silently changes nothing.
                    self.scheduler.reconfigure(self.cfg, self.api)
                    self.watchdog.reconfigure(self.cfg, self.api)
                else:
                    return web.json_response({"error": f"unknown action {what}"}, status=400)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

            return web.json_response({"ok": True})

        async def list_backups(_: web.Request) -> web.Response:
            from pathlib import Path

            bs = await asyncio.to_thread(backups.listing, Path(self.cfg.backup_root))
            return web.json_response(
                [{"name": b.name, "size_mb": b.size_mb} for b in bs]
            )

        app.router.add_get("/state", state)
        app.router.add_get("/backups", list_backups)
        app.router.add_post("/action/{what}", action)
        return app

    # ---------- run ----------

    async def run(self) -> None:
        runner = web.AppRunner(self._routes())
        await runner.setup()
        # 127.0.0.1 only. This API has no auth; it must never leave the box.
        await web.TCPSite(runner, "127.0.0.1", DAEMON_PORT).start()
        print(f"[daemon] localhost API on 127.0.0.1:{DAEMON_PORT}")

        await asyncio.gather(
            self._poll_loop(),
            self.watchdog.run(),
            self.scheduler.run(),
            run_bot(self.cfg, self.api, self.bus, self.store, self.scheduler),
        )


def main() -> None:
    try:
        asyncio.run(Daemon().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
