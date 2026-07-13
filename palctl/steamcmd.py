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
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

APP_ID = "2394010"
# Valve's canonical Windows SteamCMD archive.
STEAMCMD_WIN_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"

LineSink = Callable[[str], None]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# SteamCMD prints e.g. "Update state (0x61) downloading, progress: 42.34 (123 / 456)".
_PROGRESS_RE = re.compile(r"progress:\s*([\d.]+)")


def parse_progress(line: str) -> float | None:
    """Pull the download percent out of a SteamCMD progress line, or None."""
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


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


def extract_steamcmd(zip_path: Path, dest_dir: Path) -> Path:
    """Unzip a downloaded steamcmd archive and return the path to steamcmd.exe."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest_dir)

    exe = dest_dir / "steamcmd.exe"
    if exe.exists():
        return exe
    nested = next(iter(dest_dir.rglob("steamcmd.exe")), None)
    if nested is None:
        raise FileNotFoundError("steamcmd.exe not found in the downloaded archive.")
    return nested


def download_steamcmd(dest_dir: Path, *, url: str = STEAMCMD_WIN_URL) -> Path:
    """Download and unpack SteamCMD into ``dest_dir``. Returns steamcmd.exe."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with urllib.request.urlopen(url) as resp, tmp_path.open("wb") as f:
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
