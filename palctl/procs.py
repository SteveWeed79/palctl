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


# psutil.Process.cpu_percent(interval=None) is a delta against the *same object's*
# previous call, so the first call on any given Process returns a meaningless 0.0.
# proc_stats() is polled repeatedly (daemon loop, /state), and a fresh
# find_process() each time hands cpu_percent a brand-new object every call — it
# never gets a prior sample, so CPU reads 0.0 forever. We cache the Process by pid
# and reuse it, letting cpu_percent measure across our own poll interval instead.
_tracked: psutil.Process | None = None


def _tracked_process() -> psutil.Process | None:
    """find_process(), but return the *same* psutil.Process object across calls
    for a given pid so cpu_percent(interval=None) has a baseline to diff against.
    A restarted server (new pid) or a stopped one transparently rebinds/clears."""
    global _tracked
    found = find_process()
    if found is None:
        _tracked = None
        return None
    if _tracked is not None and _tracked.pid == found.pid:
        return _tracked
    _tracked = found
    return found


def proc_stats() -> ProcStats | None:
    p = _tracked_process()
    if p is None:
        return None
    try:
        with p.oneshot():
            mem = p.memory_info().rss / 1_048_576
            # Raw psutil cpu_percent sums across cores (can exceed 100% on an
            # N-core box). Normalize to 0-100% of the whole machine — that's what
            # a "CPU" status tile reads as.
            cores = psutil.cpu_count() or 1
            cpu = p.cpu_percent(interval=None) / cores
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


def _run_capture(cmd: list[str], timeout: float = 30.0) -> str:
    """Run a service-control command and capture stdout. Bounded and
    non-raising: a hung sc.exe/systemctl (or a Linux box with no systemd at
    all — FileNotFoundError) must degrade to UNKNOWN, not wedge the daemon's
    event loop or 500 the control API."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, creationflags=_NO_WINDOW,
            timeout=timeout,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


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


# The async wrappers run every blocking subprocess call in a worker thread:
# these coroutines execute on the daemon's single event loop, and a slow
# sc.exe/systemctl on the loop thread stalls polling, the watchdog, and the
# control API all at once.


async def _wait_for(service_name: str, target: str, timeout: float = 120.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await asyncio.to_thread(service_state, service_name) == target:
            return True
        await asyncio.sleep(2)
    return False


async def start_service(service_name: str) -> bool:
    if await asyncio.to_thread(service_state, service_name) == "RUNNING":
        return True
    await asyncio.to_thread(_run_capture, _action_command("start", service_name))
    return await _wait_for(service_name, "RUNNING")


async def stop_service(service_name: str) -> bool:
    if await asyncio.to_thread(service_state, service_name) == "STOPPED":
        return True
    await asyncio.to_thread(_run_capture, _action_command("stop", service_name))
    return await _wait_for(service_name, "STOPPED")


async def wait_stopped(service_name: str, timeout: float = 60.0) -> bool:
    """Wait for the service manager to report STOPPED, without re-issuing a stop.
    Used by the force-kill escalation: once the process is dead, the SCM/systemd
    catches up to STOPPED on its own."""
    return await _wait_for(service_name, "STOPPED", timeout)


# ---------------- force-kill escalation ----------------
#
# A truly wedged PalServer-Win64-Shipping.exe — the classic memory-leak hang —
# can sit in STOP_PENDING forever, ignoring the SCM/systemd stop. When that
# happens for an unattended caller (watchdog, auto-recovery, scheduled restart),
# we escalate to signalling the process directly by the PID find_process()
# already knows: terminate() first (SIGTERM on POSIX, so a healthy-enough server
# can still flush and exit; on Windows this is TerminateProcess), then kill()
# (SIGKILL) if it survives.


def _signal_and_wait(proc: psutil.Process, *, hard: bool, timeout: float) -> bool:
    """Send terminate()/kill() to a process and wait up to `timeout` for it to
    actually exit. Returns True if it's gone. Never raises: a process that
    vanished (on its own or from the signal) counts as success; a signal we
    can't send (AccessDenied) falls through to the wait, which reports the truth."""
    try:
        proc.kill() if hard else proc.terminate()
    except psutil.NoSuchProcess:
        return True  # already gone — the goal
    except psutil.Error:
        pass  # couldn't signal (e.g. AccessDenied); the wait below tells the truth
    try:
        proc.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        pass
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        pass
    try:
        return not proc.is_running()
    except psutil.Error:
        return False


async def terminate_process(proc: psutil.Process, timeout: float = 10.0) -> bool:
    """Graceful stop: terminate() the process and wait for it to go."""
    return await asyncio.to_thread(_signal_and_wait, proc, hard=False, timeout=timeout)


async def kill_process(proc: psutil.Process, timeout: float = 10.0) -> bool:
    """Hard stop: kill() the process and wait for it to go."""
    return await asyncio.to_thread(_signal_and_wait, proc, hard=True, timeout=timeout)
