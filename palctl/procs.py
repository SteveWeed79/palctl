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


# How long proc_stats() samples CPU for. cpu_percent measures work done over a
# window, so it needs a real window — see the comment in proc_stats().
_CPU_SAMPLE_SECONDS = 0.3


def proc_stats() -> ProcStats | None:
    p = find_process()
    if p is None:
        return None
    try:
        # CPU has to be measured over a real interval, and it must be measured
        # BEFORE (and outside) the oneshot() block below.
        #
        # The obvious call, cpu_percent(interval=None), is a delta against the
        # *same Process object's* previous call — so the first call on any object
        # returns 0.0, and a caller that only reads once (the bot's /status, a
        # `palctl status` right after start) gets 0.0 every time. A shared "prime
        # the object once and reuse it" cache tried to paper over this, but it
        # still reads 0.0 on the first sample and whenever two of our callers
        # (poll loop, /state, the bot) land back-to-back, and it goes stale the
        # moment the poll loop that primed it stops running (e.g. the REST API is
        # unreachable). So we take a real measurement over a fixed window on every
        # call: cpu_percent(interval>0) snapshots CPU time, sleeps, snapshots
        # again, and returns a meaningful number the first time and every time.
        # The sleep is fine because every caller runs proc_stats in a worker
        # thread (asyncio.to_thread), off the daemon's event loop.
        #
        # It must stay outside oneshot(): oneshot() caches cpu_times(), so an
        # interval sample taken inside it diffs a value against itself and reads
        # 0.0 — exactly the bug we're fixing.
        cpu_raw = p.cpu_percent(interval=_CPU_SAMPLE_SECONDS)
        # Raw psutil cpu_percent sums across cores (can exceed 100% on an N-core
        # box). Normalize to 0-100% of the whole machine — that's what a "CPU"
        # status tile reads as.
        cpu = cpu_raw / (psutil.cpu_count() or 1)
        with p.oneshot():
            return ProcStats(
                pid=p.pid,
                memory_mb=p.memory_info().rss / 1_048_576,
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


# Windows records the *result of the last start attempt* in a service's
# WIN32_EXIT_CODE. `sc query` prints it, but _parse_sc_state reads only the
# STATE token and throws the code away — so a service that registered fine yet
# can't START (the classic post-install permission failure) otherwise looks like
# a plain STOPPED with no reason, and the daemon appears down for no visible
# cause. These map the common start-failure codes to a fix the user can act on.
SERVICE_START_ERRORS = {
    1069: (
        "Error 1069: the service's logon account was rejected. A PIN-only or "
        "passwordless Windows account can't host a service logon — re-enter the "
        "account password, or switch to password-free login startup "
        "(palctl-daemon install-startup)."
    ),
    1053: "Error 1053: the service didn't respond to the start request in time.",
    5: "Error 5: access denied — starting the service needs an administrator.",
}


def _parse_sc_exit_code(out: str) -> int | None:
    """The WIN32_EXIT_CODE from `sc query` output (0 = clean), or None when the
    line is absent/unparseable. The line reads e.g.
    ``WIN32_EXIT_CODE    : 1069  (0x42d)`` — take the first token after the colon
    (SERVICE_EXIT_CODE is a different line and never matches this substring)."""
    for line in out.splitlines():
        if "WIN32_EXIT_CODE" in line:
            tail = line.split(":", 1)[-1].split()
            if tail:
                try:
                    return int(tail[0])
                except ValueError:
                    return None
    return None


def service_failure_reason(service_name: str) -> str | None:
    """A plain-language reason a Windows service is registered but not running,
    read from the SCM's recorded start result. ``None`` off Windows, or when the
    code is 0/unknown — so a caller appends it only when there's something real
    to say, and never fabricates a problem on a healthy service."""
    if not IS_WINDOWS:
        return None
    code = _parse_sc_exit_code(_run_capture(_state_command(service_name)))
    if not code:
        return None
    return SERVICE_START_ERRORS.get(code, f"the service reported start error {code}.")


def service_diagnostics(service_name: str) -> str:
    """Raw service-manager status for a diagnostics bundle: `sc query` + `sc qc`
    on Windows (qc shows the logon account and binary path — the two things a
    permission/1069 report needs, and neither is a secret), or `systemctl status`
    on Linux. Bounded and non-raising, like every call in this module."""
    if IS_WINDOWS:
        blocks = []
        for cmd in (["sc.exe", "query", service_name], ["sc.exe", "qc", service_name]):
            blocks.append(f"$ {' '.join(cmd)}\n{_run_capture(cmd) or '(no output)'}")
        return "\n\n".join(blocks)
    cmd = ["systemctl", "status", "--no-pager", service_name]
    return f"$ {' '.join(cmd)}\n{_run_capture(cmd) or '(systemctl unavailable / no output)'}"


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
