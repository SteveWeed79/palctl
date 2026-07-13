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
import time
from dataclasses import dataclass

import psutil

PAL_PROCESS_NAMES = ("PalServer-Win64-Shipping.exe", "PalServer.exe")


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


# ---------------- service control ----------------


def _sc(*args: str) -> str:
    return subprocess.run(
        ["sc.exe", *args],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def service_state(service_name: str) -> str:
    out = _sc("query", service_name)
    for state in ("RUNNING", "STOPPED", "START_PENDING", "STOP_PENDING"):
        if state in out:
            return state
    return "UNKNOWN"


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
    _sc("start", service_name)
    return await _wait_for(service_name, "RUNNING")


async def stop_service(service_name: str) -> bool:
    if service_state(service_name) == "STOPPED":
        return True
    _sc("stop", service_name)
    return await _wait_for(service_name, "STOPPED")
