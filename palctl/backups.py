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


def _safe_backup_path(backup_root: Path, name: str) -> Path:
    """
    Resolve `name` to a backup directory *directly under* backup_root, or raise
    ValueError.

    Rejects path-traversal (`..`, separators) and — critically — the empty
    string and `.`, both of which `backup_root / name` collapses back to
    backup_root itself. Without this a name of "" or "." sails past the old
    substring check and makes restore()/delete() operate on the entire backups
    folder (rmtree the lot, or copy every backup over the live world).
    """
    if not name or not name.strip() or name in (".", ".."):
        raise ValueError(f"Invalid backup name: {name!r}")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid backup name: {name!r}")
    src = backup_root / name
    if src.resolve().parent != backup_root.resolve():
        raise ValueError(f"Invalid backup name: {name!r}")
    return src


def is_restorable(backup_root: Path, name: str) -> bool:
    """True if `name` is a safe, existing backup directory — the non-raising
    pre-check callers use to reject a bad name before taking the server down."""
    try:
        return _safe_backup_path(backup_root, name).is_dir()
    except ValueError:
        return False


def restore(backup_root: Path, name: str, savegames: Path) -> None:
    """
    CALLER MUST STOP THE SERVER FIRST — copying over a live save corrupts it.

    Snapshots the current world before overwriting, so restoring the wrong
    backup is itself undoable.
    """
    src = _safe_backup_path(backup_root, name)
    if not src.is_dir():
        raise ValueError(f"Invalid backup: {name}")

    if savegames.exists():
        shutil.copytree(savegames, backup_root / f"{_stamp()}-pre-restore")
        shutil.rmtree(savegames)

    shutil.copytree(src, savegames)


def mirror(backup_path: Path, mirror_root: Path) -> Path:
    """
    Copy a finished backup to a second location — ideally another disk or a
    network share. Rotating backups onto the same disk as the server protect
    against a bad update, not a dead drive; this is the honest half of the
    backup story. Same layout as backup_root, so listing() and prune() work
    on the mirror too.
    """
    mirror_root.mkdir(parents=True, exist_ok=True)
    dest = mirror_root / backup_path.name
    if dest.exists():
        return dest  # already mirrored (e.g. a retry)
    shutil.copytree(backup_path, dest)
    return dest


def delete(backup_root: Path, name: str) -> None:
    target = _safe_backup_path(backup_root, name)
    if not target.is_dir():
        raise ValueError(f"Invalid backup: {name}")
    shutil.rmtree(target)


def prune(backup_root: Path, retain: int) -> list[str]:
    """Keep the newest `retain`. Never touches -pre-restore safety copies."""
    prunable = [b for b in listing(backup_root) if not b.name.endswith("-pre-restore")]
    doomed = prunable[retain:]
    for b in doomed:
        shutil.rmtree(b.path)
    return [b.name for b in doomed]
