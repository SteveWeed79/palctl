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
from dataclasses import dataclass
from pathlib import Path

# Palworld's dedicated server, a world, and a couple of backups. Generous on
# purpose — running out mid-download is exactly the failure we're preventing.
DEFAULT_NEED_GB = 10.0

# Microsoft's evergreen link to the latest VC++ x64 redistributable.
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
# Cap on the silent vcredist install — see ensure_vcredist for why.
VCREDIST_INSTALL_TIMEOUT = 300.0

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


def is_elevated() -> bool | None:
    """Whether this process has administrator rights. ``True``/``False`` on
    Windows; ``None`` off Windows or when it genuinely can't be determined.
    Callers must treat ``None`` as "can't tell", never as a hard block — so a
    non-Windows box (or a mocked-platform test where ``ctypes.windll`` isn't
    real) is never wrongly refused a service install."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return None


def check_admin() -> Check:
    is_admin = is_elevated()
    if is_admin is None:
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


def boot_ownership_verdict(start_mode: str | None, daemon_startup: str) -> Check:
    """Who — if anyone — starts the game server after a reboot. Pure, so the
    whole truth table is testable off Windows.

    palctl registers the game service **Manual** when it runs as a boot service,
    because the daemon then starts the server itself and an intentional Stop
    survives a restart (setup_flow.server_service_start_mode). Manual is
    therefore correct in exactly one configuration, and silently fatal in the
    others: nothing is left to start the server, on any reboot, forever. That is
    the state anyone who ran `palctl-daemon uninstall-service` before the
    handback existed is sitting in right now, and it produces no error anywhere
    — the daemon whose absence causes it is not running to complain.
    """
    if start_mode is None:
        return Check("Server boot start", None, "couldn't read the service's start type")
    if start_mode == "Disabled":
        # Somebody turned it off deliberately. Not palctl's to overrule — but
        # worth saying out loud, since "my server won't start" and "I disabled
        # the service months ago" are rarely connected by the same person.
        return Check(
            "Server boot start", None,
            "the server service is Disabled, so nothing will start it",
            fix="If that wasn't deliberate, set it to Automatic in services.msc.",
        )
    if start_mode != "Manual":
        return Check("Server boot start", True, "Windows starts the server at boot")
    if daemon_startup == "service":
        return Check(
            "Server boot start", True,
            "palctl's daemon starts the server at boot (service is Manual, by design)",
        )
    where = {
        "login": "palctl only starts when you sign in",
        "none": "palctl doesn't start in the background at all",
    }.get(daemon_startup, "palctl isn't registered to start at boot")
    return Check(
        "Server boot start", False,
        f"the server service is set to Manual and {where}",
        fix="Nothing will start your server after a reboot. palctl set the "
            "service to Manual back when it ran as a boot service and took that "
            "job on itself. Either re-run setup with background startup set to "
            "\"Windows service\", or hand the job back to Windows:\n"
            "    sc config <your server service> start= auto",
    )


def check_server_boot_ownership(service_name: str, daemon_startup: str) -> Check:
    """Is anything actually going to start the game server after a reboot?"""
    if not sys.platform.startswith("win") or not service_name:
        return Check("Server boot start", None, "not applicable on this platform")
    try:
        from . import winservice

        if not winservice.service_exists(service_name):
            return Check(
                "Server boot start", None,
                f"no '{service_name}' service registered yet",
            )
        mode = winservice.start_mode_of(service_name)
    except Exception as e:
        return Check("Server boot start", None, f"couldn't check: {e}")
    return boot_ownership_verdict(mode, daemon_startup)


def check_backup_volume(server_root: str | Path, backup_root: str | Path) -> Check:
    """Whether backups land on a different disk from the server.

    Backups on the server's own volume cover a bad update, a botched restore and
    a corrupt save — but not the disk, which is the failure the word "backup"
    makes people assume they are covered for. Never a hard failure: one disk is
    a perfectly reasonable place to start, and the off-site mirror is the real
    answer. It just should not be a silent default.
    """
    from . import backups as _backups

    same = _backups.same_volume(Path(server_root), Path(backup_root))
    if same is None:
        return Check("Backup location", None, "couldn't tell which disk the backups are on")
    if not same:
        return Check("Backup location", True, "backups are on a different disk from the server")
    return Check(
        "Backup location",
        None,
        "backups are on the same disk as the server — that disk failing takes both",
        fix=(
            "Point the backup folder at another drive, or turn on off-site "
            "backups in Config so a second copy leaves this machine."
        ),
    )


def run_all(
    server_root: str | Path,
    api_port: int,
    *,
    need_install: bool = True,
    need_admin: bool = True,
    service_name: str = "",
    daemon_startup: str = "",
    backup_root: str | Path = "",
) -> list[Check]:
    """The checks relevant to what the user is about to do.

    `service_name`/`daemon_startup` come from the config; without them the
    boot-ownership check is skipped rather than guessed at, so older callers
    keep working unchanged. `backup_root` is the same deal for the
    backup-volume check."""
    checks: list[Check] = []
    if need_install:
        checks.append(check_disk_space(server_root))
        checks.append(check_vcredist())
    checks.append(check_port_free(api_port))
    checks.append(check_single_server_instance())
    if service_name:
        checks.append(check_server_boot_ownership(service_name, daemon_startup))
    if backup_root:
        checks.append(check_backup_volume(server_root, backup_root))
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
    can't be hash-pinned like the service wrapper, so the Microsoft signature is the integrity
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
        from . import fetch

        # Timeout so a hung CDN can't stall the wizard indefinitely; fetch
        # retries verification against certifi when the system trust fails
        # (AV HTTPS-scanning, broken cert store).
        with fetch.open_url(VCREDIST_URL, timeout=120) as resp, path.open("wb") as f:
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
        try:
            # Bounded: a quiet MSI that blocks on another installer holding the
            # Windows Installer mutex would otherwise park the setup wizard on
            # this line with no output and no way out but killing the app. Five
            # minutes is far more than this package needs.
            return subprocess.run(
                [str(path), "/install", "/quiet", "/norestart"],
                timeout=VCREDIST_INSTALL_TIMEOUT,
            ).returncode
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "The Visual C++ runtime installer did not finish within "
                f"{VCREDIST_INSTALL_TIMEOUT / 60:.0f} minutes. Another install may "
                "be running — finish or cancel it (check Windows Update too), then "
                "retry. You can also install 'Microsoft Visual C++ Redistributable "
                "(x64)' by hand and re-run setup."
            ) from None
    finally:
        path.unlink(missing_ok=True)
