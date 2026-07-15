"""Backup / restore / prune of the SaveGames folder."""

from __future__ import annotations

import os
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
    # False when the server wrote the world during every copy attempt, so the
    # files may be from different moments (a "torn" backup). Kept anyway — a
    # probably-fine backup beats none — but callers should warn. listing()
    # can't know this after the fact, so it reports True; only the Backup
    # returned by create() carries a real verdict.
    consistent: bool = True


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _dir_size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576


def _copytree_staged(src: Path, dest: Path) -> None:
    """
    Copy `src` to `dest` via a temporary `.partial` sibling and a rename, so an
    interrupted copy (daemon killed, disk full, share dropped) never leaves a
    directory at `dest` that looks like a finished backup. `dest` existing
    genuinely means "complete" — listing() skips `.partial` names.
    """
    tmp = dest.parent / f"{dest.name}.partial"
    if tmp.exists():
        shutil.rmtree(tmp)  # leftover from a previous failed attempt
    try:
        shutil.copytree(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """(size, mtime_ns) of every file under `root`, keyed by relative path.
    Two identical fingerprints straddling a copy mean nothing was written
    while the copy ran — every copied file is from the same moment."""
    out: dict[str, tuple[int, int]] = {}
    for f in root.rglob("*"):
        if f.is_file():
            st = f.stat()
            out[f.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def _copy_matches(before: dict[str, tuple[int, int]], copied: Path) -> bool:
    """Every file the source had going in exists in the copy at the same size.
    Catches a file the server deleted/replaced mid-copy and a short read the
    filesystem didn't report — both of which mean this copy is torn."""
    for rel, (size, _mtime) in before.items():
        f = copied / rel
        try:
            if f.stat().st_size != size:
                return False
        except OSError:
            return False
    return True


def create(
    savegames: Path,
    backup_root: Path,
    label: str = "manual",
    *,
    consistency_retries: int = 2,
) -> Backup:
    if not savegames.exists():
        raise FileNotFoundError(
            f"SaveGames not found at {savegames}. Check the server root path."
        )

    backup_root.mkdir(parents=True, exist_ok=True)
    dest = backup_root / f"{_stamp()}-{label}"
    tmp = dest.parent / f"{dest.name}.partial"

    # This is usually a HOT copy — the server keeps running (that's the point
    # of scheduled backups). Saving first (the scheduler does) makes a torn
    # copy unlikely, not impossible: Palworld can write a .sav mid-copy.
    # Fingerprint the source before and after each attempt; identical
    # fingerprints prove a quiet window, so retry a couple of times for one.
    # Autosaves are minutes apart and the copy takes seconds, so a retry
    # almost always lands clean. If every attempt was dirty, keep the last
    # copy anyway — flagged, because a probably-fine backup beats none.
    consistent = False
    for _attempt in range(max(1, consistency_retries + 1)):
        if tmp.exists():
            shutil.rmtree(tmp)  # leftover from a previous failed/dirty attempt
        before = _fingerprint(savegames)
        try:
            shutil.copytree(savegames, tmp)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        if _copy_matches(before, tmp) and _fingerprint(savegames) == before:
            consistent = True
            break

    os.replace(tmp, dest)
    return Backup(dest.name, dest, _dir_size_mb(dest), datetime.now(), consistent)


def listing(backup_root: Path) -> list[Backup]:
    if not backup_root.exists():
        return []
    out = [
        Backup(d.name, d, _dir_size_mb(d), datetime.fromtimestamp(d.stat().st_mtime))
        for d in backup_root.iterdir()
        if d.is_dir() and not d.name.endswith(".partial")  # skip in-progress copies
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

    Stages the full backup copy *next to* SaveGames before touching the live
    world, so a mid-copy failure (disk full, unreadable backup) leaves the
    current world exactly as it was. Snapshots the current world before the
    swap, so restoring the wrong backup is itself undoable.
    """
    src = _safe_backup_path(backup_root, name)
    if not src.is_dir():
        raise ValueError(f"Invalid backup: {name}")

    staged = savegames.parent / f"{savegames.name}.partial-restore"
    if staged.exists():
        shutil.rmtree(staged)  # leftover from a previous failed attempt
    try:
        shutil.copytree(src, staged)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise

    # The staged copy is complete — only now touch the live world. If the
    # snapshot or swap below still fails, the staged dir is deliberately left
    # in place: past this point it may be the only good copy.
    if savegames.exists():
        _copytree_staged(savegames, backup_root / f"{_stamp()}-pre-restore")
        shutil.rmtree(savegames)
    os.replace(staged, savegames)


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
    _copytree_staged(backup_path, dest)
    return dest


def test_mirror(target: str) -> tuple[bool, str]:
    """Check a backup-mirror target is usable before backups start relying on
    it. Remotes (`remote:path`) go through rclone (auth reachable); a local path
    must be a directory we can create and write into. Returns (ok, message)."""
    from . import rclone

    if not target.strip():
        return False, "No mirror target set."
    if rclone.is_remote(target):
        return rclone.test_remote(target)
    root = Path(target)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".palctl-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return False, f"Not writable: {e}"
    return True, f"Writable — {root}"


def delete(backup_root: Path, name: str) -> None:
    target = _safe_backup_path(backup_root, name)
    if not target.is_dir():
        raise ValueError(f"Invalid backup: {name}")
    shutil.rmtree(target)


def prune(backup_root: Path, retain: int) -> list[str]:
    """Keep the newest `retain`. Never touches -pre-restore safety copies.

    `retain` is clamped to at least 1: a hand-edited (or future-version)
    config with backup_retain <= 0 must read as "keep the latest", never as
    "delete every backup ever taken" — prune runs right after each create.
    """
    retain = max(1, retain)
    prunable = [b for b in listing(backup_root) if not b.name.endswith("-pre-restore")]
    doomed = prunable[retain:]
    for b in doomed:
        shutil.rmtree(b.path)
    return [b.name for b in doomed]
