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
import json
import os
import secrets
import sys
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from . import backups, inifile, leak, localauth, netinfo, procs
from .api import PalApi, PalApiError
from .bot import run_bot
from .client import DAEMON_PORT
from .config import Config, config_dir, get_admin_password
from .control import ServerController
from .events import Event, EventBus, PlayerTracker, SessionStore
from .logging_setup import setup_logging
from .scheduler import Scheduler
from .watchdog import Watchdog

SERVICE_NAME = "palctl-daemon"  # the Windows service name NSSM registers

# The admin's Stop intent, persisted so it survives daemon restarts (crash +
# NSSM restart, a palctl upgrade, a manual service bounce). Without this the
# in-memory flag resets to True and the daily restart / auto-update schedule
# would resurrect a server that was deliberately taken down for maintenance.
_STATE_PATH = config_dir() / "daemon_state.json"


def _load_desired_running() -> bool:
    try:
        state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return bool(state["desired_running"])
    except (OSError, ValueError, KeyError, TypeError):
        return True  # no/unreadable state (e.g. first run) = normal behavior


def _save_desired_running(value: bool) -> None:
    try:
        tmp = _STATE_PATH.with_name(_STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps({"desired_running": value}), encoding="utf-8")
        os.replace(tmp, _STATE_PATH)
    except OSError:
        pass  # best effort — worst case is the old resets-to-True behavior


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


def _busy_response(current_op: str | None) -> web.Response:
    """409: the server is mid-operation; the client should retry, not queue.
    Queueing a Start behind a 10-minute restart countdown surprises everyone."""
    return web.json_response(
        {"error": f"busy: {current_op or 'another operation'} is in progress"},
        status=409,
    )


def service_account_warning(username: str, cfg_dir: str) -> str | None:
    """
    The message to log when the daemon is running under a machine account
    (LocalSystem shows up as 'SYSTEM', or as 'HOSTNAME$' via %USERNAME%).
    Such an account has its own %APPDATA% and Credential Manager, so unless
    the service was registered by palctl (which redirects APPDATA) the daemon
    reads a DIFFERENT config and token than the user's GUI/CLI — the classic
    symptom is the GUI stuck on 'unauthorized'. Pure, so it's testable.
    """
    u = username.strip().lower()
    if u != "system" and not u.endswith("$"):
        return None
    return (
        f"Running as '{username}', a machine account with its own %APPDATA% and "
        f"Credential Manager. Config/token are being read from {cfg_dir}. If the "
        "GUI or CLI reports 'unauthorized', or your settings don't seem to apply, "
        "re-register the service under your account: "
        "palctl-daemon install-service --as-user"
    )


class _BadRequest(Exception):
    """A client error in an /action request (missing/invalid field). Surfaces as
    HTTP 400 with a useful message, rather than the bare KeyError repr + 500 a
    raw ``body["field"]`` would produce."""


# The dashboard inlines this same shield-with-heartbeat as its <link rel="icon">
# (a data: URI). Browsers still probe /favicon.ico on their own, so we serve it
# here too — otherwise every dashboard visit logs a 401 for a file the token
# gate had no business rejecting. SVG with the right content-type renders as a
# favicon in every current browser.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
    "stroke='#2a78d6' stroke-width='2' stroke-linecap='round' "
    "stroke-linejoin='round'>"
    "<path d='M12 2.5 L19.5 5.5 V11.5 C19.5 16.3 16.2 20.1 12 21.5 "
    "C7.8 20.1 4.5 16.3 4.5 11.5 V5.5 Z'/>"
    "<path d='M6.5 12 H9 L10.5 8.5 L13 15.5 L14.5 12 H17.5'/></svg>"
)


def lan_exposure_warning(host: str) -> str | None:
    """The line to log when the dashboard/control API is bound beyond loopback.

    Binding to 0.0.0.0 is a deliberate opt-in (so the dashboard works from other
    devices on the LAN), but it moves the whole security boundary onto the token:
    it is now the only thing between the network and start/stop/restore/kick/ban,
    and it rides over plain HTTP. Fine on a home LAN, never on the internet — say
    so, once, at startup. Pure, so it's testable without a live socket."""
    if netinfo.is_loopback(host):
        return None
    return (
        f"dashboard/control API bound to {host} — it is now reachable from other "
        "devices on this network. The per-user token in the dashboard URL is the "
        "only credential and it travels over plain HTTP, so keep this to a LAN "
        f"you trust and NEVER port-forward port {DAEMON_PORT} to the internet."
    )


def make_auth_middleware(token: str, exempt: frozenset[str] = frozenset()):
    """aiohttp middleware that rejects any request without the shared token.
    `exempt` paths skip the check — only the dashboard page itself, which
    contains no data (its /state calls still need the token)."""

    @web.middleware
    async def _auth(request: web.Request, handler):
        if exempt and request.path in exempt:
            return await handler(request)
        sent = request.headers.get(localauth.TOKEN_HEADER, "")
        if not secrets.compare_digest(sent, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    return _auth


class Daemon:
    def __init__(self) -> None:
        self.log = setup_logging()
        self.log.info("config dir: %s", config_dir())
        self._warn_if_machine_account()
        self._token = localauth.get_or_create_token()
        self.cfg = Config.load()
        self.bus = EventBus()
        self.store = SessionStore()
        self.api = PalApi(
            self.cfg.api_host, self.cfg.api_port, self._admin_password()
        )
        self.tracker = PlayerTracker(self.bus, self.store)
        # One lock for everything that stops the server. The scheduler, the
        # watchdog, and auto-recovery all share it, so a scheduled restart
        # can't fire mid-update and a watchdog restart can't race a restore.
        self.control = ServerController(self.cfg, self.api)
        # `intent_running` lets the scheduler see the admin's Stop intent so a
        # time-triggered restart/update won't resurrect a deliberately-stopped
        # server. Read lazily (lambda) — `_desired_running` is set just below.
        self.scheduler = Scheduler(
            self.cfg, self.api, self.bus, self.control,
            intent_running=lambda: self._desired_running,
            # Lets a Discord /start or /stop persist the admin's intent through
            # the same property setter the GUI/CLI use, so auto-recovery never
            # fights a stop issued from Discord.
            set_intent=lambda running: setattr(self, "_desired_running", running),
        )
        self.watchdog = Watchdog(self.cfg, self.api, self.bus, self.control)
        self.bot = None  # set by run_bot if the Discord bot is enabled
        # The run_bot task. reload-config relaunches it when it has finished
        # (bot was disabled / token missing or rejected at the last attempt),
        # so enabling the bot from the GUI doesn't need a daemon restart.
        self._bot_task: asyncio.Task | None = None

        # None = haven't polled yet; don't announce "up" just because the
        # daemon (not the server) was restarted.
        self._alive: bool | None = None
        self._last_metrics = None
        # Rolling metrics for the GUI graphs and the leak forecaster. Seeded
        # from SQLite so a daemon restart doesn't blank the graphs.
        self._history: list[dict] = self.store.recent_metrics(720)
        self._tasks: set[asyncio.Task] = set()

        # Crash/hang auto-recovery bookkeeping. ("palctl is doing this on
        # purpose" now lives in the ServerController's operation lock.)
        self._ever_alive = False           # only recover a server that WAS up
        # User "Stop" flips this off. Loaded from disk so a daemon restart
        # can't forget an intentional stop (the setter persists changes).
        self._desired_running = _load_desired_running()
        self._down_polls = 0               # consecutive unreachable polls
        self._api_fail_streak = 0          # debounce for the down/up announcement
        self._autorestart_times: list[float] = []

        # Leak forecasting. _epoch_at marks the last server (re)start we saw:
        # samples from a previous process would poison the fit, so the
        # forecaster only looks at samples after it.
        self._epoch_at = 0.0
        self._predict_warned = False

        self.bus.on_any(self._persist)
        self.bus.on_any(self._log_event)

    def _warn_if_machine_account(self) -> None:
        try:
            import getpass

            warning = service_account_warning(getpass.getuser(), str(config_dir()))
        except Exception:
            # getuser() can fail in odd service environments; the warning is
            # best-effort diagnostics, never worth failing startup over.
            warning = None
        if warning:
            self.log.warning("%s", warning)

    def _sync_dashboard_firewall(self, host: str) -> None:
        """On Windows, a non-loopback bind still needs a firewall rule or other
        devices on the LAN can't reach the dashboard — so binding 0.0.0.0 alone
        is a silent no-op. Open the port (private networks) when LAN access is
        on, and close it again when off. Best-effort: a non-elevated daemon can't
        touch the firewall, so log the one-line manual command instead."""
        if not sys.platform.startswith("win"):
            return
        from . import firewall

        try:
            if netinfo.is_loopback(host):
                if firewall.remove_rule() == "removed":
                    self.log.info("closed the dashboard firewall rule (LAN access off)")
                return
            outcome = firewall.ensure_rule(DAEMON_PORT)
            if outcome == "added":
                self.log.info(
                    "opened the Windows Firewall for the dashboard on port %d "
                    "(private networks only)", DAEMON_PORT,
                )
            elif outcome == "failed":
                self.log.warning(
                    "couldn't open the Windows Firewall for the dashboard — other "
                    "devices on your LAN stay blocked until you run this once as "
                    "administrator:\n    %s", firewall.manual_add_command(DAEMON_PORT),
                )
        except Exception as e:  # firewall trouble must never break startup
            self.log.warning("dashboard firewall setup failed: %s", e)

    def _warn_if_cloud_mirror_broken(self) -> None:
        """If the backup mirror is an rclone remote that's misconfigured — no
        dedicated folder, or rclone not installed — every scheduled mirror will
        fail. Say so once at startup rather than only in a buried error event
        after the first backup."""
        from . import rclone

        target = self.cfg.backup_mirror
        if not target or not self.cfg.backup_mirror_enabled or not rclone.is_remote(target):
            return
        if not rclone.has_subpath(target):
            self.log.warning(
                "backup mirror '%s' points at the remote root — set a dedicated "
                "folder like `gdrive:PalworldBackups`, so retention only ever "
                "touches palctl's own backups and never the rest of your drive.",
                target,
            )
            return
        ok, detail = rclone.check()
        if not ok:
            self.log.warning(
                "backup mirror '%s' is a cloud remote but %s — install rclone "
                "and run `rclone config`, or backups won't reach the cloud.",
                target, detail,
            )

    def _admin_password(self) -> str:
        """Keyring first; fall back to AdminPassword in the server's own ini
        for daemons that can't see the per-user keyring (LocalSystem service,
        headless Linux with no keyring backend)."""
        pw = get_admin_password()
        if pw:
            return pw
        pw = inifile.read_admin_password(self.cfg.live_ini)
        if pw:
            self.log.info(
                "admin password read from PalWorldSettings.ini (keyring had none "
                "for this account)"
            )
        return pw

    def _set_bot(self, bot) -> None:
        self.bot = bot

    def _start_bot(self) -> None:
        """Launch (or relaunch) the Discord bot task. run_bot itself returns
        immediately when the bot is disabled or has no token, so calling this
        is always safe — except while a previous task is still running, which
        callers must rule out via ``self._bot_task``."""
        self._bot_task = self._spawn(
            self._supervised(
                "discord bot",
                run_bot(
                    self.cfg, self.api, self.bus, self.store, self.scheduler,
                    on_created=self._set_bot,
                ),
            )
        )

    def _reload_bot(self) -> None:
        """Apply a config reload to the Discord bot.

        A finished run_bot means the bot was disabled, had no token, or its
        token was rejected at the last start — the GUI's "Save & reload" used
        to leave it that way until a full daemon restart. Relaunch it with the
        fresh config instead (run_bot's finally already unhooked the old
        client from the bus, so the stale self.bot is just a dead reference).
        A *running* bot only gets the new config pushed in; swapping the token
        of a live client still needs a daemon restart."""
        if self._bot_task is not None and self._bot_task.done():
            self.bot = None
            self._start_bot()
        elif self.bot is not None:
            self.bot.reconfigure(self.cfg, self.api)

    @property
    def _desired_running(self) -> bool:
        return self.__desired_running

    @_desired_running.setter
    def _desired_running(self, value: bool) -> None:
        self.__desired_running = value
        _save_desired_running(value)

    def _spawn(self, coro) -> asyncio.Task:
        # asyncio holds only weak refs to tasks; keep one or it can be GC'd mid-run.
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._spawned_done)
        return t

    def _spawned_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        # Without this, a failed operation surfaces only as asyncio's GC-time
        # "Task exception was never retrieved" on stderr — which service mode
        # discards entirely (NSSM captures no stdio; only the file log survives).
        if not t.cancelled() and t.exception() is not None:
            self.log.error(
                "background operation failed", exc_info=t.exception()
            )

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
            # Clamp: a hand-edited 0/negative poll_seconds would tight-loop the
            # REST API and process enumeration (the scheduler clamps likewise).
            await asyncio.sleep(max(1, self.cfg.poll_seconds))

    async def _poll(self) -> None:
        try:
            metrics = await self.api.metrics()
            players = await self.api.players()
        except PalApiError:
            self._api_fail_streak += 1
            # One failed poll is not an outage: a server answering in >6s under
            # memory pressure is exactly when polls time out, and a false flip
            # spams down/up announcements, splits playtime records, and resets
            # the leak forecaster's history right when it's needed. Declare
            # down only on the same consecutive-miss streak crash recovery
            # uses. Auto-recovery still sees every miss (it has its own
            # confirmation counter).
            if self._alive and self._api_fail_streak >= max(
                1, self.cfg.watchdog.crash_confirm_polls
            ):
                await self.tracker.handle_server_down()
                await self.bus.emit(Event("server_down", "🔴 Server is **down**."))
                self._alive = False
            await self._maybe_autorecover()
            return

        first_poll = self._alive is None
        if self._alive is False:
            await self.bus.emit(Event("server_up", "🟢 Server is **up**."))
            self._epoch_at = time.time()  # fresh process; old memory samples don't apply
        self._alive = True
        self._ever_alive = True
        self._down_polls = 0
        self._api_fail_streak = 0

        await self.tracker.update(players)

        stats = await asyncio.to_thread(procs.proc_stats)  # psutil enumeration off the loop
        self._last_metrics = metrics

        if first_poll:
            # Daemon (re)started while the server was already up: `_history` was
            # seeded from SQLite and may span a *previous* server process whose
            # restart drop would flatten the leak fit. Anchor the forecaster to
            # this server process's start so those older samples are excluded
            # (fall back to now if we can't read the process — safe, just
            # discards the seeded history for forecasting).
            self._epoch_at = time.time() - (stats.uptime_seconds if stats else 0.0)
        sample = {
            "at": time.time(),
            "fps": metrics.server_fps,
            "frame_time": metrics.server_frame_time,
            "players": metrics.current_players,
            "memory_mb": stats.memory_mb if stats else 0.0,
            "cpu": stats.cpu_percent if stats else 0.0,
        }
        self._history.append(sample)
        del self._history[:-720]  # ~2h at 10s polling
        await asyncio.to_thread(self.store.log_metrics, sample)

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
            busy=self.control.busy,
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
        self._spawn(self._autorecover())

    async def _autorecover(self) -> None:
        op = self.control.try_operation("auto-recover")
        if op is None:
            return  # something else took the server in the meantime
        try:
            async with op:
                await self.bus.emit(
                    Event(
                        "watchdog",
                        "🚑 Server unreachable and palctl didn't stop it — "
                        "auto-recovering.",
                        {"action": "autorecover"},
                    )
                )
                # Stop first, in case it's hung rather than gone — then start
                # clean. Escalate: an unreachable server is exactly the hang the
                # plain service stop can't clear, so force-kill if it won't die.
                ok = await self.control.restart_cycle(
                    stop_delay=2,
                    escalate=True,
                    on_escalate=lambda m: self.bus.emit(
                        Event("watchdog", f"🔨 {m}", {"action": "force_stop"})
                    ),
                )
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

    # ---------- leak forecasting ----------

    async def _predict_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await self._predict_tick()
            except Exception as e:
                self.log.warning("leak forecast failed: %s", e)

    async def _predict_tick(self) -> None:
        wd = self.cfg.watchdog
        if not (wd.enabled and (wd.predict_notify or wd.preempt_restart)):
            self._predict_warned = False
            return
        if not self._alive or self.control.busy:
            return

        samples = [
            (s["at"], s["memory_mb"])
            for s in self._history
            if s.get("at", 0.0) >= self._epoch_at
        ]
        ttl = leak.time_to_limit_minutes(samples, wd.memory_limit_mb)
        if ttl is None or ttl > wd.preempt_horizon_minutes:
            self._predict_warned = False  # re-arm; a new episode gets a new warning
            return

        if wd.preempt_restart and not self.tracker.online:
            # Empty server + limit approaching: restart NOW, on our terms,
            # instead of at the threshold later with players mid-session.
            await self.bus.emit(
                Event(
                    "watchdog",
                    f"🔮 Memory on pace to hit the limit in {leak.fmt_minutes(ttl)} "
                    "and the server is empty — restarting now instead of "
                    "mid-session later.",
                    {"action": "preempt", "minutes_to_limit": round(ttl)},
                )
            )
            self._predict_warned = False
            self._spawn(
                self.scheduler.restart_quick(
                    "Pre-emptive maintenance restart (memory)", skip_if_busy=True
                )
            )
        elif wd.predict_notify and not self._predict_warned:
            self._predict_warned = True
            await self.bus.emit(
                Event(
                    "watchdog",
                    f"🔮 On the current pace, memory hits the watchdog limit "
                    f"({wd.memory_limit_mb:,} MB) in {leak.fmt_minutes(ttl)}. "
                    "The watchdog will handle it — but now would be a good "
                    "moment for a restart on your terms.",
                    {"action": "forecast", "minutes_to_limit": round(ttl)},
                )
            )

    # ---------- localhost API for the GUI ----------

    def _routes(self) -> web.Application:
        # Every request must carry the shared token — see localauth. The
        # exceptions are "/", the dashboard page (static markup, no data), and
        # "/favicon.ico", which browsers fetch on their own before they could
        # ever attach a token.
        app = web.Application(
            middlewares=[
                make_auth_middleware(self._token, exempt=frozenset({"/", "/favicon.ico"}))
            ]
        )

        dashboard = Path(__file__).with_name("dashboard.html")

        async def index(_: web.Request) -> web.Response:
            try:
                html = await asyncio.to_thread(dashboard.read_text, "utf-8")
            except OSError:
                return web.Response(status=404, text="dashboard not bundled")
            return web.Response(text=html, content_type="text/html")

        async def favicon(_: web.Request) -> web.Response:
            return web.Response(body=_FAVICON_SVG, content_type="image/svg+xml")

        async def state(_: web.Request) -> web.Response:
            # Both of these block (psutil enumeration; an sc.exe subprocess), so
            # keep them off the event loop — the GUI polls /state every ~2s.
            stats = await asyncio.to_thread(procs.proc_stats)
            service = await asyncio.to_thread(procs.service_state, self.cfg.service_name)
            return web.json_response(
                {
                    "service": service,
                    "alive": self._alive,
                    "restarting": self.watchdog.is_restarting,
                    "operation": self.control.current_op,
                    "memory_limit_mb": self.cfg.watchdog.memory_limit_mb,
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
            try:
                body = await request.json() if request.can_read_body else {}
            except (json.JSONDecodeError, ValueError):
                return web.json_response(
                    {"error": "request body is not valid JSON"}, status=400
                )
            if not isinstance(body, dict):
                return web.json_response(
                    {"error": "request body must be a JSON object"}, status=400
                )
            what = request.match_info["what"]

            def require(field: str) -> str:
                value = body.get(field)
                if not isinstance(value, str) or not value:
                    raise _BadRequest(f"missing required field: {field}")
                return value

            try:
                if what == "start":
                    # One implementation for start/stop (also the bot's /start,
                    # /stop): sets the desired-running intent and drives control.
                    if await self.scheduler.start_server() == "busy":
                        return _busy_response(self.control.current_op)
                elif what == "stop":
                    result = await self.scheduler.stop_server()
                    if result == "busy":
                        return _busy_response(self.control.current_op)
                    if result == "failed":
                        # The world was saved and the Stop intent recorded, but
                        # the service never confirmed STOPPED — surface that
                        # instead of a misleading "ok" (matches the bot's /stop).
                        return web.json_response(
                            {
                                "error": "The world was saved and the server was "
                                "told to stop, but it didn't confirm STOPPED — it "
                                "may be hung. Check the server, or try a restart."
                            },
                            status=502,
                        )
                elif what == "restart":
                    if self.control.busy:
                        return _busy_response(self.control.current_op)
                    self._desired_running = True
                    self._spawn(
                        self.scheduler.restart_with_countdown(
                            body.get("reason", "Admin restart")
                        )
                    )
                elif what == "announce":
                    await self.api.announce(require("message"))
                elif what == "save":
                    await self.api.save()
                elif what == "backup":
                    if self.control.busy:
                        return _busy_response(self.control.current_op)
                    self._spawn(self.scheduler.backup_now("gui"))
                elif what == "update-server":
                    if self.control.busy:
                        return _busy_response(self.control.current_op)
                    self._desired_running = True
                    self._spawn(self.scheduler.update_server())
                elif what == "restore":
                    if self.control.busy:
                        return _busy_response(self.control.current_op)
                    self._desired_running = True
                    self._spawn(self.scheduler.restore_backup(require("name")))
                elif what == "kick":
                    await self.api.kick(require("user_id"), body.get("reason", ""))
                elif what == "ban":
                    await self.api.ban(require("user_id"), body.get("reason", ""))
                elif what == "unban":
                    await self.api.unban(require("user_id"))
                elif what == "reload-config":
                    self.cfg = Config.load()
                    self.api = PalApi(
                        self.cfg.api_host, self.cfg.api_port, self._admin_password()
                    )
                    # The workers hold their own cfg/api references; swap them
                    # too or the reload silently changes nothing.
                    self.control.reconfigure(self.cfg, self.api)
                    self.scheduler.reconfigure(self.cfg, self.api)
                    self.watchdog.reconfigure(self.cfg, self.api)
                    self._reload_bot()
                else:
                    return web.json_response({"error": f"unknown action {what}"}, status=400)
            except _BadRequest as e:
                return web.json_response({"error": str(e)}, status=400)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

            return web.json_response({"ok": True})

        async def list_backups(_: web.Request) -> web.Response:
            from pathlib import Path

            bs = await asyncio.to_thread(backups.listing, Path(self.cfg.backup_root))
            return web.json_response(
                [{"name": b.name, "size_mb": b.size_mb} for b in bs]
            )

        app.router.add_get("/", index)
        app.router.add_get("/favicon.ico", favicon)
        app.router.add_get("/state", state)
        app.router.add_get("/backups", list_backups)
        app.router.add_post("/action/{what}", action)
        return app

    # ---------- run ----------

    async def run(self) -> None:
        runner = web.AppRunner(self._routes())
        await runner.setup()
        # Bind loopback by default (localhost only); every request must still
        # carry the per-user token (see localauth). The admin can opt into
        # `ui_bind_host = "0.0.0.0"` to reach the dashboard from other devices on
        # the LAN — the warning below spells out that the token then stands alone.
        host = self.cfg.ui_bind_host or "127.0.0.1"
        await web.TCPSite(runner, host, DAEMON_PORT).start()
        self.log.info("daemon up; control API on %s:%d", host, DAEMON_PORT)
        warning = lan_exposure_warning(host)
        if warning:
            self.log.warning("%s", warning)
        self._sync_dashboard_firewall(host)
        self._warn_if_cloud_mirror_broken()

        if self.cfg.check_for_updates:
            self._spawn(self._check_palctl_update())

        self._start_bot()
        await asyncio.gather(
            self._supervised("poll loop", self._poll_loop()),
            self._supervised("watchdog", self.watchdog.run()),
            self._supervised("scheduler", self.scheduler.run()),
            self._supervised("leak forecaster", self._predict_loop()),
            self._supervised("update check", self._update_check_loop()),
        )

    async def _supervised(self, name: str, coro) -> None:
        """One escaped exception in any loop must not kill the whole daemon.

        Every loop guards its tick body, but errors can still raise outside
        those guards — a wrong-typed hand-edited config value at loop setup,
        or a startup-time failure. gather() propagates the first one and
        cancels everything: watchdog, scheduler, control API, bot, all gone.
        Log it, tell the event feed, and keep the rest of the daemon alive."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log.error("%s crashed; the rest of palctl keeps running", name, exc_info=True)
            with contextlib.suppress(Exception):
                await self.bus.emit(
                    Event("error", f"{name} crashed and is disabled until restart: {e}")
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
    (exe, args, app_dir) to run *the daemon* as a service — correct whether we're
    a PyInstaller-frozen build or `python -m palctl.daemon` in a dev checkout.
    The installer and the wizard both register the service off this, so there's
    one source of truth for "how do you run the daemon".

    In the frozen onedir build, palctl-daemon.exe and palctl-gui.exe sit side by
    side. The wizard registers the daemon service from *inside the GUI process*,
    where sys.executable is palctl-gui.exe — so we must resolve the sibling
    daemon exe explicitly, not launch whatever exe happens to be running. (This
    bug pointed the daemon service at the GUI, so the daemon never started and
    every GUI action got a connection-refused.)
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        name = "palctl-daemon.exe" if sys.platform.startswith("win") else "palctl-daemon"
        daemon_exe = exe_dir / name
        if not daemon_exe.exists():
            # Unexpected layout — fall back to the running exe rather than
            # registering a path that doesn't exist.
            daemon_exe = Path(sys.executable)
        return str(daemon_exe), "", str(exe_dir)
    return sys.executable, "-m palctl.daemon", str(Path(__file__).resolve().parents[1])


def install_service(as_user: bool = False) -> None:
    """Register (and start) the palctl daemon as a service — NSSM on Windows,
    systemd on Linux.

    On Windows the account matters (see winservice.install_commands). With
    `as_user` we register the service under the invoking account, which shares
    its %APPDATA% and DPAPI secrets with the GUI/CLI; otherwise we stay on
    LocalSystem but redirect %APPDATA% to the installing user's, and the
    daemon falls back to reading AdminPassword from the server's ini.
    """
    exe, args, app_dir = service_target()
    if sys.platform.startswith("win"):
        import getpass
        import os

        from . import winservice

        user = password = None
        if as_user:
            username = os.environ.get("USERNAME", "")
            user = f".\\{username}"
            print(
                f"[daemon] The service will log on as {user} so it shares your\n"
                "         config, token, and saved secrets. Windows needs your\n"
                "         account password to register that (palctl does not\n"
                "         store it — it goes straight to the service manager)."
            )
            password = getpass.getpass(f"Password for {user}: ")

        nssm = winservice.ensure_nssm(config_dir() / "bin")
        winservice.install_service(
            nssm, SERVICE_NAME, exe, args, app_dir,
            user=user, password=password, appdata=os.environ.get("APPDATA"),
        )
        # A user-account service is the one path that can hit Error 1069 (the
        # account has no password / is PIN-only). If it didn't come up, don't
        # leave the user staring at a dead service — point them at login startup.
        if as_user and procs.service_state(SERVICE_NAME) != "RUNNING":
            print(
                "[daemon] The service registered but did NOT start. This is almost\n"
                "         always Error 1069: a PIN-only or passwordless Windows\n"
                "         account can't host a service logon. Remove it and use\n"
                "         password-free login startup instead:\n"
                "             palctl-daemon uninstall-service\n"
                "             palctl-daemon install-startup"
            )
            return
    else:
        from . import systemd

        # Writing to /etc/systemd/system needs root, so this runs under sudo —
        # but the daemon itself must NOT: without User= the unit runs as root,
        # its config/token/secrets land under /root, and the invoking user's
        # `palctl` CLI can never authenticate to it (401 on every call). Run
        # the unit as the user who ran sudo, so daemon and CLI share the same
        # ~/.config/palctl. A genuine root login keeps the old behavior.
        run_as = os.environ.get("SUDO_USER") or None
        if run_as == "root":
            run_as = None
        exec_start = f"{exe} {args}".strip()
        systemd.install_service(
            SERVICE_NAME, exec_start, description="palctl daemon",
            working_dir=app_dir, user=run_as,
        )
        if run_as:
            print(f"[daemon] the service runs as '{run_as}' (not root), sharing that")
            print("         account's ~/.config/palctl token with the palctl CLI.")
    print(f"[daemon] service '{SERVICE_NAME}' installed and started.")


def uninstall_service() -> None:
    if sys.platform.startswith("win"):
        from . import winservice

        # Don't download NSSM just to uninstall: if the service was never
        # registered and there's no cached nssm.exe, there's nothing to remove.
        cached = config_dir() / "bin" / "nssm.exe"
        if not cached.exists() and not winservice.service_exists(SERVICE_NAME):
            print(f"[daemon] service '{SERVICE_NAME}' is not registered; nothing to remove.")
            return
        nssm = winservice.ensure_nssm(config_dir() / "bin")
        winservice.remove_service(nssm, SERVICE_NAME)
        # Don't leave the dashboard firewall port open after uninstall.
        from . import firewall

        firewall.remove_rule()
    else:
        from . import systemd

        systemd.remove_service(SERVICE_NAME)
    print(f"[daemon] service '{SERVICE_NAME}' removed.")


def install_startup() -> None:
    """Register the daemon to start at login via the current user's Run key —
    the password-free path that avoids the service-logon Error 1069 entirely.
    Windows-only; a headless Linux box uses the systemd service instead."""
    if not sys.platform.startswith("win"):
        print("[daemon] login startup is Windows-only; on Linux use install-service.")
        return
    from . import startup

    exe, args, _ = service_target()
    startup.install_startup(exe, args)
    print(
        "[daemon] palctl will start automatically when you log in — no password "
        "or Windows service needed."
    )


def uninstall_startup() -> None:
    if not sys.platform.startswith("win"):
        return
    from . import startup

    startup.uninstall_startup()
    print("[daemon] removed palctl from login startup.")


def _daemon_reachable() -> bool:
    """Is a daemon already answering on the localhost control port?"""
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", DAEMON_PORT)) == 0


def _stop_daemon_process() -> None:
    """Stop whatever process is serving the daemon control port, so a freshly
    spawned daemon can bind it. Best-effort: anything we can't see or can't
    kill is left alone rather than guessed at. Uses the same terminate → kill
    ladder the server force-stop uses."""
    import psutil

    try:
        conns = psutil.net_connections(kind="tcp")
    except Exception:
        return
    pids = {
        c.pid
        for c in conns
        if c.pid
        and c.pid != os.getpid()
        and c.status == psutil.CONN_LISTEN
        and c.laddr
        and c.laddr.port == DAEMON_PORT
    }
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            if not asyncio.run(procs.terminate_process(proc)):
                asyncio.run(procs.kill_process(proc))
        except Exception:
            pass


def start_detached() -> bool:
    """Launch the daemon now, in the background, hidden — used right after
    registering login startup so the user doesn't have to log out and back in
    first. Returns whether a daemon is running afterward. Windows-only.

    Re-running setup lands here too, and any daemon already up is the OLD
    build/config — so it must be replaced, not skipped. Order matters: a
    leftover *service* registration has to go first, because its manager would
    resurrect anything we kill and the resurrected copy would fight the fresh
    daemon over the port (it would also double-start the daemon at next boot).
    Only then is it safe to stop a surviving detached daemon and spawn."""
    if not sys.platform.startswith("win"):
        return False
    from . import winservice

    if winservice.service_exists(SERVICE_NAME):
        uninstall_service()
    if _daemon_reachable():
        _stop_daemon_process()
    import subprocess

    exe, args, app_dir = service_target()
    argv = [exe, *(args.split() if args else []), "run", "--headless"]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    subprocess.Popen(argv, cwd=app_dir, creationflags=flags, close_fds=True)
    return True


def _hide_console() -> None:
    """Hide our own console window (the --headless login-startup path), so
    logging in doesn't flash a black box. No-op if there's no console."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def main() -> None:
    import argparse

    from . import __version__

    parser = argparse.ArgumentParser(prog="palctl-daemon")
    parser.add_argument("--version", action="version", version=f"palctl {__version__}")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=[
            "run",
            "install-service",
            "uninstall-service",
            "install-startup",
            "uninstall-startup",
        ],
        help="run the daemon (default); (un)register a Windows service; or "
        "(un)register password-free login startup",
    )
    parser.add_argument(
        "--as-user",
        action="store_true",
        help="register the Windows service under your account (asks for your "
        "Windows password) instead of LocalSystem — recommended if you use "
        "the Discord bot or saved the admin password in the GUI",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="hide the console window (used by the login-startup entry)",
    )
    args = parser.parse_args()

    if args.command == "install-service":
        install_service(as_user=args.as_user)
        return
    if args.command == "uninstall-service":
        uninstall_service()
        return
    if args.command == "install-startup":
        install_startup()
        return
    if args.command == "uninstall-startup":
        uninstall_startup()
        return

    if args.headless:
        _hide_console()
    try:
        asyncio.run(Daemon().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
