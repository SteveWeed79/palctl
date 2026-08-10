"""Notice when `PalWorldSettings.ini` stops being what palctl wrote.

That file has more than one author. palctl writes it (the REST API settings, the
admin password, the wizard's choices), the admin edits it by hand, and a Steam
update can put a pristine copy back over the top. The worst single bug of the
1.2.5 cycle was the third case going unnoticed: an update reset the file,
`RESTAPIEnabled` went back to `False`, and palctl was permanently unable to see
a server that was running perfectly well — reporting an outage that didn't
exist, on a box where nothing was actually wrong.

The fix for that case was a heuristic ("is the file blank?"), which missed the
variant that actually happened ("is the file back at Steam's defaults?"). Adding
a third heuristic for the next variant is not the way out. Drift is knowable
exactly: palctl records what it wrote, and anything else is drift. What *kind*
of drift it is only decides the wording.

The snapshot lives in palctl's own config dir, never next to the ini — a Steam
update that replaces the server directory would take the evidence with it.

No secrets are stored. The snapshot keeps a hash per setting, not values, so a
snapshot file can never leak the admin password sitting in that ini (see
SECURITY.md — anything that writes a secret to disk in the clear is a
vulnerability, and this file would otherwise be exactly that).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import config_dir
from .inifile import PalSettings

SNAPSHOT_NAME = "settings_snapshot.json"

# Settings whose loss breaks palctl itself rather than just changing the game.
# Drift in any of these is reported as urgent, because the symptom an admin sees
# — "palctl says my server is down and it isn't" — points nowhere near the ini.
CRITICAL_KEYS = ("RESTAPIEnabled", "RESTAPIPort", "AdminPassword")


def _hash(value: object) -> str:
    """A stable hash of one setting's value. Hashed rather than stored: this
    file must never become a plaintext copy of AdminPassword."""
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:16]


@dataclass
class Snapshot:
    path: str
    at: float
    values: dict[str, str] = field(default_factory=dict)  # key -> hash

    def as_dict(self) -> dict:
        return {"path": self.path, "at": self.at, "values": self.values}

    @classmethod
    def from_dict(cls, d: dict) -> Snapshot | None:
        try:
            return cls(
                path=str(d["path"]),
                at=float(d.get("at", 0.0)),
                values={str(k): str(v) for k, v in dict(d["values"]).items()},
            )
        except (KeyError, TypeError, ValueError):
            return None  # a hand-mangled snapshot is no snapshot, not a crash


@dataclass(frozen=True)
class Drift:
    kind: str          # none | no_snapshot | missing | blank | reset | edited
    changed: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()      # keys palctl set that are gone entirely
    critical: tuple[str, ...] = ()  # the subset that breaks palctl itself

    @property
    def matters(self) -> bool:
        return self.kind not in ("none", "no_snapshot")


def snapshot_path() -> Path:
    return config_dir() / SNAPSHOT_NAME


def take(ini: Path) -> Snapshot | None:
    """Record what the ini says right now. Call it after every write palctl
    makes, so 'what palctl wrote' stays current."""
    try:
        settings = PalSettings.load(Path(ini))
    except (OSError, ValueError):
        return None
    return Snapshot(
        path=str(ini),
        at=time.time(),
        values={key: _hash(settings.get(key)) for key in settings.keys()},
    )


def save(snap: Snapshot | None) -> None:
    if snap is None:
        return
    try:
        path = snapshot_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap.as_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # best effort: losing the snapshot must never fail a real operation


def load() -> Snapshot | None:
    try:
        return Snapshot.from_dict(
            json.loads(snapshot_path().read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None


def record(ini: Path) -> None:
    """take() + save(), the one call every writer of the ini should make."""
    save(take(ini))


def compare(saved: Snapshot | None, ini: Path, default_ini: Path | None = None) -> Drift:
    """What happened to the ini since palctl last wrote it.

    `default_ini` is Steam's `DefaultPalWorldSettings.ini`. It only refines the
    wording — a file that matches Steam's defaults exactly is a reset, which
    tells the admin *why* their settings are gone — and its absence never
    changes whether drift is reported.
    """
    ini = Path(ini)
    if saved is None:
        return Drift("no_snapshot")
    if not ini.exists():
        return Drift("missing", critical=CRITICAL_KEYS)

    try:
        current = PalSettings.load(ini)
    except (OSError, ValueError):
        return Drift("blank", critical=CRITICAL_KEYS)
    keys = current.keys()
    if not keys:
        return Drift("blank", critical=CRITICAL_KEYS)

    now = {key: _hash(current.get(key)) for key in keys}
    changed = tuple(sorted(k for k, h in saved.values.items() if k in now and now[k] != h))
    lost = tuple(sorted(k for k in saved.values if k not in now))
    if not changed and not lost:
        return Drift("none")

    kind = "edited"
    if default_ini and _matches_default(current, Path(default_ini)):
        kind = "reset"
    critical = tuple(k for k in CRITICAL_KEYS if k in changed or k in lost)
    return Drift(kind, changed=changed, lost=lost, critical=critical)


def _matches_default(current: PalSettings, default_ini: Path) -> bool:
    """Whether the live file is byte-equivalent to Steam's shipped defaults, in
    the only sense that matters: same settings, same values."""
    try:
        default = PalSettings.load(default_ini)
    except (OSError, ValueError):
        return False
    if sorted(current.keys()) != sorted(default.keys()):
        return False
    return all(current.get(k) == default.get(k) for k in default.keys())


def describe(drift: Drift) -> str:
    """The message an admin gets. Leads with the consequence, because the
    setting names mean nothing to most people and 'palctl can't see your
    server' means everything."""
    if not drift.matters:
        return ""
    if drift.kind == "missing":
        return (
            "⚠️ **PalWorldSettings.ini is gone.** The server will start with "
            "Steam's defaults — including the REST API turned off, which is how "
            "palctl talks to it. Run **Update / repair** or re-run setup to put "
            "it back."
        )
    if drift.kind == "blank":
        return (
            "⚠️ **PalWorldSettings.ini is empty or unreadable**, so every setting "
            "in it — the REST API among them — is back to Steam's defaults. "
            "Re-run setup to restore it."
        )

    lead = (
        "⚠️ **A Steam update reset PalWorldSettings.ini to Steam's defaults.**"
        if drift.kind == "reset"
        else "ℹ️ **PalWorldSettings.ini changed outside palctl.**"
    )
    if drift.critical:
        return (
            f"{lead} The settings palctl needs to reach the server "
            f"({', '.join(drift.critical)}) are no longer what it set, which is "
            "why it may report the server as down while it is running fine. "
            "Re-run setup, or use **Update / repair**, to put them back."
        )
    n = len(drift.changed) + len(drift.lost)
    return (
        f"{lead} {n} setting(s) differ from what palctl last wrote "
        f"({', '.join((drift.changed + drift.lost)[:6])}"
        f"{', …' if n > 6 else ''}). If that was you, nothing needs doing — "
        "palctl will take the current file as the new baseline."
    )
