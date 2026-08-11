"""
Install and update the Palworld dedicated server via SteamCMD.

The Config already carried ``steamcmd_path`` and ``app_id`` — and then never
touched them. There was no way to install or update the server from palctl at
all. This closes that loop: it can bootstrap SteamCMD itself, run
``app_update 2394010 validate``, and — because that ``validate`` is the exact
thing that blanks ``PalWorldSettings.ini`` — back the ini up first so a caller
can put it straight back.

The argv builder and the archive extraction are pure and unit tested; the
download and the process runners are thin wrappers over them.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

APP_ID = "2394010"
# Valve's canonical SteamCMD archives, per platform.
STEAMCMD_WIN_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
STEAMCMD_LINUX_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"

LineSink = Callable[[str], None]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_STEAMCMD_BINARIES = ("steamcmd.exe", "steamcmd.sh")

# How long a SteamCMD run may go without printing ANYTHING before we call it
# hung and kill it.
#
# A total time cap is the wrong tool here: a first install or a validate over a
# multi-GB depot on a slow line is legitimately long, and any cap generous
# enough for that is too generous to be a useful hang detector. But SteamCMD is
# extremely chatty while it works — it reprints its "Update state (0x61)
# downloading, progress: …" line continuously — so *silence* is the reliable
# signal. Sustained silence means the run is wedged (the classic ones: a depot
# server that accepted the connection and stopped sending, or steamcmd blocking
# on a Steam Guard / login prompt nobody will ever answer).
#
# Twenty minutes, not ten, because the two errors are not symmetric. A real
# wedge is permanent, so waiting an extra ten minutes to notice costs ten
# minutes, once. Killing a *healthy* run costs the user a failed install — and
# SteamCMD does have a legitimately quiet stretch: the commit/verify phase after
# a large depot downloads can sit without printing while it moves files, which
# on a slow disk is minutes. Erring long keeps the hang protection while making
# a false positive on a slow first install unlikely.
#
# This matters far more than a tidy error message. The daemon runs updates while
# holding the one server-operation lock, and it stops the game server *before*
# the update. A SteamCMD that never exits therefore leaves the server down and
# the lock held forever: the watchdog, auto-recovery, scheduled restarts and
# every Start button in the GUI, dashboard and Discord bot all answer "busy:
# update is in progress" until someone restarts the daemon by hand.
STEAMCMD_STALL_SECONDS = 1200.0

# Metadata queries (app_info_print) are a small round-trip to Steam, so they get
# a plain overall cap rather than a stall timer.
STEAMCMD_META_SECONDS = 120.0


class SteamCmdStalled(RuntimeError):
    """SteamCMD produced no output for the stall timeout and was killed."""


def _stall_duration(seconds: float) -> str:
    """'10 minutes' / '45 seconds' — so the message reads right for a short
    timeout too (tests use one), instead of '0 minutes'."""
    if seconds >= 120:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds:.0f} seconds"


# How long to spend reaping a killed SteamCMD before giving up and moving on.
# Bounded on purpose: see _kill_async.
_REAP_SECONDS = 10.0


async def _kill_async(proc: asyncio.subprocess.Process) -> None:
    """Kill a hung SteamCMD — the whole tree — and reap it. Never raises and
    never blocks indefinitely; both properties matter, because every caller is
    on the recovery path out of a hang and must not acquire a new one.

    Two traps here, and each on its own is enough to hang the daemon forever:

    1. ``proc.kill()`` signals only the process we launched. SteamCMD is a
       launcher: ``steamcmd.sh`` re-execs ``linux32/steamcmd``, and the Windows
       build spawns helpers. Those children inherit our stdout pipe and keep it
       open after the parent dies — so the reader never sees EOF.
    2. asyncio's subprocess transport only completes ``wait()`` once the process
       has exited *and* its pipes have closed. With (1) unfixed, ``await
       proc.wait()`` after a kill blocks for as long as the orphan lives.

    So: kill the descendants first (which releases the pipes), then our own
    child, and bound the wait anyway in case something still holds on.
    """
    if proc.returncode is not None:
        return
    from . import procs

    with contextlib.suppress(Exception):
        await procs.kill_descendants_async(proc.pid, timeout=_REAP_SECONDS)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=_REAP_SECONDS)


def default_steamcmd_url() -> str:
    return STEAMCMD_WIN_URL if sys.platform.startswith("win") else STEAMCMD_LINUX_URL

# SteamCMD prints e.g. "Update state (0x61) downloading, progress: 42.34 (123 / 456)".
_PROGRESS_RE = re.compile(r"progress:\s*([\d.]+)")
_BUILDID_RE = re.compile(r'"buildid"\s*"(\d+)"')


def parse_progress(line: str) -> float | None:
    """Pull the download percent out of a SteamCMD progress line, or None."""
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_installed_buildid(acf_text: str) -> str | None:
    """The build id from a Steam appmanifest_<appid>.acf (the installed build)."""
    m = _BUILDID_RE.search(acf_text)
    return m.group(1) if m else None


def parse_latest_buildid(app_info_text: str) -> str | None:
    """
    The public-branch build id from `steamcmd +app_info_print` output (the latest
    available build). The first buildid after the "public" branch key is it.
    """
    idx = app_info_text.find('"public"')
    if idx == -1:
        return None
    m = _BUILDID_RE.search(app_info_text, idx)
    return m.group(1) if m else None


def manifest_path(server_root: str | Path, app_id: str = APP_ID) -> Path | None:
    """
    Find Steam's ``appmanifest_<app_id>.acf`` for an installed server.

    There are two layouts on disk and palctl has to read both:

      * SteamCMD with ``+force_install_dir <root>`` (what palctl's own installer
        does) puts it at ``<root>/steamapps/appmanifest_<id>.acf``.
      * A Steam *library* — the Steam client's "Palworld Dedicated Server" tool,
        or a plain SteamCMD run without force_install_dir — installs the game to
        ``<lib>/steamapps/common/PalServer`` and keeps the manifest two levels up,
        at ``<lib>/steamapps/appmanifest_<id>.acf``.

    Only looking in the first place meant the build id read as "unknown" for
    everyone on the second layout (which ``discovery`` detects on purpose), so
    the update check silently never fired and their server sat on an old build
    until players hit a version mismatch on the join screen.

    The walk up stops after three levels — enough to cover ``steamapps/common/
    PalServer`` — so it can't wander off and pick up an unrelated install's
    manifest from higher up the drive.
    """
    name = f"appmanifest_{app_id}.acf"
    root = Path(server_root)
    for base in (root, *list(root.parents)[:3]):
        candidate = base / "steamapps" / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def installed_buildid(server_root: str | Path, app_id: str = APP_ID) -> str | None:
    """Read the installed build id from the server's Steam manifest, if present."""
    acf = manifest_path(server_root, app_id)
    if acf is None:
        return None
    try:
        return parse_installed_buildid(acf.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return None


async def latest_buildid(
    steamcmd: str | Path,
    app_id: str = APP_ID,
    *,
    timeout: float = STEAMCMD_META_SECONDS,
) -> str | None:
    """Ask Steam for the latest public build id. Best-effort; None on any failure.

    Bounded: this runs from the daemon's six-hourly update check, and a steamcmd
    that hangs on the metadata query (Steam unreachable, or a login prompt) would
    otherwise park a child process forever and stop that loop from ever ticking
    again. On timeout we kill it and report "don't know", which is exactly how
    every other failure here is already handled."""
    cmd = [
        str(steamcmd), "+login", "anonymous",
        "+app_info_update", "1", "+app_info_print", str(app_id), "+quit",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _kill_async(proc)
        return None
    return parse_latest_buildid(out.decode(errors="replace"))


def update_command(
    steamcmd: str | Path,
    install_dir: str | Path,
    app_id: str = APP_ID,
    *,
    validate: bool = True,
    username: str = "anonymous",
) -> list[str]:
    """
    Build the SteamCMD argv.

    ``+force_install_dir`` MUST come before ``+login`` / ``+app_update`` — put it
    after and SteamCMD silently ignores it and installs into its own directory,
    which is the single most common "why did it download to the wrong place"
    mistake.
    """
    args = [
        str(steamcmd),
        "+force_install_dir", str(install_dir),
        "+login", username,
        "+app_update", str(app_id),
    ]
    if validate:
        args.append("validate")
    args.append("+quit")
    return args


def _members_within(t: tarfile.TarFile, dest_dir: Path) -> list[tarfile.TarInfo]:
    """The members of `t` that land inside `dest_dir`, links dropped.

    Stands in for tarfile's `data` filter on the Pythons that predate it.
    `requires-python` is >=3.11 but the filter only arrived in 3.11.4, so on
    3.11.0–3.11.3 the fallback was a plain extractall — which honours `../..`
    and absolute member names and will happily write outside the destination.
    An archive that could do that is an archive that could drop a file anywhere
    the installing user can write, so pre-filtering is what the fallback owes
    the caller.
    """
    root = dest_dir.resolve()
    keep: list[tarfile.TarInfo] = []
    for m in t.getmembers():
        if m.issym() or m.islnk():
            continue  # a link is a second chance to escape, once followed
        target = (dest_dir / m.name).resolve()
        if target == root or root in target.parents:
            keep.append(m)
    return keep


def extract_steamcmd(archive_path: Path, dest_dir: Path) -> Path:
    """Unpack a steamcmd archive (.zip on Windows, .tar.gz on Linux) and return
    the path to the steamcmd binary."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if str(archive_path).endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path) as t:
            try:
                t.extractall(dest_dir, filter="data")  # py3.12+/3.11.4+: safe extract
            except TypeError:
                t.extractall(dest_dir, members=_members_within(t, dest_dir))  # noqa: S202
    else:
        with zipfile.ZipFile(archive_path) as z:
            # Safe as-is: ZipFile._extract_member drops the drive, leading
            # separators and every '..' component from each name before joining,
            # so a member cannot land outside dest_dir. (Python also writes
            # symlink members as ordinary files, so there's no link escape.)
            z.extractall(dest_dir)  # noqa: S202

    for name in _STEAMCMD_BINARIES:
        direct = dest_dir / name
        if direct.exists():
            return direct
    nested = next(
        (p for n in _STEAMCMD_BINARIES for p in dest_dir.rglob(n)), None
    )
    if nested is None:
        raise FileNotFoundError("steamcmd binary not found in the downloaded archive.")
    return nested


def download_steamcmd(dest_dir: Path, *, url: str | None = None) -> Path:
    """Download and unpack SteamCMD into ``dest_dir``. Returns the steamcmd binary."""
    url = url or default_steamcmd_url()
    suffix = ".tar.gz" if url.endswith((".tar.gz", ".tgz")) else ".zip"
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        from . import fetch

        # Timeout so a hung CDN doesn't stall setup forever. Integrity relies on
        # the TLS connection to Valve's steamcdn host; fetch retries
        # verification against certifi when the system trust can't chain it.
        with fetch.open_url(url, timeout=120) as resp, tmp_path.open("wb") as f:
            shutil.copyfileobj(resp, f)
        return extract_steamcmd(tmp_path, dest_dir)
    finally:
        tmp_path.unlink(missing_ok=True)


def backup_file(path: Path) -> Path | None:
    """
    Timestamped side copy of a single file. Used to guard PalWorldSettings.ini
    across a ``validate`` — a plain copy, not a parse, so it works even when the
    ini is blank or malformed.
    """
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_suffix(path.suffix + f".{stamp}.bak")
    shutil.copy2(path, bak)
    return bak


def run_update(
    steamcmd: str | Path,
    install_dir: str | Path,
    *,
    app_id: str = APP_ID,
    validate: bool = True,
    on_line: LineSink | None = None,
    stall_timeout: float = STEAMCMD_STALL_SECONDS,
) -> int:
    """
    Run SteamCMD to completion, streaming stdout lines to ``on_line``. Returns
    the exit code. Blocking — call it off any UI thread (the GUI does).

    Raises :class:`SteamCmdStalled` if SteamCMD prints nothing for
    ``stall_timeout`` seconds. Without it the reading loop below blocks forever
    on a wedged SteamCMD, and the setup wizard sits on "Installing the server…"
    with no way forward but killing the app.
    """
    cmd = update_command(steamcmd, install_dir, app_id, validate=validate)
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=_NO_WINDOW,
    ) as proc:
        assert proc.stdout is not None
        # The readline() below can't be interrupted, so the stall guard is a
        # side thread that kills the process — which closes stdout, which is
        # what actually breaks the loop.
        last_output = [time.monotonic()]
        finished = threading.Event()
        stalled = threading.Event()

        def _guard() -> None:
            while not finished.wait(min(5.0, stall_timeout / 4)):
                if time.monotonic() - last_output[0] < stall_timeout:
                    continue
                stalled.set()
                # Descendants first: one of them holding the inherited stdout
                # pipe would keep the read loop below running forever, which is
                # the hang this guard exists to break. See procs.kill_descendants.
                from . import procs

                with contextlib.suppress(Exception):
                    procs.kill_descendants(proc.pid, timeout=_REAP_SECONDS)
                with contextlib.suppress(OSError):
                    proc.kill()
                return

        watcher = threading.Thread(target=_guard, daemon=True)
        watcher.start()
        try:
            for line in proc.stdout:
                last_output[0] = time.monotonic()
                if on_line:
                    on_line(line.rstrip())
            code = proc.wait()
        finally:
            finished.set()
            watcher.join(timeout=5)

        if stalled.is_set():
            raise SteamCmdStalled(
                f"SteamCMD printed nothing for {_stall_duration(stall_timeout)} "
                "and was killed. It usually means the download stalled or "
                "SteamCMD is waiting on a login/Steam Guard prompt. Try again, "
                "or run SteamCMD by hand once to clear any prompt it's stuck on."
            )
        return code


async def run_update_async(
    steamcmd: str | Path,
    install_dir: str | Path,
    *,
    app_id: str = APP_ID,
    validate: bool = True,
    on_line: LineSink | None = None,
    stall_timeout: float = STEAMCMD_STALL_SECONDS,
) -> int:
    """Async twin of :func:`run_update`, for the daemon's event loop.

    Raises :class:`SteamCmdStalled` if SteamCMD goes ``stall_timeout`` seconds
    without printing a line — see the constant for why silence, not total
    runtime, is the thing worth timing out on, and what it costs when nothing
    does. The caller's ``finally`` still restores the ini and restarts the
    server, so a stall lands in the same place as any other failed update
    instead of parking the daemon.
    """
    cmd = update_command(steamcmd, install_dir, app_id, validate=validate)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=_NO_WINDOW,
    )
    assert proc.stdout is not None
    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=stall_timeout
                )
            except TimeoutError:
                await _kill_async(proc)
                raise SteamCmdStalled(
                    f"SteamCMD printed nothing for {_stall_duration(stall_timeout)} "
                    "and was killed. It usually means the download stalled or "
                    "SteamCMD is waiting on a login/Steam Guard prompt. The server "
                    "is being started again; re-run the update when you can watch it."
                ) from None
            if not raw:  # EOF — SteamCMD closed stdout, it's on its way out
                break
            if on_line:
                on_line(raw.decode(errors="replace").rstrip())
        return await proc.wait()
    except asyncio.CancelledError:
        # Daemon shutdown or an operator cancel: don't leave SteamCMD rewriting
        # the install behind our back.
        await _kill_async(proc)
        raise
