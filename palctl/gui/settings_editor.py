"""
The settings editor.

Turns Palworld's one-line 3,000-character OptionSettings blob into a searchable,
grouped form with real widgets and range validation — and writes it back without
losing keys it doesn't recognise.

Design rules learned from watching other tools eat people's configs:
  * unknown keys from future patches are PRESERVED, shown under "Other"
  * every save takes a timestamped .bak (SteamCMD validate wipes this file)
  * the server must be restarted for changes to apply — we say so, loudly
  * blank live ini is normal; offer to seed it from DefaultPalWorldSettings.ini
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..inifile import PalSettings, ValueKind, is_blank, seed_from_default

# Grouping is cosmetic but it's the difference between usable and a wall of 200
# fields. Anything unmatched falls into "Other settings" and stays editable.
GROUPS: dict[str, tuple[str, ...]] = {
    "Server": (
        "ServerName", "ServerDescription", "ServerPassword", "AdminPassword",
        "PublicPort", "PublicIP", "ServerPlayerMaxNum", "bIsUseBackupSaveData",
        "RESTAPIEnabled", "RESTAPIPort", "RCONEnabled", "RCONPort", "LogFormatType",
        "bShowPlayerList", "AllowConnectPlatform", "CrossplayPlatforms",
        "bIsMultiplay", "Region", "BanListURL",
    ),
    "Difficulty & rates": (
        "Difficulty", "DayTimeSpeedRate", "NightTimeSpeedRate", "ExpRate",
        "PalCaptureRate", "PalSpawnNumRate", "CollectionDropRate",
        "CollectionObjectHpRate", "CollectionObjectRespawnSpeedRate",
        "EnemyDropItemRate", "DeathPenalty", "bHardcore", "bPalLost",
        "bEnableNonLoginPenalty", "RandomizerType", "RandomizerSeed",
        "bIsRandomizerPalLevelRandom",
    ),
    "Combat": (
        "PalDamageRateAttack", "PalDamageRateDefense", "PlayerDamageRateAttack",
        "PlayerDamageRateDefense", "bEnablePlayerToPlayerDamage",
        "bEnableFriendlyFire", "bEnableInvaderEnemy", "bEnableAimAssistPad",
        "bEnableAimAssistKeyboard", "bAllowGlobalPalboxExport",
        "bAllowGlobalPalboxImport", "bEnablePlayerToPlayerDamageDrop",
    ),
    "Survival": (
        "PlayerStomachDecreaceRate", "PlayerStaminaDecreaceRate",
        "PlayerAutoHPRegeneRate", "PlayerAutoHpRegeneRateInSleep",
        "PalStomachDecreaceRate", "PalStaminaDecreaceRate", "PalAutoHPRegeneRate",
        "PalAutoHpRegeneRateInSleep",
    ),
    "Base & building": (
        "BuildObjectDamageRate", "BuildObjectDeteriorationDamageRate",
        "BaseCampMaxNum", "BaseCampWorkerMaxNum", "BaseCampMaxNumInGuild",
        "DropItemMaxNum", "DropItemMaxNum_UNKO", "DropItemAliveMaxHours",
        "bBuildAreaLimit", "bIsUseBackupSaveData",
    ),
    "Guild": (
        "GuildPlayerMaxNum", "bAutoResetGuildNoOnlinePlayers",
        "AutoResetGuildTimeNoOnlinePlayers",
    ),
    "Pals & eggs": (
        "PalEggDefaultHatchingTime", "bActiveUNKO", "bCanPickupOtherGuildDeathPenaltyDrop",
        "MaxBuildingLimitNum", "bEnablePredatorBossPal",
    ),
    "Performance": (
        "ServerReplicatePawnCullDistance", "bIsUseBackupSaveData",
        "WorkSpeedRate", "bUseAuth", "SupplyDropSpan",
    ),
}

# Sane ranges — reject obviously broken values before they hit the game.
RANGES: dict[str, tuple[float, float]] = {
    "ExpRate": (0.1, 20.0),
    "PalCaptureRate": (0.5, 2.0),
    "ServerPlayerMaxNum": (1, 32),
    "GuildPlayerMaxNum": (1, 100),
    "BaseCampMaxNum": (1, 512),
    "PublicPort": (1, 65535),
    "RESTAPIPort": (1, 65535),
    "RCONPort": (1, 65535),
}

SECRET_KEYS = {"AdminPassword", "ServerPassword"}


class SettingsEditor(QWidget):
    saved = Signal()

    def __init__(self, live_ini: Path, default_ini: Path) -> None:
        super().__init__()
        self._live = live_ini
        self._default = default_ini
        self._settings: PalSettings | None = None
        self._widgets: dict[str, QWidget] = {}

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self._search = QLineEdit(placeholderText="Filter settings…")
        self._search.textChanged.connect(self._filter)
        bar.addWidget(self._search)

        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self.reload)
        bar.addWidget(reload_btn)

        save_btn = QPushButton("Save to ini")
        save_btn.clicked.connect(self._save)
        bar.addWidget(save_btn)
        root.addLayout(bar)

        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color:#d29922;")
        root.addWidget(self._note)

        self._scroll = QScrollArea(widgetResizable=True)
        root.addWidget(self._scroll, 1)

        self.reload()

    # ---------- load ----------

    def reload(self) -> None:
        if is_blank(self._live):
            self._offer_seed()
            return

        try:
            self._settings = PalSettings.load(self._live)
        except Exception as e:
            self._note.setText(f"Couldn't parse {self._live}: {e}")
            return

        self._note.setText(
            "⚠️ Changes take effect on the next server restart — the game reads this "
            "file only at startup. Every save takes a timestamped .bak, because a "
            "SteamCMD validate can wipe this file."
        )
        self._build()

    def _offer_seed(self) -> None:
        if not self._default.exists():
            self._note.setText(
                f"Live ini is blank and I can't find {self._default}. "
                "Check the server root path in Config."
            )
            return

        answer = QMessageBox.question(
            self,
            "Blank config",
            "PalWorldSettings.ini is empty.\n\n"
            "That's normal — Palworld ships it blank and expects you to copy "
            "DefaultPalWorldSettings.ini into it. (Editing the Default file itself "
            "does nothing, which is why most 'my settings won't apply' threads exist.)\n\n"
            "Seed it from the default now?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            seed_from_default(self._default, self._live)
            self.reload()
        else:
            self._note.setText("Live ini is blank. Nothing to edit until it's seeded.")

    # ---------- build form ----------

    def _build(self) -> None:
        assert self._settings is not None
        s = self._settings
        self._widgets.clear()

        container = QWidget()
        layout = QVBoxLayout(container)

        grouped: set[str] = set()

        for title, keys in GROUPS.items():
            present = [k for k in keys if k in s]
            if not present:
                continue
            grouped.update(present)
            layout.addWidget(self._group(title, present))

        leftover = [k for k in s.keys() if k not in grouped]
        if leftover:
            layout.addWidget(
                self._group(
                    f"Other settings ({len(leftover)}) — unrecognised, preserved as-is",
                    leftover,
                )
            )

        layout.addStretch(1)
        self._scroll.setWidget(container)

    def _group(self, title: str, keys: list[str]) -> QGroupBox:
        assert self._settings is not None
        box = QGroupBox(title)
        form = QFormLayout(box)

        for key in keys:
            opt = self._settings.option(key)
            w = self._widget_for(key, opt.kind, opt.value)
            self._widgets[key] = w
            form.addRow(QLabel(key), w)

        return box

    def _widget_for(self, key: str, kind: str, value: object) -> QWidget:
        lo, hi = RANGES.get(key, (None, None))  # type: ignore[assignment]

        if kind == ValueKind.BOOL:
            w = QCheckBox()
            w.setChecked(bool(value))
            return w

        if kind == ValueKind.INT:
            w = QSpinBox()
            w.setRange(int(lo) if lo is not None else -1, int(hi) if hi is not None else 1_000_000)
            w.setValue(int(value))  # type: ignore[arg-type]
            return w

        if kind == ValueKind.FLOAT:
            w = QDoubleSpinBox()
            w.setDecimals(6)
            w.setSingleStep(0.1)
            w.setRange(float(lo) if lo is not None else -1.0,
                       float(hi) if hi is not None else 1_000_000.0)
            w.setValue(float(value))  # type: ignore[arg-type]
            return w

        w = QLineEdit(str(value) if not isinstance(value, list) else ",".join(value))
        if key in SECRET_KEYS:
            w.setEchoMode(QLineEdit.EchoMode.Password)
        return w

    # ---------- save ----------

    def _save(self) -> None:
        if self._settings is None:
            return

        for key, w in self._widgets.items():
            if isinstance(w, QCheckBox):
                self._settings.set(key, w.isChecked())
            elif isinstance(w, QSpinBox):
                self._settings.set(key, w.value())
            elif isinstance(w, QDoubleSpinBox):
                self._settings.set(key, w.value())
            elif isinstance(w, QLineEdit):
                opt = self._settings.option(key)
                if opt.kind == ValueKind.TUPLE:
                    self._settings.set(key, [p.strip() for p in w.text().split(",") if p.strip()])
                else:
                    self._settings.set(key, w.text())

        try:
            bak = self._settings.save(self._live)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return

        QMessageBox.information(
            self,
            "Saved",
            f"Written to {self._live.name}."
            + (f"\nBackup: {bak.name}" if bak else "")
            + "\n\nRestart the server for changes to take effect.",
        )
        self.saved.emit()

    # ---------- filter ----------

    def _filter(self, text: str) -> None:
        needle = text.lower().strip()
        container = self._scroll.widget()
        if container is None:
            return

        for box in container.findChildren(QGroupBox):
            form = box.layout()
            if not isinstance(form, QFormLayout):
                continue

            any_visible = False
            for row in range(form.rowCount()):
                label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                field_item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
                if not (label_item and field_item):
                    continue

                label = label_item.widget()
                field = field_item.widget()
                visible = not needle or needle in label.text().lower()
                label.setVisible(visible)
                field.setVisible(visible)
                any_visible |= visible

            box.setVisible(any_visible)
