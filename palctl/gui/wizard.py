"""
First-run setup wizard.

Everything palctl needs to actually start managing a server used to be a wall of
manual steps spread across the README: find four paths, hand-edit three ini
keys, run two sets of `nssm install` lines in an admin terminal. This does all of
it from one dialog — detect the paths, turn the REST API on, optionally install
the server with SteamCMD, and register both Windows services — while streaming a
live log so nothing happens behind your back.

Two later features — an off-site backup mirror and the Discord bot — hang off
this same flow as *optional* sections: unchecked, the wizard ignores them
entirely; checked, it walks the user through the fields and writes the config,
so a first-run user can discover and set them up without hunting through the
Config tab afterwards.

The heavy lifting lives in the tested, GUI-free modules (discovery, serversetup,
steamcmd, winservice); this file is just the form and a worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    Config,
    config_dir,
    get_discord_token,
    set_admin_password,
    set_discord_token,
)
from ..discovery import detect_server_roots, detect_steamcmd, is_server_root, is_steamcmd
from . import icons
from .main import PathPicker, _MirrorTestWorker
from .widgets import NoScrollSpinBox

# Widely-recommended launch flags for the Palworld dedicated server.
PALSERVER_ARGS = "-useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS"


def _default_backup_dir(cfg: Config) -> str:
    """A backup folder that actually exists to pre-fill the wizard with.

    The built-in default is ``D:\\PalworldBackups``, which doesn't exist on the
    common single-``C:``-drive box — so scheduled backups (on by default) would
    fail their `mkdir` every few hours. Keep the configured folder when its drive
    is real; otherwise fall back to the user's home, which always exists and is
    writable."""
    current = cfg.backup_root
    try:
        anchor = Path(current).anchor if current else ""
        if anchor and Path(anchor).exists():
            return current
    except OSError:
        pass
    return str(Path.home() / "PalworldBackups")


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


class SetupWorker(QThread):
    """Runs the (slow, networked) setup steps off the UI thread."""

    line = Signal(str)
    done = Signal(bool)

    def __init__(self, cfg: Config, plan: SetupPlan) -> None:
        super().__init__()
        self._cfg = cfg
        self._plan = plan
        # Whether the PalServer service actually got registered (a partial
        # install skips it); read by the completion dialog so it stays truthful.
        self.server_registered = False

    def _log(self, msg: str) -> None:
        self.line.emit(msg)

    def run(self) -> None:
        plan = self._plan
        cfg = self._cfg
        try:
            if not self._preflight(plan):
                self.done.emit(False)
                return

            self._log("Saving configuration…")
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
            if plan.setup_discord:
                cfg.discord.enabled = True
                cfg.discord.channel_id = plan.discord_channel_id
                cfg.discord.admin_role_id = plan.discord_admin_id
            cfg.save()
            set_admin_password(plan.password)
            if plan.setup_discord:
                set_discord_token(plan.discord_token)

            self._log(
                f"  Backups: every {plan.backup_hours}h to {plan.backup_root}."
            )
            if plan.backup_mirror_enabled and plan.backup_mirror:
                self._log(f"  Off-site copy: {plan.backup_mirror}.")
            if plan.setup_discord:
                self._log(
                    "  Discord bot configured — it will come online shortly "
                    "after palctl starts."
                )

            if plan.install_server:
                self._install_server(cfg, plan)

            self._log("Enabling the REST API in PalWorldSettings.ini…")
            from ..serversetup import ensure_rest_api

            ensure_rest_api(
                cfg.live_ini, cfg.default_ini,
                port=plan.api_port, password=plan.password,
            )
            self._log("  REST API enabled, port and admin password set.")

            server_registered = False
            if plan.register_server_service:
                server_registered = self._register_server_service(cfg, plan)
            self.server_registered = server_registered
            if plan.daemon_startup == "login":
                self._setup_login_startup()
            elif plan.daemon_startup == "service":
                self._register_daemon_service()

            self._verify_and_report(plan, server_registered)

            self._log("\n✅ Setup complete.")
            self.done.emit(True)
        except Exception as e:
            self._log(f"\n❌ Setup failed: {e}")
            self.done.emit(False)

    def _preflight(self, plan: SetupPlan) -> bool:
        """Run readiness checks. Returns False (abort) only on a blocking failure —
        no disk space to install into, or no admin rights to register services.
        Everything else is a warning the run can proceed past."""
        from .. import preflight

        self._log("Running readiness checks…")
        checks = preflight.run_all(
            plan.server_root, plan.api_port,
            need_install=plan.install_server,
            # Login startup needs no elevation; only a service does.
            need_admin=plan.register_server_service or plan.daemon_startup == "service",
        )
        blocking = False
        for c in checks:
            self._log(f"  {c.icon} {c.name}: {c.detail}")
            if c.ok is not False:
                continue
            if c.name.startswith("Visual C++") and plan.install_vcredist:
                self._install_vcredist()
            else:
                self._log(f"     → {c.fix}")
                if c.name in ("Disk space", "Administrator"):
                    blocking = True
        if blocking:
            self._log("\n❌ Fix the ❌ item(s) above, then run setup again.")
        return not blocking

    def _install_vcredist(self) -> None:
        from .. import preflight

        code = preflight.install_vcredist(on_line=lambda m: self._log(f"     {m}"))
        self._log(f"     Visual C++ runtime installer finished (exit {code}).")

    def _install_server(self, cfg: Config, plan: SetupPlan) -> None:
        import asyncio
        import shutil

        from .. import backups, procs, steamcmd
        from ..inifile import is_blank

        steam = Path(plan.steamcmd_path)
        if not is_steamcmd(steam):
            target_dir = steam.parent if plan.steamcmd_path else config_dir() / "steamcmd"
            self._log(f"Downloading SteamCMD into {target_dir}…")
            steam = steamcmd.download_steamcmd(target_dir)
            cfg.steamcmd_path = str(steam)
            cfg.save()
            self._log(f"  SteamCMD ready at {steam}")

        # Re-running setup over an existing install is supported (our own error
        # messages say "run setup again"), so this path needs the same guards
        # the scheduled update has: never let SteamCMD rewrite files under a
        # running server, and keep the world and the tuned ini safe across
        # `validate` (which blanks PalWorldSettings.ini).
        if procs.find_process() is not None:
            self._log("The server is running — stopping it before the update…")
            try:
                asyncio.run(procs.stop_service(plan.service_name))
            except Exception as e:
                self._log(f"  couldn't stop the service: {e}")
            if procs.find_process() is not None:
                raise RuntimeError(
                    "The Palworld server is still running — updating its files "
                    "now could corrupt them. Stop the server, then run setup "
                    "again."
                )

        savegames = cfg.savegames_dir
        if savegames.exists():
            self._log("Backing up the world before the update…")
            b = backups.create(savegames, Path(cfg.backup_root), "pre-update")
            self._log(f"  world backed up to {b.path}")

        ini_backup = steamcmd.backup_file(cfg.live_ini)

        self._log(
            f"Installing / updating the Palworld server into {plan.server_root} "
            "(this downloads a few GB the first time)…"
        )
        self._last_pct = -1
        try:
            code = steamcmd.run_update(
                steam, plan.server_root, app_id=cfg.app_id, on_line=self._steam_sink
            )
        finally:
            if ini_backup and is_blank(cfg.live_ini):
                shutil.copy2(ini_backup, cfg.live_ini)
                self._log(
                    "  SteamCMD blanked PalWorldSettings.ini — restored it from "
                    "the pre-update backup."
                )
        self._log(f"  SteamCMD finished (exit {code}).")

        # SteamCMD's exit code is famously unreliable, so verify the actual
        # artifact: if the server files aren't there, the download didn't finish.
        # Abort with the real reason instead of falling through to ensure_rest_api's
        # misleading "is the server installed at that path?" error a step later.
        if not cfg.default_ini.exists():
            raise RuntimeError(
                f"The Palworld server files aren't present after SteamCMD ran "
                f"(exit {code}). The download didn't finish — usually a dropped "
                "internet connection or the disk filling up. Check both, then run "
                "setup again."
            )

    def _steam_sink(self, line: str) -> None:
        """Turn SteamCMD's chatty output into a single, updating percentage so a
        multi-GB download doesn't look frozen or spam the log."""
        from .. import steamcmd

        pct = steamcmd.parse_progress(line)
        if pct is not None:
            whole = int(pct)
            if whole != self._last_pct:
                self._last_pct = whole
                self._log(f"  downloading… {whole}%")
        elif line.strip():
            self._log(f"  {line}")

    def _verify_and_report(self, plan: SetupPlan, server_registered: bool) -> None:
        """The payoff: actually start the server and confirm the REST API answers,
        then print the address players connect to. 'It works' beats 'it's set up'.

        Only starts/verifies when the server service was actually registered —
        on a partial install (server files present but PalServer.exe missing)
        registration is skipped, and starting a service that doesn't exist would
        hang for the full timeout and then falsely claim it 'Registered'."""
        import asyncio

        from .. import netinfo, procs
        from ..api import PalApi

        if server_registered:
            self._log("Starting the server to check it actually works…")
            try:
                asyncio.run(procs.start_service(plan.service_name))
            except Exception as e:
                self._log(f"  couldn't start the service: {e}")

            self._log("  waiting for the server to answer (this can take a minute)…")
            api = PalApi("127.0.0.1", plan.api_port, plan.password)
            ok = False
            try:
                ok = asyncio.run(api.wait_until_alive(timeout=240))
            except Exception as e:
                self._log(f"  REST check error: {e}")
            self._log(
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
        self._log("\nTell your friends to connect to:")
        if lan:
            self._log(f"  On your network:   {lan}:{port}")
        if pub:
            self._log(f"  Over the internet:  {pub}:{port}")
        self._log(
            f"  For internet play you must forward UDP port {port} on your router "
            "to this PC — palctl can't do that part for you."
        )

    def _register_server_service(self, cfg: Config, plan: SetupPlan) -> bool:
        """Register the PalServer Windows service. Returns False (skipped) when
        PalServer.exe isn't there yet, so the caller doesn't try to start a
        service that was never created."""
        from .. import winservice

        exe = Path(plan.server_root) / "PalServer.exe"
        if not exe.exists():
            self._log(
                f"  ⚠️ {exe} not found — skipping the server service. Install the "
                "server (tick “Install / update the server”) or fix the server "
                "root, then run setup again."
            )
            return False
        self._log(f"Registering the '{plan.service_name}' Windows service…")
        nssm = winservice.ensure_nssm(config_dir() / "bin")
        winservice.install_service(
            nssm, plan.service_name, exe, PALSERVER_ARGS, plan.server_root, start=False
        )
        self._log(f"  Service '{plan.service_name}' registered.")
        return True

    def _register_daemon_service(self) -> None:
        from .. import daemon

        self._log(f"Registering the '{daemon.SERVICE_NAME}' Windows service…")
        daemon.install_service()
        self._log(f"  Service '{daemon.SERVICE_NAME}' registered and started.")

    def _setup_login_startup(self) -> None:
        from .. import daemon

        self._log("Setting palctl to start when you log in (no password needed)…")
        daemon.install_startup()
        # Register-only would wait for the next login; start it now so the
        # dashboard works immediately.
        if daemon.start_detached():
            self._log("  Done — palctl is running now and will start at each login.")
        else:
            self._log("  Done — palctl will start at your next login.")


class SetupWizard(QDialog):
    def __init__(
        self, cfg: Config, parent: QWidget | None = None, *, first_run: bool = False
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._worker: SetupWorker | None = None
        self.setWindowTitle("palctl setup")
        self.setWindowIcon(icons.load_icon("wizard"))
        self.resize(760, 620)

        root = QVBoxLayout(self)

        intro = QLabel(
            "Let's get palctl talking to your server. It'll auto-detect what it "
            "can — fix anything with a red ✗, then run setup.\n\n"
            "The Palworld dedicated server itself comes from Steam. If it isn't "
            "installed yet, tick “Install / update the server” and this will fetch "
            "it with SteamCMD for you. Backups run automatically once set up; "
            "off-site copies and the Discord bot are optional extras below."
            if first_run
            else "Re-run any part of setup. Detected paths are pre-filled."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        # The input groups live in a scroll area so the optional Backups and
        # Discord sections can't push the dialog taller than a small screen; the
        # progress bar, log, and buttons stay pinned below it.
        scroll = QScrollArea(widgetResizable=True)
        form_host = QWidget()
        scroll.setWidget(form_host)
        form = QVBoxLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)

        paths = QGroupBox("Paths")
        pf = QFormLayout(paths)
        self.server_root = PathPicker(
            cfg.server_root, pick_file=False,
            detect=detect_server_roots, validate=is_server_root,
        )
        self.steamcmd = PathPicker(
            cfg.steamcmd_path, pick_file=True,
            detect=detect_steamcmd, validate=is_steamcmd,
            file_filter="steamcmd.exe (steamcmd.exe);;All files (*.*)",
        )
        pf.addRow("Server root", self.server_root)
        pf.addRow("steamcmd.exe", self.steamcmd)
        form.addWidget(paths)

        api = QGroupBox("REST API")
        af = QFormLayout(api)
        self.port = NoScrollSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(cfg.api_port)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Admin password (pick something)")
        gen = QPushButton("Generate")
        gen.clicked.connect(self._generate_password)
        pw_row = QHBoxLayout()
        pw_row.addWidget(self.password, 1)
        pw_row.addWidget(gen)
        pw_widget = QWidget()
        pw_widget.setLayout(pw_row)
        af.addRow("Port", self.port)
        af.addRow("Admin password", pw_widget)
        form.addWidget(api)

        steps = QGroupBox("Do now")
        sf = QVBoxLayout(steps)
        self.install_server = QCheckBox(
            "Install / update the server with SteamCMD (needed if it isn't installed)"
        )
        self.install_vc = QCheckBox(
            "Install the Visual C++ runtime if it's missing (the server needs it)"
        )
        self.install_vc.setChecked(True)
        self.reg_server = QCheckBox("Register the Palworld server as a Windows service")
        self.reg_server.setChecked(True)
        for cb in (self.install_server, self.install_vc, self.reg_server):
            sf.addWidget(cb)
        form.addWidget(steps)

        # How the daemon runs in the background. Login startup is the default:
        # it needs no password (unlike a user service, which fails with Error
        # 1069 on PIN-only accounts) and gets full user context incl. the
        # Discord token. A service is offered for boxes that must run before
        # anyone logs in.
        bg = QGroupBox("Keep palctl running in the background")
        bg.setCheckable(True)
        bg.setChecked(True)
        bgf = QVBoxLayout(bg)
        self.startup_login = QRadioButton(
            "Start when I log in  —  recommended (no password, works with the "
            "Discord bot)"
        )
        self.startup_login.setChecked(True)
        self.startup_service = QRadioButton(
            "Run as a Windows service  —  starts on boot before login, but can't "
            "use the Discord bot"
        )
        bgf.addWidget(self.startup_login)
        bgf.addWidget(self.startup_service)
        self._bg_group = bg
        form.addWidget(bg)

        form.addWidget(self._build_backups_group(cfg))
        form.addWidget(self._build_discord_group(cfg))
        form.addStretch(1)
        root.addWidget(scroll, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate; hidden until running
        self.progress.hide()
        root.addWidget(self.progress)

        self.log = QTextEdit(readOnly=True)
        self.log.setPlaceholderText("Setup progress will appear here…")
        self.log.setMinimumHeight(150)  # never let the scroll area squeeze it away
        root.addWidget(self.log, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        self.check_btn = QPushButton(
            icons.load_icon("status-ok", color=icons.OK), "Check readiness"
        )
        self.check_btn.clicked.connect(self._check_readiness)
        self.run_btn = QPushButton(icons.load_icon("wizard"), "Run setup")
        self.run_btn.clicked.connect(self._run)
        buttons.addWidget(self.close_btn)
        buttons.addWidget(self.check_btn)
        buttons.addWidget(self.run_btn)
        root.addLayout(buttons)

    def _build_backups_group(self, cfg: Config) -> QGroupBox:
        """Local backups always run — this group sets where and how often, and
        offers an optional off-site copy. Not checkable, unlike Discord: backups
        are the safety net, not something to skip. Pre-filled with a folder that
        exists, because scheduled backups run by default and the built-in D:\\
        default silently fails on a box without that drive."""
        bk = QGroupBox("Backups")
        bkf = QFormLayout(bk)
        self.backup_root = PathPicker(_default_backup_dir(cfg), pick_file=False)
        bkf.addRow("Backup folder", self.backup_root)

        # Capped at 24h: local backups always happen at least once a day.
        self.backup_hours = NoScrollSpinBox()
        self.backup_hours.setRange(1, 24)
        self.backup_hours.setSuffix(" h")
        self.backup_hours.setValue(min(24, max(1, cfg.schedule.backup_hours)))
        self.backup_hours.setToolTip(
            "Local backups always run — this is only how often. At least once a "
            "day; pick anything more frequent."
        )
        bkf.addRow("Back up every", self.backup_hours)

        # Off-site copy: opt-in, off by default. The location can be typed while
        # disabled, but only takes effect once the box is ticked.
        self.mirror_enabled = QCheckBox(
            "Also copy each backup off-site (survives a dead disk)"
        )
        self.mirror_enabled.setChecked(cfg.backup_mirror_enabled)
        bkf.addRow(self.mirror_enabled)

        # Not a PathPicker: an rclone remote like gdrive:Pal isn't a filesystem
        # path. Test proves the target works before backups rely on it.
        mirror_row = QHBoxLayout()
        self.backup_mirror = QLineEdit(cfg.backup_mirror)
        self.backup_mirror.setPlaceholderText(
            r"D:\Backups, \\nas\pal, or gdrive:PalworldBackups"
        )
        self._mirror_test = QPushButton("Test")
        self._mirror_test.clicked.connect(self._test_mirror)
        mirror_row.addWidget(self.backup_mirror, 1)
        mirror_row.addWidget(self._mirror_test)
        self._mirror_widget = QWidget()
        self._mirror_widget.setLayout(mirror_row)
        bkf.addRow("Off-site location", self._mirror_widget)

        bk_help = QLabel(
            "An off-site copy — another disk, a network share, or the cloud — "
            "survives this disk failing. For cloud (Google Drive, Dropbox, S3…), "
            'install <a href="https://rclone.org">rclone</a>, run '
            "<code>rclone config</code>, then enter a dedicated folder like "
            "<code>gdrive:PalworldBackups</code> — palctl only ever touches that "
            "one folder. Use <b>Test</b> to check it first."
        )
        bk_help.setOpenExternalLinks(True)
        bk_help.setWordWrap(True)
        bkf.addRow("", bk_help)

        # Grey out the location until off-site is ticked.
        self.mirror_enabled.toggled.connect(self._mirror_widget.setEnabled)
        self._mirror_widget.setEnabled(self.mirror_enabled.isChecked())
        return bk

    def _build_discord_group(self, cfg: Config) -> QGroupBox:
        """Optional. Ticked, the wizard stores the token and IDs and enables the
        bot; unticked (the default on first run), it leaves Discord off. Mirrors
        the Config tab's fields and help so the bot can be set up here directly."""
        dc = QGroupBox("Set up the Discord bot  —  optional")
        dc.setCheckable(True)
        dc.setChecked(cfg.discord.enabled)
        df = QFormLayout(dc)
        self.dc_token = QLineEdit(get_discord_token())
        self.dc_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.dc_channel = QLineEdit(str(cfg.discord.channel_id or ""))
        self.dc_role = QLineEdit(str(cfg.discord.admin_role_id or ""))
        df.addRow("Bot token", self.dc_token)
        token_help = QLabel(
            'No token yet? <a href="https://discord.com/developers/applications">'
            "Open the Discord Developer Portal</a> → <b>New Application</b> → "
            "<b>Bot</b> → <b>Reset Token</b> → copy it here, then invite the bot "
            "with the <b>bot</b> and <b>applications.commands</b> scopes."
        )
        token_help.setOpenExternalLinks(True)
        token_help.setWordWrap(True)
        df.addRow(token_help)
        df.addRow("Channel ID", self.dc_channel)
        df.addRow("Admin role/user ID", self.dc_role)
        # Both IDs are numeric, so a wrong-*kind* of ID (user pasted for a role)
        # passes the save-time number check but never matches — spell out where
        # each comes from, as the Config tab does.
        id_help = QLabel(
            "Turn on Discord <b>Developer Mode</b> (Settings → Advanced), then "
            "right-click the channel → <b>Copy Channel ID</b>. For who may run "
            "/announce, /restart, /kick, etc., copy a <b>role</b> ID (everyone "
            "with it) <b>or</b> a single <b>user</b> ID — or leave it blank to "
            "allow anyone with <b>Manage Server</b>."
        )
        id_help.setWordWrap(True)
        df.addRow(id_help)
        guide = QLabel(
            'Full walkthrough: <a href="https://github.com/SteveWeed79/palctl/'
            'blob/main/docs/discord.md">Discord bot setup guide</a>.'
        )
        guide.setOpenExternalLinks(True)
        guide.setWordWrap(True)
        df.addRow(guide)
        self._discord_group = dc
        return dc

    def _test_mirror(self) -> None:
        target = self.backup_mirror.text().strip()
        if not target:
            QMessageBox.information(
                self, "Off-site mirror",
                "No mirror set — that's fine. A mirror keeps a second copy on "
                "another disk, a network share, or a cloud remote (remote:path) "
                "so your backups survive this disk failing.",
            )
            return
        self._mirror_test.setEnabled(False)
        self._mirror_test.setText("Testing…")
        # Held on self so the thread isn't garbage-collected mid-run.
        self._mirror_worker = _MirrorTestWorker(target)
        self._mirror_worker.done.connect(self._mirror_test_done)
        self._mirror_worker.start()

    def _mirror_test_done(self, ok: bool, msg: str) -> None:
        self._mirror_test.setEnabled(True)
        self._mirror_test.setText("Test")
        if ok:
            QMessageBox.information(self, "Off-site mirror — reachable", msg)
        else:
            QMessageBox.warning(self, "Off-site mirror — problem", msg)

    def _check_readiness(self) -> None:
        """A quick, side-effect-free preview of the preflight checks so the user
        can fix problems before committing to a multi-GB download."""
        from .. import preflight

        self.log.clear()
        self.log.append("Readiness check:")
        checks = preflight.run_all(
            self.server_root.text().strip(), self.port.value(),
            need_install=self.install_server.isChecked(),
            need_admin=self.reg_server.isChecked() or self._daemon_startup() == "service",
        )
        for c in checks:
            self.log.append(f"  {c.icon} {c.name}: {c.detail}")
            if c.ok is False and c.fix:
                self.log.append(f"     → {c.fix}")

    def _generate_password(self) -> None:
        import secrets

        self.password.setText(secrets.token_urlsafe(12))
        self.password.setEchoMode(QLineEdit.EchoMode.Normal)

    def _daemon_startup(self) -> str:
        if not self._bg_group.isChecked():
            return "none"
        return "service" if self.startup_service.isChecked() else "login"

    def _setup_running(self) -> bool:
        worker = getattr(self, "_worker", None)
        return bool(worker and worker.isRunning())

    def reject(self) -> None:
        # Disabling the Close button doesn't stop Esc or the title-bar X from
        # dismissing a QDialog — which would hide the log while SetupWorker
        # keeps registering services and downloading gigabytes invisibly.
        # Block dismissal until the run finishes.
        if self._setup_running():
            QMessageBox.information(
                self, "Setup is running",
                "Setup is still working — closing this window now would leave "
                "it running invisibly in the background. Wait for it to finish "
                "(the log below shows progress).",
            )
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._setup_running():
            event.ignore()
            self.reject()  # shows the same explanation
            return
        super().closeEvent(event)

    def _run(self) -> None:
        password = self.password.text().strip()
        if not password:
            QMessageBox.warning(
                self, "Admin password needed",
                "Set an admin password — it's what secures the REST API. "
                "Use Generate if you don't care what it is.",
            )
            return

        # Off-site copy is opt-in: if ticked, it needs a location to copy to.
        mirror_enabled = self.mirror_enabled.isChecked()
        backup_mirror = self.backup_mirror.text().strip()
        if mirror_enabled and not backup_mirror:
            QMessageBox.warning(
                self, "Off-site location needed",
                "You ticked “Also copy each backup off-site”, but there's no "
                "location. Enter another disk, a network share, or a cloud "
                "remote (remote:path) — or untick it to keep backups on this "
                "disk only.",
            )
            return

        # Optional Discord section: only validate its fields when it's ticked,
        # so junk left in an unused section can't block setup.
        setup_discord = self._discord_group.isChecked()
        discord_token = self.dc_token.text().strip()
        channel_id = admin_id = 0
        if setup_discord:
            if not discord_token:
                QMessageBox.warning(
                    self, "Discord bot token needed",
                    "You ticked “Set up the Discord bot”, but there's no bot "
                    "token. Paste the token from the Discord Developer Portal, "
                    "or untick that section to skip the bot for now.",
                )
                return
            try:
                channel_id = int(self.dc_channel.text().strip() or 0)
                admin_id = int(self.dc_role.text().strip() or 0)
            except ValueError:
                QMessageBox.warning(
                    self, "Invalid Discord ID",
                    "Channel ID and Admin role/user ID must be numbers (or "
                    "blank).\nIn Discord: Settings → Advanced → Developer Mode, "
                    "then right-click the channel / role / member → Copy ID.",
                )
                return

        plan = SetupPlan(
            server_root=self.server_root.text().strip(),
            steamcmd_path=self.steamcmd.text().strip(),
            api_port=self.port.value(),
            password=password,
            install_server=self.install_server.isChecked(),
            install_vcredist=self.install_vc.isChecked(),
            register_server_service=self.reg_server.isChecked(),
            daemon_startup=self._daemon_startup(),
            service_name=self._cfg.service_name or "PalServer",
            backup_root=self.backup_root.text().strip(),
            backup_hours=self.backup_hours.value(),
            backup_mirror_enabled=mirror_enabled,
            backup_mirror=backup_mirror,
            setup_discord=setup_discord,
            discord_token=discord_token,
            discord_channel_id=channel_id,
            discord_admin_id=admin_id,
        )
        # Kept so the completion dialog can describe what actually ran.
        self._plan = plan

        self.run_btn.setEnabled(False)
        self.check_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.progress.show()
        self.log.clear()

        self._worker = SetupWorker(self._cfg, plan)
        self._worker.line.connect(self._append)
        self._worker.done.connect(self._finished)
        self._worker.start()

    def _append(self, line: str) -> None:
        self.log.append(line)

    def _completion_message(self) -> str:
        """Describe only what actually ran. Claiming 'the server was started'
        when the user unticked service registration (or ran nothing in the
        background) is how the old dialog misled people onto a dead dashboard."""
        plan = getattr(self, "_plan", None)
        worker = getattr(self, "_worker", None)
        # What actually ran, not what was requested: a partial install can skip
        # server registration even when the box was ticked.
        started_server = bool(worker and getattr(worker, "server_registered", False))
        daemon_running = bool(plan and plan.daemon_startup != "none")

        if started_server:
            lead = (
                "palctl is configured and your server was started. The log below "
                "shows the address your friends connect to."
            )
        else:
            lead = (
                "palctl is configured. You chose not to register the server as a "
                "service, so start it yourself when you're ready."
            )

        if daemon_running:
            tail = "\n\nClick Finish to open the dashboard."
        else:
            tail = (
                "\n\nNothing is running in the background yet — the dashboard will "
                "say the daemon is down until you start palctl (re-run setup and "
                "pick a background option, or launch palctl-daemon)."
            )

        # Note the optional bits the user set up, so the finish line matches
        # what actually happened rather than only the core install.
        extras = []
        if plan and plan.setup_discord:
            extras.append(
                "The Discord bot is configured — it comes online a few seconds "
                "after palctl starts."
            )
        if plan and plan.backup_mirror_enabled and plan.backup_mirror:
            extras.append("Off-site backup copies are set up.")
        extra = ("\n\n" + " ".join(extras)) if extras else ""
        return lead + extra + tail

    def _finished(self, ok: bool) -> None:
        self.progress.hide()
        self.run_btn.setEnabled(True)
        self.check_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        if ok:
            # Give the wizard an unmistakable finish line: promote "Close" to the
            # primary "Finish" button so it doesn't just sit there after setup.
            self.close_btn.setText("Finish")
            self.close_btn.setDefault(True)
            self.close_btn.setFocus()
            QMessageBox.information(
                self, "Setup complete", self._completion_message(),
            )
        else:
            # Failure used to be silent — the wizard just sat there. Say so.
            QMessageBox.warning(
                self, "Setup didn't finish",
                "A step didn't complete — check the log for which one. Fix it and "
                "run setup again, or Close to finish the rest by hand.",
            )
