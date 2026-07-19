"""
GUI-free first-run setup orchestration.

The wizard's ``SetupWorker`` (``palctl/gui/wizard.py``) is a thin QThread wrapper
around this: it feeds a ``Config`` + ``SetupPlan`` in and streams the log lines
back to the UI through a callback. Keeping the orchestration here — preflight,
save config, the SteamCMD install with its guards (stop-server, world backup,
blanked-ini restore), enable the REST API, register services, verify — means the
most consequential sequence in the app is testable on any OS with faked
subprocesses (no Qt, no Windows), the same way the daemon's control flow is.

Every step imports its heavy / platform-specific dependency lazily, so importing
this module (e.g. from a test) stays cheap and side-effect free.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config, config_dir, set_admin_password, set_discord_token
from .discovery import is_steamcmd

# A log sink: the wizard passes ``QThread.line.emit``; tests pass ``list.append``.
Log = Callable[[str], None]

# Widely-recommended launch flags for the Palworld dedicated server.
PALSERVER_ARGS = "-useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS"


def _invoking_username() -> str:
    """The account the user-account services should log on as. %USERNAME% is what
    Windows sets for an interactive process; getpass.getuser() is the fallback
    (and what the faked-platform tests, which don't set %USERNAME%, resolve to).
    Returns "" only when neither yields a name — the one case setup must refuse
    rather than register a nameless (→ LocalSystem) service account."""
    import getpass
    import os

    name = os.environ.get("USERNAME")
    if not name:
        try:
            name = getpass.getuser()
        except Exception:
            name = ""
    return (name or "").strip()


@dataclass
class SetupPlan:
    server_root: str
    steamcmd_path: str
    api_port: int
    password: str
    install_server: bool
    install_vcredist: bool
    register_server_service: bool
    # How the daemon starts in the background: "service" (Windows service under
    # your account, starts on boot — the wizard's default), "none", or "login"
    # (password-free HKCU Run key). "login" is the CLI/legacy path only
    # (palctl-daemon install-startup, or configs from before the service
    # default); the wizard no longer offers it — see the background-group
    # comment in gui/wizard.py.
    daemon_startup: str
    service_name: str
    # Windows account password for user-account services (Path A: the daemon and
    # the game server both run as the invoking user, so palctl can read the
    # server process it watches AND its own DPAPI secrets). Set → both services
    # register under your account; blank → LocalSystem, which can't see a
    # user-owned server, can't read the Discord token, and — for a login-startup
    # daemon — leaves the watchdog watching the wrong process. The wizard
    # requires it for the "Windows service" option.
    service_password: str = ""
    # Backups. Local always runs (folder + cadence written unconditionally); the
    # off-site copy is the opt-in part, gated by backup_mirror_enabled.
    backup_root: str = ""
    backup_hours: int = 6
    backup_mirror_enabled: bool = False
    backup_mirror: str = ""
    # Discord is optional — only written when its section is ticked.
    setup_discord: bool = False
    discord_token: str = ""
    discord_channel_id: int = 0
    discord_admin_id: int = 0


@dataclass
class SetupResult:
    ok: bool
    # Whether the PalServer service actually got registered (a partial install
    # skips it); the completion dialog reads this so it stays truthful.
    server_registered: bool = False


def run_setup(cfg: Config, plan: SetupPlan, log: Log) -> SetupResult:
    """Run the full first-run setup sequence. Returns whether it succeeded and
    whether the server service was registered. Never raises — any failure is
    logged and reported as ``ok=False``, because the caller is a UI thread."""
    try:
        if not _preflight(plan, log):
            return SetupResult(False)

        # Refuse — don't just warn — any combo that would run palctl and the
        # server on different accounts. That split is what silently blinds the
        # memory watchdog, so it must be impossible to install, not merely
        # detectable afterward.
        if would_split_accounts(
            daemon_startup=plan.daemon_startup,
            service_password=plan.service_password,
            register_server_service=plan.register_server_service,
        ):
            log(
                "\n❌ This setup would run palctl as your user but the Palworld "
                "server as SYSTEM — two different Windows accounts. palctl can't "
                "read a server owned by another account, so the memory-leak "
                "watchdog would be blind. Enter your Windows password so the "
                "server runs under your account too, or untick “Register the "
                "Palworld server as a Windows service.”"
            )
            return SetupResult(False)

        # Anything that has to be downloaded comes FIRST, before a single byte
        # of config/ini is touched: a blocked download (AV HTTPS-scanning, no
        # network) then aborts a setup that hasn't changed anything, instead of
        # dying halfway with the config saved, the ini edited, and no services.
        if sys.platform.startswith("win") and (
            plan.register_server_service or plan.daemon_startup == "service"
        ):
            from . import winservice

            log("Fetching the service wrapper…")
            winservice.ensure_winsw(config_dir() / "bin")
            log("  Service wrapper ready.")

        log("Saving configuration…")
        cfg.server_root = plan.server_root
        cfg.steamcmd_path = plan.steamcmd_path
        cfg.api_port = plan.api_port
        cfg.service_name = plan.service_name
        cfg.daemon_startup = plan.daemon_startup
        # Local backups always run — write the folder and cadence
        # unconditionally; the off-site copy is the opt-in part.
        cfg.backup_root = plan.backup_root
        cfg.schedule.backup_hours = plan.backup_hours
        cfg.backup_mirror_enabled = plan.backup_mirror_enabled
        cfg.backup_mirror = plan.backup_mirror
        # Write the enabled flag both ways: on a re-run the Discord group starts
        # ticked when it was already on, so unticking it must actually turn the
        # bot off — not silently leave the saved enabled=True. When disabling,
        # the token, channel, and role are left in place so the bot can be
        # switched back on later without re-entering them.
        cfg.discord.enabled = plan.setup_discord
        if plan.setup_discord:
            cfg.discord.channel_id = plan.discord_channel_id
            cfg.discord.admin_role_id = plan.discord_admin_id
        cfg.save()
        set_admin_password(plan.password)
        if plan.setup_discord:
            set_discord_token(plan.discord_token)

        log(f"  Backups: every {plan.backup_hours}h to {plan.backup_root}.")
        if plan.backup_mirror_enabled and plan.backup_mirror:
            log(f"  Off-site copy: {plan.backup_mirror}.")
        if plan.setup_discord:
            log(
                "  Discord bot configured — it will come online shortly "
                "after palctl starts."
            )

        if plan.install_server:
            _install_server(cfg, plan, log)

        log("Enabling the REST API in PalWorldSettings.ini…")
        from .serversetup import ensure_rest_api

        ensure_rest_api(
            cfg.live_ini, cfg.default_ini,
            port=plan.api_port, password=plan.password,
        )
        log("  REST API enabled, port and admin password set.")

        server_registered = False
        if plan.register_server_service:
            server_registered = _register_server_service(cfg, plan, log)
        if plan.daemon_startup == "login":
            _setup_login_startup(log)
        elif plan.daemon_startup == "service":
            _register_daemon_service(plan, log)
        elif plan.daemon_startup == "none":
            _remove_daemon_startup(log)

        _verify_and_report(plan, server_registered, log)

        log("\n✅ Setup complete.")
        return SetupResult(True, server_registered)
    except Exception as e:
        log(f"\n❌ Setup failed: {e}")
        return SetupResult(False)


def needs_admin(*, register_server_service: bool, daemon_startup: str) -> bool:
    """Whether this setup needs an elevated process. Registering any service
    does — and so does *removing* one: switching the daemon to login startup
    or to none while its service is still registered has to unregister it,
    and an unelevated attempt fails silently, leaving the OLD daemon running.
    The wizard's readiness preview uses this too, so both stay in sync."""
    if register_server_service or daemon_startup == "service":
        return True
    from . import daemon, winservice

    return winservice.service_exists(daemon.SERVICE_NAME)


def should_prompt_setup(
    *, config_exists: bool, daemon_reachable: bool, daemon_startup: str
) -> bool:
    """Whether the GUI should open the setup wizard at launch, unasked.

    The wizard used to auto-open only when no config existed — so a setup that
    died PARTWAY (config saved, then a failed download / refused service
    registration) never re-prompted: the user landed in a GUI wired to a daemon
    that isn't running, with no signpost back to the thing that fixes it. The
    rule now: prompt until the daemon is actually up. The one exception is an
    explicit daemon_startup="none" — the user said no background palctl, and
    nagging them every launch would punish a deliberate choice. Pure, so the
    GUI can't drift from it."""
    if not config_exists:
        return True  # true first run
    if daemon_reachable:
        return False  # setup produced a live daemon; nothing to prompt about
    return daemon_startup != "none"


def would_split_accounts(
    *, daemon_startup: str, service_password: str, register_server_service: bool
) -> bool:
    """True if this plan would put the daemon and the game server on DIFFERENT
    Windows accounts — the split that blinds the memory watchdog (palctl can't
    read a server owned by another account). Pure, so setup and the wizard share
    one rule and can't disagree.

    The daemon runs as the invoking user in login mode, and in service mode when
    a Windows password is supplied; the game server runs as the user only when a
    password is supplied. So the mismatch is exactly: palctl registers the
    server, the daemon is the user, and no password was given to put the server
    on that same account. (No server service, or no daemon, means nothing to
    split; a passwordless service leaves both on LocalSystem — same account.)"""
    if not register_server_service or daemon_startup == "none":
        return False
    daemon_is_user = daemon_startup == "login" or (
        daemon_startup == "service" and bool(service_password)
    )
    game_is_user = bool(service_password)
    return daemon_is_user and not game_is_user


def _preflight(plan: SetupPlan, log: Log) -> bool:
    """Run readiness checks. Returns False (abort) only on a blocking failure —
    no disk space to install into, or no admin rights to register services.
    Everything else is a warning the run can proceed past."""
    from . import preflight

    log("Running readiness checks…")
    checks = preflight.run_all(
        plan.server_root, plan.api_port,
        need_install=plan.install_server,
        need_admin=needs_admin(
            register_server_service=plan.register_server_service,
            daemon_startup=plan.daemon_startup,
        ),
    )
    blocking = False
    for c in checks:
        log(f"  {c.icon} {c.name}: {c.detail}")
        if c.ok is not False:
            continue
        if c.name.startswith("Visual C++") and plan.install_vcredist:
            _install_vcredist(log)
        else:
            log(f"     → {c.fix}")
            if c.name in ("Disk space", "Administrator"):
                blocking = True
    if blocking:
        log("\n❌ Fix the ❌ item(s) above, then run setup again.")
    return not blocking


def _install_vcredist(log: Log) -> None:
    from . import preflight

    code = preflight.install_vcredist(on_line=lambda m: log(f"     {m}"))
    log(f"     Visual C++ runtime installer finished (exit {code}).")


def _install_server(cfg: Config, plan: SetupPlan, log: Log) -> None:
    import asyncio
    import shutil

    from . import backups, procs, steamcmd
    from .inifile import is_blank

    steam = Path(plan.steamcmd_path)
    if not is_steamcmd(steam):
        target_dir = steam.parent if plan.steamcmd_path else config_dir() / "steamcmd"
        log(f"Downloading SteamCMD into {target_dir}…")
        steam = steamcmd.download_steamcmd(target_dir)
        cfg.steamcmd_path = str(steam)
        cfg.save()
        log(f"  SteamCMD ready at {steam}")

    # Re-running setup over an existing install is supported (our own error
    # messages say "run setup again"), so this path needs the same guards the
    # scheduled update has: never let SteamCMD rewrite files under a running
    # server, and keep the world and the tuned ini safe across `validate` (which
    # blanks PalWorldSettings.ini).
    if procs.find_process() is not None:
        log("The server is running — stopping it before the update…")
        try:
            asyncio.run(procs.stop_service(plan.service_name))
        except Exception as e:
            log(f"  couldn't stop the service: {e}")
        if procs.find_process() is not None:
            raise RuntimeError(
                "The Palworld server is still running — updating its files "
                "now could corrupt them. Stop the server, then run setup "
                "again."
            )

    savegames = cfg.savegames_dir
    if savegames.exists():
        log("Backing up the world before the update…")
        b = backups.create(savegames, Path(cfg.backup_root), "pre-update")
        log(f"  world backed up to {b.path}")

    ini_backup = steamcmd.backup_file(cfg.live_ini)

    log(
        f"Installing / updating the Palworld server into {plan.server_root} "
        "(this downloads a few GB the first time)…"
    )

    # SteamCMD is chatty; collapse its output into a single updating percentage
    # so a multi-GB download doesn't look frozen or spam the log.
    last_pct = [-1]

    def sink(line: str) -> None:
        pct = steamcmd.parse_progress(line)
        if pct is not None:
            whole = int(pct)
            if whole != last_pct[0]:
                last_pct[0] = whole
                log(f"  downloading… {whole}%")
        elif line.strip():
            log(f"  {line}")

    try:
        code = steamcmd.run_update(steam, plan.server_root, app_id=cfg.app_id, on_line=sink)
    finally:
        if ini_backup and is_blank(cfg.live_ini):
            shutil.copy2(ini_backup, cfg.live_ini)
            log(
                "  SteamCMD blanked PalWorldSettings.ini — restored it from "
                "the pre-update backup."
            )
    log(f"  SteamCMD finished (exit {code}).")

    # SteamCMD's exit code is famously unreliable, so verify the actual artifact:
    # if the server files aren't there, the download didn't finish. Abort with
    # the real reason instead of falling through to ensure_rest_api's misleading
    # "is the server installed at that path?" error a step later.
    if not cfg.default_ini.exists():
        raise RuntimeError(
            f"The Palworld server files aren't present after SteamCMD ran "
            f"(exit {code}). The download didn't finish — usually a dropped "
            "internet connection or the disk filling up. Check both, then run "
            "setup again."
        )


def _verify_and_report(plan: SetupPlan, server_registered: bool, log: Log) -> None:
    """The payoff: actually start the server and confirm the REST API answers,
    then print the address players connect to. 'It works' beats 'it's set up'.

    Only starts/verifies when the server service was actually registered — on a
    partial install (server files present but PalServer.exe missing) registration
    is skipped, and starting a service that doesn't exist would hang for the full
    timeout and then falsely claim it 'Registered'."""
    import asyncio

    from . import netinfo, procs
    from .api import PalApi

    if server_registered:
        log("Starting the server to check it actually works…")
        started = False
        try:
            started = asyncio.run(procs.start_service(plan.service_name))
        except Exception as e:
            log(f"  couldn't start the service: {e}")
        if not started:
            # The SCM refused or the service died — say WHY (Error 1069 & co.,
            # read from the service's recorded exit code) instead of sitting
            # out a four-minute REST wait for a server that never launched.
            reason = procs.service_failure_reason(plan.service_name)
            log(
                f"  ❌ The '{plan.service_name}' service did not start"
                + (f" — {reason}" if reason else "")
                + " Check services.msc, then run setup again."
            )
            return

        log("  waiting for the server to answer (this can take a minute)…")
        api = PalApi("127.0.0.1", plan.api_port, plan.password)
        ok = False
        try:
            ok = asyncio.run(api.wait_until_alive(timeout=240))
        except Exception as e:
            log(f"  REST check error: {e}")
        log(
            "  ✅ Server is up and answering — it works."
            if ok
            else "  ⚠️ Registered, but the server hasn't answered yet. Give "
            "it a minute (Palworld is slow to boot) and watch the Dashboard. "
            "If it never answers, confirm RESTAPIEnabled=True landed in the "
            "live PalWorldSettings.ini under Saved/Config — not the Default "
            "one — and check the log."
        )

    lan = netinfo.lan_ip()
    pub = netinfo.public_ip()
    port = netinfo.GAME_PORT_DEFAULT
    log("\nTell your friends to connect to:")
    if lan:
        log(f"  On your network:   {lan}:{port}")
    if pub:
        log(f"  Over the internet:  {pub}:{port}")
    log(
        f"  For internet play you must forward UDP port {port} on your router "
        "to this PC — palctl can't do that part for you."
    )


def _register_server_service(cfg: Config, plan: SetupPlan, log: Log) -> bool:
    """Register the PalServer Windows service. Returns False (skipped) when
    PalServer.exe isn't there yet, so the caller doesn't try to start a service
    that was never created.

    With a service password (Path A), it registers under the invoking user — so
    palctl, running as that same user, can actually read the server process it
    watches; otherwise it stays LocalSystem."""
    from . import winservice

    exe = Path(plan.server_root) / "PalServer.exe"
    if not exe.exists():
        log(
            f"  ⚠️ {exe} not found — skipping the server service. Install the "
            "server (tick “Install / update the server”) or fix the server "
            "root, then run setup again."
        )
        return False
    user = password = None
    if plan.service_password:
        # The account the service must log on as. `.\` + empty is a nameless
        # account spec that WinSW silently turns into LocalSystem — re-creating
        # the very account split Path A exists to prevent — so resolve a real
        # name (%USERNAME%, else getpass) and refuse with the cause if there
        # genuinely isn't one, rather than registering a broken service.
        username = _invoking_username()
        if not username:
            raise RuntimeError(
                "Windows didn't report a user account for this process, so palctl "
                "can't register the server under your account. Run setup from your "
                "normal signed-in session."
            )
        user = f".\\{username}"
        password = plan.service_password

    bin_dir = config_dir() / "bin"
    # PalServer flushes the world on the way down; a wrapper that kills it at the
    # default 30s on a plain `net stop` or system shutdown risks the save. 90s
    # matches how long palctl itself waits for a stopping server.
    stop_timeout = "90 sec"

    # Never bounce a healthy, already-correct server on a wizard re-run. If the
    # service is already registered with exactly the config we'd write,
    # re-registering (stop → delete → register → start) would only restart a
    # running server — disconnecting players — for no change at all. Leave it be.
    # Both the start=False registration below and _verify_and_report's
    # start_service are no-ops on an already-running service, so skipping here
    # means the whole re-run never touches the server.
    if winservice.config_is_current(
        bin_dir, plan.service_name, exe, PALSERVER_ARGS, plan.server_root,
        user=user, stop_timeout=stop_timeout,
    ):
        log(
            f"  Service '{plan.service_name}' is already registered and unchanged "
            "— leaving it, and your running server, exactly as they are."
        )
        return True

    log(f"Registering the '{plan.service_name}' Windows service…")
    winsw = winservice.ensure_winsw(bin_dir)
    winservice.install_service(
        winsw, plan.service_name, exe, PALSERVER_ARGS, plan.server_root,
        user=user, password=password, start=False,
        stop_timeout=stop_timeout,
    )
    log(
        f"  Service '{plan.service_name}' registered"
        + (f" (runs as {user})." if user else ".")
    )
    return True


def _register_daemon_service(plan: SetupPlan, log: Log) -> None:
    from . import daemon

    # Path A: with a service password, run the daemon as the invoking user so it
    # shares that account with the game server — and can read the server it
    # manages plus its own DPAPI secrets (the Discord token). No password keeps
    # the old LocalSystem behaviour.
    as_user = bool(plan.service_password)
    log(
        f"Registering the '{daemon.SERVICE_NAME}' service"
        + (" under your account…" if as_user else "…")
    )
    if daemon.install_service(as_user=as_user, password=plan.service_password or None):
        log(f"  Service '{daemon.SERVICE_NAME}' registered and started.")
    else:
        log(
            f"  ⚠️ Service '{daemon.SERVICE_NAME}' registered, but the daemon "
            "isn't answering yet — check the service in services.msc "
            "(or systemctl on Linux)."
        )


def _remove_daemon_startup(log: Log) -> None:
    """The background group was unticked. Same contract the Discord toggle
    documents in run_setup: unticking on a re-run must actually turn the thing
    off — remove whichever startup mechanism a previous run registered and
    stop any daemon it left running."""
    from . import daemon

    log("Background startup is unticked — removing palctl autostart…")
    try:
        daemon.disable_background_startup()
    except OSError as e:
        # Best-effort: an unremovable unit file (e.g. no root on Linux) must
        # not fail the whole setup that just succeeded.
        log(f"  ⚠️ couldn't fully remove the old autostart: {e}")
        return
    log("  palctl will not run in the background.")


def _setup_login_startup(log: Log) -> None:
    from . import daemon

    log("Setting palctl to start when you log in (no password needed)…")
    daemon.install_startup()
    # Register-only would wait for the next login; start it now so the dashboard
    # works immediately.
    if daemon.start_detached():
        log("  Done — palctl is running now and will start at each login.")
    else:
        log(
            "  ⚠️ Login startup is registered, but the daemon isn't confirmed "
            "running yet — it will start at your next login if not sooner."
        )
