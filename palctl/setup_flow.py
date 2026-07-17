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

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config, config_dir, set_admin_password, set_discord_token
from .discovery import is_steamcmd

# A log sink: the wizard passes ``QThread.line.emit``; tests pass ``list.append``.
Log = Callable[[str], None]

# Widely-recommended launch flags for the Palworld dedicated server.
PALSERVER_ARGS = "-useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS"


@dataclass
class SetupPlan:
    server_root: str
    steamcmd_path: str
    api_port: int
    password: str
    install_server: bool
    install_vcredist: bool
    register_server_service: bool
    # How the daemon starts in the background: "login" (password-free HKCU Run
    # key, the default), "service" (Windows service, starts on boot), or "none".
    daemon_startup: str
    service_name: str
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

        log("Saving configuration…")
        cfg.server_root = plan.server_root
        cfg.steamcmd_path = plan.steamcmd_path
        cfg.api_port = plan.api_port
        cfg.service_name = plan.service_name
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
            _register_daemon_service(log)
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
        try:
            asyncio.run(procs.start_service(plan.service_name))
        except Exception as e:
            log(f"  couldn't start the service: {e}")

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
    that was never created."""
    from . import winservice

    exe = Path(plan.server_root) / "PalServer.exe"
    if not exe.exists():
        log(
            f"  ⚠️ {exe} not found — skipping the server service. Install the "
            "server (tick “Install / update the server”) or fix the server "
            "root, then run setup again."
        )
        return False
    log(f"Registering the '{plan.service_name}' Windows service…")
    nssm = winservice.ensure_nssm(config_dir() / "bin")
    winservice.install_service(
        nssm, plan.service_name, exe, PALSERVER_ARGS, plan.server_root, start=False
    )
    log(f"  Service '{plan.service_name}' registered.")
    return True


def _register_daemon_service(log: Log) -> None:
    from . import daemon

    log(f"Registering the '{daemon.SERVICE_NAME}' Windows service…")
    daemon.install_service()
    log(f"  Service '{daemon.SERVICE_NAME}' registered and started.")


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
        log("  Done — palctl will start at your next login.")
