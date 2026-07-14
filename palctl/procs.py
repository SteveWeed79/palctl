"""
Windows service control + real process metrics.

This is the bit a web panel or a remote SaaS bridge structurally cannot do: we
are ON the box, so we can read PalServer.exe's actual RSS and CPU from the OS.

The REST API's /metrics tells you the server's FPS. It does NOT tell you the
process is sitting at 14GB and about to fall over. Only psutil does.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass

import psutil

IS_WINDOWS = sys.platform.startswith("win")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Shipping binaries first (Windows then Linux), then the thin launchers — so the
# leak watchdog watches the process that actually holds the memory, on either OS.
PAL_PROCESS_NAMES = (
    "PalServer-Win64-Shipping.exe",
    "PalServer-Linux-Shipping",
    "PalServer.exe",
    "PalServer.sh",
)


@dataclass(frozen=True)
class ProcStats:
    pid: int
    memory_mb: float
    cpu_percent: float
    threads: int
    uptime_seconds: float


def find_process() -> psutil.Process | None:
    """
    Prefer the Shipping binary — PalServer.exe is a thin launcher that spawns it
    and holds almost no memory. Watching the launcher would mean the leak
    watchdog never fires, which is the whole point of this module.
    """
    candidates: dict[str, psutil.Process] = {}
    for p in psutil.process_iter(["name"]):
        name = p.info.get("name") or ""
        if name in PAL_PROCESS_NAMES:
            candidates[name] = p

    for name in PAL_PROCESS_NAMES:  # ordered: Shipping first
        if name in candidates:
            return candidates[name]
    return None


# The actual server binaries (one live process per running instance). The thin
# launchers in PAL_PROCESS_NAMES are deliberately excluded: a single healthy
# server shows both a launcher and a Shipping process, so counting launchers
# would double-count. Two Shipping processes == two real server instances.
SHIPPING_PROCESS_NAMES = ("PalServer-Win64-Shipping.exe", "PalServer-Linux-Shipping")


def shipping_processes() -> list[psutil.Process]:
    """Every running Palworld server process. find_process() returns the single
    one the watchdog should watch; this returns them all, so preflight can flag
    two instances fighting over the game (8211) and REST (8212) ports — the
    classic result of a leftover second service."""
    out: list[psutil.Process] = []
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "") in SHIPPING_PROCESS_NAMES:
            out.append(p)
    return out


def proc_stats() -> ProcStats | None:
    p = find_process()
    if p is None:
        return None
    try:
        with p.oneshot():
            mem = p.memory_info().rss / 1_048_576
            cpu = p.cpu_percent(interval=None)
            return ProcStats(
                pid=p.pid,
                memory_mb=mem,
                cpu_percent=cpu,
                threads=p.num_threads(),
                uptime_seconds=max(0.0, time.time() - p.create_time()),
            )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


# ---------------- service control (Windows sc.exe / Linux systemd) ----------------
#
# The rest of palctl only knows service_state / start_service / stop_service; the
# platform difference is confined here. The command builders and output parsers
# are pure so they're testable on any OS.


def _run_capture(cmd: list[str]) -> str:
    return subprocess.run(
        cmd, capture_output=True, text=True, creationflags=_NO_WINDOW
    ).stdout


def _parse_sc_state(out: str) -> str:
    for state in ("RUNNING", "STOPPED", "START_PENDING", "STOP_PENDING"):
        if state in out:
            return state
    return "UNKNOWN"


def _parse_systemctl_state(out: str) -> str:
    return {
        "active": "RUNNING",
        "inactive": "STOPPED",
        "failed": "STOPPED",
        "activating": "START_PENDING",
        "deactivating": "STOP_PENDING",
    }.get(out.strip(), "UNKNOWN")


def _state_command(name: str) -> list[str]:
    return ["sc.exe", "query", name] if IS_WINDOWS else ["systemctl", "is-active", name]


def _action_command(action: str, name: str) -> list[str]:
    """action is 'start' or 'stop'. sc.exe and systemctl happen to share verbs."""
    return ["sc.exe", action, name] if IS_WINDOWS else ["systemctl", action, name]


def service_state(service_name: str) -> str:
    out = _run_capture(_state_command(service_name))
    return _parse_sc_state(out) if IS_WINDOWS else _parse_systemctl_state(out)


async def _wait_for(service_name: str, target: str, timeout: float = 120.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if service_state(service_name) == target:
            return True
        await asyncio.sleep(2)
    return False


async def start_service(service_name: str) -> bool:
    if service_state(service_name) == "RUNNING":
        return True
    _run_capture(_action_command("start", service_name))
    return await _wait_for(service_name, "RUNNING")


async def stop_service(service_name: str) -> bool:
    if service_state(service_name) == "STOPPED":
        return True
    _run_capture(_action_command("stop", service_name))
    return await _wait_for(service_name, "STOPPED")
