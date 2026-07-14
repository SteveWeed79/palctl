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
from PySide6.QtCore import QEvent, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QPixmap
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
    QScrollArea,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
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
from ..localauth import TOKEN_HEADER, get_or_create_token
from . import icons
from .settings_editor import SettingsEditor
from .widgets import NoScrollSpinBox, NoScrollTimeEdit

DAEMON = f"http://127.0.0.1:{DAEMON_PORT}"

_token_cache: str | None = None


def _auth_headers() -> dict:
    global _token_cache
    if _token_cache is None:
        _token_cache = get_or_create_token()
    return {TOKEN_HEADER: _token_cache}


class DaemonError(RuntimeError):
    """A daemon call didn't succeed — always shown to the user as its own
    message, never a raw socket error or a bare HTTP status line."""


class DaemonDown(DaemonError):
    """Specifically: the daemon isn't reachable (connection refused / 401). The
    GUI is only a view onto the daemon, so 'it's not running' is the answer."""


def call(path: str, body: dict | None = None) -> dict:
    headers = _auth_headers()
    try:
        with httpx.Client(timeout=10) as c:
            r = (
                c.post(f"{DAEMON}{path}", json=body or {}, headers=headers)
                if body is not None or path.startswith("/action")
                else c.get(f"{DAEMON}{path}", headers=headers)
            )
    except httpx.RequestError as e:
        # Connection refused etc. — nothing is listening on the daemon port.
        raise DaemonDown(
            "Can't reach the palctl daemon — it runs in the background and this "
            "window only talks to it. If you haven't set palctl up yet, open "
            "Setup → Run setup wizard. Otherwise start palctl in the background "
            "(the wizard's background option, or the palctl-daemon service), "
            "then try again."
        ) from e
    if r.status_code == 401:
        raise DaemonDown(
            "The daemon rejected the token. The GUI and the daemon must run as "
            "the same Windows user; if the daemon runs as a service, re-register "
            "it with:  palctl-daemon install-service --as-user"
        )
    if r.status_code >= 400:
        # The daemon puts a human reason in {"error": ...} — e.g. a 409
        # "busy: <operation> is in progress" from the server-op lock. Surface
        # THAT, not httpx's generic "Client error '409 Conflict'".
        try:
            reason = r.json().get("error")
        except Exception:
            reason = None
        raise DaemonError(reason or f"The daemon returned HTTP {r.status_code}.")
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
        from PySide6.QtGui import QColor, QPainter, QPalette, QPen
        from PySide6.QtWidgets import QApplication

        w, h = max(self.width(), 120), self._h
        pm = QPixmap(w, h)
        # Follow the theme instead of a hardcoded dark fill, so the graph isn't a
        # black rectangle on a light-themed Windows.
        pm.fill(QApplication.palette().color(QPalette.ColorRole.Base))

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

        fps_box = QGroupBox("Server FPS — recent trend")
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
            when = (e.get("at") or "")[11:19]  # HH:MM:SS from the ISO timestamp
            prefix = f"{when}  " if when else ""
            self.events.addItem(f"{prefix}[{e['kind']}] {e['message']}")


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
        self._icon_buttons: list[tuple[QPushButton, str]] = []
        for label, action in (("Kick", "kick"), ("Ban", "ban")):
            b = QPushButton(icons.load_icon(f"action-{action}"), label)
            b.clicked.connect(lambda _=False, a=action: self._moderate(a))
            row.addWidget(b)
            self._icon_buttons.append((b, f"action-{action}"))
        row.addStretch(1)
        v.addLayout(row)

        self._players: list[dict] = []

    def retint(self) -> None:
        for btn, name in self._icon_buttons:
            btn.setIcon(icons.load_icon(name))

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
            QMessageBox.information(
                self, f"{action.title()} player",
                f"Select a player in the table first, then click {action.title()}.",
            )
            return
        p = self._players[row]

        # Ban persists — confirm it, like the Console does for Stop/Restart.
        # A kick just drops the player and needs no gate.
        if action == "ban":
            ok = QMessageBox.question(
                self, f"Ban {p['name']}?",
                f"Ban {p['name']}? They won't be able to rejoin until you unban them.",
            )
            if ok != QMessageBox.StandardButton.Yes:
                return

        reason, ok = QInputDialog.getText(
            self, f"{action.title()} {p['name']}", "Reason shown to the player:"
        )
        if not ok:
            return
        try:
            call(f"/action/{action}", {"user_id": p["user_id"], "reason": reason})
            QMessageBox.information(self, "Done", f"{action.title()}ed {p['name']}.")
        except Exception as e:
            QMessageBox.critical(self, "Failed", str(e))


class Console(QWidget):
    def __init__(self) -> None:
        super().__init__()
        v = QVBoxLayout(self)

        row = QHBoxLayout()
        self._icon_buttons: list[tuple[QPushButton, str]] = []
        self.msg = QLineEdit(placeholderText="Announcement (spaces work — REST, not RCON)")
        row.addWidget(self.msg, 1)
        b = QPushButton(icons.load_icon("action-announce"), "Announce")
        b.clicked.connect(self._announce)
        row.addWidget(b)
        self._icon_buttons.append((b, "action-announce"))
        v.addLayout(row)

        row2 = QGridLayout()
        # `confirm` is the consequence spelled out for operations that take the
        # server down, or None for the safe ones; `icon` names the glyph where it
        # doesn't match the action (update-server -> action-update).
        _down = "This takes the server down"
        for i, (label, action, confirm, icon) in enumerate((
            ("Save world", "save", None, "action-save"),
            ("Backup now", "backup", None, "action-backup"),
            ("Start", "start", None, "action-start"),
            ("Stop", "stop",
             f"Stop the server? {_down} and disconnects everyone. It stays down "
             "until you Start it.", "action-stop"),
            ("Restart (countdown)", "restart",
             "Restart the server? Players get an in-game countdown first, then it "
             "goes down and comes back.", "action-restart"),
            ("Update (SteamCMD)", "update-server",
             f"Update the server via SteamCMD? {_down} for several minutes while "
             "it downloads. The world is backed up first.", "action-update"),
        )):
            btn = QPushButton(icons.load_icon(icon), label)
            btn.clicked.connect(
                lambda _=False, a=action, c=confirm, text=label: self._act(a, c, text)
            )
            row2.addWidget(btn, i // 3, i % 3)
            self._icon_buttons.append((btn, icon))
        v.addLayout(row2)

        restore_btn = QPushButton(icons.load_icon("action-restore"), "Restore backup…")
        restore_btn.clicked.connect(self._restore)
        v.addWidget(restore_btn)
        self._icon_buttons.append((restore_btn, "action-restore"))

        self.log = QTextEdit(readOnly=True)
        v.addWidget(self.log, 1)

    def retint(self) -> None:
        for btn, name in self._icon_buttons:
            btn.setIcon(icons.load_icon(name))

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

    def _act(self, action: str, confirm: str | None, label: str) -> None:
        if confirm:
            ok = QMessageBox.question(self, label, confirm)
            if ok != QMessageBox.StandardButton.Yes:
                return
        try:
            call(f"/action/{action}", {})
            self.log.append(f"→ {label}")
        except Exception as e:
            self.log.append(f"❌ {e}")

    def _restore(self) -> None:
        try:
            backups = call("/backups")  # GET → [{name, size_mb}, ...]
        except Exception as e:
            self.log.append(f"❌ couldn't list backups: {e}")
            return
        if not backups:
            QMessageBox.information(self, "No backups", "No backups found yet.")
            return

        names = [b["name"] for b in backups]
        name, ok = QInputDialog.getItem(
            self, "Restore backup",
            "Pick a backup to restore (the server will restart):",
            names, 0, False,
        )
        if not ok or not name:
            return
        confirm = QMessageBox.question(
            self, "Restore?",
            f"Restore '{name}'?\n\nThis overwrites the current world and restarts "
            "the server. A safety copy of the current world is taken first.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            call("/action/restore", {"name": name})
            self.log.append(f"→ restore {name}")
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
        # Everything lives inside a scroll area so the tab doesn't force a huge
        # minimum window size (the "rigid, won't resize" complaint) and scrolls
        # cleanly on small screens.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(widgetResizable=True)
        outer.addWidget(scroll)
        inner = QWidget()
        scroll.setWidget(inner)
        v = QVBoxLayout(inner)

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
        self.api_port = NoScrollSpinBox()
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
        self.wd_limit = NoScrollSpinBox()
        self.wd_limit.setRange(1000, 64000)
        self.wd_limit.setSuffix(" MB")
        self.wd_limit.setValue(cfg.watchdog.memory_limit_mb)
        self.wd_hard = NoScrollSpinBox()
        self.wd_hard.setRange(1000, 64000)
        self.wd_hard.setSuffix(" MB")
        self.wd_hard.setValue(cfg.watchdog.hard_limit_mb)
        self.wd_skip = QCheckBox(checked=cfg.watchdog.skip_if_players_online)
        self.wd_autorec = QCheckBox(checked=cfg.watchdog.auto_restart_on_crash)
        wf.addRow("Enabled", self.wd_enabled)
        wf.addRow("Restart above", self.wd_limit)
        wf.addRow("Force even with players above", self.wd_hard)
        wf.addRow("Hold off while players online", self.wd_skip)
        wf.addRow("Auto-restart on crash / hang", self.wd_autorec)
        v.addWidget(wd)

        sch = QGroupBox("Schedule")
        sf = QFormLayout(sch)
        self.sch_enabled = QCheckBox(checked=cfg.schedule.enabled)
        self.sch_restart = QCheckBox(checked=cfg.schedule.daily_restart)
        self.sch_time = NoScrollTimeEdit()
        from PySide6.QtCore import QTime

        hh, _, mm = cfg.schedule.daily_restart_at.partition(":")
        self.sch_time.setTime(QTime(int(hh), int(mm or 0)))
        self.sch_backup = NoScrollSpinBox()
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
        # A clickable path to the token, so nobody has to hunt for the portal.
        # Discord tokens aren't scope-parameterised like a GitHub PAT URL, so we
        # link the page and spell out the two scopes the invite needs.
        token_help = QLabel(
            'No token yet? <a href="https://discord.com/developers/applications">'
            "Open the Discord Developer Portal</a> → <b>New Application</b> → "
            "<b>Bot</b> → <b>Reset Token</b> → Copy it here. Then invite the bot "
            "to your server with the <b>bot</b> and <b>applications.commands</b> "
            "scopes."
        )
        token_help.setOpenExternalLinks(True)
        token_help.setWordWrap(True)
        df.addRow(token_help)
        df.addRow("Channel ID", self.dc_channel)
        df.addRow("Admin role ID", self.dc_role)
        df.addRow(
            QLabel(
                "Token is stored encrypted in Windows Credential Manager.\n"
                "Note: no chat relay — Palworld exposes no chat-read endpoint."
            )
        )
        v.addWidget(dc)

        save = QPushButton(icons.load_icon("action-save"), "Save config && reload daemon")
        save.clicked.connect(self._save)
        v.addWidget(save)

        diag = QPushButton(
            icons.load_icon("export-diagnostics"),
            "Export diagnostics (logs + config) for a bug report…",
        )
        diag.clicked.connect(self._export_diagnostics)
        v.addWidget(diag)
        v.addStretch(1)
        self._icon_buttons: list[tuple[QPushButton, str]] = [
            (save, "action-save"),
            (diag, "export-diagnostics"),
        ]

    def retint(self) -> None:
        for btn, name in self._icon_buttons:
            btn.setIcon(icons.load_icon(name))

    def _export_diagnostics(self) -> None:
        from ..diagnostics import build_bundle

        default = str(Path.home() / "palctl-diagnostics.zip")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save diagnostics", default, "Zip archive (*.zip)"
        )
        if not path:
            return
        try:
            build_bundle(Path(path))
            QMessageBox.information(
                self, "Diagnostics saved",
                f"Saved to:\n{path}\n\nAttach this to a bug report. It contains "
                "your logs and config — but no passwords (those never leave "
                "Windows Credential Manager).",
            )
        except Exception as e:
            QMessageBox.critical(self, "Failed", str(e))

    def _save(self) -> None:
        c = self._cfg
        c.server_root = self.server_root.text()
        c.steamcmd_path = self.steamcmd.text()
        c.backup_root = self.backup_root.text()
        c.service_name = self.service.text()
        c.api_port = self.api_port.value()

        if self.wd_hard.value() < self.wd_limit.value():
            QMessageBox.warning(
                self, "Check the memory limits",
                "The hard limit (force a restart even with players online) is "
                "below the restart limit. It should be the higher of the two — "
                "raise it above the restart limit.",
            )
            return

        c.watchdog.enabled = self.wd_enabled.isChecked()
        c.watchdog.memory_limit_mb = self.wd_limit.value()
        c.watchdog.hard_limit_mb = self.wd_hard.value()
        c.watchdog.skip_if_players_online = self.wd_skip.isChecked()
        c.watchdog.auto_restart_on_crash = self.wd_autorec.isChecked()

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
        self.setWindowIcon(icons.app_icon())
        self.resize(1000, 780)

        self.tabs = QTabWidget()
        self.dash = Dashboard()
        self.players = Players()
        self.console = Console()
        self.editor = SettingsEditor(self.cfg.live_ini, self.cfg.default_ini)
        self.config = ConfigTab(self.cfg)

        self._tab_icons = [
            "tab-dashboard", "tab-players", "tab-console", "tab-settings", "tab-config",
        ]
        for widget, icon, label in (
            (self.dash, "tab-dashboard", "Dashboard"),
            (self.players, "tab-players", "Players"),
            (self.console, "tab-console", "Console"),
            (self.editor, "tab-settings", "Settings"),
            (self.config, "tab-config", "Config"),
        ):
            self.tabs.addTab(widget, icons.load_icon(icon), label)
        self.setCentralWidget(self.tabs)

        # Setup wizard reachable from the window itself, not only the tray icon.
        # A reinstall keeps your %APPDATA% config, so the first-run auto-popup
        # won't fire on an upgrade — this is how you re-run setup on purpose
        # (e.g. to repoint the server service or re-enable the REST API).
        setup_menu = self.menuBar().addMenu("&Setup")
        self._wizard_action = QAction(icons.load_icon("wizard"), "Run setup wizard…", self)
        self._wizard_action.triggered.connect(lambda: self._open_wizard(first_run=False))
        setup_menu.addAction(self._wizard_action)

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

    def changeEvent(self, event) -> None:
        # Re-tint the palette-following glyph icons when the OS light/dark theme
        # flips — otherwise they'd keep the old tint until the app is relaunched.
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.ThemeChange,
        ):
            self._retint_icons()
        super().changeEvent(event)

    def _retint_icons(self) -> None:
        # Guard: changeEvent can fire during construction before these exist.
        if not hasattr(self, "tabs"):
            return
        for i, name in enumerate(self._tab_icons):
            self.tabs.setTabIcon(i, icons.load_icon(name))
        self._wizard_action.setIcon(icons.load_icon("wizard"))
        if hasattr(self, "_tray_setup_action"):
            self._tray_setup_action.setIcon(icons.load_icon("wizard"))
        for tab in (self.players, self.console, self.config):
            tab.retint()
        # Window/app icon (full-colour brand tile) and the tray icon (fixed
        # semantic colours) don't follow the palette, so they need no redo.

    def _on_state(self, s: dict) -> None:
        self.dash.update_state(s)
        self.players.update_state(s)
        svc = s.get("service", "?")
        n = len(s.get("players", []))
        self.status.showMessage(f"Daemon OK · service {svc} · {n} online")
        self._set_tray_state(*self._tray_state_for(s))

    def _tray_state_for(self, s: dict) -> tuple[str, str]:
        """Map daemon state to a tray (icon-state, tooltip). Idle/green only when
        the server is up and its REST API is answering; amber for anything
        transitional or not-yet-serving; the daemon itself being reachable means
        it's never 'error' here (that's reserved for _on_failed)."""
        svc = s.get("service", "UNKNOWN")
        n = len(s.get("players", []))
        if s.get("restarting") or svc in ("START_PENDING", "STOP_PENDING"):
            return "warning", f"palctl — server {svc.replace('_', ' ').lower()}"
        if svc == "RUNNING":
            if s.get("alive"):
                return "idle", f"palctl — server running · {n} online"
            return "warning", "palctl — server up, REST API not answering yet"
        return "warning", "palctl — server stopped"

    def _set_tray_state(self, state: str, tooltip: str) -> None:
        # Only touch the tray when the state actually changes — repainting the
        # icon every 2s poll is needless churn.
        if getattr(self, "_tray_state", None) == state:
            self.tray.setToolTip(tooltip)
            return
        self._tray_state = state
        self.tray.setIcon(icons.tray_icon(state))
        self.tray.setToolTip(tooltip)

    def _on_failed(self, err: str) -> None:
        # `err` is already a plain-English sentence for the common case (daemon
        # down / token mismatch); show it as-is rather than wrapping it again.
        self.status.showMessage(err)
        self._set_tray_state("error", "palctl — can't reach the daemon")

    def _tray(self) -> None:
        self._tray_state: str | None = None
        self.tray = QSystemTrayIcon(icons.tray_icon("idle"), self)
        self._tray_state = "idle"
        menu = QMenu()
        show = QAction("Show", self)
        show.triggered.connect(self.showNormal)
        setup = QAction(icons.load_icon("wizard"), "Setup wizard…", self)
        self._tray_setup_action = setup
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
