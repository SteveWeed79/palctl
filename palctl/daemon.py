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
import secrets
import sys
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from . import backups, localauth, procs
from .api import PalApi, PalApiError
from .bot import run_bot
from .config import Config, config_dir, get_admin_password
from .events import Event, EventBus, PlayerTracker, SessionStore
from .logging_setup import setup_logging
from .scheduler import Scheduler
from .watchdog import Watchdog

DAEMON_PORT = 8830  # localhost only
SERVICE_NAME = "palctl-daemon"  # the Windows service name NSSM registers


def _within_window(times: list[float], now: float, window: float = 3600.0) -> list[float]:
    """Timestamps from the last `window` seconds. Used to rate-limit auto-recovery."""
    return [t for t in times if t >= now - window]


def autorecover_phase(
    *,
    enabled: bool,
    ever_alive: bool,
    busy: bool,
    restarting: bool,
    desired_running: bool,
) -> str:
    """
    First half of the crash-recovery decision — the guards. Pure, so the whole
    'never fight an intentional stop' rule is testable without a live daemon.

    Returns:
      'ignore' — feature off, or the server never came up; do nothing.
      'reset'  — palctl took the server down on purpose (stop/restart/update/
                 restore/watchdog); clear the down-streak, do nothing.
      'count'  — a genuine unexpected outage; count this poll toward recovery.
    """
    if not enabled or not ever_alive:
        return "ignore"
    if busy or restarting or not desired_running:
        return "reset"
    return "count"


def should_recover_now(
    *, down_polls: int, confirm_polls: int, recent_restarts: int, cap: int
) -> bool:
    """Second half: only recover after N confirming polls, and not if we've
    already restarted `cap` times this hour (a real crash-loop needs a human)."""
    if down_polls < max(1, confirm_polls):
        return False
    return recent_restarts < cap


def make_auth_middleware(token: str):
    """aiohttp middleware that rejects any request without the shared token."""

    @web.middleware
    async def _auth(request: web.Request, handler):
        sent = request.headers.get(localauth.TOKEN_HEADER, "")
        if not secrets.compare_digest(sent, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    return _auth


class Daemon:
    def __init__(self) -> None:
        self.log = setup_logging()
        self._token = localauth.get_or_create_token()
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

        # Crash/hang auto-recovery bookkeeping.
        self._ever_alive = False           # only recover a server that WAS up
        self._desired_running = True       # user "Stop" flips this off
        self._busy = False                 # a palctl op (restart/update/restore) is running
        self._down_polls = 0               # consecutive unreachable polls
        self._autorestart_times: list[float] = []

        self.bus.on_any(self._persist)
        self.bus.on_any(self._log_event)

    def _spawn(self, coro) -> None:
        # asyncio holds only weak refs to tasks; keep one or it can be GC'd mid-run.
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _spawn_op(self, coro) -> None:
        """Spawn a palctl-initiated server operation, holding `_busy` for its
        duration so crash auto-recovery doesn't fight an intentional stop."""
        async def _run() -> None:
            self._busy = True
            try:
                await coro
            finally:
                self._busy = False

        self._spawn(_run())

    async def _persist(self, e: Event) -> None:
        await asyncio.to_thread(self.store.log_event, e)

    async def _log_event(self, e: Event) -> None:
        level = self.log.error if e.kind == "error" else self.log.info
        level("%s: %s", e.kind, e.message)

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
            await self._maybe_autorecover()
            return

        if self._alive is False:
            await self.bus.emit(Event("server_up", "🟢 Server is **up**."))
        self._alive = True
        self._ever_alive = True
        self._down_polls = 0

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

    # ---------- crash / hang auto-recovery ----------

    async def _maybe_autorecover(self) -> None:
        """
        Called on every poll where the REST API is unreachable. Brings the server
        back only when it was up before, palctl didn't stop it, and we haven't
        already restarted too many times this hour.
        """
        wd = self.cfg.watchdog
        phase = autorecover_phase(
            enabled=wd.auto_restart_on_crash,
            ever_alive=self._ever_alive,
            busy=self._busy,
            restarting=self.watchdog.is_restarting,
            desired_running=self._desired_running,
        )
        if phase == "ignore":
            return
        if phase == "reset":
            self._down_polls = 0
            return

        # phase == "count": a genuine unexpected outage.
        self._down_polls += 1
        now = time.time()
        self._autorestart_times = _within_window(self._autorestart_times, now)
        if not should_recover_now(
            down_polls=self._down_polls,
            confirm_polls=wd.crash_confirm_polls,
            recent_restarts=len(self._autorestart_times),
            cap=wd.crash_restart_max_per_hour,
        ):
            return

        self._down_polls = 0
        self._autorestart_times.append(now)
        await self.bus.emit(
            Event(
                "watchdog",
                "🚑 Server unreachable and palctl didn't stop it — auto-recovering.",
                {"action": "autorecover"},
            )
        )
        self._spawn_op(self._autorecover())

    async def _autorecover(self) -> None:
        try:
            # Stop first, in case it's hung rather than gone — then start clean.
            await procs.stop_service(self.cfg.service_name)
            await asyncio.sleep(2)
            await procs.start_service(self.cfg.service_name)
            ok = await self.api.wait_until_alive(timeout=240)
            await self.bus.emit(
                Event(
                    "watchdog",
                    "✅ Server recovered."
                    if ok
                    else "❌ Auto-recover ran but the server is still down.",
                    {"recovered": ok},
                )
            )
        except Exception as e:
            await self.bus.emit(Event("error", f"Auto-recover failed: {e}"))

    # ---------- localhost API for the GUI ----------

    def _routes(self) -> web.Application:
        # Every request must carry the shared token — see localauth.
        app = web.Application(middlewares=[make_auth_middleware(self._token)])

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
                    self._desired_running = True
                    await procs.start_service(self.cfg.service_name)
                elif what == "stop":
                    # Intentional: don't let auto-recovery start it back up.
                    self._desired_running = False
                    with contextlib.suppress(PalApiError):
                        await self.api.save()
                    await procs.stop_service(self.cfg.service_name)
                elif what == "restart":
                    self._desired_running = True
                    self._spawn_op(
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
                elif what == "update-server":
                    self._desired_running = True
                    self._spawn_op(self.scheduler.update_server())
                elif what == "restore":
                    self._desired_running = True
                    self._spawn_op(self.scheduler.restore_backup(body["name"]))
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
        self.log.info("daemon up; localhost API on 127.0.0.1:%d", DAEMON_PORT)

        if self.cfg.check_for_updates:
            self._spawn(self._check_palctl_update())

        await asyncio.gather(
            self._poll_loop(),
            self.watchdog.run(),
            self.scheduler.run(),
            self._update_check_loop(),
            run_bot(self.cfg, self.api, self.bus, self.store, self.scheduler),
        )

    async def _update_check_loop(self) -> None:
        """Ask Steam whether a newer server build exists, a couple of minutes
        after start and then every few hours. Purely a notification."""
        await asyncio.sleep(120)
        while True:
            try:
                await self.scheduler.check_update_available()
            except Exception as e:
                self.log.warning("server update check failed: %s", e)
            await asyncio.sleep(6 * 3600)

    async def _check_palctl_update(self) -> None:
        from . import __version__, selfupdate

        try:
            newer = await asyncio.to_thread(selfupdate.check)
        except Exception:
            newer = None
        if newer:
            await self.bus.emit(
                Event(
                    "update_available",
                    f"⬆️ palctl **{newer}** is available (you have {__version__}). "
                    "Grab it from the GitHub releases.",
                    {"component": "palctl", "latest": newer},
                )
            )


def service_target() -> tuple[str, str, str]:
    """
    (exe, args, app_dir) to run *this* daemon as a Windows service — correct
    whether we're a PyInstaller-frozen palctl-daemon.exe or `python -m
    palctl.daemon` in a dev checkout. The installer and the wizard both register
    the service off this, so there's one source of truth for "how do you run me".
    """
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, "", str(Path(exe).parent)
    return sys.executable, "-m palctl.daemon", str(Path(__file__).resolve().parents[1])


def install_service() -> None:
    """Register (and start) the palctl daemon as a Windows service via NSSM."""
    from . import winservice

    nssm = winservice.ensure_nssm(config_dir() / "bin")
    exe, args, app_dir = service_target()
    winservice.install_service(nssm, SERVICE_NAME, exe, args, app_dir)
    print(f"[daemon] service '{SERVICE_NAME}' installed and started.")


def uninstall_service() -> None:
    from . import winservice

    nssm = winservice.ensure_nssm(config_dir() / "bin")
    winservice.remove_service(nssm, SERVICE_NAME)
    print(f"[daemon] service '{SERVICE_NAME}' removed.")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="palctl-daemon")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "install-service", "uninstall-service"],
        help="run the daemon (default), or (un)register it as a Windows service",
    )
    args = parser.parse_args()

    if args.command == "install-service":
        install_service()
        return
    if args.command == "uninstall-service":
        uninstall_service()
        return

    try:
        asyncio.run(Daemon().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
