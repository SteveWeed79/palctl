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

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
from .widgets import NoScrollDoubleSpinBox, NoScrollSpinBox

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

# Settings whose value is one of a fixed, named set. Palworld classifies these
# as bare identifiers, so the parser sees an "enum" and — before this — dropped
# them into a free-text box, where a typo ("Nomal", "itemandequipment") is a
# silently-broken setting the game just ignores. A dropdown makes that
# impossible. Values are the exact tokens the game writes, case included.
ENUM_CHOICES: dict[str, tuple[str, ...]] = {
    # None here is "custom" — the individual rate settings take over.
    "Difficulty": ("None", "Casual", "Normal", "Hard"),
    "DeathPenalty": ("None", "Item", "ItemAndEquipment", "All"),
    "RandomizerType": ("None", "Region", "All"),
    "LogFormatType": ("Text", "Json"),
    # Deprecated in favour of CrossplayPlatforms, but still honoured; single value.
    "AllowConnectPlatform": ("Steam", "Xbox", "PS5", "Mac"),
}

# Tuple settings whose value is a subset of a fixed list — a row of checkboxes,
# not a comma-string you have to spell perfectly.
MULTI_CHOICES: dict[str, tuple[str, ...]] = {
    "CrossplayPlatforms": ("Steam", "Xbox", "PS5", "Mac"),
}

# One-line helpers for the settings whose behaviour bites people. Shown as a
# hover tooltip, with an ⓘ flag on the label so you know to hover — a raw key
# like "bPalLost" or "AllowConnectPlatform" tells you nothing about the footgun
# behind it. Kept terse: a form of 200 fields can't afford paragraphs.
HELP: dict[str, str] = {
    "Difficulty": (
        "None = use the custom rates below. A preset (Casual/Normal/Hard) "
        "overrides most of those multipliers."
    ),
    "DeathPenalty": (
        "What you drop when you die:  None = nothing · Item = inventory · "
        "ItemAndEquipment = + equipped gear · All = + your active Pals."
    ),
    "RESTAPIEnabled": (
        "palctl drives the server through this REST API — it must be True, or "
        "palctl can't talk to the server at all."
    ),
    "AdminPassword": (
        "Also the REST API password palctl uses. The game stores it here in "
        "cleartext, so this is where palctl reads it from."
    ),
    "AllowConnectPlatform": (
        "Deprecated — set CrossplayPlatforms instead. Kept only for old configs."
    ),
    "CrossplayPlatforms": (
        "Which platforms may connect. Uncheck all but Steam for a PC-only server."
    ),
    "bHardcore": "Permadeath: a player who dies can't respawn on this server.",
    "bPalLost": (
        "In hardcore, a player's Pals are gone for good on death — not just "
        "dropped to be recaptured."
    ),
    "RandomizerType": (
        "Shuffle where Pals spawn:  None = normal · Region = within each region · "
        "All = globally."
    ),
    "RandomizerSeed": (
        "Only matters when RandomizerType isn't None. The same seed reproduces "
        "the same shuffle."
    ),
    "LogFormatType": (
        "Server log format. Text is human-readable; Json is for log-analysis tools."
    ),
}

SECRET_KEYS = {"AdminPassword", "ServerPassword"}


class MultiSelect(QWidget):
    """A row of checkboxes for a tuple-of-known-values setting.

    Any currently-set value we don't recognise (a future platform, say) is kept
    as its own checked box, so saving never silently drops it."""

    def __init__(self, options: tuple[str, ...], selected: list[str]) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        selected_set = {str(s) for s in selected}
        # Known options first, then anything set that we don't know about.
        ordered = list(options) + [s for s in selected_set if s not in options]

        self._boxes: list[QCheckBox] = []
        for name in ordered:
            box = QCheckBox(name)
            box.setChecked(name in selected_set)
            self._boxes.append(box)
            lay.addWidget(box)
        lay.addStretch(1)

    def values(self) -> list[str]:
        return [b.text() for b in self._boxes if b.isChecked()]


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
            "SteamCMD validate can wipe this file. Note: once a world exists, the game "
            "copies most of these into that world's WorldOption.sav and reads them from "
            "there — server name, ports and player caps still come from here, but a "
            "changed rate may not apply until WorldOption.sav is removed."
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

            label = QLabel(key)
            help_text = HELP.get(key)
            if help_text:
                # ⓘ flags that there's a tooltip — invisible help helps no one.
                label.setText(f"{key} ⓘ")
                label.setToolTip(help_text)
                w.setToolTip(help_text)
            form.addRow(label, w)

        return box

    def _widget_for(self, key: str, kind: str, value: object) -> QWidget:
        # Fixed-choice settings get real pickers regardless of how the parser
        # classified the raw text — a dropdown/checkboxes can't hold a typo.
        if key in ENUM_CHOICES:
            return self._enum_combo(key, value)
        if key in MULTI_CHOICES:
            selected = value if isinstance(value, list) else [str(value)] if value else []
            return MultiSelect(MULTI_CHOICES[key], selected)

        lo, hi = RANGES.get(key, (None, None))  # type: ignore[assignment]

        if kind == ValueKind.BOOL:
            w = QCheckBox()
            w.setChecked(bool(value))
            return w

        if kind == ValueKind.INT:
            w = NoScrollSpinBox()
            w.setRange(int(lo) if lo is not None else -1, int(hi) if hi is not None else 1_000_000)
            w.setValue(int(value))  # type: ignore[arg-type]
            return w

        if kind == ValueKind.FLOAT:
            w = NoScrollDoubleSpinBox()
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

    def _enum_combo(self, key: str, value: object) -> QComboBox:
        w = QComboBox()
        choices = list(ENUM_CHOICES[key])
        current = str(value)
        # Preserve a value we don't recognise (custom, or from a future patch)
        # by making it a selectable option rather than throwing it away.
        if current and current not in choices:
            choices.insert(0, current)
        w.addItems(choices)
        w.setCurrentText(current)
        return w

    # ---------- save ----------

    def _save(self) -> None:
        if self._settings is None:
            return

        for key, w in self._widgets.items():
            if isinstance(w, MultiSelect):
                self._settings.set(key, w.values())
            elif isinstance(w, QComboBox):
                self._settings.set(key, w.currentText())
            elif isinstance(w, QCheckBox):
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
