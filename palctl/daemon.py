"""
The daemon. Headless, wrapped in a service (WinSW/systemd), always running.

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
import logging
import os
import secrets
import sys
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from . import backups, inifile, leak, localauth, netinfo, procs
from .alerts import WebhookAlerter
from .api import PalApi, PalApiError, PalApiUnauthorized
from .bot import run_bot
from .client import DAEMON_PORT
from .config import Config, config_dir, get_admin_password
from .control import ServerController
from .events import Event, EventBus, PlayerTracker, SessionStore
from .logging_setup import setup_logging
from .scheduler import Scheduler
from .watchdog import Watchdog

SERVICE_NAME = "palctl-daemon"  # the Windows service name palctl registers


def sd_notify(state: str) -> None:
    """Send a notification to systemd over $NOTIFY_SOCKET, if we're running under
    a systemd unit with Type=notify. A no-op everywhere else (Windows, a unit
    without notify, a dev run) — best-effort, never raises. This is what lets
    systemd's WatchdogSec detect a *hung* (not crashed) daemon: as long as the
    poll loop is healthy we send WATCHDOG=1, and if the event loop wedges the
    pings stop and systemd restarts us."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        import socket

        # An abstract-namespace socket path starts with '@' -> leading NUL.
        path = "\0" + addr[1:] if addr.startswith("@") else addr
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(path)
            s.sendall(state.encode("utf-8"))
    except OSError:
        pass

# The admin's Stop intent, persisted so it survives daemon restarts (crash +
# wrapper restart, a palctl upgrade, a manual service bounce). Without this the
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


def _tail_log_file(n: int) -> str:
    """Last `n` lines of the daemon's rotating log. Blocking — call via
    to_thread. Never raises: a missing/unreadable log returns a note, not a 500."""
    path = config_dir() / "logs" / "palctl.log"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no daemon log available)"


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
    contains no data (its /state calls still need the token).

    Rejections are logged (with the peer address) so probing is visible when the
    API is LAN-bound — the token is the only credential there, and silence would
    hide someone guessing at it. Rate-limited so a misconfigured client polling
    every 2s can't flood the log: the first few are logged, then every 100th."""
    rejects = {"n": 0}
    log = logging.getLogger("palctl.daemon")

    @web.middleware
    async def _auth(request: web.Request, handler):
        if exempt and request.path in exempt:
            return await handler(request)
        sent = request.headers.get(localauth.TOKEN_HEADER, "")
        if not secrets.compare_digest(sent, token):
            rejects["n"] += 1
            n = rejects["n"]
            if n <= 5 or n % 100 == 0:
                log.warning(
                    "rejected request #%d without a valid token: %s %s from %s",
                    n, request.method, request.path,
                    request.remote or "unknown",
                )
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
        # Second alert channel (webhook) besides Discord + the GUI/log. Subscribes
        # to the bus itself; reconfigured in place on config reload.
        self.alerter = WebhookAlerter(self.cfg, self.bus)
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
        # A 401 from the REST API means the server is UP but the admin password
        # is wrong (rotated out from under us, say) — NOT an outage. Warn once,
        # not every poll, and never let it drive down-detection/auto-recovery.
        self._auth_warned = False
        # Wall-clock of the last completed poll, for the /healthz liveness probe.
        self._last_poll_at = 0.0
        # Set by a SIGTERM/SIGINT handler to unblock run() into graceful shutdown.
        self._stop: asyncio.Event | None = None
        # Short-TTL cache for service_state so /state (polled ~every 2s per open
        # GUI/dashboard) doesn't spawn an sc.exe/systemctl subprocess every time.
        self._svc_cache: tuple[float, str] = (0.0, "UNKNOWN")

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

    def _sync_alerts(self) -> None:
        """Apply a config reload to the webhook alerter (enable/disable/URL)."""
        self.alerter.reconfigure(self.cfg)

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
        # discards entirely (the service wrapper captures no stdio; only the file log survives).
        if not t.cancelled() and t.exception() is not None:
            self.log.error(
                "background operation failed", exc_info=t.exception()
            )

    def _spawn_exclusive(self, name: str, coro) -> bool:
        """Spawn a server-exclusive operation (restart/backup/update/restore) as
        a background task, but only if the server is free — reserving it
        synchronously so two near-simultaneous requests can't both get past a
        `busy` check and queue a second operation behind the first. Returns False
        (and the caller should answer 409) if something already holds the server.
        The reservation is cleared when the operation takes the real lock, or by
        the finally here if it returns/raises before ever getting that far."""
        if not self.control.reserve(name):
            coro.close()  # we won't run it; don't leave an un-awaited coroutine
            return False

        async def _run() -> None:
            try:
                await coro
            finally:
                self.control.clear_reservation(name)

        self._spawn(_run())
        return True

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
        # Bind the API once: reload-config can swap self.api between awaits, and
        # a poll that read metrics from the old endpoint and players from the new
        # one would be internally inconsistent.
        api = self.api
        try:
            metrics = await api.metrics()
            players = await api.players()
        except PalApiUnauthorized:
            # The server is answering — it just rejected the password. Restarting
            # can't fix that, so this must never look like a crash. Say so once.
            if not self._auth_warned:
                self._auth_warned = True
                await self.bus.emit(
                    Event(
                        "error",
                        "The Palworld REST API rejected the admin password — the "
                        "server is up but palctl can't authenticate. Fix the "
                        "password (GUI Config, or AdminPassword in "
                        "PalWorldSettings.ini) and reload. Not treating this as an "
                        "outage; auto-recovery will NOT restart the server.",
                    )
                )
            return
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
                # Don't keep serving the last-seen FPS/frametime/uptime next to a
                # server that's down — /state would read as if it were still up.
                self._last_metrics = None
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
        self._auth_warned = False  # a good poll re-arms the password warning

        await self.tracker.update(players)

        stats = await asyncio.to_thread(procs.proc_stats)  # psutil enumeration off the loop
        self._last_metrics = metrics
        self._last_poll_at = time.time()

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
        self._spawn(self._autorecover())

    async def _autorecover(self) -> None:
        op = self.control.try_operation("auto-recover")
        if op is None:
            return  # something else took the server in the meantime
        # Count the attempt against the hourly cap only now that we actually hold
        # the lock — recording it before the try_operation race would spend the
        # budget on restarts that never happened and could throttle a real one.
        self._autorestart_times.append(time.time())
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
                make_auth_middleware(
                    self._token, exempt=frozenset({"/", "/favicon.ico", "/healthz"})
                )
            ]
        )

        dashboard = Path(__file__).with_name("dashboard.html")
        dashboard_cache: dict[str, str] = {}

        async def index(_: web.Request) -> web.Response:
            # The page is static; read it once and serve from memory thereafter.
            if "html" not in dashboard_cache:
                try:
                    dashboard_cache["html"] = await asyncio.to_thread(
                        dashboard.read_text, "utf-8"
                    )
                except OSError:
                    return web.Response(status=404, text="dashboard not bundled")
            return web.Response(text=dashboard_cache["html"], content_type="text/html")

        async def favicon(_: web.Request) -> web.Response:
            return web.Response(body=_FAVICON_SVG, content_type="image/svg+xml")

        async def healthz(_: web.Request) -> web.Response:
            # Liveness/readiness for an external monitor. No token (no data), so
            # exempt in the auth middleware. 503 when the poll loop hasn't
            # completed a cycle in a while — a wedged event loop or a dead poller.
            age = time.time() - self._last_poll_at if self._last_poll_at else None
            stale = age is not None and age > max(30, self.cfg.poll_seconds * 6)
            ok = self._last_poll_at == 0.0 or not stale  # starting up counts as ok
            return web.json_response(
                {
                    "status": "ok" if ok else "stale",
                    "alive": self._alive,
                    "last_poll_age_seconds": round(age, 1) if age is not None else None,
                },
                status=200 if ok else 503,
            )

        async def state(_: web.Request) -> web.Response:
            stats = await asyncio.to_thread(procs.proc_stats)  # psutil enum off the loop
            service = await self._service_state_cached()
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
                    if not self._spawn_exclusive(
                        "restart",
                        self.scheduler.restart_with_countdown(
                            body.get("reason", "Admin restart")
                        ),
                    ):
                        return _busy_response(self.control.current_op)
                    self._desired_running = True
                elif what == "announce":
                    await self.api.announce(require("message"))
                elif what == "save":
                    await self.api.save()
                elif what == "backup":
                    if not self._spawn_exclusive("backup", self.scheduler.backup_now("gui")):
                        return _busy_response(self.control.current_op)
                elif what == "update-server":
                    if not self._spawn_exclusive("update", self.scheduler.update_server()):
                        return _busy_response(self.control.current_op)
                    self._desired_running = True
                elif what == "restore":
                    name = require("name")
                    if not self._spawn_exclusive(
                        "restore", self.scheduler.restore_backup(name)
                    ):
                        return _busy_response(self.control.current_op)
                    self._desired_running = True
                elif what == "kick":
                    await self.api.kick(require("user_id"), body.get("reason", ""))
                elif what == "ban":
                    await self.api.ban(require("user_id"), body.get("reason", ""))
                elif what == "unban":
                    await self.api.unban(require("user_id"))
                elif what == "reload-config":
                    # Don't swap cfg/api out from under a running operation (a
                    # restart mid-countdown, an update) — let it finish first.
                    if self.control.busy:
                        return _busy_response(self.control.current_op)
                    old_api = self.api
                    # Config.load() and the keyring/ini read both hit disk; keep
                    # them off the event loop.
                    self.cfg = await asyncio.to_thread(Config.load)
                    password = await asyncio.to_thread(self._admin_password)
                    self.api = PalApi(self.cfg.api_host, self.cfg.api_port, password)
                    # The workers hold their own cfg/api references; swap them
                    # too or the reload silently changes nothing.
                    self.control.reconfigure(self.cfg, self.api)
                    self.scheduler.reconfigure(self.cfg, self.api)
                    self.watchdog.reconfigure(self.cfg, self.api)
                    self._reload_bot()
                    self._sync_alerts()
                    with contextlib.suppress(Exception):
                        await old_api.aclose()  # drop the old client's connection
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

        async def tail_logs(request: web.Request) -> web.Response:
            # Token-gated remote read of the daemon's own rotating log, so the
            # daemon can be diagnosed without getting onto the box. Bounded to the
            # last N lines (?n=, default 200, capped) so it can't stream the whole
            # file. Read off the loop.
            try:
                n = min(2000, max(1, int(request.query.get("n", "200"))))
            except ValueError:
                n = 200
            text = await asyncio.to_thread(_tail_log_file, n)
            return web.Response(text=text, content_type="text/plain")

        app.router.add_get("/", index)
        app.router.add_get("/favicon.ico", favicon)
        app.router.add_get("/healthz", healthz)
        app.router.add_get("/state", state)
        app.router.add_get("/backups", list_backups)
        app.router.add_get("/logs", tail_logs)
        app.router.add_post("/action/{what}", action)
        return app

    async def _service_state_cached(self, ttl: float = 2.0) -> str:
        """service_state() shells out to sc.exe/systemctl; /state is polled every
        ~2s per open GUI/dashboard, so cache the result briefly to avoid a
        subprocess per request. Single event loop, so the check is race-free
        enough — a rare double-miss just runs the query twice."""
        now = time.monotonic()
        ts, val = self._svc_cache
        if now - ts < ttl:
            return val
        val = await asyncio.to_thread(procs.service_state, self.cfg.service_name)
        self._svc_cache = (time.monotonic(), val)
        return val

    # ---------- run ----------

    async def run(self) -> None:
        self._stop = asyncio.Event()
        runner = web.AppRunner(self._routes())
        await runner.setup()
        # Bind loopback by default (localhost only); every request must still
        # carry the per-user token (see localauth). The admin can opt into
        # `ui_bind_host = "0.0.0.0"` to reach the dashboard from other devices on
        # the LAN — the warning below spells out that the token then stands alone.
        host = self.cfg.ui_bind_host or "127.0.0.1"
        try:
            await web.TCPSite(runner, host, DAEMON_PORT).start()
        except OSError as e:
            # The likeliest startup failure — another daemon already on the port.
            # It must reach the *file* log, not just stderr the service discards.
            self.log.error(
                "could not bind the control API on %s:%d (%s) — another palctl "
                "daemon may already be running. This daemon is exiting.",
                host, DAEMON_PORT, e,
            )
            await runner.cleanup()
            raise
        self.log.info("daemon up; control API on %s:%d", host, DAEMON_PORT)
        warning = lan_exposure_warning(host)
        if warning:
            self.log.warning("%s", warning)
        self._sync_dashboard_firewall(host)
        self._warn_if_cloud_mirror_broken()

        self._install_signal_handlers()

        if self.cfg.check_for_updates:
            self._spawn(self._check_palctl_update())

        self._start_bot()
        for name, coro in (
            ("poll loop", self._poll_loop()),
            ("watchdog", self.watchdog.run()),
            ("scheduler", self.scheduler.run()),
            ("leak forecaster", self._predict_loop()),
            ("update check", self._update_check_loop()),
            ("disk watch", self._disk_loop()),
            ("liveness", self._liveness_loop()),
        ):
            self._spawn(self._supervised(name, coro))

        sd_notify("READY=1")
        await self._stop.wait()  # runs until SIGTERM/SIGINT
        await self._graceful_shutdown(runner)

    def _install_signal_handlers(self) -> None:
        """Wire SIGTERM/SIGINT to a clean shutdown. `systemctl stop` and WinSW's
        stop both send a signal; without this the process is just killed
        mid-write. add_signal_handler is the asyncio-native path (POSIX); the
        Windows event loop doesn't support it, so fall back to signal.signal."""
        import signal

        loop = asyncio.get_running_loop()

        def _request_stop(*_a: object) -> None:
            if self._stop is not None and not self._stop.is_set():
                self.log.info("shutdown signal received")
                loop.call_soon_threadsafe(self._stop.set)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except (NotImplementedError, AttributeError, ValueError):
                with contextlib.suppress(ValueError, OSError, RuntimeError):
                    signal.signal(sig, _request_stop)

    async def _graceful_shutdown(self, runner: web.AppRunner) -> None:
        """Bounded, best-effort teardown. Must finish well inside the service
        manager's stop timeout, so every step is guarded and time-boxed. Does
        NOT touch the game server — that's a separate service and stopping the
        daemon doesn't stop the game, so there's nothing to announce to players."""
        self.log.info("shutting down")
        sd_notify("STOPPING=1")
        # A maintenance stop often precedes a reboot; flush the world if it's up,
        # but never let a slow/hung save hold the shutdown open.
        if self._alive:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.api.save(), timeout=10)
        for t in list(self._tasks):
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await runner.cleanup()
        with contextlib.suppress(Exception):
            await self.api.aclose()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.store.close)
        self.log.info("shutdown complete")

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

    async def _liveness_loop(self) -> None:
        """systemd Type=notify watchdog ping. A no-op without $WATCHDOG_USEC. The
        ping runs as an ordinary task on the event loop, so if the loop *wedges*
        (a blocking call, a lock deadlock) this stops firing and systemd — with
        WatchdogSec set — restarts the daemon. That's the one failure the process
        supervisor can't see on its own: a hung-but-alive daemon."""
        usec = os.environ.get("WATCHDOG_USEC")
        if not usec:
            return
        try:
            interval = max(1.0, int(usec) / 1_000_000 / 2)  # ping at half the deadline
        except ValueError:
            return
        while True:
            await asyncio.sleep(interval)
            sd_notify("WATCHDOG=1")

    def _lowest_free_gb(self) -> float | None:
        """Free GB on the least-free of the server and backup volumes, or None if
        neither path can be read. Blocking (statvfs) — call via to_thread."""
        import shutil

        frees: list[float] = []
        for p in {self.cfg.server_root, self.cfg.backup_root}:
            if not p:
                continue
            try:
                frees.append(shutil.disk_usage(p).free / (1024**3))
            except OSError:
                pass
        return min(frees) if frees else None

    async def _disk_loop(self) -> None:
        """Warn once per episode when free disk runs low on the server or backup
        volume. Re-arms when space recovers, so a genuinely low disk doesn't spam
        but a fresh dip is announced. Not a restart trigger — only a human can
        free space — but a loud, early heads-up beats silent backup failures."""
        warned = False
        while True:
            await asyncio.sleep(300)
            try:
                min_gb = self.cfg.watchdog.disk_min_free_gb
                if min_gb <= 0:
                    warned = False
                    continue
                low = await asyncio.to_thread(self._lowest_free_gb)
                if low is not None and low < min_gb:
                    if not warned:
                        warned = True
                        await self.bus.emit(
                            Event(
                                "error",
                                f"⚠️ Low disk space: {low:.1f} GB free on the "
                                f"server/backup volume (below the {min_gb} GB "
                                "floor). A full disk corrupts saves and stops "
                                "backups — free some space.",
                                {"free_gb": round(low, 1), "min_gb": min_gb},
                            )
                        )
                else:
                    warned = False
            except Exception as e:
                self.log.warning("disk check failed: %s", e)

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


def _wait_until(predicate, timeout: float, interval: float = 1.0) -> bool:
    """Poll `predicate` until it's true or `timeout` elapses. Sync — used only
    by the install/CLI paths, never on the daemon's event loop."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def install_service(as_user: bool = False) -> bool:
    """Register (and start) the palctl daemon as a service — WinSW on Windows,
    systemd on Linux. Returns whether the daemon is confirmed up afterward
    (its control port answering) — success is verified, never assumed.

    On Windows the account matters (see winservice.install_commands). With
    `as_user` we register the service under the invoking account, which shares
    its %APPDATA% and DPAPI secrets with the GUI/CLI; otherwise we stay on
    LocalSystem but redirect %APPDATA% to the installing user's, and the
    daemon falls back to reading AdminPassword from the server's ini.
    """
    exe, args, app_dir = service_target()
    if sys.platform.startswith("win"):
        import getpass

        from . import startup, winservice

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

        # Wrapper first: if the download fails, nothing has been touched yet.
        winsw = winservice.ensure_winsw(config_dir() / "bin")
        # Switching FROM login startup: drop the Run key, or the next login
        # spawns a second daemon that fights this service over the control port.
        startup.uninstall_startup()
        winservice.install_service(
            winsw, SERVICE_NAME, exe, args, app_dir,
            user=user, password=password, appdata=os.environ.get("APPDATA"),
            start=False,
        )
        # The registration is fresh and stopped, so anything still holding the
        # control port is a leftover login-startup (or dev) daemon. Stop it
        # before starting, or the service daemon can't bind the port and the
        # wrapper restart-loops it while the old daemon keeps serving.
        if _daemon_reachable():
            _stop_daemon_process()
        winservice.start_service(SERVICE_NAME)
        # A user-account service is the one path that can hit Error 1069 (the
        # account has no password / is PIN-only). If it didn't come up, don't
        # leave the user staring at a dead service — point them at login startup.
        if as_user and not _wait_until(
            lambda: procs.service_state(SERVICE_NAME) == "RUNNING", timeout=15.0
        ):
            print(
                "[daemon] The service registered but did NOT start. This is almost\n"
                "         always Error 1069: a PIN-only or passwordless Windows\n"
                "         account can't host a service logon. Remove it and use\n"
                "         password-free login startup instead:\n"
                "             palctl-daemon uninstall-service\n"
                "             palctl-daemon install-startup"
            )
            return False
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
        # A stray daemon the unit doesn't own (e.g. a dev `python -m
        # palctl.daemon` in a terminal) holds the control port and would
        # crash-loop the fresh unit. The unit's own daemon needs no killing —
        # the `systemctl restart` inside install replaces it.
        if _daemon_reachable() and not systemd.is_active(SERVICE_NAME):
            _stop_daemon_process()
        exec_start = f"{exe} {args}".strip()
        systemd.install_service(
            SERVICE_NAME, exec_start, description="palctl daemon",
            working_dir=app_dir, user=run_as,
        )
        if run_as:
            print(f"[daemon] the service runs as '{run_as}' (not root), sharing that")
            print("         account's ~/.config/palctl token with the palctl CLI.")
    # Don't claim success on the service manager's say-so — the daemon's own
    # control port answering is the signal that it actually came up.
    if _wait_until(_daemon_reachable, timeout=30.0):
        print(f"[daemon] service '{SERVICE_NAME}' installed and started.")
        return True
    hint = (
        "run `palctl-daemon run` in a console to see the startup error"
        if sys.platform.startswith("win")
        else f"check `systemctl status {SERVICE_NAME}` and "
        f"`journalctl -u {SERVICE_NAME}`"
    )
    print(
        f"[daemon] service '{SERVICE_NAME}' is registered, but the daemon is "
        f"not answering on its control port — {hint}."
    )
    return False


def uninstall_service() -> None:
    if sys.platform.startswith("win"):
        from . import winservice

        # Removal goes through plain sc.exe — no wrapper download needed, and
        # it works on services registered by the old NSSM builds too.
        if not winservice.service_exists(SERVICE_NAME):
            print(f"[daemon] service '{SERVICE_NAME}' is not registered; nothing to remove.")
            return
        winservice.remove_service(SERVICE_NAME)
        # Best-effort: drop the per-service wrapper copy + config so nothing in
        # the cache still describes a service that no longer exists.
        for p in winservice.wrapper_paths(config_dir() / "bin", SERVICE_NAME):
            p.unlink(missing_ok=True)
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


def disable_background_startup() -> None:
    """Turn background startup fully off: remove BOTH autostart mechanisms
    (whichever a previous install registered) and stop any daemon still
    running. Setup's 'background box unticked' path — unticking must actually
    turn it off. A first run with nothing registered is a harmless no-op."""
    uninstall_startup()
    uninstall_service()
    if _daemon_reachable():
        _stop_daemon_process()


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
        # Can't enumerate sockets at all (no privileges). Say so — the caller
        # only got here because something IS on the port, and silently returning
        # makes "couldn't stop it" look like "nothing to stop".
        print(
            "[daemon] something is answering on the daemon port but the process "
            "couldn't be identified (no permission to inspect sockets) — the new "
            "daemon may lose the port to it. Stop the old daemon manually if so."
        )
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
    if not pids:
        # Same story: the port answers but no visible owner (another user's
        # process on Linux without root shows pid=None). Fail loudly, not open.
        print(
            "[daemon] something is answering on the daemon port but no owning "
            "process is visible (try again as root/administrator) — the new "
            "daemon may lose the port to it."
        )
        return
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
        if winservice.service_exists(SERVICE_NAME):
            # Removal failed — almost always: not elevated. Killing the daemon
            # process now would just get it resurrected by the service manager,
            # and a fresh spawn would lose the port fight to it, so stop here
            # with the actual fix instead of pretending it worked.
            print(
                "[daemon] The existing palctl-daemon service could not be removed\n"
                "         (removing a service needs an administrator prompt). Run:\n"
                "             palctl-daemon uninstall-service\n"
                "         as administrator, then set up login startup again."
            )
            return False
    if _daemon_reachable():
        _stop_daemon_process()
    import subprocess

    exe, args, app_dir = service_target()
    argv = [exe, *(args.split() if args else []), "run", "--headless"]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    subprocess.Popen(argv, cwd=app_dir, creationflags=flags, close_fds=True)
    # Verified, not assumed: True only once the control port actually answers.
    return _wait_until(_daemon_reachable, timeout=30.0)


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

    # The install commands exit nonzero on a verified failure, so scripts and
    # CI can assert the outcome instead of parsing prose.
    if args.command == "install-service":
        sys.exit(0 if install_service(as_user=args.as_user) else 1)
    if args.command == "uninstall-service":
        uninstall_service()
        return
    if args.command == "install-startup":
        install_startup()
        # Replace any running daemon now (removing a leftover service first),
        # the same way setup does — the Run key alone only takes effect at the
        # NEXT login, which would leave an old daemon serving until then.
        if sys.platform.startswith("win"):
            ok = start_detached()
            if ok:
                print("[daemon] palctl is running now — no logout needed.")
            sys.exit(0 if ok else 1)
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
    except Exception:
        # In service mode the wrapper discards stderr, so an unhandled startup
        # failure (a port already bound, a broken config at construction) would
        # otherwise vanish — the daemon just restart-loops with no trace. Make it
        # land in the rotating file log before we exit non-zero.
        setup_logging().exception("daemon exited with an unhandled error")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
