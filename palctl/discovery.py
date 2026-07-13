"""
Find the Palworld dedicated server and steamcmd on this machine.

The old Config tab handed you four blank Windows text boxes and a default that
was wrong for most people. But almost everyone installs the server one of two
ways — SteamCMD into ``C:\\steamcmd``, or the Steam client's "Palworld Dedicated
Server" tool into a Steam library — and both leave a trail we can follow: the
Steam registry key, ``libraryfolders.vdf``, and (if it's already running) the
server process's own image path.

Everything here is best-effort and side-effect-free: it reads the registry and
the disk, returns only candidates it could actually verify, and never writes
anything. The platform-neutral parts (vdf parsing, path validation) are unit
tested on any OS; the registry and process lookups simply return nothing when
they're not on Windows.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path

APP_ID = "2394010"  # Palworld Dedicated Server on Steam

# A folder is a Palworld server root if it carries one of these. Default-
# PalWorldSettings.ini is the most reliable — it ships with the server and
# nothing else on disk has that name — so it goes first.
_SERVER_MARKERS: tuple[str, ...] = (
    "DefaultPalWorldSettings.ini",
    "PalServer.exe",
    str(Path("Pal") / "Binaries" / "Win64" / "PalServer-Win64-Shipping.exe"),
)

_LIB_PATH_RE = re.compile(r'"path"\s*"([^"]+)"')

# Where people most commonly end up, checked after the registry-driven guesses.
_COMMON_SERVER_DIRS: tuple[str, ...] = (
    r"C:\steamcmd\steamapps\common\PalServer",
    r"C:\PalServer",
    r"C:\Program Files (x86)\Steam\steamapps\common\PalServer",
    r"C:\Program Files\Steam\steamapps\common\PalServer",
)
_COMMON_STEAMCMD: tuple[str, ...] = (
    r"C:\steamcmd\steamcmd.exe",
    r"C:\SteamCMD\steamcmd.exe",
)


# ---------------- validation (pure) ----------------


def is_server_root(path: Path) -> bool:
    """True if ``path`` looks like an installed Palworld dedicated server."""
    try:
        return path.is_dir() and any((path / m).exists() for m in _SERVER_MARKERS)
    except OSError:
        return False


def is_steamcmd(path: Path) -> bool:
    """True if ``path`` is an existing steamcmd.exe."""
    try:
        return path.is_file() and path.name.lower() == "steamcmd.exe"
    except OSError:
        return False


def parse_library_folders(vdf_text: str) -> list[Path]:
    """
    Pull every library root out of Steam's ``libraryfolders.vdf``.

    It's Valve KeyValues, not JSON; we only need the ``"path" "..."`` lines, and
    those paths are backslash-escaped (``C:\\\\Games\\\\Steam``). A regex is both
    enough and far more robust to Valve's format churn than a full KV parser.
    """
    return [Path(raw.replace("\\\\", "\\")) for raw in _LIB_PATH_RE.findall(vdf_text)]


# ---------------- Windows sources ----------------


def _win_steam_dirs() -> list[Path]:
    """Steam install dir(s) from the registry. Empty off Windows / if absent."""
    dirs: list[Path] = []
    try:
        import winreg  # Windows-only; ImportError everywhere else
    except ImportError:
        return dirs

    for root, key, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(root, key) as k:
                raw, _ = winreg.QueryValueEx(k, value)
        except OSError:
            continue
        if raw:
            dirs.append(Path(raw))
    return _dedup(dirs)


def _steam_library_roots() -> list[Path]:
    """Every Steam library root: the install dir plus each entry in the vdf."""
    roots: list[Path] = []
    for steam in _win_steam_dirs():
        roots.append(steam)
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        roots.extend(parse_library_folders(text))
    return _dedup(roots)


def server_root_from_process() -> Path | None:
    """
    If the server is running, its image path pins the install exactly — no
    guessing. ``PalServer-Win64-Shipping.exe`` lives at ``<root>/Pal/Binaries/
    Win64/``; the thin ``PalServer.exe`` launcher sits at ``<root>/``.
    """
    try:
        from . import procs

        proc = procs.find_process()
        if proc is None:
            return None
        exe = Path(proc.exe())
    except Exception:
        return None

    if exe.name.lower() == "palserver-win64-shipping.exe" and len(exe.parents) >= 4:
        root = exe.parents[3]
    else:
        root = exe.parent
    return root if is_server_root(root) else None


# ---------------- detection (public) ----------------


def detect_server_roots() -> list[Path]:
    """Verified Palworld server roots, best guess first. May be empty."""
    candidates: list[Path] = []

    proc_root = server_root_from_process()
    if proc_root:
        candidates.append(proc_root)

    for lib in _steam_library_roots():
        candidates.append(lib / "steamapps" / "common" / "PalServer")

    candidates.extend(Path(p) for p in _COMMON_SERVER_DIRS)

    return _dedup_valid(candidates, is_server_root)


def detect_steamcmd() -> list[Path]:
    """Verified steamcmd.exe paths, best guess first. May be empty."""
    candidates = [Path(p) for p in _COMMON_STEAMCMD]
    candidates.extend(steam / "steamcmd.exe" for steam in _win_steam_dirs())

    found = shutil.which("steamcmd.exe") or shutil.which("steamcmd")
    if found:
        candidates.append(Path(found))

    return _dedup_valid(candidates, is_steamcmd)


def best_server_root() -> Path | None:
    roots = detect_server_roots()
    return roots[0] if roots else None


def best_steamcmd() -> Path | None:
    hits = detect_steamcmd()
    return hits[0] if hits else None


# ---------------- helpers ----------------


def _dedup(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _dedup_valid(paths: list[Path], predicate: Callable[[Path], bool]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        try:
            key = str(p).lower()
        except (TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        if predicate(p):
            out.append(p)
    return out
