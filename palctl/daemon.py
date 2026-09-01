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

from . import backups, countdown, inifile, leak, localauth, netinfo, procs, supervisor
from .alerts import WebhookAlerter
from .api import PalApi, PalApiError, PalApiUnauthorized
from .bot import run_bot
from .client import DAEMON_PORT
from .config import Config, config_dir, get_admin_password
from .control import ServerController
from .decisions import DecisionLog, summarize
from .events import Event, EventBus, PlayerTracker, SessionStore
from .logging_setup import setup_logging
from .scheduler import Scheduler
from .supervisor import Action, Observation, is_boot_start
from .watchdog import Watchdog

# How long shutdown waits for cancelled background tasks to unwind before
# finishing without them. Comfortably inside WinSW's and systemd's own stop
# timeouts — see _graceful_shutdown for why this can't be unbounded.
SHUTDOWN_TASK_GRACE = 10.0


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
#
# `ever_alive` rides in the same file for the opposite reason: it is the guard
# that stops auto-recovery restarting a server palctl has never seen working,
# and losing it across a restart is what turns a recoverable outage into a
# permanent one. See _load_ever_alive.
_STATE_PATH = config_dir() / "daemon_state.json"

# A worker loop that raises out of its own guards is restarted rather than
# retired: a daemon still answering /healthz with its memory watchdog quietly
# gone is the worst of both worlds. The budget is what stops a genuinely
# unstartable loop (bad config, missing dependency) from spinning forever —
# after it, the daemon is degraded and says so. See DaemonApp._supervised.
_WORKER_RESTART_BUDGET = 3
_WORKER_RESTART_BACKOFF = (5.0, 30.0, 120.0, 600.0)


def _read_state() -> dict:
    try:
        state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(**changes: object) -> None:
    """Merge `changes` into the state file. Read-modify-write so one key's
    setter can't drop the other's value — both are written from the same
    single-threaded event loop, so there's no lost-update race between them."""
    state = _read_state()
    state.update(changes)
    try:
        tmp = _STATE_PATH.with_name(_STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, _STATE_PATH)
    except OSError:
        pass  # best effort — worst case is the old resets-to-default behavior


def _load_desired_running() -> bool:
    value = _read_state().get("desired_running")
    return True if value is None else bool(value)  # no state (first run) = normal


def _save_desired_running(value: bool) -> None:
    _write_state(desired_running=value)


def _load_ever_alive() -> bool:
    """Whether palctl has ever seen this server answering.

    Persisted because auto-recovery refuses to touch a server it has never seen
    up — a sane guard against restart-looping a box with no server installed,
    but one that inverts badly when the flag is lost. A daemon that restarts
    *during* an outage (a crash, an upgrade, the health task) came back with
    ever_alive=False and so would never recover the server, which then stayed
    down until a human noticed. Remembering it means an outage that spans a
    daemon restart still gets recovered."""
    return bool(_read_state().get("ever_alive", False))


def _save_ever_alive(value: bool) -> None:
    _write_state(ever_alive=value)


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


def poll_loop_is_live(
    *, last_poll_at: float, now: float, poll_seconds: int
) -> tuple[bool, float | None]:
    """The /healthz verdict: (is the poll loop still turning, age of its last
    cycle). Pure, because getting it wrong is expensive in both directions —
    too strict and the health task restarts a healthy daemon, too loose and a
    genuinely wedged one is never healed.

    `last_poll_at` must be stamped on every *completed* cycle, not every
    successful one. The distinction is the whole bug this function documents:
    stamping it only when the game server answered made "the game server is
    down" indistinguishable from "this daemon is wedged", and the only consumer
    that acts on the verdict responds by restarting the daemon — which does
    nothing for a down game server, and kills the auto-recovery that was
    handling it.

    A daemon that has not completed its first cycle yet reads as live: it is
    starting up, not stuck.
    """
    if not last_poll_at:
        return True, None
    age = now - last_poll_at
    return age <= max(30.0, poll_seconds * 6), age


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
        # Only recover a server that WAS up — persisted, so an outage that
        # spans a daemon restart is still recoverable (see _load_ever_alive).
        self.__ever_alive = _load_ever_alive()
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
        # One-shot per outage: the server is down and palctl has been told
        # not to restart it. Re-armed by a good poll, like the 401 warning.
        self._recovery_off_warned = False
        # Consecutive polls where the SCM says STOPPED and palctl didn't do
        # it — see _adopt_external_stop.
        self._external_stop_polls = 0
        # Whether this daemon has seen the server up at all yet. Adoption needs
        # it: you cannot *stop* something that was never running, so a service
        # that has been STOPPED for this daemon's whole life is a server that
        # failed to start, not one somebody turned off — and reporting the
        # second when it's the first sends the admin looking for a culprit that
        # doesn't exist. Per-session on purpose (unlike the persisted
        # _ever_alive), because the question is about *this* stop.
        self._service_seen_up = False
        # Cleared once _restore_boot_intent has had its say. Until then a
        # STOPPED service is the state palctl is about to act on, not evidence
        # of anything — see supervisor.decide's AWAIT_STARTUP branch.
        self._boot_intent_pending = True
        # Why palctl is doing (or not doing) something. The event feed says what
        # happened; this says what was decided — the half an admin can't infer.
        self.decisions = DecisionLog()
        # One-shot: warn if the server process runs under a different account
        # than the daemon (the watchdog-blinding split — see _maybe_warn_account_mismatch).
        self._account_warned = False
        # One-shot: warn if the running server was started from a different
        # install than the one updates rewrite (see _maybe_warn_wrong_server_root).
        self._root_warned = False
        # Worker loops that crashed past their restart budget: name -> last
        # error. Non-empty means palctl is running with something missing, and
        # /healthz must stop claiming everything is fine.
        self.degraded: dict[str, str] = {}
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
        touch the firewall, so log the one-line manual command instead.

        Blocking (up to three netsh invocations) — the caller runs it via
        to_thread. It must never sit on the event loop: netsh against a sick
        MpsSvc can take tens of seconds, and this happens *after* the HTTP site
        is bound, so a blocked loop means a daemon whose port accepts
        connections and then answers nothing at all. That looks like a hung app
        from every client, and no "is the port open?" check can see it."""
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

    async def _startup_side_effects(self, host: str) -> None:
        """The two startup chores that shell out, moved off the event loop and
        out of the startup critical path. Neither gates anything, so a slow
        netsh or rclone now delays only its own log line — not the poll loop,
        not the control API, and not the READY signal the service manager is
        waiting on."""
        await asyncio.to_thread(self._sync_dashboard_firewall, host)
        await self._check_settings_drift()
        await asyncio.to_thread(self._warn_if_cloud_mirror_broken)

    async def _check_settings_drift(self) -> None:
        """Say when PalWorldSettings.ini stopped being what palctl wrote.

        The failure this exists for is silent by construction: a Steam update
        puts Steam's defaults back, `RESTAPIEnabled` returns to False, and from
        then on palctl reports an outage on a server that is running perfectly
        well. Nothing else in palctl can see the difference between that and a
        genuinely down server, so nothing else can say it.

        Non-critical drift is reported once and then adopted as the new
        baseline — an admin who edits their own settings shouldn't be nagged
        every restart. Critical drift is not adopted: it keeps saying so until
        somebody puts the REST API settings back, because until then palctl is
        blind.
        """
        from . import inidrift

        drift = await asyncio.to_thread(
            inidrift.compare, inidrift.load(), self.cfg.live_ini, self.cfg.default_ini
        )
        if not drift.matters:
            return
        self._decide(
            "settings_drift",
            f"PalWorldSettings.ini differs from what palctl wrote ({drift.kind})",
            kind=drift.kind,
            critical=list(drift.critical),
        )
        await self.bus.emit(
            Event(
                "error" if drift.critical else "info",
                inidrift.describe(drift),
                {"action": "settings_drift", "kind": drift.kind},
            )
        )
        if not drift.critical:
            await asyncio.to_thread(inidrift.record, self.cfg.live_ini)

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
                lambda: run_bot(
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

    @property
    def _ever_alive(self) -> bool:
        return self.__ever_alive

    @_ever_alive.setter
    def _ever_alive(self, value: bool) -> None:
        # Only ever flips False -> True, and only on a poll that got an answer,
        # so this writes the state file exactly once in the daemon's life
        # rather than on every poll.
        if value and not self.__ever_alive:
            self.__ever_alive = True
            _save_ever_alive(True)
        else:
            self.__ever_alive = value

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

    def _countdown_response(self, what: str) -> web.Response:
        """Answer /action/cancel-countdown and /action/skip-countdown.

        Three outcomes, three answers, because "it didn't work" is not one
        thing: the admin either stopped the countdown, arrived after it ran
        out, or there was never one running. The last two are 409 (the request
        made sense, the state didn't allow it) with the reason spelled out —
        telling someone who clicked Cancel a second too late that "nothing was
        running" reads as a broken button.
        """
        skipping = what == "skip-countdown"
        result = (
            self.scheduler.skip_countdown() if skipping
            else self.scheduler.cancel_countdown()
        )
        if result in ("cancelled", "skipped"):
            return web.json_response({"ok": True, "result": result})
        op = self.control.current_op
        if result == "too_late":
            # Deliberately not "the countdown is over": this also covers an
            # operation that never had one (a watchdog restart hands its timer
            # to the game itself), and claiming otherwise would send the admin
            # looking for a clock that was never there.
            reason = (
                f"too late — the {op or 'operation'} is already under way, past "
                "the point where it can be called off."
            )
        elif op:
            # Busy, but with something that never counts down (a backup, an
            # update, the boot-time start). Say which, so "nothing is counting
            # down" doesn't read as a contradiction of the status badge.
            reason = (
                f"nothing is counting down right now — {op} is running, and it "
                "has no countdown to interrupt."
            )
        else:
            reason = (
                "nothing is counting down right now. Start a restart or a "
                "restore first."
            )
        return web.json_response({"error": reason, "result": result}, status=409)

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
            # Stamped here, on every completed cycle, and NOT inside _poll() —
            # /healthz asks "is this daemon alive", and the daemon is alive
            # whether or not the game server answered. Stamping it only on a
            # successful poll made an unreachable *game server* read as a wedged
            # *daemon*: /healthz went 503, and the Windows health task then
            # restarted a perfectly healthy daemon roughly a quarter of an hour
            # into every outage — precisely while auto-recovery was working the
            # problem. See the handler for what the probe is actually for.
            self._last_poll_at = time.time()
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
        self._recovery_off_warned = False  # ...and the recovery-is-off notice
        self._external_stop_polls = 0      # a live server isn't a stopped one
        self._service_seen_up = True       # ...and it's one adoption can apply to
        await self._maybe_warn_account_mismatch()
        await self._maybe_warn_wrong_server_root()

        await self.tracker.update(players)

        stats = await asyncio.to_thread(  # psutil enumeration off the loop
            procs.proc_stats, self.cfg.server_root
        )
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

    async def _maybe_warn_account_mismatch(self) -> None:
        """Once per daemon run: if the server process runs under a different
        account than the daemon, say so loudly. That split is what makes the
        watchdog watch the idle ~7 MB launcher instead of the real multi-GB
        server — memory/CPU read wrong and the leak watchdog can never fire. The
        fix is to run both under the same account (Path A: services as-user).
        psutil enumeration runs off the event loop."""
        if self._account_warned:
            return
        import getpass

        try:
            checked, warning = await asyncio.to_thread(
                procs.server_account_check, getpass.getuser()
            )
        except Exception:
            checked, warning = False, None
        if not checked:
            # Inconclusive — don't burn the one-shot on it. This used to latch
            # before the check even ran, so a single poll where the process
            # wasn't readable suppressed the warning permanently. That is
            # exactly backwards: an account split is a reason the read fails, so
            # the servers that most need telling were the ones told least.
            return
        self._account_warned = True
        if warning:
            self.log.warning("%s", warning)
            await self.bus.emit(Event("error", "⚠️ " + warning))

    async def _maybe_warn_wrong_server_root(self) -> None:
        """Once per daemon run: if the running server was started from a
        different folder than the one palctl updates, say so. That split makes
        every update land on a copy nobody runs — the update reports success and
        the live server stays on its old build, which players meet as a version
        mismatch. psutil enumeration runs off the event loop.

        The one-shot is only spent on a *conclusive* reading, for the same
        reason as the account check above — and it is the same trap, because it
        is the same psutil call underneath. `server_root_from_process` answers
        None both for "no server process to read" and for "found it, couldn't
        read its image path", and the second is routine: an AccessDenied on
        `proc.exe()` is exactly what a server running as SYSTEM under a
        login-user daemon produces. Latching before the check ran meant one
        early poll — the first successful one, while the game process is still
        settling — silenced this warning for the life of the daemon. Silencing
        it is expensive: a wrong server root is the reason updates land on an
        install nobody starts, and this is the only thing in palctl that says
        so."""
        if self._root_warned:
            return
        from . import discovery

        try:
            running = await asyncio.to_thread(discovery.server_root_from_process)
        except Exception:
            running = None
        if running is None:
            return  # inconclusive — ask again on the next poll
        self._root_warned = True
        warning = discovery.root_mismatch_warning(self.cfg.server_root, running)
        if warning:
            self.log.warning("%s", warning)
            await self.bus.emit(Event("error", "⚠️ " + warning))

    # ---------- crash / hang auto-recovery ----------

    async def _warn_recovery_is_off(self, wd) -> None:
        """Say — once per outage — that the server is down and palctl has been
        told not to fix it.

        `auto_restart_on_crash` is opt-in, and deliberately so: restarting
        someone's server unasked is not a default to take lightly. But the
        silence around it is the problem. Watching a real hang from the outside,
        the entire output is one "🔴 Server is down." and then nothing, forever
        — identical to palctl being broken, and the single most likely reason
        someone concludes it is. The feature that would have fixed it is one
        unticked box away, and nothing ever mentions it.

        Only fires once the outage is confirmed (`_alive is False`, same
        threshold as the down announcement) and only for a server palctl has
        actually seen working, so a box with no server installed stays quiet.
        Re-armed when the server comes back, so each outage says it once."""
        if wd.auto_restart_on_crash or self._recovery_off_warned:
            return
        if self._alive is not False or not self._ever_alive:
            return
        self._recovery_off_warned = True
        await self.bus.emit(
            Event(
                "error",
                "⚠️ The server is down and **auto-recovery is turned off**, so "
                "palctl will not restart it — it is only reporting. Turn on "
                "*Auto-restart on crash/hang* in Config (or set "
                "`watchdog.auto_restart_on_crash` to `true`) and reload, and "
                "palctl will bring the server back on its own next time.",
                {"action": "recovery_disabled"},
            )
        )

    def _decide(self, action: str, why: str, **data) -> None:
        """Record a decision and log it once. Repeats collapse in the log (the
        poll loop revisits the same conclusion every few seconds), so this is
        cheap to call from anywhere a choice is made."""
        entry = self.decisions.record(action, why, data=data or None)
        if entry.count == 1:
            self.log.info("decision: %s — %s", action, why)

    async def _restore_boot_intent(self) -> None:
        """After a reboot, put the server back the way it was left.

        Setup registers the game service **Manual** whenever palctl runs as a
        boot service (setup_flow.server_service_start_mode), which is what lets
        a deliberate Stop survive a restart — the SCM no longer starts PalServer
        regardless of intent. This is the other half of that trade: if the
        recorded intent says the server should be running, palctl now has to be
        the one to start it, because nothing else will.

        Deliberately not gated on `auto_restart_on_crash`. That setting is about
        restarting a server that *failed*; this only re-issues the start the
        SCM's Automatic startmode used to do on its own, so a host who leaves
        recovery off still gets their server back after a reboot exactly as
        before. `is_boot_start` keeps it to that: a daemon restarted on a
        running box (installer upgrade, health task) never starts anything.

        Runs as a spawned task, not inline in run(): start_service waits for the
        SCM to report RUNNING, and nothing about that should sit in front of
        READY=1 or the poll loop.
        """
        try:
            if not self._desired_running:
                self._decide(
                    "boot_stand_down",
                    "the server was stopped on purpose before the restart, so "
                    "palctl is leaving it stopped",
                )
                return
            if not is_boot_start(time.time(), procs.boot_time()):
                self._decide(
                    "boot_not_a_boot",
                    "palctl restarted but the machine didn't, so it won't start "
                    "or stop anything on its own",
                )
                return  # a daemon restart, not a boot; not ours to act on
            state = await self._service_state_cached(ttl=0)
            if state != "STOPPED":
                self._decide(
                    "boot_already_up",
                    "the server was already coming up after the restart",
                )
                return  # already up (or the SCM is mid-start) — nothing to do
            self._decide(
                "boot_start",
                "the machine restarted and the server is meant to be running — "
                "starting it",
            )
            # The shared implementation, so this takes the operation lock and
            # records intent exactly like the GUI's Start button.
            if await self.scheduler.start_server() == "busy":
                return
            if await self._service_state_cached(ttl=0) != "RUNNING":
                self._decide(
                    "boot_start_failed",
                    "palctl tried to start the server after the restart and the "
                    "service didn't reach RUNNING — this is a failure to start, "
                    "not somebody stopping it",
                )
                await self.bus.emit(
                    Event(
                        "error",
                        "⚠️ The machine restarted and palctl tried to bring the "
                        "server back up, but the service didn't reach RUNNING. "
                        "Check the server in palctl (or services.msc) — palctl "
                        "still has it recorded as *should be running*, so this "
                        "is a failure to start, not somebody stopping it.",
                        {"action": "boot_start_failed"},
                    )
                )
        except Exception:
            # Never let this take the daemon's startup down with it; the poll
            # loop and recovery still run either way.
            self.log.exception("restoring the boot-time server state failed")
        finally:
            self._boot_intent_pending = False

    async def _maybe_autorecover(self) -> None:
        """Called on every poll where the REST API is unreachable.

        The decision itself is `supervisor.decide` — one pure function over one
        observation. This method's only jobs are to assemble that observation,
        record what was decided and why, and carry it out. Keeping those apart
        is deliberate: the two most recent bugs in this area were both ordering
        mistakes between guard clauses that used to live here, and neither was
        reachable by a test that didn't boot a daemon.
        """
        wd = self.cfg.watchdog
        self._autorestart_times = _within_window(self._autorestart_times, time.time())
        # Skip the service query for a server that is meant to be down (or a
        # startup that hasn't decided yet) — those answers don't depend on it,
        # and this runs every poll, forever, on a deliberately stopped server.
        needs_state = supervisor.needs_service_state(
            desired_running=self._desired_running,
            startup_pending=self._boot_intent_pending,
        )
        obs = Observation(
            # ttl=0: never decide from the display cache. That cache exists so
            # a dashboard polling /state twice a second doesn't run `sc query`
            # twice a second, and its staleness is harmless for *showing* a
            # state. It is not harmless for deciding one: with a short poll
            # interval, two consecutive polls can be served the same 2-second-
            # old "RUNNING" reading, which is long enough for palctl to conclude
            # a server somebody had just stopped was a crash and restart it —
            # the exact bug the external-stop detector exists to prevent. One
            # extra subprocess, only on polls where the server isn't answering.
            service_state=await self._service_state_cached(ttl=0) if needs_state else "",
            desired_running=self._desired_running,
            ever_alive=self._ever_alive,
            seen_service_up=self._service_seen_up,
            busy=self.control.busy,
            restarting=self.watchdog.is_restarting,
            startup_pending=self._boot_intent_pending,
            recovery_enabled=wd.auto_restart_on_crash,
            down_polls=self._down_polls,
            external_stop_polls=self._external_stop_polls,
            recent_restarts=len(self._autorestart_times),
            confirm_polls=wd.crash_confirm_polls,
            restart_cap=wd.crash_restart_max_per_hour,
        )
        decision = supervisor.decide(obs)
        self._decide(decision.action.value, decision.why)
        await self._apply_decision(decision, obs)

    async def _apply_decision(self, decision, obs: Observation) -> None:
        """Carry out one supervisor decision. No conditions of its own beyond
        the action it was handed — anything that looks like a policy judgement
        belongs in supervisor.decide, where it is testable without a daemon."""
        action = decision.action

        if action in (Action.STAND_DOWN, Action.AWAIT_STARTUP):
            return

        # A service somebody stopped, or one that never started. Both leave the
        # server down; only the first is an instruction.
        if action is Action.REPORT_NEVER_STARTED:
            return  # the boot-start path owns this message; don't double-report
        if action is Action.CONFIRM_EXTERNAL_STOP:
            self._external_stop_polls += 1
            return
        if action is Action.ADOPT_EXTERNAL_STOP:
            self._external_stop_polls = 0
            self._desired_running = False
            self._down_polls = 0
            await self.bus.emit(
                Event(
                    "server_down",
                    "⏹️ The server was stopped outside palctl (services.msc, "
                    "`sc stop`, or Task Manager). Taking that as deliberate: "
                    "palctl will **not** restart it, and the schedule won't "
                    "either. Use Start in palctl when you want it back.",
                    {"action": "external_stop"},
                )
            )
            return

        # Past here the service is not sitting stopped, so any half-counted
        # external stop was a blip on the way through a restart.
        self._external_stop_polls = 0
        if obs.service_state in supervisor.UP_STATES:
            # Seeing it up is what makes a *later* stop attributable to somebody.
            self._service_seen_up = True

        if action is Action.IGNORE:
            await self._warn_recovery_is_off(self.cfg.watchdog)
            return
        if action is Action.RESET:
            self._down_polls = 0
            return
        if action in (Action.COUNT_DOWN_POLL, Action.THROTTLED):
            self._down_polls += 1
            return
        if action is Action.RECOVER:
            self._down_polls = 0
            self._autorestart_times = _within_window(
                self._autorestart_times, time.time()
            )
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
            # The forecast is the one moment the admin is definitely thinking
            # about the leak, so it's where the other half of the accepted
            # mitigation belongs — not buried in a tooltip they'd have to go
            # looking for. Only mentioned when raids are actually on.
            await self.bus.emit(
                Event(
                    "watchdog",
                    f"🔮 On the current pace, memory hits the watchdog limit "
                    f"({wd.memory_limit_mb:,} MB) in {leak.fmt_minutes(ttl)}. "
                    "The watchdog will handle it — but now would be a good "
                    "moment for a restart on your terms."
                    + await self._raids_hint(),
                    {"action": "forecast", "minutes_to_limit": round(ttl)},
                )
            )

    async def _raids_hint(self) -> str:
        """RAIDS_HINT when raids are on, empty otherwise. Never raises and never
        guesses: an unreadable ini says nothing rather than giving the admin
        advice about a setting palctl couldn't actually read."""
        from .serversetup import RAIDS_HINT, raids_enabled

        try:
            on = await asyncio.to_thread(raids_enabled, self.cfg.live_ini)
        except Exception:
            return ""
        return RAIDS_HINT if on else ""

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
            #
            # This is strictly about THIS PROCESS. It must never go 503 because
            # the game server is down: the only consumer that acts on it is the
            # health task, whose remedy is restarting the daemon, and that does
            # nothing for a down game server — it just kills the thing that was
            # about to recover it. Game-server reachability is reported by
            # `alive` below, and acted on by the watchdog and auto-recovery.
            ok, age = poll_loop_is_live(
                last_poll_at=self._last_poll_at,
                now=time.time(),
                poll_seconds=self.cfg.poll_seconds,
            )
            # A worker that crashed past its restart budget is not a healthy
            # daemon, even with the poll loop turning: "ok" here is what the
            # health task and any external monitor trust, and it must not be
            # true while the watchdog or the scheduler is gone. Reported as
            # `degraded`, distinct from `stale` — the remedy differs. Still
            # 200: restarting the daemon is the operator's call once the cause
            # is fixed, and the health task's blind restart would only loop.
            return web.json_response(
                {
                    "status": "ok" if ok else "stale",
                    "alive": self._alive,
                    "last_poll_age_seconds": round(age, 1) if age is not None else None,
                    "degraded": dict(self.degraded),
                },
                status=200 if ok else 503,
            )

        async def state(_: web.Request) -> web.Response:
            stats = await asyncio.to_thread(  # psutil enum off the loop
                procs.proc_stats, self.cfg.server_root
            )
            service = await self._service_state_cached()
            return web.json_response(
                {
                    "service": service,
                    "alive": self._alive,
                    "restarting": self.watchdog.is_restarting,
                    "operation": self.control.current_op,
                    # The live restart/restore countdown, or None. Published so
                    # every client shows the same clock and the same two escape
                    # hatches — before this, a countdown was invisible to
                    # everything except the Discord channel it announced in.
                    "countdown": self.scheduler.countdown_state(),
                    "memory_limit_mb": self.cfg.watchdog.memory_limit_mb,
                    "metrics": asdict(self._last_metrics) if self._last_metrics else None,
                    "process": asdict(stats) if stats else None,
                    "players": [asdict(p) for p in self.tracker.online],
                    "history": self._history[-360:],
                    "events": [
                        {"kind": e.kind, "message": e.message, "at": e.at.isoformat()}
                        for e in self.bus.recent(60)
                    ],
                    # What palctl decided, and why. `why` is the one-liner a
                    # dashboard can put next to the status without the reader
                    # having to open a log; `decisions` is the recent history.
                    "why": summarize(self.decisions.latest()),
                    "decisions": self.decisions.entries(10),
                    # Standing, not a scrolled-away event: a server left on an
                    # old build refuses players with a version mismatch while
                    # every other reading says it's healthy.
                    "update": self.scheduler.update_status,
                }
            )

        async def decisions(request: web.Request) -> web.Response:
            """The full recent decision history — "why isn't palctl doing
            anything" as data instead of an inference from the log."""
            try:
                limit = max(1, min(200, int(request.query.get("n", "50"))))
            except ValueError:
                limit = 50
            return web.json_response({"decisions": self.decisions.entries(limit)})

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

            def optional_seconds() -> int | None:
                """`{"seconds": N}` overrides the configured countdown for this
                one call — 0 means "no warning, go now". Absent = use the
                config. Validated here so a bad value is a 400 with a reason
                rather than a countdown of NaN seconds."""
                if "seconds" not in body or body["seconds"] is None:
                    return None
                value = body["seconds"]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise _BadRequest("'seconds' must be a whole number of seconds")
                if not 0 <= value <= countdown.MAX_SECONDS:
                    raise _BadRequest(
                        f"'seconds' must be between 0 and {countdown.MAX_SECONDS}"
                    )
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
                            body.get("reason", "Admin restart"),
                            seconds=optional_seconds(),
                        ),
                    ):
                        return _busy_response(self.control.current_op)
                    self._desired_running = True
                elif what in ("cancel-countdown", "skip-countdown"):
                    # The two escape hatches from a running countdown. Not
                    # server-exclusive operations themselves — they interrupt
                    # one — so they never go through _spawn_exclusive.
                    return self._countdown_response(what)
                elif what == "announce":
                    await self.api.announce(require("message"))
                elif what == "save":
                    await self.api.save()
                elif what == "backup":
                    if not self._spawn_exclusive("backup", self.scheduler.backup_now("gui")):
                        return _busy_response(self.control.current_op)
                elif what == "update-server":
                    # `{"validate": true}` asks SteamCMD to re-verify every file
                    # against Steam's manifest — a repair for a suspected-broken
                    # install, not part of a routine update. It is slow (a full
                    # multi-GB pass) and it restores files that differ, which is
                    # what resets PalWorldSettings.ini, so it is opt-in per call.
                    if not self._spawn_exclusive(
                        "update",
                        self.scheduler.update_server(
                            validate=bool(body.get("validate", False))
                        ),
                    ):
                        return _busy_response(self.control.current_op)
                    self._desired_running = True
                elif what == "restore":
                    name = require("name")
                    seconds = optional_seconds()
                    # Check the name *here*, not only inside the spawned task.
                    # The task reports a typo as an event nobody is watching,
                    # while this handler had already answered 200 — so `palctl
                    # restore typo` printed "Restoring…" and did nothing.
                    if not await asyncio.to_thread(
                        backups.is_restorable, Path(self.cfg.backup_root), name
                    ):
                        raise _BadRequest(
                            f"no such backup: {name!r} (see `palctl backups`)"
                        )
                    if not self._spawn_exclusive(
                        "restore", self.scheduler.restore_backup(name, seconds=seconds)
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
        app.router.add_get("/decisions", decisions)
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
        # Both shell out (netsh; rclone version) and both are pure diagnostics —
        # nothing below depends on them, so they run off the loop and, more
        # importantly, must not delay the workers or READY=1 behind a slow
        # external tool. Spawned rather than awaited for the same reason.
        self._spawn(
            self._supervised(
                "startup checks",
                lambda: self._startup_side_effects(host),
                restarts=0,  # one-shot diagnostics; retrying just re-runs netsh
            )
        )

        self._install_signal_handlers()

        if self.cfg.check_for_updates:
            self._spawn(self._check_palctl_update())

        # Before the workers, so the poll loop's first pass already knows
        # whether a STOPPED service is about to be started (the flag it clears
        # gates external-stop adoption) — but spawned, because waiting for the
        # SCM must not delay READY=1.
        self._spawn(
            self._supervised(
                "boot intent",
                self._restore_boot_intent,
                restarts=0,  # one-shot; a second pass would fight the operator
            )
        )

        self._start_bot()
        # Factories, not coroutines: _supervised restarts a crashed loop, and a
        # coroutine object cannot be awaited a second time.
        for name, factory in (
            ("poll loop", self._poll_loop),
            ("watchdog", self.watchdog.run),
            ("scheduler", self.scheduler.run),
            ("leak forecaster", self._predict_loop),
            ("update check", self._update_check_loop),
            ("disk watch", self._disk_loop),
            ("liveness", self._liveness_loop),
        ):
            self._spawn(self._supervised(name, factory))

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
        # Cancelling is a request, not a guarantee: a task parked in
        # asyncio.to_thread (a service query, a psutil sweep, a disk stat) can't
        # be interrupted and only unwinds when its worker thread returns. Give
        # them a moment and then move on, or "stop the daemon" waits on the
        # slowest external tool and the service manager kills us mid-teardown
        # instead — losing the event-store close below, which is the one step
        # here that touches a file.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=SHUTDOWN_TASK_GRACE,
            )
        with contextlib.suppress(Exception):
            await runner.cleanup()
        with contextlib.suppress(Exception):
            await self.api.aclose()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.store.close)
        self.log.info("shutdown complete")

    async def _supervised(
        self, name: str, factory, *, restarts: int = _WORKER_RESTART_BUDGET
    ) -> None:
        """One escaped exception in any loop must not kill the whole daemon —
        and must not silently retire that loop either.

        Every loop guards its tick body, but errors can still raise outside
        those guards — a wrong-typed hand-edited config value at loop setup,
        or a startup-time failure. gather() propagates the first one and
        cancels everything: watchdog, scheduler, control API, bot, all gone.

        This used to catch that, log it, and stop. The daemon stayed up, the
        control API kept answering and /healthz kept saying "ok" — with the
        memory watchdog, or the scheduler, simply gone. A supervisor that
        outlives the thing it supervises and still reports healthy is the
        exact silent failure this codebase exists to avoid.

        So: restart the loop, backing off 5s → 30s → 2m → 10m, and give up
        only after _WORKER_RESTART_BUDGET failures — a genuinely unstartable
        loop (bad config, missing dependency) must not spin forever. Once the
        budget is spent the daemon is *degraded*, and says so where it counts:
        the event feed, the log, and `degraded` in /state and /healthz.

        `factory` is a zero-argument callable returning the coroutine, not a
        coroutine — a coroutine object cannot be awaited twice, so restarting
        means building a fresh one.
        """
        for attempt in range(restarts + 1):
            try:
                await factory()
                return  # a loop that returns on its own is done, not crashed
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last = attempt >= restarts
                self.log.error(
                    "%s crashed (attempt %d/%d)%s",
                    name,
                    attempt + 1,
                    restarts + 1,
                    "" if last else "; restarting it",
                    exc_info=True,
                )
                if last:
                    self.degraded[name] = str(e)
                    with contextlib.suppress(Exception):
                        await self.bus.emit(
                            Event(
                                "error",
                                f"🛑 {name} crashed {restarts + 1} time(s) "
                                f"and has stopped for good: {e}. palctl is "
                                "running degraded — restart the daemon once the "
                                "cause is fixed.",
                                {"worker": name},
                            )
                        )
                    return
                with contextlib.suppress(Exception):
                    await self.bus.emit(
                        Event("error", f"{name} crashed and is restarting: {e}")
                    )
                await asyncio.sleep(
                    _WORKER_RESTART_BACKOFF[min(attempt, len(_WORKER_RESTART_BACKOFF) - 1)]
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




# ---------------- lifecycle CLI ----------------
#
# Installing, starting, healing and removing the daemon lives in daemoncli — a
# separate program that happens to share this name. It is imported here, at the
# bottom, so every one of those names stays reachable as `daemon.install_service`
# and friends: that is the surface the setup flow, the GUI wizard, the CLI and
# the console entry point all use, and moving the code must not move the API.
#
# Imported last, and importing nothing from here at *its* module level, so there
# is no cycle: by the time daemoncli runs, this module is fully defined.
# `_stop_daemon_process` is underscore-private by convention and re-exported
# anyway: the install-lifecycle CI job calls `daemon._stop_daemon_process()`
# directly, and leaving it behind broke that job on the first push. A name with
# a caller outside the package is part of the surface whatever it's spelled
# like. tests/test_daemoncli.py reads ci.yml and checks every name it uses, so
# the next move can't reopen this.
from .daemoncli import (  # noqa: E402,F401  (re-exported on purpose)
    SERVICE_NAME,
    _stop_daemon_process,
    disable_background_startup,
    install_service,
    install_startup,
    main,
    run_health_check,
    service_target,
    start_detached,
    uninstall_service,
    uninstall_startup,
)

if __name__ == "__main__":
    # `python -m palctl.daemon` is a supported way to run this — the console
    # entry point, the dev checkout's service/Run-key command line
    # (service_target), run-daemon.bat and the README all use it. The CLI moving
    # to daemoncli must not change that, so the guard stays here too.
    main()
