"""Backup / restore / prune of the SaveGames folder."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Backup:
    name: str
    path: Path
    size_mb: float
    created: datetime


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _dir_size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576


def create(savegames: Path, backup_root: Path, label: str = "manual") -> Backup:
    if not savegames.exists():
        raise FileNotFoundError(
            f"SaveGames not found at {savegames}. Check the server root path."
        )

    backup_root.mkdir(parents=True, exist_ok=True)
    dest = backup_root / f"{_stamp()}-{label}"
    shutil.copytree(savegames, dest)

    return Backup(dest.name, dest, _dir_size_mb(dest), datetime.now())


def listing(backup_root: Path) -> list[Backup]:
    if not backup_root.exists():
        return []
    out = [
        Backup(d.name, d, _dir_size_mb(d), datetime.fromtimestamp(d.stat().st_mtime))
        for d in backup_root.iterdir()
        if d.is_dir()
    ]
    return sorted(out, key=lambda b: b.name, reverse=True)


def restore(backup_root: Path, name: str, savegames: Path) -> None:
    """
    CALLER MUST STOP THE SERVER FIRST — copying over a live save corrupts it.

    Snapshots the current world before overwriting, so restoring the wrong
    backup is itself undoable.
    """
    src = backup_root / name
    if not src.is_dir() or ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"Invalid backup: {name}")

    if savegames.exists():
        shutil.copytree(savegames, backup_root / f"{_stamp()}-pre-restore")
        shutil.rmtree(savegames)

    shutil.copytree(src, savegames)


def delete(backup_root: Path, name: str) -> None:
    target = backup_root / name
    if not target.is_dir() or ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"Invalid backup: {name}")
    shutil.rmtree(target)


def prune(backup_root: Path, retain: int) -> list[str]:
    """Keep the newest `retain`. Never touches -pre-restore safety copies."""
    prunable = [b for b in listing(backup_root) if not b.name.endswith("-pre-restore")]
    doomed = prunable[retain:]
    for b in doomed:
        shutil.rmtree(b.path)
    return [b.name for b in doomed]
