"""
palctl GUI.

A *view* onto the daemon. Close it and the server is still managed — the daemon
keeps polling, watching memory, running schedules, and driving the Discord bot.
That split is the whole point: a GUI alone only helps when you're sitting at the
server PC, which is exactly the situation you're trying to get out of.
"""

from __future__ import annotations

import sys
from collections import deque
from collections.abc import Callable
from pathlib import Path

import httpx
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    CONFIG_PATH,
    Config,
    get_admin_password,
    get_discord_token,
    set_admin_password,
    set_discord_token,
)
from ..daemon import DAEMON_PORT
from ..discovery import (
    detect_server_roots,
    detect_steamcmd,
    is_server_root,
    is_steamcmd,
)
from .settings_editor import SettingsEditor

DAEMON = f"http://127.0.0.1:{DAEMON_PORT}"


def call(path: str, body: dict | None = None) -> dict:
    with httpx.Client(timeout=10) as c:
        r = (
            c.post(f"{DAEMON}{path}", json=body or {})
            if body is not None or path.startswith("/action")
            else c.get(f"{DAEMON}{path}")
        )
        r.raise_for_status()
        return r.json()


class Poller(QThread):
    """Pull daemon state off the UI thread."""

    state = Signal(dict)
    failed = Signal(str)

    def run(self) -> None:
        while not self.isInterruptionRequested():
            try:
                self.state.emit(call("/state"))
            except Exception as e:
                self.failed.emit(str(e))
            self.msleep(2000)


class Sparkline(QLabel):
    """Tiny inline graph. No pyqtgraph dependency for something this simple."""

    def __init__(self, colour: str = "#47c8ff", height: int = 46) -> None:
        super().__init__()
        self._colour = colour
        self._h = height
        self.setMinimumHeight(height)
        self._points: deque[float] = deque(maxlen=180)

    def push(self, value: float) -> None:
        self._points.append(value)
        self._render()

    def _render(self) -> None:
        from PySide6.QtGui import QColor, QPainter, QPen

        w, h = max(self.width(), 120), self._h
        pm = QPixmap(w, h)
        pm.fill(QColor("#0d1117"))

        pts = list(self._points)
        if len(pts) > 1:
            lo, hi = min(pts), max(pts)
            span = (hi - lo) or 1.0

            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor(self._colour), 2))

            step = w / (len(pts) - 1)
            prev = None
            for i, v in enumerate(pts):
                x = i * step
                y = h - 4 - ((v - lo) / span) * (h - 8)
                if prev:
                    p.drawLine(int(prev[0]), int(prev[1]), int(x), int(y))
                prev = (x, y)
            p.end()

        self.setPixmap(pm)


class Dashboard(QWidget):
    def __init__(self) -> None:
        super().__init__()
        grid = QGridLayout(self)

        self.tiles: dict[str, QLabel] = {}
        for i, key in enumerate(
            ["Service", "Players", "Server FPS", "Frame time", "Memory", "CPU",
             "Uptime", "In-game day", "Base camps"]
        ):
            box = QGroupBox(key)
            v = QVBoxLayout(box)
            lab = QLabel("—")
            lab.setStyleSheet("font-size:22px;font-weight:600;")
            v.addWidget(lab)
            self.tiles[key] = lab
            grid.addWidget(box, i // 3, i % 3)

        fps_box = QGroupBox("Server FPS")
        fv = QVBoxLayout(fps_box)
        self.fps_spark = Sparkline("#3fb950")
        fv.addWidget(self.fps_spark)
        grid.addWidget(fps_box, 3, 0, 1, 3)

        mem_box = QGroupBox("Memory (MB) — watchdog watches this")
        mv = QVBoxLayout(mem_box)
        self.mem_spark = Sparkline("#d29922")
        mv.addWidget(self.mem_spark)
        grid.addWidget(mem_box, 4, 0, 1, 3)

        ev_box = QGroupBox("Events")
        ev = QVBoxLayout(ev_box)
        self.events = QListWidget()
        ev.addWidget(self.events)
        grid.addWidget(ev_box, 5, 0, 1, 3)

    def update_state(self, s: dict) -> None:
        m = s.get("metrics") or {}
        p = s.get("process") or {}

        self.tiles["Service"].setText(s.get("service", "—"))
        self.tiles["Players"].setText(
            f"{m.get('current_players', 0)}/{m.get('max_players', 0)}" if m else "—"
        )
        self.tiles["Server FPS"].setText(str(m.get("server_fps", "—")))
        self.tiles["Frame time"].setText(
            f"{m.get('server_frame_time', 0):.1f} ms" if m else "—"
        )
        self.tiles["Memory"].setText(f"{p.get('memory_mb', 0):,.0f} MB" if p else "—")
        self.tiles["CPU"].setText(f"{p.get('cpu_percent', 0):.0f}%" if p else "—")
        up = m.get("uptime", 0)
        self.tiles["Uptime"].setText(f"{up // 3600}h {(up % 3600) // 60}m" if m else "—")
        self.tiles["In-game day"].setText(str(m.get("days", "—")))
        self.tiles["Base camps"].setText(str(m.get("base_camps", "—")))

        hist = s.get("history") or []
        if hist:
            last = hist[-1]
            self.fps_spark.push(float(last.get("fps", 0)))
            self.mem_spark.push(float(last.get("memory_mb", 0)))

        self.events.clear()
        for e in reversed(s.get("events", [])):
            self.events.addItem(f"[{e['kind']}] {e['message']}")


class Players(QWidget):
    def __init__(self) -> None:
        super().__init__()
        v = QVBoxLayout(self)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Level", "Ping", "Buildings", "Location", "User ID"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table)

        row = QHBoxLayout()
        for label, action in (("Kick", "kick"), ("Ban", "ban")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, a=action: self._moderate(a))
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        self._players: list[dict] = []

    def update_state(self, s: dict) -> None:
        self._players = s.get("players", [])
        self.table.setRowCount(len(self._players))
        for r, p in enumerate(self._players):
            cells = [
                p["name"],
                str(p["level"]),
                f"{p['ping']:.0f} ms",
                str(p["building_count"]),
                f"{p['location_x']:.0f}, {p['location_y']:.0f}",
                p["user_id"],
            ]
            for c, text in enumerate(cells):
                self.table.setItem(r, c, QTableWidgetItem(text))

    def _moderate(self, action: str) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._players):
            return
        p = self._players[row]

        reason, ok = QInputDialog.getText(
            self, f"{action.title()} {p['name']}", "Reason shown to the player:"
        )
        if not ok:
            return
        try:
            call(f"/action/{action}", {"user_id": p["user_id"], "reason": reason})
        except Exception as e:
            QMessageBox.critical(self, "Failed", str(e))


class Console(QWidget):
    def __init__(self) -> None:
        super().__init__()
        v = QVBoxLayout(self)

        row = QHBoxLayout()
        self.msg = QLineEdit(placeholderText="Announcement (spaces work — REST, not RCON)")
        row.addWidget(self.msg, 1)
        b = QPushButton("Announce")
        b.clicked.connect(self._announce)
        row.addWidget(b)
        v.addLayout(row)

        row2 = QHBoxLayout()
        for label, action, confirm in (
            ("Save world", "save", False),
            ("Backup now", "backup", False),
            ("Start", "start", False),
            ("Stop", "stop", True),
            ("Restart (countdown)", "restart", True),
            ("Update (SteamCMD)", "update-server", True),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(
                lambda _=False, a=action, c=confirm, text=label: self._act(a, c, text)
            )
            row2.addWidget(btn)
        v.addLayout(row2)

        self.log = QTextEdit(readOnly=True)
        v.addWidget(self.log, 1)

    def _announce(self) -> None:
        text = self.msg.text().strip()
        if not text:
            return
        try:
            call("/action/announce", {"message": text})
            self.log.append(f"📣 {text}")
            self.msg.clear()
        except Exception as e:
            self.log.append(f"❌ {e}")

    def _act(self, action: str, confirm: bool, label: str) -> None:
        if confirm:
            ok = QMessageBox.question(self, label, f"{label}?")
            if ok != QMessageBox.StandardButton.Yes:
                return
        try:
            call(f"/action/{action}", {})
            self.log.append(f"→ {label}")
        except Exception as e:
            self.log.append(f"❌ {e}")


class PathPicker(QWidget):
    """
    A path field with Browse, Auto-detect, and a live ✓/✗ validity check.

    The old Config tab was four blank text boxes, and getting any of them wrong
    quietly pointed every backup, restore, and update at the wrong folder. Browse
    kills the typos, Auto-detect kills the guessing, and the tick tells you —
    before you hit Save — whether the path is really a server (or a real
    steamcmd.exe), instead of finding out when a command silently does nothing.
    """

    def __init__(
        self,
        value: str,
        *,
        pick_file: bool,
        detect: Callable[[], list[Path]] | None = None,
        validate: Callable[[Path], bool] | None = None,
        file_filter: str = "All files (*.*)",
    ) -> None:
        super().__init__()
        self._pick_file = pick_file
        self._detect = detect
        self._validate = validate
        self._filter = file_filter

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(value)
        row.addWidget(self.edit, 1)

        self._status = QLabel()
        if validate is not None:
            row.addWidget(self._status)
            self.edit.textChanged.connect(self._revalidate)

        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)

        if detect is not None:
            auto = QPushButton("Auto-detect")
            auto.clicked.connect(self._auto)
            row.addWidget(auto)

        self._revalidate()

    def text(self) -> str:
        return self.edit.text()

    def _browse(self) -> None:
        current = self.edit.text()
        if self._pick_file:
            path, _ = QFileDialog.getOpenFileName(self, "Select file", current, self._filter)
        else:
            path = QFileDialog.getExistingDirectory(self, "Select folder", current)
        if path:
            self.edit.setText(path)

    def _auto(self) -> None:
        found = self._detect() if self._detect else []
        if found:
            self.edit.setText(str(found[0]))
        else:
            self._status.setText("none found")
            self._status.setStyleSheet("color:#d29922;")

    def _revalidate(self, *_: object) -> None:
        if self._validate is None:
            return
        text = self.edit.text().strip()
        ok = bool(text) and self._validate(Path(text))
        self._status.setText("✓" if ok else "✗")
        self._status.setStyleSheet("color:#3fb950;" if ok else "color:#f85149;")


class ConfigTab(QWidget):
    """Paths, watchdog thresholds, schedules, and secrets. All entered here."""

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self._cfg = cfg
        v = QVBoxLayout(self)

        paths = QGroupBox("Paths")
        pf = QFormLayout(paths)
        self.server_root = PathPicker(
            cfg.server_root,
            pick_file=False,
            detect=detect_server_roots,
            validate=is_server_root,
        )
        self.steamcmd = PathPicker(
            cfg.steamcmd_path,
            pick_file=True,
            detect=detect_steamcmd,
            validate=is_steamcmd,
            file_filter="steamcmd.exe (steamcmd.exe);;All files (*.*)",
        )
        self.backup_root = PathPicker(cfg.backup_root, pick_file=False)
        self.service = QLineEdit(cfg.service_name)
        pf.addRow("Server root", self.server_root)
        pf.addRow("steamcmd.exe", self.steamcmd)
        pf.addRow("Backup folder", self.backup_root)
        pf.addRow("Service name", self.service)
        v.addWidget(paths)

        api = QGroupBox("REST API")
        af = QFormLayout(api)
        self.api_port = QSpinBox()
        self.api_port.setRange(1, 65535)
        self.api_port.setValue(cfg.api_port)
        self.admin_pw = QLineEdit(get_admin_password())
        self.admin_pw.setEchoMode(QLineEdit.EchoMode.Password)
        af.addRow("Port", self.api_port)
        af.addRow("Admin password", self.admin_pw)
        af.addRow(
            QLabel(
                "Must match AdminPassword in PalWorldSettings.ini, and "
                "RESTAPIEnabled must be True.\nStored in Windows Credential "
                "Manager (DPAPI), never in a config file."
            )
        )
        v.addWidget(api)

        wd = QGroupBox("Memory-leak watchdog")
        wf = QFormLayout(wd)
        self.wd_enabled = QCheckBox(checked=cfg.watchdog.enabled)
        self.wd_limit = QSpinBox()
        self.wd_limit.setRange(1000, 64000)
        self.wd_limit.setSuffix(" MB")
        self.wd_limit.setValue(cfg.watchdog.memory_limit_mb)
        self.wd_hard = QSpinBox()
        self.wd_hard.setRange(1000, 64000)
        self.wd_hard.setSuffix(" MB")
        self.wd_hard.setValue(cfg.watchdog.hard_limit_mb)
        self.wd_skip = QCheckBox(checked=cfg.watchdog.skip_if_players_online)
        wf.addRow("Enabled", self.wd_enabled)
        wf.addRow("Restart above", self.wd_limit)
        wf.addRow("Force even with players above", self.wd_hard)
        wf.addRow("Hold off while players online", self.wd_skip)
        v.addWidget(wd)

        sch = QGroupBox("Schedule")
        sf = QFormLayout(sch)
        self.sch_enabled = QCheckBox(checked=cfg.schedule.enabled)
        self.sch_restart = QCheckBox(checked=cfg.schedule.daily_restart)
        self.sch_time = QTimeEdit()
        from PySide6.QtCore import QTime

        hh, _, mm = cfg.schedule.daily_restart_at.partition(":")
        self.sch_time.setTime(QTime(int(hh), int(mm or 0)))
        self.sch_backup = QSpinBox()
        self.sch_backup.setRange(1, 48)
        self.sch_backup.setSuffix(" h")
        self.sch_backup.setValue(cfg.schedule.backup_hours)
        sf.addRow("Enabled", self.sch_enabled)
        sf.addRow("Daily restart", self.sch_restart)
        sf.addRow("At", self.sch_time)
        sf.addRow("Backup every", self.sch_backup)
        v.addWidget(sch)

        dc = QGroupBox("Discord bot")
        df = QFormLayout(dc)
        self.dc_enabled = QCheckBox(checked=cfg.discord.enabled)
        self.dc_token = QLineEdit(get_discord_token())
        self.dc_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.dc_channel = QLineEdit(str(cfg.discord.channel_id or ""))
        self.dc_role = QLineEdit(str(cfg.discord.admin_role_id or ""))
        df.addRow("Enabled", self.dc_enabled)
        df.addRow("Bot token", self.dc_token)
        df.addRow("Channel ID", self.dc_channel)
        df.addRow("Admin role ID", self.dc_role)
        df.addRow(
            QLabel(
                "Token is stored encrypted in Windows Credential Manager.\n"
                "Note: no chat relay — Palworld exposes no chat-read endpoint."
            )
        )
        v.addWidget(dc)

        save = QPushButton("Save config && reload daemon")
        save.clicked.connect(self._save)
        v.addWidget(save)
        v.addStretch(1)

    def _save(self) -> None:
        c = self._cfg
        c.server_root = self.server_root.text()
        c.steamcmd_path = self.steamcmd.text()
        c.backup_root = self.backup_root.text()
        c.service_name = self.service.text()
        c.api_port = self.api_port.value()

        c.watchdog.enabled = self.wd_enabled.isChecked()
        c.watchdog.memory_limit_mb = self.wd_limit.value()
        c.watchdog.hard_limit_mb = self.wd_hard.value()
        c.watchdog.skip_if_players_online = self.wd_skip.isChecked()

        c.schedule.enabled = self.sch_enabled.isChecked()
        c.schedule.daily_restart = self.sch_restart.isChecked()
        c.schedule.daily_restart_at = self.sch_time.time().toString("HH:mm")
        c.schedule.backup_hours = self.sch_backup.value()

        c.discord.enabled = self.dc_enabled.isChecked()
        try:
            c.discord.channel_id = int(self.dc_channel.text().strip() or 0)
            c.discord.admin_role_id = int(self.dc_role.text().strip() or 0)
        except ValueError:
            QMessageBox.warning(
                self, "Invalid ID",
                "Channel ID and Admin role ID must be numbers.\n"
                "In Discord: Settings → Advanced → Developer Mode, then "
                "right-click the channel/role → Copy ID.",
            )
            return

        c.save()
        set_admin_password(self.admin_pw.text())
        set_discord_token(self.dc_token.text())

        try:
            call("/action/reload-config", {})
            QMessageBox.information(
                self, "Saved",
                "Config saved and daemon reloaded.\n\n"
                "Discord bot changes need a daemon restart to take effect.",
            )
        except Exception:
            QMessageBox.information(
                self, "Saved",
                "Config saved. The daemon isn't running — start it to apply.",
            )


class Main(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config.load()
        self.setWindowTitle("palctl — Palworld server control")
        self.resize(1000, 780)

        tabs = QTabWidget()
        self.dash = Dashboard()
        self.players = Players()
        self.console = Console()
        self.editor = SettingsEditor(self.cfg.live_ini, self.cfg.default_ini)
        self.config = ConfigTab(self.cfg)

        tabs.addTab(self.dash, "Dashboard")
        tabs.addTab(self.players, "Players")
        tabs.addTab(self.console, "Console")
        tabs.addTab(self.editor, "Settings")
        tabs.addTab(self.config, "Config")
        self.setCentralWidget(tabs)

        self.status = self.statusBar()
        self.status.showMessage("Connecting to daemon…")

        self.poller = Poller()
        self.poller.state.connect(self._on_state)
        self.poller.failed.connect(self._on_failed)
        self.poller.start()

        self._tray()

        # First launch (no config yet): walk the user through setup rather than
        # dropping them onto a Config tab full of blank boxes. Deferred so the
        # main window is up before the dialog appears.
        if not CONFIG_PATH.exists():
            QTimer.singleShot(300, lambda: self._open_wizard(first_run=True))

    def _open_wizard(self, *, first_run: bool = False) -> None:
        from .wizard import SetupWizard

        SetupWizard(self.cfg, self, first_run=first_run).exec()
        # Paths / password may have changed; reload and push it to the daemon.
        self.cfg = Config.load()
        try:
            call("/action/reload-config", {})
        except Exception:
            pass

    def _on_state(self, s: dict) -> None:
        self.dash.update_state(s)
        self.players.update_state(s)
        svc = s.get("service", "?")
        n = len(s.get("players", []))
        self.status.showMessage(f"Daemon OK · service {svc} · {n} online")

    def _on_failed(self, err: str) -> None:
        self.status.showMessage(
            f"Daemon unreachable ({err}). Start it: python -m palctl.daemon"
        )

    def _tray(self) -> None:
        icon = QIcon.fromTheme("applications-games")
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        show = QAction("Show", self)
        show.triggered.connect(self.showNormal)
        setup = QAction("Setup wizard…", self)
        setup.triggered.connect(lambda: self._open_wizard(first_run=False))
        quit_ = QAction("Quit GUI (daemon keeps running)", self)
        quit_.triggered.connect(QApplication.quit)
        menu.addAction(show)
        menu.addAction(setup)
        menu.addAction(quit_)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("palctl")
        self.tray.show()

    def closeEvent(self, event) -> None:
        # Minimise to tray. The daemon is a separate process; closing the GUI
        # never stops the server being managed.
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "palctl", "Still running in the tray. The daemon is unaffected."
        )


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = Main()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
