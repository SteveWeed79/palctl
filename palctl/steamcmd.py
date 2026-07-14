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
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
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


def installed_buildid(server_root: str | Path, app_id: str = APP_ID) -> str | None:
    """Read the installed build id from the server's Steam manifest, if present."""
    acf = Path(server_root) / "steamapps" / f"appmanifest_{app_id}.acf"
    try:
        return parse_installed_buildid(acf.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return None


async def latest_buildid(steamcmd: str | Path, app_id: str = APP_ID) -> str | None:
    """Ask Steam for the latest public build id. Best-effort; None on any failure."""
    cmd = [
        str(steamcmd), "+login", "anonymous",
        "+app_info_update", "1", "+app_info_print", str(app_id), "+quit",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
        out, _ = await proc.communicate()
    except OSError:
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
        # Timeout so a hung CDN doesn't stall setup forever. Integrity relies on
        # the TLS connection to Valve's steamcdn host.
        with urllib.request.urlopen(url, timeout=120) as resp, tmp_path.open("wb") as f:
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
) -> int:
    """
    Run SteamCMD to completion, streaming stdout lines to ``on_line``. Returns
    the exit code. Blocking — call it off any UI thread (the GUI does).
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
        for line in proc.stdout:
            if on_line:
                on_line(line.rstrip())
        return proc.wait()


async def run_update_async(
    steamcmd: str | Path,
    install_dir: str | Path,
    *,
    app_id: str = APP_ID,
    validate: bool = True,
    on_line: LineSink | None = None,
) -> int:
    """Async twin of :func:`run_update`, for the daemon's event loop."""
    cmd = update_command(steamcmd, install_dir, app_id, validate=validate)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=_NO_WINDOW,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        if on_line:
            on_line(raw.decode(errors="replace").rstrip())
    return await proc.wait()
