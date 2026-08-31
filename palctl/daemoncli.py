"""Installing, starting, healing and removing the daemon — the other program in
this file.

`palctl/daemon.py` is the runtime: a poll loop, a control API, workers. This is
its lifecycle CLI: register a Windows service or a systemd unit, register login
startup, start a detached daemon, run the scheduled health check, tear all of it
down again. The two share a name and almost nothing else — one runs for weeks,
the other runs once and exits — and keeping them in one 2,200-line module made
the runtime harder to read for no benefit to either.

Everything here stays importable from `palctl.daemon` (see the re-export at the
bottom of that module): `daemon.install_service` is the public surface, and the
setup flow, the GUI wizard and the CLI all call it by that name.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

from . import procs, startup, winservice
from .client import DAEMON_PORT
from .config import Config, config_dir
from .logging_setup import setup_logging

SERVICE_NAME = "palctl-daemon"  # the Windows service name palctl registers


def _runtime():
    """The runtime module, imported on use.

    Lazy on purpose: palctl.daemon imports *this* module at the bottom of its
    own body to re-export these names, so a module-level import back the other
    way would be a cycle. It also keeps `palctl-daemon install-service` from
    paying for aiohttp and discord.py.
    """
    from . import daemon

    return daemon


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


def install_service(as_user: bool = False, password: str | None = None) -> bool:
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

        from . import preflight, startup, winservice

        # Registering a Windows service needs elevation. Without this gate a
        # non-elevated `install-service` let sc.exe/WinSW fail silently, then
        # waited out the 30s reachability probe and reported the WRONG cause
        # ("registered but not answering") — the real reason (no admin) never
        # surfaced. Fail fast with the actual fix. Only a definite False blocks;
        # None (can't tell / not really Windows, e.g. a mocked-platform test)
        # proceeds, so nothing that isn't genuinely unelevated is refused.
        if preflight.is_elevated() is False:
            print(
                "[daemon] Registering a Windows service needs administrator "
                "rights, but palctl isn't elevated.\n"
                "         Re-launch from an admin console (right-click → Run as "
                "administrator),\n"
                "         or use password-free login startup instead (no admin, "
                "no service):\n"
                "             palctl-daemon install-startup"
            )
            return False

        user = None
        if as_user:
            # %USERNAME%, else getpass — `.\` + empty is a nameless account spec
            # that WinSW silently falls back to LocalSystem for, exactly the
            # account split --as-user exists to avoid. Fail with the cause, don't
            # register a service that claims your account but runs as SYSTEM.
            username = (os.environ.get("USERNAME") or "").strip()
            if not username:
                try:
                    username = getpass.getuser().strip()
                except Exception:
                    username = ""
            if not username:
                print(
                    "[daemon] Windows didn't report a user account for this "
                    "process, so --as-user can't register the service under your "
                    "account. Run this from your normal signed-in session."
                )
                return False
            user = f".\\{username}"
            # A password passed in (the setup flow / GUI, which collect it in a
            # field) is used as-is; only an interactive CLI run prompts for it.
            if password is None:
                print(
                    f"[daemon] The service will log on as {user} so it shares your\n"
                    "         config, token, and saved secrets. Windows needs your\n"
                    "         account password to register that (palctl does not\n"
                    "         store it — it goes straight to the service manager)."
                )
                password = getpass.getpass(f"Password for {user}: ")
        else:
            password = None  # LocalSystem — no account password

        # Wrapper first: if the download fails, nothing has been touched yet. A
        # blocked download (corporate proxy, antivirus, offline box) or a
        # tampered binary must say so plainly, not crash with a traceback or
        # leave the user staring at a service that never registered.
        try:
            winsw = winservice.ensure_winsw(config_dir() / "bin")
        except winservice.WrapperChecksumError as e:
            print(f"[daemon] {e}")
            return False
        except OSError as e:
            print(
                "[daemon] couldn't download the WinSW service wrapper from "
                f"GitHub ({e}). A proxy, firewall, or antivirus may be blocking "
                "github.com, or the box is offline. Fix the network and retry, "
                "or use password-free login startup instead (no download, no "
                "service):\n"
                "             palctl-daemon install-startup"
            )
            return False
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
        # SYSTEM: restarting a service needs elevation, and the healer must
        # run with nobody logged in — matching when a service-mode daemon runs.
        _register_health_task(as_system=True)
        return True

    # It didn't come up. Say WHY as specifically as we can rather than one
    # catch-all line: the registration was blocked (nothing to answer with), or
    # the service exists but the SCM refused to start it (a logon failure —
    # Error 1069 &c.), or a genuine daemon startup error the console/journal
    # will show.
    if sys.platform.startswith("win"):
        from . import winservice

        if not winservice.service_exists(SERVICE_NAME):
            print(
                f"[daemon] the '{SERVICE_NAME}' service did not get registered — "
                "the registration was blocked. Run this from an administrator "
                "console (right-click → Run as administrator), or use "
                "`palctl-daemon install-startup`."
            )
            return False
        reason = procs.service_failure_reason(SERVICE_NAME)
        if reason:
            print(
                f"[daemon] service '{SERVICE_NAME}' is registered but won't "
                f"start — {reason}"
            )
            return False
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
        from . import preflight, winservice

        # Removal goes through plain sc.exe — no wrapper download needed, and
        # it works on services registered by the old NSSM builds too.
        if not winservice.service_exists(SERVICE_NAME):
            print(f"[daemon] service '{SERVICE_NAME}' is not registered; nothing to remove.")
            return
        # Removing a service needs elevation too. Without this, sc.exe fails
        # access-denied and this used to print "removed" anyway — leaving the
        # old daemon registered (and, on a mode switch, fighting the new one for
        # the port). Say the truth instead. Only a definite False blocks.
        if preflight.is_elevated() is False:
            print(
                f"[daemon] removing the '{SERVICE_NAME}' service needs "
                "administrator rights. Re-run from an admin console "
                "(right-click → Run as administrator)."
            )
            return
        winservice.remove_service(SERVICE_NAME)
        # Best-effort: drop the per-service wrapper copy + config so nothing in
        # the cache still describes a service that no longer exists.
        for p in winservice.wrapper_paths(config_dir() / "bin", SERVICE_NAME):
            p.unlink(missing_ok=True)
        # Don't leave the dashboard firewall port open after uninstall — nor a
        # health task that would resurrect a daemon the user just removed.
        from . import firewall, wintask

        firewall.remove_rule()
        wintask.remove_health_task()
        # The daemon that was starting the game server at boot is gone; give
        # that job back to Windows before we finish. See _hand_back_server_boot.
        _hand_back_server_boot("no longer runs as a service")
    else:
        from . import systemd

        systemd.remove_service(SERVICE_NAME)
    print(f"[daemon] service '{SERVICE_NAME}' removed.")


def _hand_back_server_boot(why: str) -> None:
    """Give the game server's boot start back to Windows, because palctl is
    about to stop being the thing that provides it.

    Setup registers PalServer **Manual** when palctl runs as a boot service, so
    that a server somebody deliberately stopped stays stopped across a reboot —
    palctl's daemon starts it instead (setup_flow.server_service_start_mode,
    daemon._restore_boot_intent). That trade only holds while a boot-time daemon
    exists to keep its half.

    Nothing used to hand it back. Switching to login startup left a daemon that
    only exists after somebody signs in, so the server came up only if a human
    logged in within daemon.BOOT_INTENT_WINDOW (15 minutes) of boot — and on a
    headless box, never. Removing the service left PalServer Manual with nothing
    alive to start it at all: a server that silently never came back after any
    reboot, with no message anywhere and nothing in palctl that checked.

    Only Manual is touched. Automatic already boots. **Disabled is left alone**
    — that is somebody deliberately turning the server off, and quietly
    re-enabling their server on the way out would be palctl making a decision
    that isn't its to make, which is the same mistake in the other direction.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        name = Config.load().service_name
        if not name or not winservice.service_exists(name):
            return
        if winservice.start_mode_of(name) != "Manual":
            return
        changed = winservice.set_start_mode(name, "Automatic")
    except Exception:
        # Best-effort by construction. This runs at the tail of an uninstall or
        # a startup-mode switch, and a courtesy step must never be the thing
        # that makes one of those fail — an unreadable config, an sc.exe that
        # isn't on PATH, a wedged SCM all mean "couldn't hand it back", not
        # "abandon the teardown half-done".
        return

    if not changed:
        print(
            f"[daemon] NOTE: the '{name}' service is set to Manual start, and "
            f"palctl {why} — so nothing will start your server after a reboot.\n"
            "         palctl couldn't change it (this needs an administrator). "
            "Set it to Automatic yourself:\n"
            f"             sc config {name} start= auto"
        )
        return
    with contextlib.suppress(Exception):
        winservice.sync_config_start_mode(config_dir() / "bin", name, "Automatic")
    print(
        f"[daemon] '{name}' start mode set back to Automatic — palctl {why}, so "
        "Windows starts\n         your server at boot again (palctl had taken "
        "that job while it was running as a service)."
    )


def _register_health_task(*, as_system: bool) -> None:
    """Best-effort: put the hung-daemon healer in place (Windows Task
    Scheduler). The wrapper restarts a crash; this catches the wedge — alive
    but not polling — that nothing on Windows acted on before. Never fatal:
    a daemon without its healer is still a daemon."""
    if not sys.platform.startswith("win"):
        return
    from . import wintask

    exe, args, app_dir = service_target()
    # A source install's command is `python -m palctl.daemon`, which only
    # resolves from the checkout — and a scheduled task runs from System32
    # unless told otherwise. The frozen build needs no directory (absolute exe,
    # no -m), so only pass one when there are arguments to resolve.
    if wintask.register_health_task(
        exe, args, as_system=as_system, app_dir=app_dir if args else None
    ):
        print(
            "[daemon] health watchdog scheduled: every 5 minutes palctl checks "
            "itself and auto-restarts a hung daemon."
        )
    else:
        print(
            "[daemon] couldn't schedule the health watchdog (Task Scheduler "
            "refused) — a hung daemon will need a manual restart."
        )


def install_startup() -> None:
    """Register the daemon to start at login via the current user's Run key —
    the password-free path that avoids the service-logon Error 1069 entirely.
    Windows-only; a headless Linux box uses the systemd service instead."""
    if not sys.platform.startswith("win"):
        print("[daemon] login startup is Windows-only; on Linux use install-service.")
        return

    exe, args, _ = service_target()
    startup.install_startup(exe, args)
    # The healer runs as the user (no elevation needed to restart your own
    # process) and thus only while logged in — exactly when a login-mode
    # daemon is supposed to exist at all.
    _register_health_task(as_system=False)
    # A login-startup daemon does not exist until somebody signs in, so it
    # cannot be what starts the server at boot. See _hand_back_server_boot.
    _hand_back_server_boot("now starts at login rather than at boot")
    print(
        "[daemon] palctl will start automatically when you log in — no password "
        "or Windows service needed."
    )


def uninstall_startup() -> None:
    if not sys.platform.startswith("win"):
        return
    from . import wintask

    startup.uninstall_startup()
    # The healer must go with the thing it heals, or it resurrects a daemon
    # the user just turned off.
    wintask.remove_health_task()
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

    exe, args, app_dir = service_target()
    argv = [exe, *(args.split() if args else []), "run", "--headless"]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    subprocess.Popen(argv, cwd=app_dir, creationflags=flags, close_fds=True)
    # Verified, not assumed: True only once the control port actually answers.
    return _wait_until(_daemon_reachable, timeout=30.0)


def _heal_daemon() -> bool:
    """Restart the daemon the way it's actually deployed. Called by the
    scheduled health check after confirmed consecutive /healthz failures.
    Service mode: bounce the service (stopping first clears a wedged process;
    force-kill anything still on the port so the fresh start can bind it).
    Login mode: replace the process, exactly like start_detached. Returns
    whether a daemon answers afterward — verified, never assumed."""
    if sys.platform.startswith("win"):
        from . import winservice

        if winservice.service_exists(SERVICE_NAME):
            procs._run_capture(["sc.exe", "stop", SERVICE_NAME])
            _wait_until(
                lambda: procs.service_state(SERVICE_NAME) in ("STOPPED", "UNKNOWN"),
                timeout=60.0,
            )
            if _daemon_reachable():
                _stop_daemon_process()  # wedged process survived the SCM stop
            winservice.start_service(SERVICE_NAME)
            return _wait_until(_daemon_reachable, timeout=30.0)
        return start_detached()
    # Linux: systemd's WatchdogSec is the primary wedge-recovery; this path
    # exists so a manual `palctl-daemon health-check` still heals there too.
    procs._run_capture(["systemctl", "restart", SERVICE_NAME])
    return _wait_until(_daemon_reachable, timeout=30.0)


def run_health_check(threshold: int | None = None) -> int:
    """The `health-check` command the scheduled task runs. Probes /healthz,
    counts consecutive failures across runs, and heals only on a confirmed
    streak — one blip (a restart in progress, a box waking from sleep) never
    triggers anything. Exit code: 0 unless a heal was attempted and failed,
    so the Task Scheduler history shows real failures and nothing else."""
    from . import healthcheck

    log = setup_logging()
    threshold = threshold if threshold is not None else healthcheck.DEFAULT_THRESHOLD
    healthy = healthcheck.probe()
    action, failures = healthcheck.decide(
        healthy=healthy, failures=healthcheck.load_failures(), threshold=threshold
    )
    healthcheck.save_failures(failures)
    if action == "ok":
        return 0
    if action == "wait":
        log.warning(
            "health-check: daemon unhealthy (%d/%d consecutive probes)",
            failures, threshold,
        )
        return 0
    log.warning(
        "health-check: daemon unhealthy for %d consecutive probes — restarting it",
        threshold,
    )
    ok = _heal_daemon()
    if ok:
        log.warning("health-check: daemon restarted and answering again")
        return 0
    log.error(
        "health-check: restart attempted but the daemon is still not answering "
        "— it needs a human (try `palctl-daemon run` in a console to see why)."
    )
    return 1


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
            "health-check",
        ],
        help="run the daemon (default); (un)register a Windows service; "
        "(un)register password-free login startup; or probe the daemon's "
        "health and restart it if it's been unresponsive (used by the "
        "scheduled health task)",
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
    if args.command == "health-check":
        sys.exit(run_health_check())

    if args.headless:
        _hide_console()
    try:
        asyncio.run(_runtime().Daemon().run())
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
