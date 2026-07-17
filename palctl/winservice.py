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

import hashlib
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Stable NSSM release. The archive ships win32/ and win64/ subfolders.
NSSM_URL = "https://nssm.cc/release/nssm-2.24.zip"

# SHA-256 of that exact archive. This is a real hole worth closing: ensure_nssm
# downloads over plain HTTP-with-TLS from a decade-old personal site, unpacks it,
# and the binary is then registered as / used to register a LocalSystem service —
# so a compromised nssm.cc or a MITM on the download is SYSTEM-level code
# execution on the user's box. NSSM 2.24 is from 2014 and will never change,
# which makes a fixed pin both safe and the right fix. Verified two independent
# ways: the value multiple moderated Chocolatey NSSM packages pin for this URL,
# and a from-the-bytes hash of an independently vendored copy of the same zip.
NSSM_SHA256 = "727d1e42275c605e0f04aba98095c38a8e1e46def453cdffce42869428aa6743"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class NssmChecksumError(RuntimeError):
    """The downloaded NSSM archive didn't match the pinned SHA-256. We refuse to
    unpack a service binary we can't vouch for rather than run it as SYSTEM."""


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
    *,
    user: str | None = None,
    password: str | None = None,
    appdata: str | None = None,
) -> list[list[str]]:
    """
    The ordered NSSM calls that create and configure a service. Pure — the
    runner just executes what this returns, which is also what makes it testable.

    The account matters more than it looks: a service defaults to LocalSystem,
    which has its OWN %APPDATA% and Credential Manager — a daemon there reads a
    different config.json and token than the user's GUI, and can't decrypt the
    user's DPAPI secrets at all. So:

      * `user`/`password` set — run the service AS that account (ObjectName).
        Everything just works; this is the recommended mode.
      * else, `appdata` set — stay LocalSystem but point %APPDATA% at the
        installing user's config dir, so at least config, token, and logs are
        shared. Per-user secrets remain unreadable (the daemon falls back to
        the AdminPassword already sitting in PalWorldSettings.ini).
    """
    cmds: list[list[str]] = [[str(nssm), "install", name, str(exe)]]
    # `nssm install` no-ops when the service already exists, freezing the exe
    # path at whatever the first install set — so a re-install could never
    # repair a service registered at the wrong exe (the documented palctl-gui.exe
    # bug). An explicit, idempotent `set Application` always corrects it.
    cmds.append([str(nssm), "set", name, "Application", str(exe)])
    if args:
        cmds.append([str(nssm), "set", name, "AppParameters", args])
    if app_dir:
        cmds.append([str(nssm), "set", name, "AppDirectory", str(app_dir)])
    if user:
        cmds.append([str(nssm), "set", name, "ObjectName", user, password or ""])
    elif appdata:
        cmds.append(
            [str(nssm), "set", name, "AppEnvironmentExtra", f"APPDATA={appdata}"]
        )
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
    user: str | None = None,
    password: str | None = None,
    appdata: str | None = None,
    start: bool = True,
) -> None:
    """Create/configure the service, then optionally start it."""
    for cmd in install_commands(
        nssm, name, exe, args, app_dir, user=user, password=password, appdata=appdata
    ):
        _run(cmd)
    if start:
        _run([str(nssm), "start", name])


def remove_service(nssm: str | Path, name: str) -> None:
    _run([str(nssm), "stop", name])
    _run([str(nssm), "remove", name, "confirm"])


def ensure_nssm(
    cache_dir: Path, *, url: str = NSSM_URL, win64: bool = True, sha256: str = NSSM_SHA256
) -> Path:
    """
    Return a usable nssm.exe, downloading and caching it under ``cache_dir`` the
    first time. Subsequent calls reuse the cached copy. The download is verified
    against the pinned SHA-256 before it is unpacked; pass ``sha256=""`` only to
    deliberately skip that (there is no good reason to in production).
    """
    cached = cache_dir / "nssm.exe"
    if cached.exists():
        return cached

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        # Timeout so a hung/blocked CDN can't stall setup (or an uninstall)
        # indefinitely.
        with urllib.request.urlopen(url, timeout=60) as resp, tmp_path.open("wb") as f:
            shutil.copyfileobj(resp, f)
        # Verify BEFORE unpacking — this binary ends up running as SYSTEM, so a
        # tampered or corrupted download must never reach disk as nssm.exe.
        actual = _sha256_of(tmp_path)
        if sha256 and actual.lower() != sha256.lower():
            raise NssmChecksumError(
                f"NSSM download from {url} failed its checksum: expected "
                f"{sha256}, got {actual}. Refusing to install an unverified "
                "service binary. Check your network/proxy for interference, or "
                "open a palctl issue if nssm.cc has genuinely reissued the "
                "archive."
            )
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
