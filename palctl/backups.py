"""Backup / restore / prune of the SaveGames folder."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
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


_STAMP_FMT = "%Y-%m-%d_%H-%M-%S"


def _stamp() -> str:
    """The timestamp a backup directory is named for — UTC, marked with `Z`.

    It used to be naive local time, and retention orders backups by name, so
    through a DST fall-back the ordering stopped matching real time: the local
    clock repeats an hour, and a backup taken at 01:15 EST sorts as older than
    one taken half an hour earlier at 01:45 EDT. prune() deletes from the tail
    of that order, so for one hour a year it deleted the newer copy and kept
    the older — on the safety net, and silently.

    UTC never repeats, so the name is monotonic and the string order is the
    real order. Names written before this change carry no `Z` and are still
    read (see `sort_key`), so an existing backup folder keeps working and
    keeps sorting correctly against new ones.
    """
    return datetime.now(UTC).strftime(_STAMP_FMT) + "Z"


# A palctl backup directory is "<stamp>-<label>" (see _stamp above). This
# identifies our own backups, so retention pointed at a shared or populated
# location — a mirror on the user's whole drive, or an rclone remote with the
# user's other folders in it — only ever prunes what palctl created, never the
# user's own data. rclone.py imports this same pattern for the cloud mirror.
# `Z` is optional: directories created before the switch to UTC don't have it.
BACKUP_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(Z?)-")


def is_backup_name(name: str) -> bool:
    """True for a directory palctl itself created and finished.

    One definition, used by everything that reads, restores, deletes or prunes,
    so those can't disagree about what a backup is — they used to: prune()
    checked the pattern, restore() and delete() accepted any directory under the
    root, and listing() showed every one of them.

    `.partial` is a copy still being written (create() and mirror() stage under
    that suffix and rename on completion), so it is a directory that exists and
    is not a backup yet.
    """
    return bool(BACKUP_NAME_RE.match(name)) and not name.endswith(".partial")


def sort_key(name: str) -> datetime:
    """The instant a backup directory name stands for, as aware UTC.

    Ordering backups is a data-safety operation — prune() deletes from the end
    of it — so it runs on an instant rather than on the string. A `Z` name is
    read as UTC. A legacy name is naive local time, which `astimezone` resolves
    against the machine's zone; that is still ambiguous inside a repeated hour,
    but such a name cannot record which side of the change it came from, so no
    reading of it can do better. New names are exact.

    A name that isn't ours sorts oldest. prune() never sees one — it filters on
    BACKUP_NAME_RE first — so this only affects display order.
    """
    m = BACKUP_NAME_RE.match(name)
    if not m:
        return datetime.min.replace(tzinfo=UTC)
    try:
        dt = datetime.strptime(m.group(1), _STAMP_FMT)
    except ValueError:  # matched the shape but isn't a real date (month 13)
        return datetime.min.replace(tzinfo=UTC)
    if m.group(2):
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)  # naive == local, by Python's rule


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


def listing(backup_root: Path) -> list[Backup]:
    if not backup_root.exists():
        return []
    out = [
        Backup(d.name, d, _dir_size_mb(d), datetime.fromtimestamp(d.stat().st_mtime))
        for d in backup_root.iterdir()
        # palctl's own finished backups only. This used to be "every directory
        # that isn't .partial", so a backup root that shares a folder with the
        # admin's own data — which the retention comments explicitly expect —
        # offered those folders in the dashboard's restore list.
        if d.is_dir() and is_backup_name(d.name)
    ]
    # Newest first, by the instant the name stands for rather than by the
    # string — see sort_key. The name breaks ties so the order stays
    # deterministic when two backups share a second.
    return sorted(out, key=lambda b: (sort_key(b.name), b.name), reverse=True)


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
    if not is_backup_name(name):
        # Being inside backup_root was the only requirement, which made every
        # directory under it restorable and deletable — and the backup root is
        # routinely a folder the admin already had (retention is written to
        # tolerate exactly that: "a mirror pointed at a populated location").
        # So `restore("Photos")` would copy their photos over the world, and
        # `restore("<name>.partial")` would restore a copy that was still being
        # written. prune() has always filtered on this pattern before deleting
        # anything; restore and delete now hold to the same rule.
        raise ValueError(
            f"Not a palctl backup: {name!r} (expected a directory named like "
            "2026-08-11_16-00-00Z-scheduled)"
        )
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
    #
    # `aside` is timestamped, and nothing here ever deletes an existing one.
    # It used to be a fixed `.pre-restore` name that was rmtree'd on the way in
    # as "a leftover from a previous failed attempt" — but between the two
    # renames below it holds the ONLY copy of the live world (a rename moved it
    # there; the archive that copies it into backup_root hasn't run yet). So if
    # the second rename failed and the admin simply retried, that rmtree
    # destroyed the world as it stood, and the retry then reported success. The
    # backup being restored survives either way; what was lost was everything
    # since it, plus the ability to undo.
    aside = savegames.parent / f"{savegames.name}.pre-restore-{_stamp()}"
    had_world = savegames.exists()
    if had_world:
        os.replace(savegames, aside)  # same directory, so this is a rename
    try:
        os.replace(staged, savegames)
    except BaseException:
        # The window this exists for: the live world is renamed away and the
        # restored copy did not land, so there is no world at all. Put the old
        # one back and fail — the caller then reports a failed restore over an
        # untouched world, which is true, instead of leaving the server with
        # nothing for Palworld to do but generate a fresh one.
        if had_world:
            with contextlib.suppress(OSError):
                os.replace(aside, savegames)
        raise

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


# A restore renames the live world aside under this prefix before renaming the
# restored copy into place. Between those two renames it is the only copy of
# that world, which is why nothing deletes one automatically.
PRE_RESTORE_PREFIX = ".pre-restore-"


def interrupted_restore(savegames: Path) -> list[Path]:
    """World copies left beside `savegames` by a restore that didn't finish.

    Empty on a healthy install: a completed restore archives its copy into the
    backup root and removes it. One appears when the archive step failed (the
    restore itself succeeded — the caller is told where it is), or when the
    process died mid-transaction, which is the case worth recovering from.
    """
    try:
        return sorted(
            p
            for p in savegames.parent.iterdir()
            if p.is_dir() and p.name.startswith(savegames.name + PRE_RESTORE_PREFIX)
        )
    except OSError:
        return []


def recover_interrupted_restore(savegames: Path) -> Path | None:
    """Put the world back after a restore died between its two renames.

    That window leaves no world at all, and the next thing to touch the install
    is usually the daemon starting the server — at which point Palworld makes a
    brand-new one, players join it, build in it, and the real world becomes
    un-mergeable. Recovering first is the difference between an interrupted
    restore and a lost server.

    Deliberately puts back the *old* world rather than the copy that was being
    restored: the backup still exists in the backup root and the restore can
    simply be run again, while the old world exists nowhere else. Returns the
    path it recovered from, or None when there was nothing to do — or when more
    than one candidate exists, which needs a human to choose rather than a
    guess between two irreplaceable worlds.
    """
    if savegames.exists():
        return None  # there is a world; nothing was interrupted
    candidates = interrupted_restore(savegames)
    if len(candidates) != 1:
        return None
    source = candidates[0]
    os.replace(source, savegames)
    return source


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
    """
    retain = max(1, retain)
    ours = [b for b in listing(backup_root) if BACKUP_NAME_RE.match(b.name)]
    scheduled = [b for b in ours if not b.name.endswith("-pre-restore")]
    pre_restore = [b for b in ours if b.name.endswith("-pre-restore")]

    doomed = scheduled[retain:] + pre_restore[PRE_RESTORE_RETAIN:]
    for b in doomed:
        shutil.rmtree(b.path)
    return [b.name for b in doomed]
