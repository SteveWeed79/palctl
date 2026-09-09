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
import logging
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
from pathlib import Path

APP_ID = "2394010"

# The Steam account every SteamCMD run logs in as. The dedicated server is a
# free tool that Valve serves to anonymous logins, so palctl never needs — and
# never asks for — a Steam account, a password, or a Steam Guard code. This is
# also what keeps the update check and the update itself unattended: a named
# login can block on a Steam Guard prompt nobody is there to answer.
STEAM_USER = "anonymous"

# Where SteamCMD keeps the app metadata `app_info_print` reads back — see
# clear_appinfo_cache for why palctl deletes it before every update check.
APPINFO_CACHE = Path("appcache") / "appinfo.vdf"
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

# Metadata queries (app_info_print) are a small round-trip to Steam — but the
# process making them is SteamCMD, which on its first run (and after every one
# of Valve's own updates to it) downloads and installs a new copy of itself
# before it gets to the query. On a slow line that alone can take minutes. So
# the query gets the same treatment as an update: it is killed for *silence*
# (STEAMCMD_META_SECONDS without a byte of output is a hung Steam connection),
# and only separately for an overall runtime nothing legitimate reaches.
STEAMCMD_META_SECONDS = 120.0
STEAMCMD_META_TOTAL_SECONDS = 900.0

_log = logging.getLogger("palctl.steamcmd")


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
# One branch inside the `branches` block: a quoted name and a flat `{ ... }` of
# key/value pairs. Branch entries never nest, which is what lets `[^{}]*` stand
# in for a real KeyValues parser here.
_BRANCH_ENTRY_RE = re.compile(r'"([^"]+)"\s*\{([^{}]*)\}')


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


def _block_after(text: str, start: int) -> str | None:
    """The inside of the first brace-balanced `{ ... }` at or after `start`."""
    opened = text.find("{", start)
    if opened == -1:
        return None
    depth = 0
    for i in range(opened, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[opened + 1 : i]
    return text[opened + 1 :]  # unterminated (a cut-off dump): take what's there


def parse_branch_buildids(app_info_text: str) -> dict[str, str]:
    """Every branch's build id from `steamcmd +app_info_print` output, keyed by
    branch name — `{"public": "20087975", "beta-xyz": "20090001"}`.

    Reads the `"branches"` block *as a block*, per branch. The previous parser
    took the first `"buildid"` after the first `"public"` in the whole dump,
    and that is wrong on the output SteamCMD actually prints: every depot
    lists its manifests per branch (`"manifests" { "public" { "gid" … } }`)
    long before the `branches` block, so the first `"public"` was a depot's,
    and the first `"buildid"` after it belonged to whichever branch Steam
    happened to list first in `branches` — which is Steam's order, not
    alphabetical, and not always `public`. Read that way, a beta branch's id
    stood in for the public build and the check reported an update that did
    not exist, or hid one that did.
    """
    idx = app_info_text.find('"branches"')
    if idx == -1:
        return {}
    block = _block_after(app_info_text, idx + len('"branches"'))
    if block is None:
        return {}
    out: dict[str, str] = {}
    for m in _BRANCH_ENTRY_RE.finditer(block):
        build = _BUILDID_RE.search(m.group(2))
        if build:
            out[m.group(1)] = build.group(1)
    return out


def parse_latest_buildid(app_info_text: str, branch: str = "") -> str | None:
    """The latest build id of `branch` (the `public` branch when empty) from
    `steamcmd +app_info_print` output, or None when the dump doesn't say.

    None rather than a guess: an output with no `branches` block is a cut-off
    or failed dump, and "don't know" is the answer that keeps a scheduled
    update from acting on it (the auto-update loop fails closed).
    """
    return parse_branch_buildids(app_info_text).get(branch or "public")


def appinfo_cache_paths(steamcmd: str | Path) -> list[Path]:
    """Where this SteamCMD may keep its `appinfo.vdf`. Pure.

    SteamCMD's own directory first (the Windows layout, and the Linux tarball
    run in place), then the Linux homes SteamCMD writes into when it is run
    from elsewhere — `~/Steam` is where a `steamcmd.sh` without a
    `force_install_dir` puts *everything*, and the Debian package keeps its
    copy under `~/.steam/steamcmd`. The Steam *client's* directories
    (`~/.local/share/Steam`, `~/.steam/steam`) are deliberately not here: that
    cache belongs to a different program, and palctl has no business in it.
    """
    exe = Path(steamcmd)
    home = Path.home()
    candidates = [
        exe.parent / APPINFO_CACHE,
        home / "Steam" / APPINFO_CACHE,
        home / ".steam" / APPINFO_CACHE,
        home / ".steam" / "steamcmd" / APPINFO_CACHE,
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for p in candidates:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def clear_appinfo_cache(steamcmd: str | Path) -> list[Path]:
    """Delete SteamCMD's app-info cache so the next `app_info_print` asks
    Steam instead of answering from disk. Returns what was removed. Never
    raises — a cache that won't delete just means a check that may be stale,
    which is the behaviour this used to have on every check.

    This is the fix for the update check that never fired. SteamCMD keeps the
    metadata `app_info_print` reads in `appcache/appinfo.vdf`, and
    `+app_info_update 1` — the documented "force a refresh" — does not
    reliably refresh it: a well-known SteamCMD quirk, and the reason every
    server manager that checks builds this way (LinuxGSM, for one) deletes the
    file first. Left in place, the print reports the build id SteamCMD cached
    the last time it ran — which, for a SteamCMD that last ran to *install*
    the current build, is the current build. Installed equals latest, no
    update is ever detected, and the first anyone hears of a patch is players
    being refused with a version mismatch. SteamCMD rebuilds the cache on the
    next run; there is nothing in it worth keeping.
    """
    removed: list[Path] = []
    for path in appinfo_cache_paths(steamcmd):
        try:
            if path.is_file():
                path.unlink()
                removed.append(path)
        except OSError as e:
            _log.warning("couldn't clear SteamCMD's app-info cache at %s: %s", path, e)
    return removed


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


def app_info_command(steamcmd: str | Path, app_id: str = APP_ID) -> list[str]:
    """The SteamCMD argv for "what is the latest build of this app?".

    An anonymous login, a forced metadata refresh, a dump of the app's info as
    Valve KeyValues, and quit. Nothing here touches the install or asks for a
    Steam account — see STEAM_USER.
    """
    return [
        str(steamcmd), "+login", STEAM_USER,
        "+app_info_update", "1", "+app_info_print", str(app_id), "+quit",
    ]


async def _run_capture_async(
    cmd: list[str], *, stall_timeout: float, total_timeout: float
) -> str | None:
    """Run a SteamCMD command line and return everything it printed.

    None when it couldn't be started, printed nothing for `stall_timeout`
    seconds, or ran for longer than `total_timeout` altogether — killed, tree
    and all, in both of the last two. Reads in chunks rather than lines so a
    dump line longer than asyncio's stream limit can't turn into an exception.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    except OSError:
        return None
    assert proc.stdout is not None
    chunks: list[bytes] = []
    deadline = time.monotonic() + total_timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                await _kill_async(proc)
                return None
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.read(65536), timeout=min(stall_timeout, left)
                )
            except TimeoutError:
                await _kill_async(proc)
                return None
            if not raw:
                break
            chunks.append(raw)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_REAP_SECONDS)
        return b"".join(chunks).decode(errors="replace")
    except asyncio.CancelledError:
        await _kill_async(proc)
        raise


async def latest_buildid(
    steamcmd: str | Path,
    app_id: str = APP_ID,
    *,
    branch: str = "",
    timeout: float = STEAMCMD_META_SECONDS,
    total_timeout: float = STEAMCMD_META_TOTAL_SECONDS,
    clear_cache: bool = True,
) -> str | None:
    """Ask Steam for the latest build id of `branch` (public by default).
    Best-effort; None on any failure — and "don't know" is what every caller
    wants on a failure, because guessing "current" hides a patch and guessing
    "behind" takes a server down for nothing.

    Anonymous, always: the dedicated server is served to anonymous logins, so
    no Steam account, password or Steam Guard code is involved (STEAM_USER).

    The stale-cache trap is handled first (clear_appinfo_cache) — without that
    this asked SteamCMD and SteamCMD answered from disk, so a server that had
    just been updated by palctl could never learn about the next patch.

    Bounded two ways: `timeout` is how long SteamCMD may go without printing
    anything (a hung Steam connection), `total_timeout` a ceiling on the whole
    run. The distinction matters on Windows, where SteamCMD's first run
    downloads a new copy of itself before it gets to the question — chatty
    and legitimately slow on a bad line, and a plain overall cap short enough
    to catch a hang killed it mid-bootstrap. Either way the process is killed
    and the answer is "don't know", never a parked child that stops the
    check loop from ticking again.
    """
    if clear_cache:
        await asyncio.to_thread(clear_appinfo_cache, steamcmd)
    out = await _run_capture_async(
        app_info_command(steamcmd, app_id),
        stall_timeout=timeout, total_timeout=total_timeout,
    )
    if out is None:
        return None
    return parse_latest_buildid(out, branch)


def update_command(
    steamcmd: str | Path,
    install_dir: str | Path,
    app_id: str = APP_ID,
    *,
    validate: bool = True,
    username: str = STEAM_USER,
    branch: str = "",
    beta_password: str = "",
) -> list[str]:
    """
    Build the SteamCMD argv.

    ``+force_install_dir`` MUST come before ``+login`` / ``+app_update`` — put it
    after and SteamCMD silently ignores it and installs into its own directory,
    which is the single most common "why did it download to the wrong place"
    mistake.

    ``username`` is the anonymous login unless a caller says otherwise, and no
    caller in palctl does: the dedicated server downloads anonymously, and a
    named login is what makes an unattended update stall on a Steam Guard
    prompt.

    ``branch`` selects a Steam beta branch (``-beta <name>``), with
    ``beta_password`` for a branch that needs one. Both are arguments *to*
    ``app_update``, so they go after the app id and before ``validate``.

    This is what a server stays on when Pocketpair ships a build you don't want
    yet: pointing at a named branch holds the install there instead of taking
    whatever `public` is serving today. It is not a full version pin — pinning
    to an exact depot manifest needs ``download_depot`` or DepotDownloader
    rather than ``app_update``, and that is a larger change; see
    docs/improvement-plan.md.
    """
    args = [
        str(steamcmd),
        "+force_install_dir", str(install_dir),
        "+login", username,
        "+app_update", str(app_id),
    ]
    if branch:
        args += ["-beta", branch]
        if beta_password:
            args += ["-betapassword", beta_password]
    if validate:
        args.append("validate")
    args.append("+quit")
    return args


def extract_steamcmd(archive_path: Path, dest_dir: Path) -> Path:
    """Unpack a steamcmd archive (.zip on Windows, .tar.gz on Linux) and return
    the path to the steamcmd binary."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if str(archive_path).endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path) as t:
            try:
                t.extractall(dest_dir, filter="data")  # py3.12+/3.11.4+: safe extract
            except TypeError:
                t.extractall(dest_dir)
    else:
        with zipfile.ZipFile(archive_path) as z:
            z.extractall(dest_dir)

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


def backup_file(path: Path, *, dest_dir: Path | None = None) -> Path | None:
    """
    Timestamped side copy of a single file. Used to guard PalWorldSettings.ini
    across a ``validate`` — a plain copy, not a parse, so it works even when the
    ini is blank or malformed.

    Bounded, and — with ``dest_dir`` — out of the way. This runs on every
    update, and the ini's own writers take a copy each as well, so without
    retention they pile up several per update with nothing clearing them out
    (see inifile.BACKUP_RETAIN). ``dest_dir`` matters more: PalWorldSettings.ini
    lives inside the install SteamCMD is about to rewrite, so the default
    sibling copy is the one thing that makes a bad update undoable, stored in
    the blast radius of the thing it protects. The update path passes palctl's
    own config directory.
    """
    if not path.exists():
        return None
    from . import inifile

    return inifile.timestamped_backup(path, dest_dir=dest_dir)


def run_update(
    steamcmd: str | Path,
    install_dir: str | Path,
    *,
    app_id: str = APP_ID,
    validate: bool = True,
    on_line: LineSink | None = None,
    stall_timeout: float = STEAMCMD_STALL_SECONDS,
    branch: str = "",
    beta_password: str = "",
) -> int:
    """
    Run SteamCMD to completion, streaming stdout lines to ``on_line``. Returns
    the exit code. Blocking — call it off any UI thread (the GUI does).

    Raises :class:`SteamCmdStalled` if SteamCMD prints nothing for
    ``stall_timeout`` seconds. Without it the reading loop below blocks forever
    on a wedged SteamCMD, and the setup wizard sits on "Installing the server…"
    with no way forward but killing the app.
    """
    cmd = update_command(
        steamcmd, install_dir, app_id, validate=validate,
        branch=branch, beta_password=beta_password,
    )
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
    branch: str = "",
    beta_password: str = "",
) -> int:
    """Async twin of :func:`run_update`, for the daemon's event loop.

    Raises :class:`SteamCmdStalled` if SteamCMD goes ``stall_timeout`` seconds
    without printing a line — see the constant for why silence, not total
    runtime, is the thing worth timing out on, and what it costs when nothing
    does. The caller's ``finally`` still restores the ini and restarts the
    server, so a stall lands in the same place as any other failed update
    instead of parking the daemon.
    """
    cmd = update_command(
        steamcmd, install_dir, app_id, validate=validate,
        branch=branch, beta_password=beta_password,
    )
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
