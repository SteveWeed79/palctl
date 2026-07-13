"""
First-run setup wizard.

Everything palctl needs to actually start managing a server used to be a wall of
manual steps spread across the README: find four paths, hand-edit three ini
keys, run two sets of `nssm install` lines in an admin terminal. This does all of
it from one dialog — detect the paths, turn the REST API on, optionally install
the server with SteamCMD, and register both Windows services — while streaming a
live log so nothing happens behind your back.

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
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    Config,
    config_dir,
    set_admin_password,
)
from ..discovery import detect_server_roots, detect_steamcmd, is_server_root, is_steamcmd
from .main import PathPicker

# Widely-recommended launch flags for the Palworld dedicated server.
PALSERVER_ARGS = "-useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS"


@dataclass
class SetupPlan:
    server_root: str
    steamcmd_path: str
    api_port: int
    password: str
    install_server: bool
    register_server_service: bool
    register_daemon_service: bool
    service_name: str


class SetupWorker(QThread):
    """Runs the (slow, networked) setup steps off the UI thread."""

    line = Signal(str)
    done = Signal(bool)

    def __init__(self, cfg: Config, plan: SetupPlan) -> None:
        super().__init__()
        self._cfg = cfg
        self._plan = plan

    def _log(self, msg: str) -> None:
        self.line.emit(msg)

    def run(self) -> None:
        plan = self._plan
        cfg = self._cfg
        try:
            self._log("Saving configuration…")
            cfg.server_root = plan.server_root
            cfg.steamcmd_path = plan.steamcmd_path
            cfg.api_port = plan.api_port
            cfg.service_name = plan.service_name
            cfg.save()
            set_admin_password(plan.password)

            if plan.install_server:
                self._install_server(cfg, plan)

            self._log("Enabling the REST API in PalWorldSettings.ini…")
            from ..serversetup import ensure_rest_api

            ensure_rest_api(
                cfg.live_ini, cfg.default_ini,
                port=plan.api_port, password=plan.password,
            )
            self._log("  REST API enabled, port and admin password set.")

            if plan.register_server_service:
                self._register_server_service(cfg, plan)
            if plan.register_daemon_service:
                self._register_daemon_service()

            self._log("\n✅ Setup complete.")
            self.done.emit(True)
        except Exception as e:
            self._log(f"\n❌ Setup failed: {e}")
            self.done.emit(False)

    def _install_server(self, cfg: Config, plan: SetupPlan) -> None:
        from .. import steamcmd

        steam = Path(plan.steamcmd_path)
        if not is_steamcmd(steam):
            target_dir = steam.parent if plan.steamcmd_path else config_dir() / "steamcmd"
            self._log(f"Downloading SteamCMD into {target_dir}…")
            steam = steamcmd.download_steamcmd(target_dir)
            cfg.steamcmd_path = str(steam)
            cfg.save()
            self._log(f"  SteamCMD ready at {steam}")

        self._log(
            f"Installing / updating the Palworld server into {plan.server_root} "
            "(this downloads a few GB the first time)…"
        )
        code = steamcmd.run_update(
            steam, plan.server_root, app_id=cfg.app_id, on_line=self._log
        )
        self._log(f"  SteamCMD finished (exit {code}).")

    def _register_server_service(self, cfg: Config, plan: SetupPlan) -> None:
        from .. import winservice

        exe = Path(plan.server_root) / "PalServer.exe"
        if not exe.exists():
            self._log(f"  ⚠️ {exe} not found — skipping the server service.")
            return
        self._log(f"Registering the '{plan.service_name}' Windows service…")
        nssm = winservice.ensure_nssm(config_dir() / "bin")
        winservice.install_service(
            nssm, plan.service_name, exe, PALSERVER_ARGS, plan.server_root, start=False
        )
        self._log(f"  Service '{plan.service_name}' registered.")

    def _register_daemon_service(self) -> None:
        from .. import daemon

        self._log(f"Registering the '{daemon.SERVICE_NAME}' Windows service…")
        daemon.install_service()
        self._log(f"  Service '{daemon.SERVICE_NAME}' registered and started.")


class SetupWizard(QDialog):
    def __init__(
        self, cfg: Config, parent: QWidget | None = None, *, first_run: bool = False
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._worker: SetupWorker | None = None
        self.setWindowTitle("palctl setup")
        self.resize(760, 620)

        root = QVBoxLayout(self)

        intro = QLabel(
            "Let's get palctl talking to your server. It'll auto-detect what it "
            "can — fix anything with a red ✗, then run setup.\n\n"
            "The Palworld dedicated server itself comes from Steam. If it isn't "
            "installed yet, tick “Install / update the server” and this will fetch "
            "it with SteamCMD for you."
            if first_run
            else "Re-run any part of setup. Detected paths are pre-filled."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

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
        root.addWidget(paths)

        api = QGroupBox("REST API")
        af = QFormLayout(api)
        self.port = QSpinBox()
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
        root.addWidget(api)

        steps = QGroupBox("Do now")
        sf = QVBoxLayout(steps)
        self.install_server = QCheckBox(
            "Install / update the server with SteamCMD (needed if it isn't installed)"
        )
        self.reg_server = QCheckBox("Register the Palworld server as a Windows service")
        self.reg_server.setChecked(True)
        self.reg_daemon = QCheckBox(
            "Register the palctl daemon as a Windows service (keeps it always running)"
        )
        self.reg_daemon.setChecked(True)
        for cb in (self.install_server, self.reg_server, self.reg_daemon):
            sf.addWidget(cb)
        root.addWidget(steps)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate; hidden until running
        self.progress.hide()
        root.addWidget(self.progress)

        self.log = QTextEdit(readOnly=True)
        self.log.setPlaceholderText("Setup progress will appear here…")
        root.addWidget(self.log, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        self.run_btn = QPushButton("Run setup")
        self.run_btn.clicked.connect(self._run)
        buttons.addWidget(self.close_btn)
        buttons.addWidget(self.run_btn)
        root.addLayout(buttons)

    def _generate_password(self) -> None:
        import secrets

        self.password.setText(secrets.token_urlsafe(12))
        self.password.setEchoMode(QLineEdit.EchoMode.Normal)

    def _run(self) -> None:
        password = self.password.text().strip()
        if not password:
            QMessageBox.warning(
                self, "Admin password needed",
                "Set an admin password — it's what secures the REST API. "
                "Use Generate if you don't care what it is.",
            )
            return

        plan = SetupPlan(
            server_root=self.server_root.text().strip(),
            steamcmd_path=self.steamcmd.text().strip(),
            api_port=self.port.value(),
            password=password,
            install_server=self.install_server.isChecked(),
            register_server_service=self.reg_server.isChecked(),
            register_daemon_service=self.reg_daemon.isChecked(),
            service_name=self._cfg.service_name or "PalServer",
        )

        self.run_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.progress.show()
        self.log.clear()

        self._worker = SetupWorker(self._cfg, plan)
        self._worker.line.connect(self._append)
        self._worker.done.connect(self._finished)
        self._worker.start()

    def _append(self, line: str) -> None:
        self.log.append(line)

    def _finished(self, ok: bool) -> None:
        self.progress.hide()
        self.run_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        if ok:
            QMessageBox.information(
                self, "Setup complete",
                "palctl is configured. Start the server from the Console tab (or "
                "it'll come up with the service), then watch the Dashboard.",
            )
