"""Mirror backups to an rclone remote (Google Drive, Dropbox, S3, OneDrive, …).

`backup_mirror` can be a local path *or* an rclone remote like
`gdrive:PalworldBackups`. When it's a remote, the second copy and its retention
run through the `rclone` binary the user configured once with `rclone config`,
so palctl never touches OAuth tokens or a cloud API itself — rclone owns the
auth, the resumable multi-GB uploads, and the retries.

This mirrors the layout `backups.mirror` uses on a local disk: one directory per
backup, named exactly like the primary (`<stamp>-<label>`), so listing and
retention line up with the local side.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .backups import BACKUP_NAME_RE

# An rclone remote is `name:path`, where name is [A-Za-z0-9_-]+. A *single*
# letter before the colon is a Windows drive (D:\Backups), not a remote — the
# same rule rclone itself uses, so `backup_mirror` can stay a single field that
# accepts either a path or a remote.
_REMOTE_RE = re.compile(r"^[A-Za-z0-9_-]+:")

# Only directories matching palctl's own backup-name pattern (BACKUP_NAME_RE,
# defined next to backups._stamp) are ours to count for retention or purge: a
# remote can legitimately be a shared folder — or a bare Drive root — holding the
# user's own directories, and we must never list, count, or delete those.

# Network metadata ops (list/test/purge/version) get a bounded timeout so a
# stalled remote can't hang a worker thread (or the GUI "Test" button) forever —
# the same discipline procs._run_capture uses for sc.exe/systemctl. The upload
# (`copy`) is deliberately left unbounded: a multi-GB world is a legitimately
# long transfer, and rclone applies its own low-level network idle timeouts.
_META_TIMEOUT = 60.0


def is_remote(target: str) -> bool:
    """True if `target` looks like an rclone remote rather than a local path."""
    if not _REMOTE_RE.match(target):
        return False
    name = target.split(":", 1)[0]
    # A one-character name before the colon is a Windows drive letter (C:\, D:\),
    # which is a local path, not a remote.
    return len(name) > 1


def _join(remote: str, name: str) -> str:
    """Append a backup dir name to a remote root. rclone paths use forward
    slashes; a bare remote root (`gdrive:`) needs no separator."""
    return remote + name if remote.endswith((":", "/")) else f"{remote}/{name}"


def has_subpath(target: str) -> bool:
    """True if a remote target names a folder under the remote (`gdrive:Pal`),
    not the bare remote root (`gdrive:`). palctl must only ever operate inside a
    dedicated folder of its own — never the whole Drive/bucket — so retention
    can't reach the user's other files."""
    _, _, path = target.partition(":")
    return bool(path.strip().strip("/"))


def _run(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("rclone")
    if exe is None:
        raise RuntimeError(
            "rclone is not installed or not on PATH. Install rclone and run "
            "`rclone config` to authorize your cloud account, then point the "
            "backup mirror at a remote like `gdrive:PalworldBackups`."
        )
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"`rclone {args[0]}` timed out after {timeout:.0f}s — the remote is "
            "unreachable or the network stalled."
        ) from None
    if proc.returncode != 0:
        # stderr is where rclone puts the useful message; fall back to stdout.
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise RuntimeError(f"`rclone {args[0]}` failed (exit {proc.returncode}): {detail}")
    return proc


_NEEDS_FOLDER = (
    "Point the backup mirror at a dedicated folder on the remote — e.g. "
    "`gdrive:PalworldBackups`, not the bare `gdrive:` root. palctl uploads to, "
    "and prunes within, that one folder only, so retention can never reach the "
    "rest of your drive."
)


def mirror(backup_path: Path, remote: str) -> str:
    """Copy a finished backup dir to `remote`/<name>. `rclone copy` skips files
    already present at the destination, so a retry is cheap and idempotent —
    the same guarantee the local mirror gives by short-circuiting on an existing
    copy. Returns the remote destination path.

    Refuses a bare remote root: palctl must scatter its dated folders into a
    dedicated directory of its own, never the top of the user's drive."""
    if not has_subpath(remote):
        raise RuntimeError(_NEEDS_FOLDER)
    dest = _join(remote, backup_path.name)
    # No timeout: a multi-GB world is a legitimately long upload. rclone applies
    # its own network idle timeouts, and this runs off the daemon's event loop.
    _run(["copy", str(backup_path), dest])
    return dest


def listing(remote: str) -> list[str]:
    """palctl backup directory names present under `remote`, newest first — the
    same order and shape as `backups.listing`, so retention lines up. Only
    directories matching the backup-name pattern are returned: a remote can hold
    the user's own folders, and those are never ours to list, count, or delete."""
    out = _run(["lsf", "--dirs-only", remote], timeout=_META_TIMEOUT).stdout
    names = [line.rstrip("/") for line in out.splitlines() if line.strip()]
    names = [n for n in names if BACKUP_NAME_RE.match(n)]
    return sorted(names, reverse=True)


def prune(remote: str, retain: int) -> list[str]:
    """Keep the newest `retain` backups on the remote; purge the rest. Mirrors
    `backups.prune`: retain is clamped to at least 1 (a bad config must never
    read as 'delete everything'), and `-pre-restore` safety copies are never
    touched — though the remote only ever receives scheduled/manual backups.

    Only ever purges directories `listing()` already vetted as palctl backups,
    inside a dedicated folder (a bare root is refused), so a shared or
    populated remote can never lose the user's own data to retention."""
    if not has_subpath(remote):
        raise RuntimeError(_NEEDS_FOLDER)
    retain = max(1, retain)
    names = [n for n in listing(remote) if not n.endswith("-pre-restore")]
    doomed = names[retain:]
    for name in doomed:
        # `purge` removes the directory and its contents; `delete` would leave an
        # empty dir behind on backends that track directories.
        _run(["purge", _join(remote, name)], timeout=_META_TIMEOUT)
    return doomed


def test_remote(target: str) -> tuple[bool, str]:
    """Verify rclone can actually reach the account behind `target` — the
    "test connection" the Settings button drives. Lists the remote *root* (not
    the backup subpath, which may not exist until the first backup), so a
    working auth reads as success even before any backup has been uploaded.
    Returns (ok, human message)."""
    if not has_subpath(target):
        return False, _NEEDS_FOLDER
    root = target.split(":", 1)[0] + ":"
    try:
        _run(["lsd", root], timeout=_META_TIMEOUT)
    except RuntimeError as e:
        return False, str(e)
    return True, f"Connected — rclone reached {root}"


def check() -> tuple[bool, str]:
    """Is the rclone binary available? Returns (ok, detail) for preflight. Does
    not validate the remote itself — that needs a network round-trip we keep out
    of the fast preflight path."""
    exe = shutil.which("rclone")
    if exe is None:
        return False, "rclone not found on PATH"
    try:
        ver = _run(["version"], timeout=_META_TIMEOUT).stdout.splitlines()[0].strip()
    except (RuntimeError, IndexError):
        return True, "rclone found"
    return True, ver
