"""
Parse and write Palworld's PalWorldSettings.ini.

The whole config lives on ONE line as an unreadable blob:

    [/Script/Pal.PalGameWorldSettings]
    OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,ServerName="My Server",
                    CrossplayPlatforms=(Steam,Xbox,PS5,Mac),...)

Turning that into typed key/value pairs and back is the entire job of a settings
editor. Two things make it non-trivial and both bite naive `.split(",")` code:

  1. Quoted strings can contain commas:  ServerDescription="Hi, welcome"
  2. Values can be nested tuples:        CrossplayPlatforms=(Steam,Xbox,PS5,Mac)

So we split on depth-0, outside-quotes commas only.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SECTION = "[/Script/Pal.PalGameWorldSettings]"
_OPTION_RE = re.compile(r"OptionSettings\s*=\s*\((.*)\)\s*$", re.DOTALL)


def _split_top_level(body: str) -> list[str]:
    """Split on commas that are at paren-depth 0 and outside double quotes."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_quotes = False

    for ch in body:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif in_quotes:
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)

    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


class ValueKind:
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    TUPLE = "tuple"
    ENUM = "enum"


@dataclass
class Option:
    """One setting, with its raw text preserved so we can round-trip losslessly."""

    key: str
    raw: str  # exactly as it appeared, minus the key= prefix
    kind: str

    @property
    def value(self) -> object:
        if self.kind == ValueKind.BOOL:
            return self.raw.strip().lower() == "true"
        if self.kind == ValueKind.INT:
            return int(float(self.raw))
        if self.kind == ValueKind.FLOAT:
            return float(self.raw)
        if self.kind == ValueKind.STRING:
            return self.raw.strip().strip('"')
        if self.kind == ValueKind.TUPLE:
            inner = self.raw.strip().lstrip("(").rstrip(")")
            return [p.strip() for p in inner.split(",") if p.strip()]
        return self.raw.strip()

    def render(self, value: object) -> str:
        """Produce the raw text for a new value, preserving Palworld's formatting."""
        if self.kind == ValueKind.BOOL:
            return "True" if value else "False"
        if self.kind == ValueKind.INT:
            return str(int(value))  # type: ignore[arg-type]
        if self.kind == ValueKind.FLOAT:
            # Palworld writes 6 decimal places. Match it, or the game may not parse.
            return f"{float(value):.6f}"  # type: ignore[arg-type]
        if self.kind == ValueKind.STRING:
            s = str(value).replace('"', "")
            return f'"{s}"'
        if self.kind == ValueKind.TUPLE:
            if isinstance(value, (list, tuple)):
                return "(" + ",".join(str(v) for v in value) + ")"
            return str(value)
        return str(value)


def _classify(raw: str) -> str:
    r = raw.strip()
    if r.startswith("(") and r.endswith(")"):
        return ValueKind.TUPLE
    if r.startswith('"'):
        return ValueKind.STRING
    if r.lower() in ("true", "false"):
        return ValueKind.BOOL
    # 1.000000 -> float; 32 -> int
    if re.fullmatch(r"-?\d+\.\d+", r):
        return ValueKind.FLOAT
    if re.fullmatch(r"-?\d+", r):
        return ValueKind.INT
    # Difficulty=None, DeathPenalty=All — bare identifiers
    return ValueKind.ENUM


class PalSettings:
    """
    Round-trippable view of PalWorldSettings.ini.

    Unknown keys from future patches are preserved verbatim — we never drop a
    setting just because we don't recognise it. That's how config editors eat
    people's servers after an update.
    """

    def __init__(self, options: dict[str, Option], order: list[str]) -> None:
        self._options = options
        self._order = order

    # ---------- parsing ----------

    @classmethod
    def parse(cls, text: str) -> PalSettings:
        m = _OPTION_RE.search(text)
        if not m:
            raise ValueError(
                "No OptionSettings=(...) block found. If the file is blank, that's "
                "normal — seed it from DefaultPalWorldSettings.ini first."
            )

        options: dict[str, Option] = {}
        order: list[str] = []

        for part in _split_top_level(m.group(1)):
            if "=" not in part:
                continue
            key, _, raw = part.partition("=")
            key = key.strip()
            options[key] = Option(key=key, raw=raw.strip(), kind=_classify(raw))
            order.append(key)

        return cls(options, order)

    @classmethod
    def load(cls, path: Path) -> PalSettings:
        return cls.parse(path.read_text(encoding="utf-8-sig"))

    # ---------- access ----------

    def __contains__(self, key: str) -> bool:
        return key in self._options

    def keys(self) -> list[str]:
        return list(self._order)

    def option(self, key: str) -> Option:
        return self._options[key]

    def get(self, key: str, default: object = None) -> object:
        opt = self._options.get(key)
        return opt.value if opt else default

    def set(self, key: str, value: object) -> None:
        opt = self._options.get(key)
        if opt is None:
            # New key from a future patch, or one the user added. Guess the kind.
            kind = (
                ValueKind.BOOL
                if isinstance(value, bool)
                else ValueKind.INT
                if isinstance(value, int)
                else ValueKind.FLOAT
                if isinstance(value, float)
                else ValueKind.STRING
            )
            opt = Option(key=key, raw="", kind=kind)
            self._options[key] = opt
            self._order.append(key)
        opt.raw = opt.render(value)

    # ---------- writing ----------

    def render(self) -> str:
        body = ",".join(f"{k}={self._options[k].raw}" for k in self._order)
        return f"{SECTION}\nOptionSettings=({body})\n"

    def save(self, path: Path, *, backup: bool = True) -> Path | None:
        """
        Write the file back. Takes a timestamped .bak first by default.

        This matters more than it looks: a SteamCMD `validate` can blow away
        PalWorldSettings.ini, and people lose hours of tuning. Backing up on every
        save means there's always a recent copy sitting next to it.
        """
        bak: Path | None = None
        if backup and path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            bak = path.with_suffix(f".ini.{stamp}.bak")
            shutil.copy2(path, bak)

        path.write_text(self.render(), encoding="utf-8")
        return bak


def seed_from_default(default_ini: Path, live_ini: Path) -> None:
    """
    Palworld ships the live PalWorldSettings.ini EMPTY. You're expected to copy
    DefaultPalWorldSettings.ini into it. Editing the Default file does nothing.

    Almost every "my settings don't apply" thread is this.
    """
    live_ini.parent.mkdir(parents=True, exist_ok=True)
    live_ini.write_text(default_ini.read_text(encoding="utf-8-sig"), encoding="utf-8")


def is_blank(path: Path) -> bool:
    if not path.exists():
        return True
    return "OptionSettings" not in path.read_text(encoding="utf-8-sig")


def read_admin_password(live_ini: Path) -> str:
    """
    AdminPassword straight from the server's own ini.

    The REST API password IS this value, and Palworld already stores it in the
    ini in cleartext — so reading it here adds no new secret to disk. It's the
    fallback for a daemon that can't see the per-user keyring (classic case: a
    Windows service running as LocalSystem, which has its own Credential
    Manager). Returns "" when the ini is missing, blank, or has no password.
    """
    try:
        value = PalSettings.load(live_ini).get("AdminPassword", "")
    except (OSError, ValueError):
        return ""
    return str(value) if value else ""
