"""Backup / restore / prune of the SaveGames folder."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# palctl's own settings/history, snapshotted INTO every backup so a dead disk
# loses zero setup (the world was covered; the config that manages it wasn't).
# Lives inside the backup dir so retention and the off-site mirror cover it
# with no extra machinery; restore() explicitly excludes it from SaveGames.
CONFIG_SNAPSHOT_NAME = "palctl-config.zip"

# Whitelist, not "everything in the config dir": logs rotate (big), bin/ holds
# binaries, and daemon_token is a local secret that must not ride a backup to
# cloud storage (it's regenerated on first run anyway; config.py's rule is that
# no secret leaves the box in the clear).
_SNAPSHOT_FILES = ("config.json", "daemon_state.json", "sessions.db")


class BackupRetentionError(RuntimeError):
    """Retention ran but could not delete every backup it wanted to.

    Carries the names it *did* delete, because this is a partial success and the
    caller has to be able to say so: a backup that was taken correctly must
    never be reported as a failed backup just because pruning an older one
    afterwards hit a file lock. See prune().
    """

    def __init__(self, message: str, *, deleted: list[str] | None = None) -> None:
        super().__init__(message)
        self.deleted = deleted or []


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


# A palctl backup directory is "<stamp>-<label>" (see _stamp above). This
# identifies our own backups, so retention pointed at a shared or populated
# location — a mirror on the user's whole drive, or an rclone remote with the
# user's other folders in it — only ever prunes what palctl created, never the
# user's own data. rclone.py imports this same pattern for the cloud mirror.
BACKUP_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-")


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
    _write_config_snapshot(dest)
    return Backup(dest.name, dest, _dir_size_mb(dest), datetime.now(), consistent)


def _write_config_snapshot(dest: Path) -> None:
    """Zip palctl's own config/history into the finished backup. Best-effort by
    design — the world copy is the point of a backup and must never fail over
    its passenger. sessions.db is copied hot (the daemon may be writing); a
    torn copy of a stats database is an acceptable DR artifact, the config.json
    beside it is the part that saves the day after a dead disk."""
    from .config import config_dir

    try:
        src_dir = config_dir()
        with zipfile.ZipFile(
            dest / CONFIG_SNAPSHOT_NAME, "w", zipfile.ZIP_DEFLATED
        ) as z:
            for name in _SNAPSHOT_FILES:
                f = src_dir / name
                if f.is_file():
                    z.write(f, name)
    except Exception:
        # Don't leave a half-written zip looking like a snapshot.
        with contextlib.suppress(OSError):
            (dest / CONFIG_SNAPSHOT_NAME).unlink(missing_ok=True)


def listing(backup_root: Path, *, with_size: bool = True) -> list[Backup]:
    """Finished backups under `backup_root`, newest first.

    `with_size=False` reports every size as 0.0 and skips the measurement.
    Sizing is not free: it stats every file of every backup, so a retained
    two dozen multi-GB worlds on a slow disk or a network share is a full
    recursive walk of the whole backup tree. Callers that only need the names
    and the order — prune(), above all, which runs right after every backup —
    have no business paying for it.
    """
    if not backup_root.exists():
        return []
    out = [
        Backup(
            d.name,
            d,
            _dir_size_mb(d) if with_size else 0.0,
            datetime.fromtimestamp(d.stat().st_mtime),
        )
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


def restore(backup_root: Path, name: str, savegames: Path) -> str | None:
    """
    CALLER MUST STOP THE SERVER FIRST — copying over a live save corrupts it.

    Returns None on a clean restore, or a warning message when the world was
    restored correctly but its undo copy could not be archived — a distinction
    the caller needs, because that case is a success with a caveat, not a
    failure.

    Three phases, ordered so the live world is never the thing at risk:

      1. **Stage.** Copy the whole backup to a sibling of SaveGames. A failure
         here — unreadable backup, full disk — leaves the current world
         untouched and nothing to undo.
      2. **Swap.** Rename the live world aside, rename the staged copy into
         place. Two renames on one filesystem, back to back, with nothing slow
         between them.
      3. **Archive.** Copy the world we swapped out into the backup folder as a
         `-pre-restore` backup, so restoring the wrong one is undoable.

    Phases 2 and 3 used to be one phase, in the other order: copy the live
    world to the backup folder (minutes, and it can fail — full backup volume,
    a file the game still holds open), *then* rmtree the live world, then move
    the staged copy in. Every failure in that window left the server with no
    world at all, and the caller then started it — so Palworld generated a
    fresh one. Archiving after the swap means the slow, failure-prone part
    happens when the live world is already correct, and a failure there costs
    only the convenience of the undo copy, which is left beside SaveGames.
    """
    src = _safe_backup_path(backup_root, name)
    if not src.is_dir():
        raise ValueError(f"Invalid backup: {name}")

    staged = savegames.parent / f"{savegames.name}.partial-restore"
    if staged.exists():
        shutil.rmtree(staged)  # leftover from a previous failed attempt
    try:
        # The config snapshot rides inside the backup dir for retention and
        # mirroring — but it is palctl's file, not the world's. Restoring it
        # into SaveGames would hand the game server a stray zip.
        shutil.copytree(
            src, staged, ignore=shutil.ignore_patterns(CONFIG_SNAPSHOT_NAME)
        )
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise

    # The staged copy is complete — only now touch the live world.
    aside = savegames.parent / f"{savegames.name}.pre-restore"
    if aside.exists():
        shutil.rmtree(aside)  # leftover from a previous failed attempt
    had_world = savegames.exists()
    if had_world:
        os.replace(savegames, aside)  # same directory, so this is a rename
    os.replace(staged, savegames)

    # From here the live world is already the restored one. Everything below is
    # the undo copy, and must never turn a completed restore into a failure.
    if not had_world:
        return None
    try:
        _copytree_staged(aside, backup_root / f"{_stamp()}-pre-restore")
    except Exception as e:
        return (
            f"The restore succeeded, but the pre-restore safety copy of the old "
            f"world could not be archived to {backup_root} ({e}). The old world "
            f"is still on disk at {aside} — move it somewhere safe (or delete it "
            "to reclaim the space) once you're happy with the restore."
        )
    shutil.rmtree(aside, ignore_errors=True)
    return None


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


# How many `-pre-restore` safety copies to keep. These are full copies of the
# world, taken automatically every time a restore runs, and they used to be
# exempt from retention entirely — "never touched", which reads as safe and is
# not: a few restores leave several multi-GB worlds sitting on the same disk as
# the live one, forever. palctl warns about low disk precisely because a full
# disk corrupts saves, so an unbounded pile of them is itself a data-safety
# problem. Keeping the newest few preserves what the exemption was actually
# for — being able to undo a restore, including the one before last.
PRE_RESTORE_RETAIN = 3


def prune(backup_root: Path, retain: int) -> list[str]:
    """Keep the newest `retain` backups, and the newest few -pre-restore copies.

    `retain` is clamped to at least 1: a hand-edited (or future-version)
    config with backup_retain <= 0 must read as "keep the latest", never as
    "delete every backup ever taken" — prune runs right after each create.

    `-pre-restore` copies are counted separately, so an ordinary retention
    setting can never delete the safety copy a restore just made in order to
    stay under its own limit — that would defeat the point of taking it.

    Only directories named like palctl's own backups are counted or deleted, so
    a mirror pointed at a populated location (another disk's root, a shared
    network folder) can never lose the user's unrelated data to retention.

    One directory that will not delete does NOT stop the sweep. Retention is the
    only thing keeping a backup folder from growing without limit, and the
    reasons a single old backup resists removal are ordinary and persistent on
    Windows — an antivirus or the search indexer holding a .sav open, a
    read-only attribute, an Explorer window parked in it. Aborting on the first
    one meant every later run stopped at the same directory, so nothing was ever
    pruned again and the volume filled up: a full disk corrupts saves, which is
    the failure backups exist to survive. So delete everything deletable, then
    raise once naming what stuck — the caller reports it without failing the
    backup that just succeeded.
    """
    retain = max(1, retain)
    # Sizes are never read here, and measuring them walks every file of every
    # backup — see listing().
    ours = [
        b for b in listing(backup_root, with_size=False) if BACKUP_NAME_RE.match(b.name)
    ]
    scheduled = [b for b in ours if not b.name.endswith("-pre-restore")]
    pre_restore = [b for b in ours if b.name.endswith("-pre-restore")]

    doomed = scheduled[retain:] + pre_restore[PRE_RESTORE_RETAIN:]
    deleted: list[str] = []
    stuck: list[str] = []
    for b in doomed:
        try:
            shutil.rmtree(b.path)
            deleted.append(b.name)
        except OSError as e:
            stuck.append(f"{b.name} ({e.strerror or e})")
    if stuck:
        raise BackupRetentionError(
            f"{len(deleted)} old backup(s) were removed from {backup_root}, but "
            f"{len(stuck)} could not be deleted: {', '.join(stuck)}. Retention "
            "will keep trying, but until they go the backup folder grows past "
            "its limit — check for a file lock (antivirus, an open Explorer "
            "window) or a read-only attribute.",
            deleted=deleted,
        )
    return deleted
