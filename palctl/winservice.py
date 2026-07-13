"""
Register Windows services with NSSM.

Two services actually have to exist for palctl to do its job, and the README
only ever documented one of them:

  * **PalServer** — the game server. It is not service-aware (it's a plain
    console exe), so Windows can't supervise it directly; it needs a shim, and
    NSSM is the community-standard shim.
  * **palctl-daemon** — the watchdog / scheduler / Discord bot. Same story.

The old setup was hand-typed ``nssm install ...`` lines in the README, which is
exactly where non-technical hosts gave up. This wraps NSSM so the installer and
the first-run wizard can register both services with nobody opening a terminal.

Windows-only in practice. The command builders and archive layout logic are
pure and unit tested anywhere; the runners no-op / raise cleanly off Windows.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Stable NSSM release. The archive ships win32/ and win64/ subfolders.
NSSM_URL = "https://nssm.cc/release/nssm-2.24.zip"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def nssm_exe_in(extract_dir: Path, *, win64: bool = True) -> Path | None:
    """
    Locate nssm.exe inside an extracted NSSM archive, preferring the matching
    architecture. Falls back to any nssm.exe if the arch folder isn't present.
    """
    want = "win64" if win64 else "win32"
    matches = [p for p in extract_dir.rglob("nssm.exe") if p.parent.name.lower() == want]
    if matches:
        return matches[0]
    return next(iter(extract_dir.rglob("nssm.exe")), None)


def install_commands(
    nssm: str | Path,
    name: str,
    exe: str | Path,
    args: str = "",
    app_dir: str | Path | None = None,
) -> list[list[str]]:
    """
    The ordered NSSM calls that create and configure a service. Pure — the
    runner just executes what this returns, which is also what makes it testable.
    """
    cmds: list[list[str]] = [[str(nssm), "install", name, str(exe)]]
    if args:
        cmds.append([str(nssm), "set", name, "AppParameters", args])
    if app_dir:
        cmds.append([str(nssm), "set", name, "AppDirectory", str(app_dir)])
    cmds.append([str(nssm), "set", name, "Start", "SERVICE_AUTO_START"])
    return cmds


# ---------------- runners (Windows) ----------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, creationflags=_NO_WINDOW
    )


def service_exists(name: str) -> bool:
    from . import procs

    return procs.service_state(name) != "UNKNOWN"


def install_service(
    nssm: str | Path,
    name: str,
    exe: str | Path,
    args: str = "",
    app_dir: str | Path | None = None,
    *,
    start: bool = True,
) -> None:
    """Create/configure the service, then optionally start it."""
    for cmd in install_commands(nssm, name, exe, args, app_dir):
        _run(cmd)
    if start:
        _run([str(nssm), "start", name])


def remove_service(nssm: str | Path, name: str) -> None:
    _run([str(nssm), "stop", name])
    _run([str(nssm), "remove", name, "confirm"])


def ensure_nssm(cache_dir: Path, *, url: str = NSSM_URL, win64: bool = True) -> Path:
    """
    Return a usable nssm.exe, downloading and caching it under ``cache_dir`` the
    first time. Subsequent calls reuse the cached copy.
    """
    cached = cache_dir / "nssm.exe"
    if cached.exists():
        return cached

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with urllib.request.urlopen(url) as resp, tmp_path.open("wb") as f:
            shutil.copyfileobj(resp, f)
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(tmp_path) as z:
                z.extractall(td)
            found = nssm_exe_in(Path(td), win64=win64)
            if found is None:
                raise FileNotFoundError("nssm.exe not found in the NSSM archive.")
            shutil.copy2(found, cached)
        return cached
    finally:
        tmp_path.unlink(missing_ok=True)
