"""
Pre-flight checks — catch the boring, common reasons a first run fails before
they turn into a half-installed server and a confused person.

The two failures that eat the most time for non-technical hosts are "the
download filled the disk" and "PalServer.exe just won't start" (nearly always a
missing Visual C++ runtime). We check for those, plus admin rights (needed to
register Windows services) and whether the REST port is already taken.

The disk and port checks are platform-neutral and unit tested. The admin and
VC++ checks report "unknown" (ok is None) anywhere that isn't Windows, so the
wizard can call them unconditionally without special-casing the OS.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Palworld's dedicated server, a world, and a couple of backups. Generous on
# purpose — running out mid-download is exactly the failure we're preventing.
DEFAULT_NEED_GB = 10.0

# Microsoft's evergreen link to the latest VC++ x64 redistributable.
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

_ONE_GB = 1_073_741_824


@dataclass
class Check:
    name: str
    ok: bool | None  # True pass, False fail, None couldn't determine / N/A
    detail: str
    fix: str = ""

    @property
    def icon(self) -> str:
        if self.ok is True:
            return "✓"
        if self.ok is False:
            return "❌"
        return "⚠️"


def _existing_ancestor(path: Path) -> Path:
    """Nearest existing directory at or above `path` — the server root may not
    exist yet, but its drive does."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def check_disk_space(server_root: str | Path, need_gb: float = DEFAULT_NEED_GB) -> Check:
    try:
        base = _existing_ancestor(Path(server_root))
        free_gb = shutil.disk_usage(base).free / _ONE_GB
    except OSError as e:
        return Check("Disk space", None, f"couldn't check: {e}")

    if free_gb >= need_gb:
        return Check("Disk space", True, f"{free_gb:.0f} GB free")
    return Check(
        "Disk space", False,
        f"only {free_gb:.0f} GB free, need ~{need_gb:.0f} GB",
        fix="Free up space, or install the server to a drive that has room.",
    )


def _palworld_server_running() -> bool:
    """Best-effort: is a Palworld dedicated server process up? Lets us tell an
    *expected* REST-port holder (the server palctl will manage) apart from a
    genuine conflict."""
    try:
        from . import procs

        return bool(procs.shipping_processes())
    except Exception:
        return False


def check_port_free(port: int, host: str = "127.0.0.1") -> Check:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return Check(f"Port {port} free", True, "available")
    except OSError:
        if _palworld_server_running():
            # The most common adoption path: palctl is pointed at a server that's
            # already running, so it legitimately holds the REST port. A red ✗
            # telling the user to change the port would break their working
            # config — RESTAPIPort *should* match it.
            return Check(
                f"Port {port} free", True,
                "in use by your running Palworld server — expected; palctl "
                "will manage it",
            )
        return Check(
            f"Port {port} free", False, f"{host}:{port} is already in use",
            fix="Another program (maybe a server already running) holds it. "
                "Pick a different REST API port, or stop that program.",
        )
    finally:
        s.close()


def check_admin() -> Check:
    try:
        import ctypes

        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return Check("Administrator", None, "not applicable on this OS")

    if is_admin:
        return Check("Administrator", True, "running elevated")
    return Check(
        "Administrator", False, "not elevated",
        fix="Registering Windows services needs admin rights. Close palctl and "
            "re-launch it with right-click → Run as administrator.",
    )


def check_vcredist() -> Check:
    """The Palworld server needs the Visual C++ x64 runtime; missing it is the
    classic 'PalServer.exe silently refuses to start'."""
    try:
        import winreg
    except ImportError:
        return Check("Visual C++ runtime", None, "not applicable on this OS")

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        ) as key:
            installed, _ = winreg.QueryValueEx(key, "Installed")
        if installed:
            return Check("Visual C++ runtime", True, "installed")
    except OSError:
        pass
    return Check(
        "Visual C++ runtime", False, "not found",
        fix="Install the Microsoft Visual C++ x64 Redistributable (the wizard "
            "can do it) — without it the server won't launch.",
    )


def check_single_server_instance() -> Check:
    """Flag more than one running Palworld server process. Two instances (almost
    always a leftover second Windows service) fight over the game and REST ports,
    so the REST API never answers and the memory watchdog can't tell which
    process to watch — a confusing failure that looks like 'the server just
    won't respond'."""
    try:
        from . import procs

        running = procs.shipping_processes()
    except Exception as e:  # psutil missing (minimal-deps) or an odd platform
        return Check("Single server instance", None, f"couldn't check: {e}")

    n = len(running)
    if n == 0:
        return Check("Single server instance", True, "no server running yet")
    if n == 1:
        return Check("Single server instance", True, "one server process")

    pids = ", ".join(str(p.pid) for p in running)
    return Check(
        "Single server instance", False,
        f"{n} Palworld server processes are running (PIDs {pids})",
        fix="Two servers are running at once — they collide on ports 8211 and "
            "8212, so the REST API won't answer. Stop and disable the extra "
            "Windows service (services.msc), leaving only the one palctl manages.",
    )


def run_all(
    server_root: str | Path,
    api_port: int,
    *,
    need_install: bool = True,
    need_admin: bool = True,
) -> list[Check]:
    """The checks relevant to what the user is about to do."""
    checks: list[Check] = []
    if need_install:
        checks.append(check_disk_space(server_root))
        checks.append(check_vcredist())
    checks.append(check_port_free(api_port))
    checks.append(check_single_server_instance())
    if need_admin:
        checks.append(check_admin())
    return checks


def _authenticode_status(path: Path) -> str:
    """The Windows Authenticode signature status of `path` ('Valid',
    'HashMismatch', 'NotSigned', …), lower-cased. Returns '' when it can't be
    determined — not on Windows, no PowerShell, or any error — which callers
    treat as 'unknown', never as 'bad'."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status",
            ],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return ""


def _signature_is_tampered(status: str) -> bool:
    """Whether to refuse an installer based on its signature status. Fail CLOSED
    only on a positive tamper signal (the bytes don't match a signature that
    should be there); fail OPEN on anything else — a machine that simply can't
    verify (missing PowerShell, an incomplete cert store → NotTrusted, offline)
    must still be able to install the runtime it needs. The evergreen aka.ms URL
    can't be hash-pinned like NSSM, so the Microsoft signature is the integrity
    anchor we have."""
    return status.strip().lower() in {"hashmismatch", "notsigned"}


def install_vcredist(on_line=None) -> int:
    """Download and silently install the VC++ x64 runtime. Windows-only; returns
    the installer's exit code."""
    with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        if on_line:
            on_line("Downloading the Visual C++ runtime…")
        # Timeout so a hung CDN can't stall the wizard indefinitely.
        with urllib.request.urlopen(VCREDIST_URL, timeout=120) as resp, path.open("wb") as f:
            shutil.copyfileobj(resp, f)
        # We can't pin a checksum (aka.ms is evergreen — Microsoft reissues this
        # exe every servicing update), so verify its Authenticode signature
        # before running it: refuse a positively tampered installer, but proceed
        # when the signature just can't be checked (see _signature_is_tampered).
        status = _authenticode_status(path)
        if _signature_is_tampered(status):
            raise RuntimeError(
                "The downloaded Visual C++ runtime failed Authenticode "
                f"verification (signature status: {status}). Refusing to run a "
                "possibly tampered installer — check your network/proxy for "
                "interference and try again."
            )
        if on_line:
            on_line("Installing the Visual C++ runtime…")
        return subprocess.run(
            [str(path), "/install", "/quiet", "/norestart"]
        ).returncode
    finally:
        path.unlink(missing_ok=True)
